"""决策入口：`Turn` → `roleCommandMap`，外加响应顶层的 `(prompt, executeCmd)`。

组装：`plan` = `_intents`（第一段，逐角色出"初步行为"）+ `_walk_out`（第二段，批量解走路意图）。
白天工人那条链在 `states.py`（`DAY_CHAIN` / `BACK_TO_POST`，本模块只调用）：建武器 → 筹资 →
砌墙 → 修墙 → 卖矿 → 升级 → 采最值钱的矿；武器是最高优先级。本模块管另外两件事：任务线
（`task_channel`）与夜里那一段（闸门 + 认领武器组 + `_defend` 开火）。
"两个模块都要问"的通用判定在 `game/utils.py`（谁能走 / 环砌在哪 / 谁会被关住 / 人手够不够）——
`_passable` / `_ring` / `_walled` / `_trapped` 这些不在本文件里定义。

每个角色每回合只有一个动作：

- 白天·开拓者：去最近一个能接的任务点 `acceptTask`；领到就被钉死（见 `_intents`）。
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
from collections.abc import Iterator, Mapping
from typing import Any

from ..agent import AGENT, cmd_explore  # 与 LLM 说什么不在策略层
from ..agent.chat import answer_of, is_prices_reply, is_summary_reply, tool_of
from ..protocol import actions  # 指令只能经 Action 产出
from ..utils import _clip  # 日志的截断规则在叶子模块里
from .grid import STEPS, Pos, base_cells, box_cells, wall_cells, weapon_sites
from .path import step_onto, step_outside, step_toward
from .roles import BaseRole, Pioneer, Worker
from .states import (
    BACK_TO_POST,
    DAY_CHAIN,
    WALL_COST,
    _Ctx,
    _Queue,
    _economy,
    _emit,
    _near_spots,
    _operator_spots,
    _post_spots,
    _sell_ore,
    _upgrade_line,
)
from .utils import (
    _passable,
    _pioneer_mans_guns,
    _ring,
    _sealed_back,
    _short_handed,
    _stuck_inside,
    _trapped,
    _walled,
    _weapon_groups,
)
from .world import Robot, Turn, Weapon

LOGGER = logging.getLogger(__name__)


#: 三座武器的种类，下标与 `grid.weapon_sites()` 的落点一一对应：前排上方相邻两格放 2 座火箭、
#: 前排下方一格放加特林。两火箭相邻 ⇒ 一个角色站在它们内侧那一格能同时贴着两座，利用 3 回合
#: 冷却交替开火 —— 2 个角色即可操作 3 座武器。放弃电磁炮：单目标、能量不穿透、群体价值最低。
WEAPONS_BY_SITE = ("rocket", "rocket", "gatling")


#: 三种武器的 L1 伤害：加特林每颗子弹 10（沿弹道命中最近一台即消耗）；电磁狙击炮能量 10
#: （沿弹道穿透、逐台扣减）；火箭中心 20、落点周围 8 格溅射 10（指哪打哪）。
#: 每升一级多发一份：加特林/火箭 = 多发多目标（个数 = 等级，见 `_fire`），电磁恒 1 个目标
#: 但能量翻倍。机器人四种血量 40/60/500/800 全都 ≥ 20 ⇒ 一发对满血机器人不可能过量伤害。
GATLING_SHOT = 10
RAILGUN_ENERGY = 10
ROCKET_CENTER = 20
ROCKET_SPLASH = 10


#: 基地升级券（夜里基地升级用）。
STATION_VOUCHERS = ("StationUpgradeVoucher1", "StationUpgradeVoucher2")

STATION_MAX_HP = {1: 1500, 2: 3000, 3: 4500}


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
    这时算路）。黑板都是回合内的决策账（建造格 / 矿格 / 炮位 / 待修墙格 / 金币预留，装在
    `_Ctx` 里，环缺口切段在本函数），逐角色顺序累计、不跨回合。工人的一串判据在 `DAY_CHAIN`。
    """
    q = _Queue(turn)
    cmds = q.cmds
    # 本回合各机器人已被许掉的伤害（多炮协防的账：先开火的记上，后开的按剩余血挑目标）
    assigned: dict[Pos, int] = {}
    # 武器缺口（份额有缺且落点为空的名额）：建武器最优先的判据，也是筹资线的开关。
    # 一次算好、链上只读 —— 改从 `ctx.slots` 现算会把那条一次性迭代器吃掉。
    pending = list(_slots(turn))
    weapon_gap = bool(pending)
    # 防御盒子的 36 格（空集 = 没基地 ⇒ 没有"里面"）
    box = box_cells(turn.map.station) if turn.map.station else frozenset()
    # 砌满墙就会被关在盒子里的人：闸门 (2) 按人放行，建墙状态随 `leaving` 待命
    leaving = _trapped(turn, box)
    # 回合内的决策账（建造格 / 矿格 / 炮位 / 待修墙格 / 金币预留）：循环里逐角色原地累计
    ctx = _Ctx(turn, q, weapon_gap=weapon_gap, leaving=leaving, slots=iter(pending))

    # 环缺口按在场工人数切段（A 领前段、B 领后段沿环同向推进 ⇒ 后段任何一格都不低于前段
    # 剩下的，优先级保住；一个工人 ⇒ 整段）；`ore_taken` 矿格认领让 B 就近换一座、不跟 A
    # 奔同一座。黑板每回合从 `Turn` 现算 —— 跨回合记忆一旦卡住会静默关掉整条线。
    workers_no = [r for r in turn.roles if isinstance(r, Worker)]
    gaps = _ring(turn)
    bounds = [len(gaps) * i // max(len(workers_no), 1) for i in range(len(workers_no) + 1)]
    segments = [gaps[bounds[i]: bounds[i + 1]] for i in range(len(workers_no))]
    worker_no = 0

    for role in turn.roles:
        # 服任务中的开拓者：钉死（离开任务点周围一格任务立即作废，所以它连夜里都不回炮位）——
        # 唯一例外是夜里人手不够（`_short_handed`：工人阵亡 ⇒ 有炮没人操），生存第一、弃任务。
        # 判据是载荷事实 `phase_task`，不是自己记的"谁领了任务"；放在循环最前面，与昼夜无关。
        if isinstance(role, Pioneer) and turn.phase_task and not _short_handed(turn):
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
            # 到位，当回合放弃开火）；② 还有会打我方的活机器人 ⇒ 回炮位开火；③ 没有了 ⇒ 工人跑
            # 整套经济兜底（与白天最后一级同一套；build/remove 夜里非法、绝不发），开拓者待命回炮位。
            # 判据是"没有打我方的活机器人"，不是"场上全空"：一夜一波（L352）但全图可见（L95）⇒
            # 打对方那一波也在 `turn.robots` 里，我们从不打它（`_fire` 同样过 `_foe_robots`）⇒
            # 照全空判的话它们整夜钉住工人、经济线永远进不去。已毁的照旧留表 ⇒ 过 `_alive`。
            if _upgrade_station(role, turn, q):
                continue
            if not _alive(_foe_robots(turn)) and isinstance(role, Worker):
                _economy(role, turn, q, ctx.sites, ctx.ore_taken, weapon_gap=weapon_gap)
                continue
            _defend(role, turn, q, ctx.taken, assigned)
            continue

        # 白天收工：环砌完了 ⇒ 只在"离夜里第一波只剩回程步数"时才回家（判据在
        # `BackToPost` 里）。补墙优先于收工，环没砌完时这一支不生效。
        if BACK_TO_POST.run(role, ctx):
            continue

        if isinstance(role, Pioneer):
            if turn.task_points:
                _take_task(role, turn, q)
            else:
                # 任务点全空 ⇒ 真空闲：领"买券 → 用券"差事（武器齐了才跑）；卖矿兜底
                # （接住它从任务/宝藏拿到可卖物的情形）；没货 ⇒ 待命。
                if not weapon_gap and not _upgrade_line(role, turn, q, ctx.sites):
                    _sell_ore(role, turn, q, ctx.sites)
            continue

        if not isinstance(role, Worker):
            continue  # 不该出现的角色（`roles.make` 已挡过一道）

        # 环缺口切段：段号在这一支的最前面就吃掉（走过前面那些闸门的角色不占段）
        segment = segments[worker_no] if worker_no < len(segments) else ()
        worker_no += 1
        ctx.target = segment[0] if segment else None
        ctx.remaining = len(segment)

        # 白天工人链：从上往下第一个"认领了这一回合"的状态说了算（顺序即策略，见 `DAY_CHAIN`）
        for state in DAY_CHAIN:
            if state.run(role, ctx):
                break

    return q, ctx.sites


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
    # 上回合若是探查命令，回执归探查收走：任务线当它没发生（见 `agent.cmd_explore`）。
    # 必须压在早返回之前 —— 任务在回执回来前就结束了的话，不认领它就粘住 `_waiting`。
    result = cmd_explore.observe(turn.cmd_result)
    # 打印CMD执行结果日志（探查自己那份不在这儿再抄一遍：原文已落盘 tmp/，而它的
    # 回执是整份文件、抄进日志只是把磁盘上的东西再写一次 stdout）
    if result:
        LOGGER.info("【CMD命令执行结果】：「%s」", _clip(result))

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
        # 沙箱探查的成果**不在这里复位**（探明的清单与正文整场累积，第 106 步）；它也**不在这
        # 里发命令** —— 探查明面上每回合都跑，但这条早返回在链尾那道闸门之前（沙盒仅任务期间
        # 可用）。回执认领在上面（`observe` 压在函数最上头）⇒ 在途那条照旧收下。
        return AGENT.news_question(turn.news), ""

    # 任务回合，但没有开拓者参与 ⇒ 不发 prompt（任务线只在开拓者身上）；命令槽交给探查。
    if not any(isinstance(r, Pioneer) for r in turn.roles):
        return "", cmd_explore.next_command()
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

    if result:  # ② 回灌结果、这轮不发 LLM 的命令（空槽照旧归链尾的探查闸门；必须压在 ③ 前）
        # 有回执 ⇒ 回灌结果
        prompt, cmd = AGENT.chat(turn.phase_task, result=result, retry=retry), ""
    elif command:  # ③ 工具给了命令 ⇒ 交给沙盒；prompt 槽留给链尾的压缩闸门
        # 有命令 ⇒ 交给沙盒
        prompt, cmd = "", command
    elif retry:
        # 答错了 ⇒ 带纠错重问
        prompt, cmd = AGENT.chat(turn.phase_task, retry=retry), ""
    elif answer:
        # 拿到了答案 ⇒ 只交答案：不发模型请求、也不压缩
        prompt, cmd = "", ""
    else:
        # 首问：把题目问出去
        prompt, cmd = AGENT.chat(turn.phase_task), ""

    # 链尾压缩闸门：答案轮不压缩（压缩与 `<answer>` 互斥），只剩命令轮会填上
    if not answer and prompt == "":
        prompt = AGENT.compression_request()
    # 链尾探查闸门：命令槽还空着 ⇒ 拿去摸沙箱环境（回执归探查自己收，不回灌）。
    # 每回合都发得出 —— 沙盒每道任务独立，探完一遍下一回合从头上再走一遍
    if cmd == "":
        cmd = cmd_explore.next_command()
    return prompt, cmd


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


# ── 拆墙放人 ────────────────────────────────────────────────────────


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

    环没砌满 ⇒ 整个不生效（`_ring` 那一句）：第 1 天那个缺口是"还没砌"、不是"刚拆的洞"，
    地图上同形，只能靠"环满不满"分开 —— 环没满时人本来就能从缺口走。第 1 天环砌完了照救。
    """
    if not turn.is_day or not isinstance(role, Worker) or not box:
        return False
    station = turn.map.station
    if station is None:
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
    # 只认**真有墙**的格（`turn.walls` 是我方围墙实体）：`wall_cells` 是几何，没砌的那一格若被
    # 机器人踩着照样挡路（`_ring` 因此以为环齐了、放这一支进来），照它拆就是拆一格没墙的空地。
    live = {w.pos for w in turn.walls}
    free = [
        (role.pos.dist(c), c)
        for c in wall
        if c in live and any(step_outside(v.pos, box, walk - {c}, size) is not None for v in stuck)
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


# ── 夜里：回炮位、开火 ──────────────────────────────────────────────


def _stands_on_a_post(role: BaseRole, turn: Turn) -> bool:
    """这个角色是不是正站在某组的操作位上（多座组那格要踩上去；单座组的岗位就是炮自己）。"""
    blocked = _passable(turn)
    return any(
        role.pos in _operator_spots(g, blocked, turn.map.size) for g in _weapon_groups(turn)
    )


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
    那个操作位**走（那格要踩上去才同时贴着两座，`step_onto` 就是为它加的）。
    不换组 —— 每回合重挑会让角色在炮位之间来回走。场上没有武器 / 都够不着 ⇒ 不动。

    岗位去不了时**不许整组无人可打**：主岗位被非我方单位堵死（机器人踩着那一格）或者走不
    进去（进洞那条一格宽的走廊被堵）⇒ 退到 `_near_spots` 给的邻座格子上，只打得了其中一座
    也照打。岗位**被同事占着**不在此列 —— 站在那格上的人贴得到每一座，那一组归他。

    开拓者是**补位炮手**（`_pioneer_mans_guns`）：工人够操满所有组时它一个组都不认领 ——
    炮位留给工人，它腾出来（用户口径）。例外：它已经站在某组的操作位上 ⇒ 认领那一组 ——
    "工人够操满"是名册口径、"谁真站得上岗位"才是事实，它蹲在岗位上一发不打、工人又进不去
    （岗位只有一格），整组就白丢一夜。
    """
    if turn.round_no < 0:
        return
    # 工人够操满所有组 ⇒ 炮位留给工人，开拓者一个组都不认领（补位炮手）
    if (
        isinstance(role, Pioneer)
        and not _pioneer_mans_guns(turn)
        and not _stands_on_a_post(role, turn)
    ):
        return
    groups = _weapon_groups(turn)
    blocked = _passable(turn)
    # 退路（贴着组内任意一座的格子）：主岗位**一个都站不上**时才用 —— 二选一时站位更重要，
    # 主岗位能站就先站（多座组那格交替得起来，退路只守得了一座）。
    fallbacks: list[tuple[Pos, tuple[Weapon, ...]]] = []
    for group in sorted(groups, key=lambda g: (min(role.pos.dist(w.pos) for w in g), min(w.id for w in g))):
        if any(w.pos in taken for w in group):
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
        spare = _operator_spots(group, blocked, turn.map.size)
        spots = _post_spots(group, turn, role)
        if spots:
            spot = min(spots, key=lambda s: (role.pos.dist(s), s))
            if q.step(role, spot, onto=onto):
                for w in group:
                    taken.add(w.pos)  # 认领发生在动身之后
                return
        elif spare and not adjacent:
            continue  # 岗位被同事占着 ⇒ 那一组归他（他站得上去、交替得起来），换下一组
        if not adjacent:
            # 主岗位站不上（被堵死 / 走不到）⇒ 记下退路，别的组都没得站时再用（走路意图进第二段）
            near = _near_spots(group, blocked, turn.map.size)
            if near:
                fallbacks.append((min(near, key=lambda s: (role.pos.dist(s), s)), group))
    for spot, group in fallbacks:
        if any(w.pos in taken for w in group):
            continue
        if q.step(role, spot, onto=True):
            for w in group:
                taken.add(w.pos)
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


def _alive(robots: tuple[Robot, ...]) -> tuple[Robot, ...]:
    """这一回合真在场上（`health != 0`）的机器人。

    `model._robots` 不像 `_walls` / `_character` 那样丢掉已毁的（`_destroyed` 的 docstring 写明
    "机器人不走那里"）⇒ 判空、看射程、记账一律得过这道筛子，别直接读 `turn.robots`。
    机器人的 `health` 字段缺失给 -1（不是 0）⇒ 未知的照旧算活着（少打不如照打）。
    """
    return tuple(r for r in robots if r.health != 0)


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
    - `targetPos` 的**个数必须等于武器等级**（接口文档 L218，多一个少一个都是指令非法）：
      电磁狙击炮恒 1 个，加特林/火箭 = 等级数。多发**全部指向同一个最优落点** —— 火箭同点
      叠加、加特林同弹道连续吃掉同一台；
    - 火箭（`_rocket_site`）：落点任选 ⇒ 中心 + 溅射全场算账，取总分最高的落点；
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
        _book_rocket(weapon, target, foes, assigned)
    else:
        victim = _beam_site(weapon, foes, assigned)
        if victim is None:
            return False
        target = victim.pos
        assigned[victim.pos] = assigned.get(victim.pos, 0) + _beam_damage(weapon)
    count = 1 if weapon.kind == "railgun" else weapon.level
    # key 是武器 id，操控角色在报文的 `controllerId` 里
    fired = _emit(
        cmds, role, actions.Attack, str(role.id), (target,) * count, key=str(weapon.id)
    )
    if fired and weapon.kind == "rocket":
        _fired[weapon.id] = turn.round_no  # 判题器不发 cooldown ⇒ 发出的那发自己记
    return fired


def _beam_damage(weapon: Weapon) -> int:
    """加特林/电磁这一炮打在一个落点上的总伤害：每级 +10（发出去的子弹/能量份数 = 等级）。

    加特林的多颗子弹同落点 ⇒ 同一条弹道连着吃掉最近那台；电磁的一束能量翻倍。只用来挑目标
    与记账，实际命中由判题器算。
    """
    base = GATLING_SHOT if weapon.kind == "gatling" else RAILGUN_ENERGY
    return base * weapon.level


def _rocket_damage(weapon: Weapon) -> tuple[int, int]:
    """火箭这一炮的 `(中心, 溅射)` 伤害：导弹数 = 等级、同落点叠加 ⇒ 每级 +20 / +10。"""
    return ROCKET_CENTER * weapon.level, ROCKET_SPLASH * weapon.level


def _beam_site(
    weapon: Weapon, robots: tuple[Robot, ...], assigned: Mapping[Pos, int]
) -> Robot | None:
    """加特林/电磁的目标：有效伤害最高的那台（并列打近的、再并列按坐标序）。

    "有效伤害" = min(伤害, 剩余血)：对满血机器人（≥40 血）等额，差别只在将死者 —— 别把整发
    浪费在已被打得差不多的人身上（`assigned` = 本回合先开火的炮记的账）。够得着的目标全是
    将死的（有效 ≤ 0）⇒ 不打。
    """
    shot = _beam_damage(weapon)

    def effective(r: Robot) -> int:
        return min(shot, max(0, r.health - assigned.get(r.pos, 0)))

    reach = [r for r in _alive(robots) if weapon.pos.dist(r.pos) <= weapon.attack_range]
    best = max(reach, key=lambda r: (effective(r), -weapon.pos.dist(r.pos), r.pos), default=None)
    return best if best is not None and effective(best) > 0 else None


def _rocket_site(
    weapon: Weapon,
    robots: tuple[Robot, ...],
    size: tuple[int, int],
    assigned: Mapping[Pos, int],
) -> Pos | None:
    """火箭的最大伤害落点：中心 + 周围 8 格溅射，取总分最高的格子。

    候选 = 机器人占的格与其 8 邻格（别的格子一分伤害都摸不到），且落点须在射程内 ——
    溅射可以够到射程之外的机器人（射程只管落点）。评分 = Σ min(伤害, 剩余血)
    （`assigned` = 本回合先开火的炮记的账），并列取坐标序最小（可复现）。
    越界格不进候选（越界落点 = 指令非法，红线不让赌）。
    """
    alive = _alive(robots)
    center, splash = _rocket_damage(weapon)
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
                center if cell == r.pos else splash if cell.dist(r.pos) == 1 else 0,
                max(0, r.health - assigned.get(r.pos, 0)),
            )
            for r in alive
        )

    in_range = [cell for cell in cands if weapon.pos.dist(cell) <= weapon.attack_range]
    return min(in_range, key=lambda cell: (-score(cell), cell), default=None)


def _book_rocket(
    weapon: Weapon, target: Pos, robots: tuple[Robot, ...], assigned: dict[Pos, int]
) -> None:
    """把火箭这一发的估计伤害记到账上（同回合后开的炮按剩余血挑目标）。"""
    center, splash = _rocket_damage(weapon)
    for r in _alive(robots):
        hit = center if r.pos == target else splash if r.pos.dist(target) == 1 else 0
        if hit:
            assigned[r.pos] = assigned.get(r.pos, 0) + hit
