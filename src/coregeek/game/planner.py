"""决策入口：`Turn` → `roleCommandMap`，外加响应顶层的 `(prompt, executeCmd)`。策略只写在这里。

每个角色每回合只有一个动作：

- 白天·开拓者：去最近一个能接的任务点 `acceptTask`；领到就被钉死（见 `_intents`）。
- 白天·工人：建武器 → 修墙 → 砌墙 → 卖矿 → 升级 → 采最值钱的矿，一件不行才轮到下一件；
  武器是最高优先级（份额有缺且钱不够 ⇒ 筹资：卖货/采贵矿）；环砌完后的白天末尾提前回炮位。
- 夜里·所有角色（含开拓者，`attack` 的可用角色是"全部"）：认领一组武器，走到那一组的岗位
  （两座火箭是共用的那一格、单座就站在炮旁）开火；能打就先打，打不了才挪岗。

两个距离口径别混用：回合预算（来不来得及来回）一律用 BFS 真实步数（`steps_between`，绕障，
-1 = 走不到）；选点/贴着用切比雪夫 `Pos.dist`（`dist <= 1` 是"站在建造位/采集位/炮位旁"的
判据，不是步数）。射程与溅射也是切比雪夫 —— 那是规则。

返回 `{角色ID: 指令}`（key 用字符串）；`attack` 是唯一的例外 —— 它的 key 是武器 id，操控者在
`controllerId` 里。

任务线不在这个返回值里：`task_channel(turn)` 单独产出 `(prompt, executeCmd)`；"跟 LLM 说
什么、怎么解析回复"在 `coregeek/agent/`，用的是包根那个单实例 `AGENT`。
"""

import logging
from collections.abc import Callable, Iterator, Mapping, Set
from typing import Any, NamedTuple

from ..agent import AGENT  # 与 LLM 说什么不在策略层
from ..agent.chat import answer_of, is_prices_reply, is_summary_reply, tool_of
from ..protocol import actions  # 指令只能经 Action 产出
from ..utils import _clip  # 日志的截断规则在叶子模块里
from .grid import (
    STEPS,
    Pos,
    base_cells,
    box_cells,
    door_cells,
    step_onto,
    step_outside,
    step_toward,
    steps_between,
    wall_cells,
    weapon_sites,
)
from .map import COPPER, IRON, STONE
from .roles import BaseRole, Pioneer, Worker
from .world import ROUNDS_PER_DAY, Robot, Turn, Wall, Weapon

LOGGER = logging.getLogger(__name__)

#: 三座武器的种类，下标与 `grid.weapon_sites()` 的落点一一对应：前排上方相邻两格放 2 座火箭、
#: 前排下方一格放加特林。两火箭相邻 ⇒ 一个角色站在它们内侧那一格能同时贴着两座，利用 3 回合
#: 冷却交替开火 —— 2 个角色即可操作 3 座武器。放弃电磁炮：单目标、能量不穿透、群体价值最低。
WEAPONS_BY_SITE = ("rocket", "rocket", "gatling")

#: 三种武器的 L1 伤害：加特林每颗子弹 10（沿弹道命中最近一台即消耗）；电磁狙击炮能量 10
#: （沿弹道穿透、逐台扣减）；火箭中心 20、落点周围 8 格溅射 10（指哪打哪、L1 一枚）。
#: 机器人四种血量 40/60/500/800 全都 ≥ 20 ⇒ L1 对满血机器人不可能过量伤害。
GATLING_SHOT = 10
RAILGUN_ENERGY = 10
ROCKET_CENTER = 20
ROCKET_SPLASH = 10

#: 建一座武器的金币（三种同价）。
WEAPON_COST = 25

#: 围墙的 `name` 与代价：石头×1，从建造者自己的背包扣（不是全队共享），拆了不返还。
WALL = "wall"
WALL_COST = 1

#: 一块石头的完整代价：采 1 回合 + 挪 1 回合 + 建 1 回合。
ROUNDS_PER_STONE = 3

#: 砌完墙后手里多留几块石头（用户口径）：墙夜里被打掉一格，第二天手里有货就能立刻补上。
#: 只抬高 `_stones_to_mine` 的上限，不额外开"回合够不够多"的开关 —— 回合少时那个上限本来
#: 就被回合预算压到很小。
STONE_RESERVE = 3

#: 容错余量（回合）：距离全按 BFS 真实步数算，这是给收尾动作留的真余量。5 是拍的。
TIME_MARGIN = 5

#: 白天收工闸门的缓冲（回合）：离天黑只剩"回程步数 + 这个数"就动身回炮位。3 是拍的。
POST_MARGIN = 3

#: 能卖给小贩的矿：三种（含多余的石头），挑哪种由 `Turn.vendor_prices` 现算。
#: 石头只在"墙砌完了"那一支里才卖得出去 —— 调用点 `_build_walls` 已保证。
SELLABLE = (STONE, IRON, COPPER)

#: 升级优先链：`(武器类别, 目标等级)`，先命中先用。加特林最优先 —— 无冷却、每回合必开火，
#: +1 颗子弹 = 每回合 +10、两颗可分打两台，射程 +2 更早接敌；双火箭次之 —— +1 枚导弹对簇
#: 约 +30/齐射（3 回合冷却、依赖扎堆）。同类出现两次 ⇒ 两座火箭都升得到。
UPGRADE_CHAIN = (
    ("gatling", 2),
    ("rocket", 2),
    ("rocket", 2),
    ("gatling", 3),
    ("rocket", 3),
    ("rocket", 3),
)

#: 升级券的商品名（`weaponShopList.name` 那套词；价目逐回合从载荷读，样例实证 100/150）。
VOUCHER = {2: "WeaponUpgradeVoucher1", 3: "WeaponUpgradeVoucher2"}

#: 基地升级券（夜里基地升级用）。
STATION_VOUCHERS = ("StationUpgradeVoucher1", "StationUpgradeVoucher2")

#: 围墙修复包：10 金、目标墙回满血。
WALLFIXER = "WallFixer"

#: 建筑满血基准：修墙的 1/4 血判据与夜里基地升级的"残血"判据用。基地 1500/3000/4500 是表格
#: 实证；墙 L2/L3 的 1500/2000 按每级 +500 推断（表格被图片截断，待实盘校准）—— 推断偏小的
#: 方向是"晚修"，安全。
WALL_MAX_HP = {1: 1000, 2: 1500, 3: 2000}
STATION_MAX_HP = {1: 1500, 2: 3000, 3: 4500}

#: 夜里怪清完后的出门半径（回炮位 BFS 步数上限，拍的）：机器人列表是视野过滤的，
#: "清完"只是看不见 —— 近矿限制让工人出得了事也回得了家。
NIGHT_WANDER = 8

#: "顺路卖矿"的绕路上限（格）：去矿的路上，绕去小贩比直走多花不超过这么多步就顺路卖掉。
DETOUR_MAX = 2

#: 拆墙放人的时间门：白天还剩这么多回合以上才允许拆 —— 拆了那格不回收、当天还得砌回来，
#: 太晚拆的洞等于整夜开着。拍的。
HOLE_MIN_LEFT = 30

#: 本地开火账（跨回合观测状态）：{武器 id: 发出 attack 的回合号}。判题器不发
#: cooldown 字段（样例如此）⇒ 火箭的冷却只能自己记：发出那回合记下，之后
#: `ROCKET_COOLDOWN` 回合内不选它，期满自动过期。武器被毁/重建换了 id ⇒ 旧账
#: 自然失效；卡住的最坏代价 = 某座火箭少打 ≤3 回合，自愈。
_fired: dict[int, int] = {}

#: 火箭发射后的冷却回合数（任务书 §4.5.1：发射后 3 回合空窗）。
ROCKET_COOLDOWN = 3


class _Move(NamedTuple):
    """第二段待解的走路意图。

    goal 型 = "朝 goal 挪一格"（BFS + 软避让，由 `_walk_out` 解）；provider 型 = 没有目标
    的走法（迈出盒子 / 挪开自己），第二段拿"当时的落子账"现算落脚格。`avoid` = 第一段已知
    的软避让；`with_paths` = 解的时候把已预留的路径也并进软避让；`reserve` = 动身成功后把
    整条路记进预留账（矿 / 卖矿 / 砌墙这些差事要 —— 后解的同事整条让开，防双双停住）；
    `onto` = 停在 goal 自己身上（默认停在贴着它的一格，见 `_Queue.step`）。
    """

    role: BaseRole
    goal: Pos | None
    avoid: frozenset[Pos] = frozenset()
    with_paths: bool = False
    reserve: bool = False
    onto: bool = False
    provider: Callable[[set[Pos]], Pos | None] | None = None


class _Queue:
    """第一段的输出收集器：能直接干的 act 当场落 `cmds`，走路只记成意图（`moves`），路径留给
    第二段统一解。`claimed` 是第一段的决策账 —— 只记 `remove` 的落点（`_rescue` 的"本回合已
    拆过墙"判据靠它）；走路的落子账在第二段（`_walk_out`）里，两本账不混。
    """

    def __init__(self, turn: Turn) -> None:
        self.turn = turn
        self.cmds: dict[str, dict[str, Any]] = {}
        self.moves: list[_Move] = []
        self.claimed: set[Pos] = set()

    def step(
        self,
        role: BaseRole,
        goal: Pos,
        *,
        avoid: Set[Pos] = frozenset(),
        with_paths: bool = False,
        reserve: bool = False,
        onto: bool = False,
    ) -> bool:
        """记一条"role 要走到 goal 去"。False = 硬障碍就走不到（调用方接着试下一个差事）；
        True = 意图已排。

        `onto` = 停在 goal 自己身上（`step_onto`），默认停在贴着它的一格（`step_toward`）——
        差事的 goal 都挡路（矿 / 建筑 / 炮位），只有共用的操作位那种空格才要走上去。
        """
        if steps_between(role.pos, goal, self.turn.map.blocked | self.claimed, self.turn.map.size) < 0:
            return False
        self.moves.append(_Move(role, goal, frozenset(avoid), with_paths, reserve, onto))
        return True

    def beside(self, role: BaseRole, provider: Callable[[set[Pos]], Pos | None]) -> None:
        """记一条没有目标的走法（迈出盒子 / 挪开自己）—— 第二段拿落子账现算。"""
        self.moves.append(_Move(role, None, provider=provider))


def plan(turn: Turn) -> dict[str, dict[str, Any]]:
    """两段式决策：`_intents` 逐角色出"初步行为"（能直接干的当场落指令、要走路的只交
    `(角色, 目标)` 意图），`_walk_out` 按同一顺序批量解走路意图（BFS 落一格、记落子账与路径
    预留账，后解的让开先落的）。

    黑板（建造格 / 矿格 / 炮位 / 修墙格认领、金币预留、环缺口切段）是第一段的决策账，逐角色
    顺序累计 —— 角色间的协调发生在决策层；第二段只协调格子。两段都不跨回合。
    """
    q, sites = _intents(turn)
    _walk_out(turn, q, sites)
    return q.cmds


def _intents(turn: Turn) -> tuple[_Queue, set[Pos]]:
    """第一段：逐角色出"初步行为"。act 直接落 `q.cmds`；走路的交 `q.step` / `q.beside`（不在
    这时算路）。黑板都是回合内的决策账：`sites` 建造格、`taken` 炮位、`repair_taken` 待修墙格、
    `ore_taken` 矿格、`budget` 金币（认领即预留）、`segments` 环缺口切段 —— 逐角色顺序累计，
    不跨回合。
    """
    q = _Queue(turn)
    cmds = q.cmds
    sites: set[Pos] = set()
    # 已被认领的武器（夜里一人只能操一座）
    taken: set[Pos] = set()
    # 本回合各机器人已被许掉的伤害（多炮协防的账：先开火的记上，后开的按剩余血挑目标）
    assigned: dict[Pos, int] = {}
    repair_taken: set[Pos] = set()
    budget = turn.gold
    # 武器缺口（份额有缺且落点为空的名额）：建武器最优先的判据，也是筹资线的开关。
    pending = list(_slots(turn))
    slots = iter(pending)
    weapon_gap = bool(pending)
    # 防御盒子的 36 格（空集 = 没基地 ⇒ 没有"里面"）
    box = box_cells(turn.map.station) if turn.map.station else frozenset()
    # 砌满墙就会被关在盒子里的人：闸门 (2) 按人放行，闸门 (1) 随 `gated` 传给 `_build_walls`
    leaving = _trapped(turn, box)

    # 环缺口按在场工人数切段（A 领前段、B 领后段沿环同向推进 ⇒ 后段任何一格都不低于前段
    # 剩下的，优先级保住；一个工人 ⇒ 整段）；`ore_taken` 矿格认领让 B 就近换一座、不跟 A
    # 奔同一座。黑板每回合从 `Turn` 现算 —— 跨回合记忆一旦卡住会静默关掉整条线。
    workers_no = [r for r in turn.roles if isinstance(r, Worker)]
    gaps = _ring(turn)
    bounds = [len(gaps) * i // max(len(workers_no), 1) for i in range(len(workers_no) + 1)]
    segments = [gaps[bounds[i]: bounds[i + 1]] for i in range(len(workers_no))]
    worker_no = 0
    ore_taken: set[Pos] = set()

    for role in turn.roles:
        # 服任务中的开拓者：钉死（离开任务点周围一格任务立即作废，所以它连夜里都不回炮位）。
        # 判据是载荷事实 `phase_task`，不是自己记的"谁领了任务"；放在循环最前面，与昼夜无关。
        if isinstance(role, Pioneer) and turn.phase_task:
            _answer_task(role, turn, cmds)
            continue

        # 要被关住的人先出来（判据 `leaving`）：静态障碍下出得去 ⇒ 交一个 provider 意图，
        # 第二段拿当时的落子账挑不相撞的那一步；出不去（已经被关死了，比如门让机器人堵满）
        # ⇒ 不占这一支，落到后面的 `_rescue` 去拆墙自救（工人自己也走那条路）。
        if turn.is_day and role.id in leaving:
            if step_outside(role.pos, box, turn.map.blocked, turn.map.size) is not None:
                q.beside(
                    role,
                    lambda claimed, r=role, b=box: step_outside(
                        r.pos, b, turn.map.blocked | claimed, turn.map.size
                    ),
                )
                continue

        # 有人被关在盒子里 ⇒ 工人去拆一格放人。与上面那道闸门是预防 vs 补救的关系（门让机器人
        # 堵死是闸门拦不住的）。放在收工闸门之前：放人比回炮位要紧，它自带时间门。
        if turn.is_day and _rescue(role, turn, q, box):
            continue

        if not turn.is_day:
            # 夜里：① 持基地券且基地残血（< 满血 1/4）⇒ 贴基地 `use` 升级（升级 + 回满血一次
            # 到位，当回合放弃开火）；② 视野里有机器人 ⇒ 回炮位开火；③ 怪清完 ⇒ 工人出门近矿
            # 经济（近矿限制，build/remove 夜里非法、绝不发），开拓者待命回炮位。
            if _upgrade_station(role, turn, q):
                continue
            if not turn.robots and isinstance(role, Worker):
                _mine_spare_ore(role, turn, q, sites, ore_taken, near=NIGHT_WANDER)
                continue
            _defend(role, turn, q, taken, assigned)
            continue

        # 白天收工：环砌完了 ⇒ 只在"离夜里第一波只剩回程步数"时才回家（判据在
        # `_leave_for_the_post` 里）。补墙优先于收工，环没砌完时这一支不生效；这一支只发
        # move，绝不能复用 `_defend` —— 它会发 attack，白天发就是非法指令。
        if not _ring(turn) and _leave_for_the_post(role, turn, q, taken):
            continue

        if isinstance(role, Pioneer):
            if turn.task_points:
                _take_task(role, turn, q)
            else:
                # 任务点全空 ⇒ 真空闲：领"买券 → 用券"差事（武器齐了才跑）；卖矿兜底
                # （接住它从任务/宝藏拿到可卖物的情形）；没货 ⇒ 待命。
                if not weapon_gap and not _upgrade_line(role, turn, q):
                    _sell_ore(role, turn, q, frozenset())
            continue

        if not isinstance(role, Worker):
            continue  # 不该出现的角色（`roles.make` 已挡过一道）

        segment = segments[worker_no] if worker_no < len(segments) else ()
        worker_no += 1
        target = segment[0] if segment else None

        # 建武器最优先（口径：无论哪一天，武器没了先建）：份额有缺且钱够 ⇒ 建/走向落点。
        # 造武器与墙无关，不参与下面的安全闸门。
        slot = next(slots, None)
        if slot is not None and budget >= WEAPON_COST:
            kind, cell = slot
            budget -= WEAPON_COST  # 认领即预留：宁可这回合少建，不可超支
            sites.add(cell)
            if role.pos.dist(cell) <= 1:
                if _emit(cmds, role, actions.Build, kind, cell):
                    continue
            elif q.step(role, cell, avoid=frozenset(sites), with_paths=True):
                continue

        # 筹资：武器还有缺但钱不够、且筹资可行（有小贩有价可卖，见 `_can_fund`）⇒ 整条墙线
        # 让位（含修墙），先卖背包里的货、再采最值钱的矿凑 25 金币（火力缺口比墙急；凑不成的
        # 地图上墙仍是剩下最值得干的事）。
        if weapon_gap and budget < WEAPON_COST and _can_fund(turn):
            if not _sell_ore(role, turn, q, sites, with_paths=True):
                _mine_spare_ore(role, turn, q, sites, ore_taken)
            continue

        # 修墙：弱墙（< 满血 1/4）按等级分派修法（见 `_repair_line`）。
        if _repair_line(role, turn, q, sites, repair_taken):
            continue

        # 安全闸门：墙格在手而砌下去会把人关住 ⇒ 待命（不发指令也不筹资 —— 别跑远，下回合
        # 缺口还在；`_build_walls` 里还有同一道闸兜底）。
        if target is not None and leaving:
            continue

        # 砌墙（平常时序）。
        if target is not None and _build_walls(
            role, turn, q, sites,
            gated=bool(leaving), target=target,
            remaining=len(segment),
            ore_taken=ore_taken,
        ):
            continue

        # 经济线兜底：卖矿 →（武器齐了才升级）→ 采闲矿。
        if not _sell_ore(role, turn, q, sites, with_paths=True):
            if weapon_gap or not _upgrade_line(role, turn, q):
                _mine_spare_ore(role, turn, q, sites, ore_taken)

    return q, sites


def _walk_out(turn: Turn, q: _Queue, sites: set[Pos]) -> None:
    """第二段：按第一段的顺序解走路意图（批量算路、逐个落子）。

    落子账 `claimed` 与路径预留账 `paths` 都在这里按意图顺序累计：先解的先落子，后解的 BFS
    把已落的子当硬障碍、把已预留的路当软避让（绕不开就退回硬障碍照走 —— 让路的代价不能是
    原地卡死）。provider 型意图拿"此刻的落子账"现算落脚格，算不出 ⇒ 这一回合不动（空指令
    合法）。普通意图静态走得通、却被本回合的落子堵死 ⇒ 同样待命 —— 极罕见，宁可少走一步不
    赌碰撞（§4.5.4：移动碰撞双双停住）。
    """
    claimed: set[Pos] = set()
    paths: set[Pos] = set()
    blocked, size = turn.map.blocked, turn.map.size
    for m in q.moves:
        if m.provider is not None:
            cell = m.provider(claimed)
            if cell is not None and _emit(q.cmds, m.role, actions.Move, cell):
                claimed.add(cell)
            continue
        avoid = m.avoid | (paths if m.with_paths else frozenset())
        walk = blocked | claimed | avoid
        # `onto` 的意图要走 goal 自己（`step_onto`），其余停在贴着它的一格
        walker = step_onto if m.onto else step_toward
        step = walker(m.role.pos, m.goal, walk, size)
        if step is None and avoid:
            step = walker(m.role.pos, m.goal, blocked | claimed, size)
        if step is None or not _emit(q.cmds, m.role, actions.Move, step):
            continue
        claimed.add(step)
        if m.reserve:
            _reserve_path(m.role, m.goal, turn, claimed, paths)


def task_channel(turn: Turn) -> tuple[str, str]:
    """处理本回合任务的 `(prompt, executeCmd)` —— 响应顶层那两个字段的唯一来源。"""

    # 打印任务信息和模型回复的日志
    if turn.phase_task or turn.llm_resp:
        LOGGER.info(
            "【本轮任务】：%s ｜ 【上一轮模型回复】：%s",
            _clip(turn.phase_task) or "无",
            _clip(turn.llm_resp) or "无",
        )
    # 打印CMD执行结果日志
    if turn.cmd_result:
        LOGGER.info("【CMD命令执行结果】：「%s」", _clip(turn.cmd_result))

    llmReply = turn.llm_resp.strip()
    # 获取摘要
    summary = is_summary_reply(llmReply)
    # 价格影响信息
    prices = is_prices_reply(llmReply)
    if summary is not None:
        # 摘要进 AGENT（不进会话表）
        AGENT.adopt_summary(summary)
        llmReply = ""
    elif prices is not None:
        # 价格期望进 AGENT（不进会话表）
        AGENT.adopt_price_hints(prices)
        llmReply = ""
    else:
        # 其余回复记进会话
        AGENT.hear(llmReply)  

    # 没任务 ⇒ 问一次新闻查价（额度 3/日，指纹去重）
    if not turn.phase_task:
        return AGENT.news_question(turn.news), ""

    # 任务回合，但没有开拓者参与 ⇒ 不发 prompt、也不发命令（任务线只在开拓者身上）。
    if not any(isinstance(r, Pioneer) for r in turn.roles):
        return "", ""
    # 解析任务答案
    answer = answer_of(llmReply) 
    # 解析工具调用
    call = tool_of(llmReply)
    command = AGENT.tool_call(*call) if call else ""  # 工具调度：副作用只发生在这一行
    # 判题器本轮报的"答案不对"（`code 2`）—— 判据 ④ 的触发条件
    rejected = any(e.code == 2 for e in turn.errors)
    # 判题器的原话：黑盒里唯一能回答"错在哪一项"的东西
    why = "；".join(e.description for e in turn.errors if e.code == 2 and e.description)

    # 构建错误信息提示
    retry = ""
    if rejected and answer:
        retry = f"{answer}\n【判题器反馈】：{why}" if why else answer
    elif rejected:
        retry = f"【判题器反馈】：{why}" if why else "（判题器未说明错在哪一项）"

    if turn.cmd_result:  # ② 回灌结果、这轮绝不发命令（必须压在 ③ 前）
        # 有回执 ⇒ 回灌结果
        prompt, cmd = AGENT.chat(turn.phase_task, result=turn.cmd_result, retry=retry), ""
    elif command:  # ③ 工具给了命令 ⇒ 交给沙盒；prompt 槽留给链尾的压缩闸门
        # 有命令 ⇒ 交给沙盒
        prompt, cmd = "", command
    elif retry:
        # 答错了 ⇒ 带纠错重问
        prompt, cmd = AGENT.chat(turn.phase_task, retry=retry), ""
    elif answer:  
        # 拿到了答案 ⇒ 只交答案：不发模型请求、也不压缩
        return "", ""
    else:  
        # 首问：把题目问出去
        prompt, cmd = AGENT.chat(turn.phase_task), ""

    # 链尾压缩闸门：只剩命令轮会到这里
    if not answer and prompt == "":
        prompt = AGENT.compression_request()
    return prompt, cmd


def _passable(turn: Turn) -> set[Pos]:
    """估算距离用的地形：`map.blocked` 剔掉我方角色站着的格子。

    与"这一回合实际怎么走"是两套口径（`_Queue.step` / `_walk_out` 那边必须把同事当硬障碍：
    撞上就是执行失败、双双停住）。这里问的是"路有多远"：`model._entries` 把我方角色写进网格，
    而环砌满之后盒子里只剩一格宽的走廊 ⇒ 同事停在走廊上就让估算判成不可达（-1），整条经济线
    跟着静默放弃、工人一整天不动。与 `_stuck_inside` / `_post_spots` 同一个口径：自己人算路过。
    """
    return turn.map.blocked - {r.pos for r in turn.roles}


def _slots(turn: Turn) -> Iterator[tuple[str, Pos]]:
    """本回合可以开建的 `(武器类别, 落点)`，按优先级排；没名额就一个都不产出。

    三道门槛：白天（`build` 仅白天）、份额 = `len(roles)`、落点是空的（建在已有武器上会把它
    覆盖成 level1，25 金币打水漂还降级）。基地没了 ⇒ 不建（安全降级）。
    """
    if not turn.is_day:
        return
    station = turn.map.station
    if station is None:
        return

    need = len(turn.roles) - len(turn.weapons)
    if need <= 0:
        return

    blocked = turn.map.blocked
    # 落点与种类绑死（`WEAPONS_BY_SITE` 与 `weapon_sites` 下标一一对应），只滤"落点是空的"：
    # 不按 `kind not in have` 过滤（有 2 座同种火箭，那道过滤会跳过第二座）；已有武器的格子
    # 在 `blocked` 里 ⇒ 不会在它上面重建（覆盖成 L1）。
    yield from [
        (kind, cell)
        for kind, cell in zip(WEAPONS_BY_SITE, weapon_sites(station, turn.map.size[0]))
        if cell not in blocked
    ][:need]


def _can_fund(turn: Turn) -> bool:
    """筹资可行：地图上有小贩、且有收购价 > 0 的矿 —— 矿挖了卖得掉，才谈得上凑
    建武器的钱。没小贩 / 没价目 ⇒ 筹不成，墙线照旧（没什么更好可干的事）。"""
    if not turn.map.vendors:
        return False
    prices = turn.vendor_prices
    return any(prices.get(kind, 0) > 0 for kind in turn.map.ores.values())


# ── 开拓者的任务线 ──────────────────────────────────────────────────
def _take_task(
    role: BaseRole, turn: Turn, q: _Queue
) -> None:
    """白天：走到最近一个能接的任务点旁边，贴着就 `acceptTask`。

    与 `build` / `collect` 同一条契约：任务点挡路，`step_toward` 天然停在贴着它的一格，而那
    正是"周围一格内"。一个能接的点都没有（都在冷却 / 已做完）⇒ 什么都不发，不去蹲守。领到
    之后本函数就再也进不来了（`_intents` 最前面那道分支会先一步接管）。
    """
    if not turn.task_points:
        return
    # "已经贴着就领"与"走过去"分两步判：合成一步会在站在两个任务点中间时舍近求远。
    if min(role.pos.dist(p) for p in turn.task_points) <= 1:
        _emit(q.cmds, role, actions.AcceptTask)
        return
    # 并列按坐标排：先后不能取决于 payload 里的顺序，否则用例复现不了。
    target = min(turn.task_points, key=lambda p: (role.pos.dist(p), p))
    q.step(role, target)


def _answer_task(role: BaseRole, turn: Turn, cmds: dict[str, dict[str, Any]]) -> None:
    """服任务中：把手上的答案原样交上去。这里从来不移动（挪出去任务即作废）。

    答案 = `answer_of(llmResp)`：先挖掉工具块、认块外 `<answer>…</answer>` 里的内容，再不然
    原文即答案（工具块内的一律不算 —— 多半是 SOP 正文里的示例）。空答案不发（可能被判成
    "字段缺失" = 指令非法，红线）。每回合都交：判题器按"提交过的通过率最高的答案"算分。
    工具调用绝不能当答案交上去，且交的这份必须与 `task_channel` 判据 ④ 骂的那份同源。
    """
    answer = answer_of(turn.llm_resp)
    if answer:
        _emit(cmds, role, actions.SubmitAnswer, answer)


# ── 白天：采石 → 砌墙 ────────────────────────────────────────────────
def _build_walls(
    role: Worker,
    turn: Turn,
    q: _Queue,
    sites: set[Pos],
    gated: bool = False,
    target: Pos | None = None,
    remaining: int = 0,
    ore_taken: set[Pos] | None = None,
) -> bool:
    """白天：先攒石头，再把墙砌到黑板分给的那一格上。返回 `True` = 本回合发了指令
    （采矿 / 挪开 / 砌墙 / 走向矿或墙）；`False` = 砌不了（没石头、采不到、被闸门挡住）⇒
    调用方接着走经济线 —— 工人不能什么都不做。每回合独立判定、不存跨回合状态：矿采没了、
    墙被别人砌了，下一回合都能自动跟着变。

    `target` / `remaining` = 黑板切段分给本工人的那一格与本段还剩几格（A 领前段、B 领后段，
    两人不挤同一段墙）。`gated` = 这一回合不许砌（砌下去会把谁关在墙里，见 `_trapped`）。
    """
    if target is None:
        return False  # 没分到墙格 ⇒ 调用方走经济线
    ore_taken = set() if ore_taken is None else ore_taken
    if gated:
        return True  # 砌下去会把人关住 ⇒ 待命（安全：别跑远，下回合缺口还在）
    # 只认石矿：墙只吃石头，铁/铜再多也砌不了墙。排除本回合别人认领的矿格（B 就近换一座）。
    # 认领只发生在"真要采"之后（want > 0）：石头已够的工人不该占住矿格 —— 它这回合用不上。
    mine = _pick_ore(
        role.pos,
        {p: k for p, k in turn.map.ores.items() if p not in ore_taken},
        turn.vendor_prices,
        want_stone=True,
    )
    want = _stones_to_mine(role, turn, target, mine, remaining)

    if want > 0 and mine is not None:
        ore_taken.add(mine)  # 挑中即登记（本回合真要采它）
        if role.pos.dist(mine) <= 1:
            if _emit(q.cmds, role, actions.Collect, mine):
                return True
        # 还没走到矿边：排一条走路意图，动身成功后由第二段把整条路记进预留账（先预留再走
        # 会让工人绕开自己刚记下的路 —— 可绕时就地多绕三步）。
        elif q.step(role, mine, avoid=frozenset(sites), with_paths=True, reserve=True):
            return True

    if role.stone >= WALL_COST:
        # 认领要发生在动身之前：等砌完再登记的话，另一个工人会在同一回合也奔着它去。
        sites.add(target)
        if role.pos == target:
            # 站在目标格上就先挪开一格、这一回合不砌：人站在墙上时那一格在网格里只剩
            # "worker"（单位铺在最后，墙被盖掉）⇒ 看不出砌过没有，而 `_ring` 的"自己人算
            # 路过"又把它复活成候选 ⇒ 每回合对同一格 `build`，石头白花。挪开一格两个方向
            # 都收敛：砌过的没人站着就现形；没砌的下一回合从邻格稳稳砌上。
            q.beside(
                role,
                lambda claimed, r=role, av=frozenset(sites): _aside_cell(r, turn, claimed, av),
            )
            return True
        elif role.pos.dist(target) <= 1:
            # 与建武器同一条契约：`step_toward` 停在贴着目标的一格，那正是 `build` 的站位
            if _emit(q.cmds, role, actions.Build, WALL, target):
                return True
        elif q.step(role, target, avoid=frozenset(sites), with_paths=True, reserve=True):
            return True
    return False  # 没石头、采不到 ⇒ 调用方走其他差事


def _sealed_back(turn: Turn) -> bool:
    """第 3 天起把背面两个角格补上（前两天的环只有 14 格，背面整列敞开）。

    判据只能用回合号：环上"没砌"与"砌了又被拆"在地图上同形（第 1 天的缺口是真的没砌）。
    """
    return turn.round_no > 2 * ROUNDS_PER_DAY


def _walled(turn: Turn) -> frozenset[Pos]:
    """"假设墙砌满"时的障碍集：现已挡路的照原样 + 那一圈墙（14 或 16 格）。

    闸门问的是将来 —— 现在走得出去不代表砌完还走得出去。补救通道（`remove` + `_rescue`）不
    构成撤销它的理由：闸门是预防（零成本），`_rescue` 是补救（1 回合 + 1 块不退的石头）。

    自己人算不算障碍只看后方通道那几格（与 `_ring` 的"自己人一律算路过"故意相反）：那圈墙里
    没有建筑 ⇒ 能堵门的只有单位，"自己人站在格子上"只在通道口有意义；环内站着的自己人是
    过路的。一律算障碍会让走廊另一头的工人被判成"砌满就出不去"⇒ 两人来回踱步、一整天不砌
    墙（实测）。守门：`test_a_colleague_in_the_door_still_holds_the_wall_back`。
    """
    station = turn.map.station
    if station is None:
        return frozenset()
    blocked = turn.map.blocked
    sealed = _sealed_back(turn)
    # 自己人站在后方通道格上的照旧算障碍；站在别处的从障碍里摘掉（见 docstring）
    door = set(door_cells(station, turn.map.size[0], sealed=sealed))
    mine = {r.pos for r in turn.roles} - door
    return (blocked - mine) | set(wall_cells(station, turn.map.size[0], sealed=sealed))


def _trapped(turn: Turn, box: frozenset[Pos]) -> frozenset[str]:
    """砌满这一圈墙之后就出不去的我方角色 id；没有就空集。

    判据是"整面墙"不是"某一格"（障碍集里永远有整圈墙）⇒ 调用方要的是一个布尔量。返回
    id 是因为配套的闸门 (2) 得知道谁先出来。后方通道就那么几格 ⇒ 能堵门的单位只要几个 ⇒
    这条判据相当常真的命中。
    """
    station = turn.map.station
    if station is None or not box:
        return frozenset()  # 没基地 ⇒ 既没有盒子也没有围墙，谁都关不住
    walled = _walled(turn)
    return frozenset(
        r.id
        for r in turn.roles
        # `r.pos in box` 这一半不能省：`step_outside` 对"本来就在外面"也返回 None，不看这一半
        # 会把所有在盒外干活的角色判成被关住（于是墙一格都不砌）。
        if r.pos in box and step_outside(r.pos, box, walled, turn.map.size) is None
    )


def _ring(turn: Turn) -> tuple[Pos, ...]:
    """按优先级排的围墙格；基地没了 ⇒ 空。

    既不在 `wall_cells` 里、也不挡路的格才算候选 —— 但自己人站的那一格除外。工人站在待砌的
    墙格上只是路过，若把它划掉，另一个工人的 `free[0]` 会整体后移、等那人一挪窝目标又变回来
    —— 两个工人在两格之间对着改目标，一格都砌不上。

    这里只管"哪些格能砌"（几何 + 占用），"这一回合还砌不砌"是 `_trapped` 的事，两者正交。
    """
    station = turn.map.station
    if station is None:
        return ()
    # 我方角色当前站的格（角色能走的都在这）
    mine = {r.pos for r in turn.roles}
    cells = wall_cells(station, turn.map.size[0], sealed=_sealed_back(turn))
    return tuple(c for c in cells if c not in turn.map.blocked or c in mine)


# ── 拆墙放人 ────────────────────────────────────────────────────────
def _after_first_day(turn: Turn) -> bool:
    """是不是第 2 天及以后 —— 允许拆墙放人的前提。

    第 1 天环上"孤零零一个缺口"既是"刚拆的洞"、也是"还差一格没砌"，地图上逐字节同形，任何
    无状态判据都分不开，只能靠时间：第 1 天缺口一律当"还没砌"（`_ring` 现在就是对的）。没有
    它，第 1 天砌到只剩中段某一格时那格会被当成"洞"推迟到当天末尾、白天再也不补。守门：
    `test_a_worker_on_the_last_cell_still_finishes_the_ring`。
    """
    return turn.round_no > ROUNDS_PER_DAY


def _stuck_inside(turn: Turn, box: frozenset[Pos]) -> tuple[BaseRole, ...]:
    """现在真的走不出盒子的人；没有就空元组。

    与 `_trapped` 的区别是用"现在"的障碍而不是"假设砌满"：闸门是预防，这里是补救，两者并存。
    自己人一律不算障碍 —— 同事站在门格上只是路过，把他算成障碍会白拆一次（1 回合 + 1 块不退
    的石头）。
    """
    if not box:
        return ()
    walk = _passable(turn)
    size = turn.map.size
    return tuple(
        r for r in turn.roles if r.pos in box and step_outside(r.pos, box, walk, size) is None
    )


def _rescue(
    role: BaseRole,
    turn: Turn,
    q: _Queue,
    box: frozenset[Pos],
) -> bool:
    """有人被关在盒子里 ⇒ 工人去拆一格放人。

    工人自己被关住时也走这里、而且先走这里（`_rescue` 排在白天所有差事之前）。被任务钉死的
    开拓者非靠别人救不可。

    开哪一格：开了之后真能让某个被困的人迈出去、且离救援者最近的那一格（并列取坐标序）。门被
    机器人堵死时命中；被同事堵住时 `_stuck_inside` 先一步把人放出来了 ⇒ 这里不命中 —— 那正是
    "不白拆一次"的意思。守门：`RescueTest`。
    """
    if not turn.is_day or not isinstance(role, Worker) or not box:
        return False
    station = turn.map.station
    if station is None or not _after_first_day(turn):
        return False
    wall = wall_cells(station, turn.map.size[0], sealed=_sealed_back(turn))
    if _ring(turn) or q.claimed & set(wall):
        return False
    if role.stone < WALL_COST or turn.day_rounds_left <= HOLE_MIN_LEFT:
        return False
    stuck = _stuck_inside(turn, box)
    if not stuck:
        return False

    walk, size = turn.map.blocked | q.claimed, turn.map.size
    free = [
        (role.pos.dist(c), c)
        for c in wall
        if any(step_outside(v.pos, box, walk - {c}, size) is not None for v in stuck)
    ]
    if not free:
        return False
    site = min(free)[1]
    if role.pos.dist(site) <= 1:
        # 登记被拆的那一格：挡住同回合的第二个工人（对寻路是空操作，它本来就在 `blocked` 里）
        q.claimed.add(site)
        return _emit(q.cmds, role, actions.Remove, site)
    # 两条路都只是"朝那一格挪一格"：贴近了下一回合自然就拆。
    return q.step(role, site)


def _weak_walls(turn: Turn, *, level_min: int = 1) -> tuple[Wall, ...]:
    """血量不到满血 1/4 的已砌墙 —— "墙不完备"的判据，按坐标序（可复现）。

    满血基准按等级查 `WALL_MAX_HP`（升级后回满血）。health 缺失（-1）⇒ 未知 ⇒ 不算弱；已毁（0）
    的墙在 `model._walls` 就丢了 —— 那是一格缺口，归 `_ring` 管重建。`level_min` 按等级筛：
    等级决定修法（L2 起用包回满血，L1 拆掉重建，见 `_repair_line`）。
    """
    return tuple(
        w
        for w in sorted(turn.walls, key=lambda w: w.pos)
        if w.level >= level_min
        and 0 < w.health
        and w.health * 4 < WALL_MAX_HP.get(w.level, WALL_MAX_HP[1])
    )


def _repair_line(
    role: Worker,
    turn: Turn,
    q: _Queue,
    sites: set[Pos],
    taken: set[Pos],
) -> bool:
    """弱墙（见 `_weak_walls`）的修复差事，按等级分派。返回 `True` = 这一轮归它了。

    ① L2+ 用修复包回满血（1 回合 + 10 金、墙不塌、不开洞）：持包 ⇒ 走到最近的那面（认领在
       动身之前，两个修墙工人不挤同一面），贴着就 `use WallFixer`（目标 = 墙坐标）；没包 ⇒
       商店可达、价目里有它、金币够就走去商店 `Buy`；
    ② L1 拆掉重建：走到那一格、贴着就 `remove`（下一回合那格自然进 `_ring` 被重砌）—— 一块
       1000 血的墙不值得 25 金的包。手里得有石头（拆了不回收，没石头就只是开个洞），且白天
       还剩 `3 * TIME_MARGIN` 以上：天黑前砌不回来的洞等于整夜开着。

    ① 优先于 ②：回血不开洞，等级越高越舍不得推倒；① 这一轮做不成（走不到/买不起）才轮到 ②。
    只在白天跑（与 `build` 同一条昼夜口径）。
    """
    budget = turn.day_rounds_left - TIME_MARGIN
    walk, size = _passable(turn), turn.map.size

    # ① L2+：包优先（回满血、不开洞），没包就去买
    repair = _weak_walls(turn, level_min=2)
    if repair:
        if WALLFIXER not in role.bag:
            price = turn.shop_prices.get(WALLFIXER, 0)
            hops = [(steps_between(role.pos, s, walk, size), s) for s in turn.map.shops]
            hops = [(d, s) for d, s in hops if d >= 0]
            to_shop, shop = min(hops) if hops else (-1, None)
            if shop is not None and price > 0 and turn.gold >= price and to_shop + 1 <= budget:
                if to_shop == 0:
                    return _emit(q.cmds, role, actions.Buy, WALLFIXER, 1)
                return q.step(role, shop, avoid=frozenset(sites))
        else:
            target = min(
                (w for w in repair if w.pos not in taken),
                key=lambda w: (role.pos.dist(w.pos), w.pos),
                default=None,
            )
            if target is not None:
                taken.add(target.pos)
                if role.pos.dist(target.pos) <= 1:
                    return _emit(q.cmds, role, actions.Use, WALLFIXER, target.pos)
                to_wall = steps_between(role.pos, target.pos, walk, size)
                if to_wall >= 0 and to_wall + 1 <= budget:
                    return q.step(role, target.pos, avoid=frozenset(sites))

    # ② L1：拆掉重建
    if role.stone < WALL_COST or turn.day_rounds_left <= 3 * TIME_MARGIN:
        return False  # 拆了砌不回来 ⇒ 不如留着那点血
    low = [w for w in _weak_walls(turn) if w.level <= 1 and w.pos not in taken]
    if not low:
        return False
    target = min(low, key=lambda w: (role.pos.dist(w.pos), w.pos))
    taken.add(target.pos)
    if role.pos.dist(target.pos) <= 1:
        return _emit(q.cmds, role, actions.Remove, target.pos)
    to_wall = steps_between(role.pos, target.pos, walk, size)
    if to_wall < 0 or to_wall + 1 > budget:
        return False  # 来不及 ⇒ 待命，明天接着走
    return q.step(role, target.pos, avoid=frozenset(sites))


def _stones_to_mine(role: Worker, turn: Turn, target: Pos, mine: Pos | None, free: int) -> int:
    """这一趟还该采几块石头 —— 按回合预算每回合现算。

    记 `s` = 手里的石头、`k` = 还要采的块数：

        走到矿 + 采 k 块 + 从矿走回工地 + 砌 s+k 座的"挪一格 + 建造"
        = d_mine + d_wall + 2s − 1 + 3k        # 到达工地那一步已贴着首格
                                            # ⇒ 每多采一块净花 3 回合

    令它 ≤ `白天还剩的回合 − TIME_MARGIN` 解出 k，再与"还差几格墙 **+ `STONE_RESERVE`**"取小
    —— 多出来的几块是砌完墙后的存货（没有转移物品的指令，多采的石头给不了别人，只能自己拿着，
    墙被打掉一格时立刻补得上）。距离一律用 BFS 真实步数：回工地常要绕整面围墙、从后方通道
    进来，切比雪夫会把 10+ 步说成 3 步。-1（走不到）⇒ 一块都别采（宁可这回合不动）。
    """
    if mine is None:
        return 0
    walk, size = _passable(turn), turn.map.size
    to_mine = steps_between(role.pos, mine, walk, size)
    to_wall = steps_between(mine, target, walk, size)
    if to_mine < 0 or to_wall < 0:
        return 0
    budget = turn.day_rounds_left - TIME_MARGIN - to_mine - to_wall - 2 * role.stone + 1
    return max(0, min(budget // ROUNDS_PER_STONE, free + STONE_RESERVE - role.stone))


def _sell_ore(
    role: BaseRole,
    turn: Turn,
    q: _Queue,
    sites: set[Pos],
    *,
    with_paths: bool = False,
) -> bool:
    """把小贩肯收的矿背过去换金币。这一回合没去卖就返回 `False`（调用方接着去采）。

    卖的可用角色是全部（§4.4）—— 工人（墙砌完后）与开拓者（任务点全空时）都走这里。四条门，
    任一条不成立就 `False`：

    ① 有货：`_best_load` 从 `SELLABLE` 里挑收购价最高的一种；
    ② 有小贩且走得到（`Map.vendors` 空、或一个都走不到就无处可卖）；
    ③ 够本：已经不贴着才算 —— 货值 < 往返回合数（`2 × 步数`）就留在矿边接着采（贴着时这趟路
       早付过了）；"1 金币 ≈ 1 回合"是拍的，唯一的调参旋钮；
    ④ 回得来：`走到小贩 + 从小贩回基地 ≤ 白天剩余 − TIME_MARGIN`（夜里必须在炮位上）。

    距离一律 BFS 真实步数（小贩常在盒子外，回基地要绕后方通道）；-1 一律当"这趟不去"。站位
    是 `sell` 要求的"小贩周围一格内"，与小贩格本身挡路正好对上。一回合只能发一条指令 ⇒ 一次
    只卖一种矿，`num` = 手上那种的全部件数（卖光）。
    """
    station = turn.map.station
    kind, num = _best_load(role, turn.vendor_prices)
    if not kind or station is None or not turn.map.vendors:
        return False
    walk, size = _passable(turn), turn.map.size
    # 并列按坐标排：先后不能取决于 payload 里的顺序。走不到的小贩直接剔掉（BFS -1）。
    hops = [(steps_between(role.pos, p, walk, size), p) for p in turn.map.vendors]
    hops = [(steps, pos) for steps, pos in hops if steps >= 0]
    if not hops:
        return False
    to_vendor, vendor = min(hops)
    if to_vendor == 0:
        # 0 = 已经贴着小贩（`steps_between` 的口径）。1 是"差一格"，那时候还不许卖
        return _emit(q.cmds, role, actions.Sell, kind, num)
    value = turn.vendor_prices.get(kind, 0) * num
    if value < 2 * to_vendor:
        return False  # ③ 为这一堆货走这么远不划算，接着采
    back = steps_between(vendor, station, walk, size)
    if back < 0 or to_vendor + back > turn.day_rounds_left - TIME_MARGIN:
        return False  # ④ 去了就赶不回来
    # 差事差事之间要互相让路（with_paths：动身成功后整条路进预留账）
    return q.step(role, vendor, avoid=frozenset(sites), with_paths=with_paths, reserve=with_paths)


def _best_load(role: Worker, prices: Mapping[str, int]) -> tuple[str, int]:
    """挑这一趟卖哪种矿：收购价最高的，同价取件数多的；挑不出来 ⇒ `("", 0)`。

    价 ≤ 0 或件数为 0 的矿跳过（小贩不收的矿换不来金币）；价目表为空 ⇒ 一件都不卖。名字参与
    比较只是为了让并列可复现。

    石头保底留 1 块不卖：收工时手里得有石头才能把正面那个口封上（`wall_cells` 的最后一格），
    封不上就是整夜的一道门。只有 1 块 ⇒ 这一趟不卖石头（另外两种矿照卖）。"墙砌完了才卖石头"
    的规则保证了这 1 块买不到墙，它的用途只有封口。
    """
    loads = [
        (prices.get(kind, 0), role.bag.get(kind, 0) - (1 if kind == STONE else 0), kind)
        for kind in SELLABLE
        if prices.get(kind, 0) > 0 and role.bag.get(kind, 0) > (1 if kind == STONE else 0)
    ]
    if not loads:
        return "", 0
    _, num, kind = max(loads)
    return kind, num


def _mine_spare_ore(
    role: Worker,
    turn: Turn,
    q: _Queue,
    sites: set[Pos],
    ore_taken: set[Pos] | None = None,
    near: int = 0,
) -> None:
    """墙砌完了 ⇒ 白天不再闲着：就近采买得动、回得来的矿。

    先把"走得动、回得来"的矿筛出来、再按性价比挑：`价 × 新闻修正 ÷ (到矿 + 采一块 + 回炮位)`，
    单位回合价值最高 —— 近的贱矿可能跑赢远的贵矿。回程参照 = 最近的武器位（没有武器才用基地）。
    新闻修正是 `AGENT.price_hint`（跨回合状态，退化 = 偏好偏一天）。先看 `_detour_buy`（修墙
    缺包 / 升级缺券），再 `_detour_sell`。夜里（`near > 0`）：怪清完才出门，`max(到矿, 回炮位)
    ≤ near` 的近矿限制 —— 机器人列表是视野过滤的，"清完"只是看不见，出得了事要回得了家。
    距离一律 BFS 真实步数；-1（走不到）的矿直接作废。
    """
    ore_taken = set() if ore_taken is None else ore_taken
    station = turn.map.station
    posts = [w.pos for w in turn.weapons] or ([station] if station else [])
    budget = turn.day_rounds_left - TIME_MARGIN
    walk, size = _passable(turn), turn.map.size
    # 先筛可行（走过去 + 回得来），再按性价比挑。BFS 是"命中目标即停"的，开销跟距离相关、
    # 不是整张图 —— 最坏 12 矿 × (1 + 3 炮) 次，实测每回合几十毫秒。
    feasible: dict[Pos, tuple[str, int, int]] = {}
    for p, kind in turn.map.ores.items():
        if p in ore_taken:
            continue  # 本回合已被人认领
        out = steps_between(role.pos, p, walk, size)
        return_home = [steps_between(post, p, walk, size) for post in posts]
        back = min((d for d in return_home if d >= 0), default=-1)
        if out < 0 or back < 0:
            continue
        if near > 0:
            if max(out, back) > near:
                continue  # 夜里近矿限制
        elif out + back > budget:
            continue  # 白天：这一趟赶不回来
        feasible[p] = (kind, out, back)
    best: tuple[float, int, Pos] | None = None
    for p, (kind, out, back) in feasible.items():
        value = turn.vendor_prices.get(kind, 0) * AGENT.price_hint(kind)
        if value <= 0:
            continue  # 小贩不收的矿不为它多走一步
        key = (-(value / (out + back + 1)), out, p)
        if best is None or key < best:
            best = key
    mine = best[2] if best else None
    if station is None or mine is None:
        return
    ore_taken.add(mine)
    if _detour_buy(role, mine, turn, q):
        return
    if _detour_sell(role, turn, q, mine):
        return
    if role.pos.dist(mine) <= 1:
        _emit(q.cmds, role, actions.Collect, mine)
        return
    if q.step(role, mine, avoid=frozenset(sites), with_paths=True, reserve=True):
        return  # 动身成功后整条路进预留账（第二段干）


def _shopping_list(role: Worker, turn: Turn) -> str | None:
    """这一趟该顺路买什么 ⇒ 商品名；什么都不缺 ⇒ `None`。

    只在买得起时才列：钱不够绕过去也白绕。优先级：WallFixer（有 L2+ 弱墙且包里没包 —— 10 金
    回满血；L1 那种是拆掉重建、用不上包）> 升级链下一张券（别人包里已有一张就不再买 —— 没有转移指令，囤两张是白花金币）。
    """
    prices = turn.shop_prices
    if (
        _weak_walls(turn, level_min=2)
        and WALLFIXER not in role.bag
        and 0 < prices.get(WALLFIXER, 0) <= turn.gold
    ):
        return WALLFIXER
    target = _upgrade_target(turn)
    if target is not None:
        voucher = target[1]
        held = any(voucher in r.bag for r in turn.roles)
        if not held and voucher not in role.bag and 0 < prices.get(voucher, 0) <= turn.gold:
            return voucher
    return None


def _detour_buy(
    role: Worker, goal: Pos, turn: Turn, q: _Queue
) -> bool:
    """去差事的路上顺路买：绕 2 格内有商店、且购物单上有东西 ⇒ 先朝商店迈一步。

    贴上商店的那回合 `Buy`（下一回合走 `use` / 升级线），之后再继续去差事。绕路账与
    `_detour_sell` 同一套（`via + after - direct ≤ DETOUR_MAX`）；-1（走不到）的绕法直接放弃。
    已经贴着差事目标就别绕了（这一回合该干活）。
    """
    want = _shopping_list(role, turn)
    if want is None or role.pos.dist(goal) <= 1:
        return False
    walk, size = _passable(turn), turn.map.size
    direct = steps_between(role.pos, goal, walk, size)
    if direct < 0:
        return False
    for shop in sorted(turn.map.shops):
        via = steps_between(role.pos, shop, walk, size)
        if via == 0:
            return False  # 已经贴着商店：绕一步 = 原地不动 = 这一回合空指令
        after = steps_between(shop, goal, walk, size)
        if via < 0 or after < 0:
            continue
        if via + after - direct <= DETOUR_MAX:
            return q.step(role, shop)
    return False


def _detour_sell(
    role: BaseRole, turn: Turn, q: _Queue, mine: Pos
) -> bool:
    """去矿的路上顺路卖矿：绕去小贩比直走多花 ≤ `DETOUR_MAX` 格 ⇒ 先朝小贩迈一步。

    贴上小贩的那一回合同一条链里更早的 `_sell_ore` 自然把货出手（贴着跳过够本门），卖完没货、
    绕路条件消失，下一回合继续去矿。已经贴着矿就别绕了（这一回合该采）。

    "有没有可卖的货"用 `_best_load` 判、不在这里重抄一遍：那边有"石头保底留 1 块"的口径，抄
    一遍就会出现"绕去卖那 1 块石头、到了却不肯卖"—— 白绕一趟。距离一律 BFS 真实步数；-1 的
    绕法直接放弃。
    """
    if role.pos.dist(mine) <= 1:
        return False
    if _best_load(role, turn.vendor_prices)[1] <= 0:
        return False
    walk, size = _passable(turn), turn.map.size
    direct = steps_between(role.pos, mine, walk, size)
    if direct < 0:
        return False
    for vendor in sorted(turn.map.vendors):
        via = steps_between(role.pos, vendor, walk, size)
        if via == 0:
            return False  # 已经贴着小贩：绕一步 = 原地不动 = 这一回合空指令
        after = steps_between(vendor, mine, walk, size)
        if via < 0 or after < 0:
            continue
        if via + after - direct <= DETOUR_MAX:
            return q.step(role, vendor)
    return False


def _upgrade_line(
    role: BaseRole, turn: Turn, q: _Queue
) -> bool:
    """墙砌完后的第二优先：买券 → 走到目标武器 → 用券。这一回合没发指令就返回 `False`。

    无状态：拿没拿券看背包（买完金变少、包里多一张，两个阶段天然可分，跨夜不丢）。跑腿者 =
    持券的那个角色（券在谁包里谁用 —— 没有转移物品的指令；开拓者也能持券）> 真空闲的开拓者
    > 名册上第一个工人。只在白天跑（夜里 `_defend` 会把人接回炮位，明早预算重算、接着走）。
    距离一律 BFS 真实步数：商店/炮位在盒子内外两侧，整趟要绕门；-1 一律当天不去。
    """
    workers = [r for r in turn.roles if isinstance(r, Worker)]
    holder = next(
        (r for r in turn.roles if any(name in r.bag for name in VOUCHER.values())), None
    )
    # 真空闲 = 无可接任务 —— 有任务在的话开拓者马上会被钉死，跑腿该让给工人
    idle = (
        None if turn.task_points else next((r for r in turn.roles if isinstance(r, Pioneer)), None)
    )
    runner = holder or idle or (workers[0] if workers else None)
    if runner is None or runner is not role:
        return False  # 跑腿的是别人；持券者不在场（比如夜里阵亡）⇒ 券先躺在包里
    target = _upgrade_target(turn)
    if target is None:
        return False
    weapon, voucher = target
    budget = turn.day_rounds_left - TIME_MARGIN
    walk, size = _passable(turn), turn.map.size
    if voucher in role.bag:
        # 持券阶段：终点就是炮位，用完正好站岗 —— 不用留回程
        if role.pos.dist(weapon.pos) <= 1:
            return _emit(q.cmds, role, actions.Use, voucher, weapon.pos)
        to_weapon = steps_between(role.pos, weapon.pos, walk, size)
        if to_weapon < 0 or to_weapon + 1 > budget:
            return False  # 走不到 / 今天来不及 ⇒ 待命，明天接着走
        return q.step(role, weapon.pos)
    # 买券阶段：整趟 = 走到商店 + 买到武器 + 买/用两个动作回合
    price = turn.shop_prices.get(voucher, 0)
    if price <= 0 or turn.gold < price:
        return False
    # 并列按坐标排：先后不能取决于 payload 里的顺序。走不到的商店直接剔掉（BFS -1）。
    hops = [(steps_between(role.pos, s, walk, size), s) for s in turn.map.shops]
    hops = [(steps, pos) for steps, pos in hops if steps >= 0]
    if not hops:
        return False  # 没有商店、或者一个都走不到
    to_shop, shop = min(hops)
    if to_shop == 0:
        # 0 = 已经贴着商店（`steps_between` 的口径）。1 是"差一格"，那时候还不许买
        return _emit(q.cmds, role, actions.Buy, voucher, 1)
    to_weapon = steps_between(shop, weapon.pos, walk, size)
    if to_weapon < 0 or to_shop + to_weapon + 2 > budget:
        return False
    return q.step(role, shop)


def _upgrade_target(turn: Turn) -> tuple[Weapon, str] | None:
    """优先链上第一座还升得动的武器 + 该买的那张券名。全升满 ⇒ `None`。

    按 `(类别, 目标等级)` 沿 `UPGRADE_CHAIN` 找；同类多座按 id 排（并列不能取决于
    payload 顺序）。券只认"从几级升"（V1 = 任何 L1 武器），不绑定类别。
    """
    by_id = sorted(turn.weapons, key=lambda w: w.id)
    for kind, want in UPGRADE_CHAIN:
        for weapon in by_id:
            if weapon.kind == kind and weapon.level == want - 1:
                return weapon, VOUCHER[want]
    return None


def _reserve_path(
    role: BaseRole,
    goal: Pos,
    turn: Turn,
    claimed: set[Pos],
    paths: set[Pos],
) -> None:
    """把"从 `role.pos` 走到 `goal` 的 BFS 路径"的中间格记进 `paths`（路径预留）。

    后解的走路意图把 `paths` 当软避让 ⇒ 选路时整条让开，而不是撞上前一个工人的本回合这一格
    才让 —— 盒子里走廊就那几条，双双停住一回合是纯亏（任务书 §4.5.4：移动碰撞双双不动）。
    避让是软的：绕不开时退回硬障碍照走（擦肩好过卡死）。算路只用 `blocked | claimed`、不含
    `paths` 自己 —— 否则第二个工人的预留会绕开第一个的、越绕越远。41×32 的图，64 步是上限。
    """
    pos, walk, size = role.pos, turn.map.blocked | claimed, turn.map.size
    for _ in range(64):
        if pos.dist(goal) <= 1:
            return
        step = step_toward(pos, goal, walk, size)
        if step is None:
            return
        paths.add(step)
        pos = step


def _pick_ore(
    pos: Pos, ores: Mapping[Pos, str], prices: Mapping[str, int], *, want_stone: bool
) -> Pos | None:
    """挑一座矿。两种口径：

    - `want_stone`（石头还不够砌完剩下的墙）⇒ 只认石矿、取最近的（铁/铜砌不了墙）；
    - 否则 ⇒ 三种矿按小贩收购价从高到低，同价再取近的。价目表里没有的矿种按 0 算 ⇒ 小贩不收
      的矿不值得为它多走一步（空价目表也就自然落成"谁也不采"）。

    价格逐回合从载荷读、不写死"铜 > 铁 > 石头"。并列按坐标排：先后不能取决于 payload 顺序。
    不认领矿：两个工人挤同一座矿的不同邻格都能采，只有"冲进同一格"才是白扔动作（`claimed` 挡着）。
    """
    if want_stone:
        return min(
            (p for p, kind in ores.items() if kind == STONE),
            key=lambda p: (pos.dist(p), p),
            default=None,
        )
    # `(取负的价, 距离, 坐标)` —— 价排第一，`min` 出来就是"价最高、同价取近的、再同取坐标最小"
    best = min(
        ((-prices.get(kind, 0), pos.dist(p), p) for p, kind in ores.items()),
        default=None,
    )
    # 价 0 ⇒ 小贩不收，不为它多走一步；一张空价目表也就自然落成"谁也不采"
    return best[2] if best is not None and best[0] < 0 else None


def _leave_for_the_post(
    role: BaseRole,
    turn: Turn,
    q: _Queue,
    taken: set[Pos],
) -> bool:
    """白天收工：离夜里的第一波只剩回程步数了就**回那一组的岗位**；这一回合到此为止 ⇒ `True`。

    白天就得动身：机器人在夜里第一个回合就全部出现，而回炮位常常要绕整面围墙、从后方通道
    进来、再横穿盒子 —— 在正面墙外干活时直线三四步、BFS 十几步。目标与夜里 `_defend` 的
    岗位同一个（`_post_spots`）：多座组站到共用的操作位、单座组站在炮旁，天黑时人已经在岗、
    第一回合就能开火。与 `_defend` 的唯一区别是它不调 `_fire`：`attack` 仅黑夜（§4.4），白天
    发就是非法指令、5 次出局。

    先看时间、再看位置：判据是"到最近那个岗位的 BFS 步数 ≥ 白天还剩的回合 − `POST_MARGIN`"
    （缓冲，拍的）；已经在岗位上时步数是 0，于是只有白天最后那几回合才轮得到"在岗待命"。
    少了时间这一道，"到岗就待命"会让早上正好站在炮边的工人整天不动。

    返回 `True` = 这一回合已由本函数处理（走了、或已在岗待命），调用方 `continue`；`False` =
    还来得及干活（或没有可去的岗位）。
    """
    walk, size = _passable(turn), turn.map.size
    # 最近、还没人认领、而且真走得到的岗位 —— 一趟判定就够：最近的都赶不上，更远的更赶不上。
    # BFS -1（不可达）剔掉，切比雪夫给不出这个值；已经在岗位上 ⇒ 步数 0，与"还差 3 步"同一刻度。
    hops: list[tuple[int, Pos, tuple[Weapon, ...], bool]] = []
    for group in _weapon_groups(turn):
        if any(w.pos in taken for w in group):
            continue
        spots = _post_spots(group, turn, role)
        if not spots:
            continue
        onto = len(group) > 1  # 多座组的岗位是空地、要站上去（与 `_defend` 同一个口径）
        for spot in spots:
            steps = _steps_to_post(role.pos, spot, onto, walk, size)
            if steps >= 0:
                hops.append((steps, spot, group, onto))
    if not hops:
        return False
    steps, spot, group, onto = min(hops, key=lambda h: (h[0], h[1]))
    if steps < turn.day_rounds_left - POST_MARGIN:
        return False  # 还剩富裕回合 ⇒ 照常干活
    for w in group:
        taken.add(w.pos)  # 定下这组了：认领，免得另一个角色也奔这里（一人只能操一座）
    if steps == 0:
        return True  # 已经在岗 ⇒ 这一回合待命（什么都不发 = 合法空指令）
    q.step(role, spot, onto=onto)  # 只发 move，绝不调 `_fire`（白天发 attack = 非法指令）
    return True


# ── 夜里：回炮位、开火 ──────────────────────────────────────────────
def _weapon_groups(turn: Turn) -> tuple[tuple[Weapon, ...], ...]:
    """把武器分成操作组：同一组的武器由同一个角色操作。

    当前阵形 = 2 火箭（相邻）+ 1 加特林 ⇒ 两组：`(rocket1, rocket2)` 和 `(gatling,)`。分组依据
    是 `weapon_sites` 的下标（0,1 = 火箭对；2 = 加特林），不按场上已有武器的种类猜（种类重复
    时猜不准）。某座还没建出来 ⇒ 那一组就只含已建的。
    """
    station = turn.map.station
    sites = weapon_sites(station, turn.map.size[0]) if station is not None else ()
    by_pos = {w.pos: w for w in turn.weapons}
    grouped: set[Pos] = set()
    groups: list[tuple[Weapon, ...]] = []
    for indices in ((0, 1), (2,)):
        group = tuple(by_pos[sites[i]] for i in indices if i < len(sites) and sites[i] in by_pos)
        if group:
            groups.append(group)
            grouped.update(w.pos for w in group)
    # 不在 `weapon_sites` 里的武器（测试手搭的位置 / 摧毁后重建的偏移）⇒ 单独成组，降级为
    # "一人操一座"，避免测试里手搭的炮没人认领。
    for w in turn.weapons:
        if w.pos not in grouped:
            groups.append((w,))
    return tuple(groups)


def _operator_spots(
    group: tuple[Weapon, ...], blocked: set[Pos], size: tuple[int, int]
) -> list[Pos]:
    """一组武器的操作站位：贴着组内所有武器的格子（去障碍）。

    单座组 ⇒ 返回那座本身（`step_toward` 会停在邻格）；多座组 ⇒ 所有武器 8 邻域的
    交集里走得通的格子（**要站上去** —— `step_onto` 就是为它加的）。调用者一律是
    `_post_spots`：`blocked` 要预先剔掉自己人，还得再排除别人占着的岗位。
    """
    if len(group) == 1:
        return [group[0].pos]
    common: set[Pos] | None = None
    for w in group:
        nbrs = {Pos(w.pos.x + d.x, w.pos.y + d.y) for d in STEPS}
        common = nbrs if common is None else common & nbrs
    if common is None:
        return []
    width, height = size
    return sorted(c for c in common if c not in blocked and 0 <= c.x < width and 0 <= c.y < height)


def _post_spots(group: tuple[Weapon, ...], turn: Turn, role: BaseRole) -> list[Pos]:
    """这一组这一回合能用的岗位：贴着组内每一座、又没被别人占着的格子。

    自己人一律从障碍里剔掉（`model._entries` 把我方角色写进网格 ⇒ 整份 `blocked` 里混着
    队友和**自己**，不放行的话站在岗位上的操作者看不见自己的岗位）；但别人**站着**的岗位
    要排除 —— 那格被占了就得换一组去（否则一个走不进去、一个干等着，两人一起卡住）。
    自己那格保留：站在岗位上的人得认得出自己的岗位。
    """
    cells = {r.pos for r in turn.roles}
    spots = _operator_spots(group, _passable(turn), turn.map.size)
    others = cells - {role.pos}
    return [s for s in spots if s not in others]


def _steps_to_post(pos: Pos, spot: Pos, onto: bool, blocked: Set[Pos], size: tuple[int, int]) -> int:
    """到岗位的 BFS 步数（走不到 -1）。口径与 `_Queue.step` 一致。

    多座组的岗位是空地、要站上去 ⇒ 比"贴着它"多一步；单座组的岗位就是那座炮本身 ⇒
    `steps_between` 的口径就是答案。已经在岗位上 ⇒ 0。
    """
    if pos == spot:
        return 0
    steps = steps_between(pos, spot, blocked, size)
    return steps + 1 if steps >= 0 and onto else steps


def _cooling(weapon: Weapon, round_no: int) -> bool:
    """这座炮这回合打不得吗：payload 的 `cooldown` 优先；火箭在字段缺失（-1）时
    查本地开火账 `_fired`（判题器不发这个字段，样例如此）。加特林/电磁恒 0，不查。"""
    if weapon.cooldown > 0:
        return True
    if weapon.kind != "rocket":
        return False
    last = _fired.get(weapon.id)
    return last is not None and round_no - last <= ROCKET_COOLDOWN


def _defend(
    role: BaseRole,
    turn: Turn,
    q: _Queue,
    taken: set[Pos],
    assigned: dict[Pos, int],
) -> None:
    """夜里：认领一组还没被本回合别人认领的武器，走到操作位，开火。所有角色都走这里。

    回合号缺失（`round_no < 0`）时一发不发：`is_day` 把缺失的回合号判成夜里，那个降级方向对
    "白天不许建造"安全、对 `attack` 就反了（白天开火非法）。

    选组：最近且没人认领的（组内任一座被认领 = 整组被认领）。**先开火、打不了才挪岗**：
    贴着组内某座就先打它（只贴着一座也打 —— 另一座就绪而这座冷却时，别白丢一回合），
    站上整组的岗位、又都打不了 ⇒ 待命；只贴着一部分、或者压根没贴着 ⇒ 朝**多座组共用的
    那个操作位**走（那格要踩上去才同时贴着两座，`step_onto` 就是为它加的；走不上去 ⇒ 不动）。
    不换组 —— 每回合重挑会让角色在炮位之间来回走。场上没有武器 / 都够不着 ⇒ 不动。
    """
    if turn.round_no < 0:
        return
    groups = _weapon_groups(turn)
    for group in sorted(groups, key=lambda g: (min(role.pos.dist(w.pos) for w in g), min(w.id for w in g))):
        if any(w.pos in taken for w in group):
            continue
        spots = _post_spots(group, turn, role)
        if not spots:
            continue
        # 多座组共用一个岗位格（要站上去）；单座组的"岗位"就是那座炮，停在它旁边就够
        onto = len(group) > 1
        # 已经贴着组内某座 ⇒ 认领整组开火。就绪的先挑（含本地开火账：发过的 3 回合内
        # 不算就绪 —— 打冷却炮是指令执行失败、白丢一回合火力）；就绪的并列按回合号
        # 轮转；都冷却 ⇒ 一发不发（待命，空指令合法）。
        adjacent = [w for w in group if role.pos.dist(w.pos) <= 1]
        if adjacent:
            for w in group:
                taken.add(w.pos)
            ready = sorted((w for w in adjacent if not _cooling(w, turn.round_no)), key=lambda w: w.id)
            cooling = sorted((w for w in adjacent if _cooling(w, turn.round_no)), key=lambda w: w.id)
            if ready:
                k = turn.round_no % len(ready)
                ready = ready[k:] + ready[:k]
            for w in ready + cooling:
                if _fire(role, w, turn, q.cmds, assigned):
                    return
            if len(adjacent) == len(group):
                return  # 站在岗位上了、这回合又打不了 ⇒ 原地待命（不换组）
        # 还差一座（或压根没贴着）⇒ 朝最近的操作位走一格（走路意图进第二段；走不到就换下一组）
        spot = min(spots, key=lambda s: (role.pos.dist(s), s))
        if not q.step(role, spot, onto=onto):
            continue  # 走不到 → 换下一组
        for w in group:
            taken.add(w.pos)  # 认领发生在动身之后
        return


def _upgrade_station(
    role: BaseRole, turn: Turn, q: _Queue
) -> bool:
    """夜里基地升级：持基地券 + 基地血量 < 满血 1/4 ⇒ 贴基地 `use`。`True` = 这一轮归它了。

    按等级查 `STATION_MAX_HP`（1500/3000/4500）。夜里人在盒内炮位、基地就在盒心 —— 站位天然
    满足"周围一格内"，最多走一两步。升级 + 回满血一次到位，是要塌的基地最好的救兵；走过去/用
    券的那个回合放弃开火 —— 只在基地真残血时才值得。station_health 缺失（-1）⇒ 未知 ⇒ 不动。
    """
    voucher = next((v for v in STATION_VOUCHERS if v in role.bag), None)
    if voucher is None or turn.station_health < 0:
        return False
    station = turn.map.station
    if station is None:
        return False
    if turn.station_health * 4 >= STATION_MAX_HP.get(turn.station_level, STATION_MAX_HP[1]):
        return False  # 还不残血
    if any(role.pos.dist(c) <= 1 for c in base_cells(station)):
        return _emit(q.cmds, role, actions.Use, voucher, station)
    return q.step(role, station)


def _foe_robots(turn: Turn) -> tuple[Robot, ...]:
    """打我方基地的机器人；`our_team` 为空（字段缺失）⇒ 不过滤，全部照打（安全降级）。

    - `robot.target_team` 为空 ⇒ 当成打我方的（照打）；
    - `== our_team` ⇒ 打我方的（照打）；
    - 其他（明确打对方）⇒ 跳过：它们不威胁我方基地，打它们既浪费本回合火力、又帮对方减轻基地
      压力。火箭射程远（L3 全图）尤其需要这道过滤，否则经常把导弹砸到对方半场的机器人簇里。
    """
    our = turn.our_team
    if not our:
        return turn.robots
    return tuple(
        r for r in turn.robots if not r.target_team or r.target_team == our
    )


def _fire(
    role: BaseRole,
    weapon: Weapon,
    turn: Turn,
    cmds: dict[str, dict[str, Any]],
    assigned: dict[Pos, int],
) -> bool:
    """贴着炮了：按最大伤害落点开火（方针：打死所有机器人）。打不了就什么都不发。

    - 冷却中不打：payload 的 `cooldown` 优先；火箭字段缺失时查本地开火账 `_fired`
      （发过之后 3 回合不选 —— 打冷却炮是指令执行失败、白丢一回合）；
    - 火箭（`_rocket_site`）：落点任选 ⇒ 中心 20 + 溅射 10 全场算账，取总分最高的落点；
    - 加特林 / 电磁（`_beam_site`）：弹道武器 ⇒ 落点打在某台机器人身上（终点必在弹道上 ⇒ 必
      命中；途中更近的先接住，那也是命中）。取有效伤害最高的，并列打近的；
    - `assigned` 记账：先开火的炮把估计伤害记在机器人身上，后开的按剩余血算 —— 不挤同一个将死
      的目标。估计有偏差（加特林实际命中的是弹道上最近那台），代价只是次优、不会非法；
    - 机器人 `health` 缺失（-1）时按"还活着"算：打空处只是执行失败，而"一律不打"会让整晚一炮
      不开。
    """
    if _cooling(weapon, turn.round_no):
        return False
    # 只打打我方的机器人：`our_team` 缺失或 `target_team` 缺失 ⇒ 照打（安全降级）；明确是对方
    # 阵营 ⇒ 跳过（打它们既浪费火力、又帮对方减轻基地压力）。
    foes = _foe_robots(turn)
    if not foes:
        return False
    if weapon.kind == "rocket":
        target = _rocket_site(weapon, foes, turn.map.size, assigned)
        if target is None:
            return False
        _book_rocket(target, foes, assigned)
    else:
        victim = _beam_site(weapon, foes, assigned)
        if victim is None:
            return False
        target = victim.pos
        shot = GATLING_SHOT if weapon.kind == "gatling" else RAILGUN_ENERGY
        assigned[victim.pos] = assigned.get(victim.pos, 0) + shot
    # key 是武器 id，操控角色在报文的 `controllerId` 里
    fired = _emit(cmds, role, actions.Attack, str(role.id), target, key=str(weapon.id))
    if fired and weapon.kind == "rocket":
        _fired[weapon.id] = turn.round_no  # 判题器不发 cooldown ⇒ 发出的那发自己记
    return fired


def _beam_site(
    weapon: Weapon, robots: tuple[Robot, ...], assigned: Mapping[Pos, int]
) -> Robot | None:
    """加特林/电磁的目标：有效伤害最高的那台（并列打近的、再并列按坐标序）。

    "有效伤害" = min(伤害, 剩余血)：L1 的 10 点对满血机器人（≥40 血）等额，差别只在将死者
    —— 别把整发浪费在已被打得差不多的人身上（`assigned` = 本回合先开火的炮记的账）。够得着的
    目标全是将死的（有效 ≤ 0）⇒ 不打。
    """
    shot = GATLING_SHOT if weapon.kind == "gatling" else RAILGUN_ENERGY

    def effective(r: Robot) -> int:
        return min(shot, max(0, r.health - assigned.get(r.pos, 0)))

    reach = [r for r in robots if r.health != 0 and weapon.pos.dist(r.pos) <= weapon.attack_range]
    best = max(reach, key=lambda r: (effective(r), -weapon.pos.dist(r.pos), r.pos), default=None)
    return best if best is not None and effective(best) > 0 else None


def _rocket_site(
    weapon: Weapon,
    robots: tuple[Robot, ...],
    size: tuple[int, int],
    assigned: Mapping[Pos, int],
) -> Pos | None:
    """火箭的最大伤害落点：中心 20 + 周围 8 格溅射 10，取总分最高的格子。

    候选 = 机器人占的格与其 8 邻格（别的格子一分伤害都摸不到），且落点须在射程内 ——
    溅射可以够到射程之外的机器人（射程只管落点）。评分 = Σ min(伤害, 剩余血)
    （`assigned` = 本回合先开火的炮记的账），并列取坐标序最小（可复现）。
    越界格不进候选（越界落点 = 指令非法，红线不让赌）。
    """
    alive = [r for r in robots if r.health != 0]
    width, height = size
    cands = {
        cell
        for r in alive
        for cell in (r.pos, *(Pos(r.pos.x + d.x, r.pos.y + d.y) for d in STEPS))
        if 0 <= cell.x < width and 0 <= cell.y < height
    }

    def score(cell: Pos) -> int:
        return sum(
            min(
                ROCKET_CENTER if cell == r.pos else ROCKET_SPLASH if cell.dist(r.pos) == 1 else 0,
                max(0, r.health - assigned.get(r.pos, 0)),
            )
            for r in alive
        )

    in_range = [cell for cell in cands if weapon.pos.dist(cell) <= weapon.attack_range]
    return min(in_range, key=lambda cell: (-score(cell), cell), default=None)


def _book_rocket(target: Pos, robots: tuple[Robot, ...], assigned: dict[Pos, int]) -> None:
    """把火箭这一发的估计伤害记到账上（同回合后开的炮按剩余血挑目标）。"""
    for r in robots:
        if r.health == 0:
            continue
        hit = ROCKET_CENTER if r.pos == target else ROCKET_SPLASH if r.pos.dist(target) == 1 else 0
        if hit:
            assigned[r.pos] = assigned.get(r.pos, 0) + hit


# ── 发指令 ──────────────────────────────────────────────────────────
def _aside_cell(
    role: BaseRole, turn: Turn, claimed: set[Pos], avoid: Set[Pos] = frozenset()
) -> Pos | None:
    """挪开自己：从脚下这一格挑一个可走的邻格；八面都走不了 ⇒ None。

    没有"目标"的走法（唯一用途见 `_build_walls` 站在待砌墙格上的那一支），所以不走
    `step_toward`——它的契约是"贴着 goal 即到"，对任意邻格都返回 None。挪到建造格上等于换个
    格子接着站，所以 `avoid`（建造格）照避；`claimed`（别人已落的脚格）由第二段传进来。
    方向顺序无所谓：任何一个可走的邻格都等价（挪开一步就够了）。
    """
    walk = turn.map.blocked | claimed | avoid
    width, height = turn.map.size
    for d in STEPS:
        cell = Pos(role.pos.x + d.x, role.pos.y + d.y)
        if cell not in walk and 0 <= cell.x < width and 0 <= cell.y < height:
            return cell
    return None


def _emit(
    cmds: dict[str, dict[str, Any]],
    role: BaseRole,
    cls: type[actions.BaseAction],
    *args: Any,
    key: str | None = None,
) -> bool:
    """造一条指令放进 `cmds`，成功返回 True。

    默认挂在 `str(role.id)` 下，只有 `attack` 例外（key 是武器 id，操控者在 `controllerId` 里）
    ⇒ 开一个 keyword-only 的口子，只有 `_fire` 那一处传 `key`。越权（`PermissionError`）只丢
    这一条并告警，不连坐同回合其他角色 —— 抛出去会变成"每回合空指令 ⇒ 全队冻结一整局"。
    """
    try:
        cmds[key if key is not None else str(role.id)] = cls(role.type_name, *args).to_wire()
    except PermissionError as exc:
        LOGGER.warning("拦下越权动作：角色 %s(%s) %s", role.id, role.type_name, exc)
        return False
    return True
