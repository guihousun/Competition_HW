"""决策入口：`Turn` → `roleCommandMap`，外加响应顶层的 `(prompt, executeCmd)`。**策略只写在这里。**

每个角色每回合只有一个动作（顺序见 `docs/策略指导.md`）：

- **白天·开拓者**：去最近一个能接的任务点 `acceptTask`；领到就被钉死（见 `plan`）。
- **白天·工人**：修墙 → 采石砌墙 → 建武器 → 卖矿 → 采最值钱的矿，一件不行才轮到下一件；
  **修墙/砌墙是最高优先级**（第一天必须把墙建好）；**环砌完后的白天末尾提前回炮位**（第 33 步）。
- **夜里·所有角色**（含开拓者，`attack` 的可用角色是"全部"）：认领一座武器走过去，贴着就开火。

⚠️ **两个距离口径别混用**（第 33 步）：**回合预算**（来不来得及来回）一律用 BFS 真实步数
（`steps_between`，绕障，**-1 = 走不到**）；**选点/贴着**用切比雪夫 `Pos.dist`
（`dist <= 1` 是"站在建造位/采集位/炮位旁"的判据，不是步数）。射程与溅射也是切比雪夫 —— 那是规则。

⚠️ **围墙是 18 格、背面只留中间 2 格**（第 33 步把门从整列 6 格收窄）：环几乎闭合之后，
进出只剩环内那几条走廊 ⇒ ① 回炮位/回工地的**真实步数远大于直线**（BFS 那条口径的来源）；
② "谁站在走廊格上"会频繁影响判据 —— `_walled` 因此把自己人**只在门口**算成障碍（见它的 docstring）。

返回 `{角色ID: 指令}`，key 用字符串（JSON 对象的 key 本来就是字符串）；
⚠️ **`attack` 是唯一的例外** —— 它的 key 是**武器 id**，操控者在 `controllerId` 里。

任务线不在这个返回值里：`task_channel(turn)` 单独产出 `(prompt, executeCmd)`。
"跟 LLM 说什么、怎么解析回复"在 `coregeek/agent/`；用的是包根那个单实例
`AGENT`（它身上带着两处跨回合状态：「沉淀的 SOP」整场存活；「任务内会话」`Context`
题目变了即换新 —— 第 25 步）。
"""

import logging
from collections.abc import Iterator, Mapping, Set
from typing import Any

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
    step_outside,
    step_toward,
    steps_between,
    wall_cells,
    weapon_sites,
)
from .map import COPPER, IRON, STONE
from .roles import BaseRole, Pioneer, Worker
from .world import ROUNDS_PER_DAY, Robot, Turn, Weapon

LOGGER = logging.getLogger(__name__)

#: 三座武器的**种类，下标与 `grid.weapon_sites()` 的落点一一对应**：后列火箭、前排两角加特林/电磁炮。
#: 按射程配（后列离机器人最远）；数量上限 = 角色数 = 3（§4.5.1 原文"每种 ≤3"与"全局 ≤3"矛盾，取保守的）。
WEAPONS_BY_SITE = ("rocket", "gatling", "railgun")

#: 三种武器的 **L1 伤害**（任务书 §4.5.1 表格 + §4.5.4 补充说明；升级线没做 ⇒ 恒为 L1）：
#: 加特林每颗子弹 10（沿弹道命中**最近**一台即消耗）；电磁狙击炮能量 10（沿弹道穿透、逐台扣减）；
#: 火箭中心 20、落点周围 8 格溅射 10（导弹指哪打哪、多枚叠加——L1 只有一枚）。
#: ⚠️ 机器人四种血量 40/60/500/800（§4.7.2）全都 ≥ 20 ⇒ L1 对满血机器人**不可能过量**，
#: 伤害的差别只在机器人被打残之后。
GATLING_SHOT = 10
RAILGUN_ENERGY = 10
ROCKET_CENTER = 20
ROCKET_SPLASH = 10

#: 建一座武器的金币（三种同价）。
WEAPON_COST = 25

#: 围墙的 `name` 与代价：石头×1，从**建造者自己的背包**扣（不是全队共享），拆了不返还。
WALL = "wall"
WALL_COST = 1

#: 一块石头的完整代价：采 1 回合 + 挪 1 回合 + 建 1 回合。
ROUNDS_PER_STONE = 3

#: 容错余量（回合）：留给"从矿走回工地、把石头砌完"这类收尾动作。
#: ⚠️ 第 33 步起下面的距离一律用 **BFS 真实步数**（`grid.steps_between`）⇒ 它现在是**真余量**，
#: 不再是"吸收切比雪夫低估"的补丁（那正是实盘里"人到不了炮位"的根因之一）。
#: 唯一还够不到的是"下一回合地图变了"——那是黑盒，留多少都不够，5 是拍的。
TIME_MARGIN = 5

#: 能卖给小贩的矿：三种（含多余的石头），顺序无关紧要，挑哪种由 `Turn.vendor_prices` 现算。
#: ⚠️ **石头只在"墙砌完了"那一支里才卖得出去** —— 调用点 `_build_walls` 已经保证了这一点。
SELLABLE = (STONE, IRON, COPPER)

#: 升级优先链：`(武器类别, 目标等级)`，先命中先用。判据是**群体打击**（第 29 步，用户授权）：
#: **加特林**最优先 —— +1 颗子弹 = 每回合 +10、无冷却、弹道必命中，两颗可分打两台
#: （90° 锥内），一夜 60 回合最多 +600，射程 +2 还让它更早接敌；**火箭**次之 —— +1 枚
#: 对簇约 +30/齐射，但 3 回合冷却一夜只 ~20 轮齐射、依赖机器人扎堆；**电磁**最后 ——
#: 单目标，能量对满血机器人（≥40 血）不穿透，群体价值最低。
UPGRADE_CHAIN = (
    ("gatling", 2),
    ("rocket", 2),
    ("gatling", 3),
    ("railgun", 2),
    ("rocket", 3),
    ("railgun", 3),
)

#: 升级券的商品名（`weaponShopList.name` 那套词；价目逐回合从载荷读，样例实证 100/150）。
VOUCHER = {2: "WeaponUpgradeVoucher1", 3: "WeaponUpgradeVoucher2"}

#: 基地升级券（第 42 步夜里基地升级用）。
STATION_VOUCHERS = ("StationUpgradeVoucher1", "StationUpgradeVoucher2")

#: 围墙修复包（第 42 步）：10 金、目标墙**回满血**（任务书消耗品表实证）。
WALLFIXER = "WallFixer"

#: 建筑**满血基准**（任务书 §4.5.1 表）：修墙的"半血"判据与夜里基地升级的"残血"判据用。
#: 基地 1500/3000/4500 是表格实证；**墙 L2/L3 的 1500/2000 按每级 +500 的规律推断**
#: （表格被图片截断，待实盘校准）—— 推断偏小的方向是"晚修"，安全。
WALL_MAX_HP = {1: 1000, 2: 1500, 3: 2000}
STATION_MAX_HP = {1: 1500, 2: 3000, 3: 4500}

#: 夜里怪清完后的出门半径（回炮位 BFS 步数上限，**拍的**）：机器人列表是视野过滤的，
#: "清完"只是看不见 —— 近矿限制让工人出得了事也回得了家（用户拍板保守方向）。
NIGHT_WANDER = 8

#: "顺路卖矿"的绕路上限（格）：去矿的路上，绕去小贩比直走多花不超过这么多步就顺路卖掉。
DETOUR_MAX = 2

#: 拆墙（第 34 步）—— **开洞能省下这么多步以上才值得拆**（用户拍的口径）。
#: ⚠️ 它只算**这一趟**的收益，而拆 + 补的账是：拆 1 回合 + 补 1 回合 + 再采一块补回去的石头
#: ≈ 2 回合（拆掉的那块石头不回收）⇒ **单向 4 回合上下**。所以 5 是"单趟回本点"：
#: 工人一天出/回各过一趟就赚，只出不回则打平。**5 是拍的，且是这一步唯一的调参旋钮。**
HOLE_MIN_SAVING = 5

#: 白天还剩这么多回合以上才允许拆（门要开得够久才回本）；剩这么多回合以内**必须补上**。
#: ⚠️ `HOLE_MIN_LEFT > HOLE_PATCH_LEFT` ⇒ **两个窗口不相交 ⇒ 一天最多拆一次** ——
#: 这才是防"拆了补、补了拆"净亏的真正机制（不是上面那个门槛）。两个数都是拍的。
HOLE_MIN_LEFT = 30
HOLE_PATCH_LEFT = 15


def plan(turn: Turn) -> dict[str, dict[str, Any]]:
    cmds: dict[str, dict[str, Any]] = {}
    #: 本回合已被认领的落脚格（两个人冲同一格会双双停住，任务书 §4.5.4）
    claimed: set[Pos] = set()
    #: 已被认领的**建造格**（武器与围墙共用），与 `claimed` 分开
    sites: set[Pos] = set()
    #: 已被认领的**武器**（夜里一人只能操一座）
    taken: set[Pos] = set()
    #: 本回合各机器人**已被许掉的伤害**（多炮协防的账：先开火的记上，后开的按剩余血挑目标）
    assigned: dict[Pos, int] = {}
    #: 本回合已认领的**待修墙格**（第 42 步：两个修墙工人不挤同一面墙）
    repair_taken: set[Pos] = set()
    budget = turn.gold
    slots = _slots(turn)
    #: 防御盒子的 36 格（空集 = 没基地 ⇒ 没有"里面"）
    box = box_cells(turn.map.station) if turn.map.station else frozenset()
    #: **砌满墙就会被关在盒子里的人**：闸门 (2) 按人放行，闸门 (1) 随 `gated` 传给 `_build_walls`
    leaving = _trapped(turn, box)

    #: ── **差事黑板**（第 40 步，回合内）──────────────────────────────
    #: 两个工人不再各自从 `free[0]` 取目标靠认领错开，而是从同一张黑板上领差事：
    #: 环缺口按**在场工人数切段**（A 领前段、B 领后段，沿环同向推进 —— 后段的任何一格
    #: 都不高于前段还剩下的，优先级保住；一个工人 ⇒ 整段，不掐半个环）；
    #: `ore_taken` = 本回合已认领的矿格（B 就近换一座，不跟着 A 奔同一座）；
    #: `paths` = 已预留的 BFS 路径（后处理的工人当 `avoid` 绕行，防双双停住）。
    #: ⚠️ **黑板每回合从 `Turn` 现算、不跨回合** —— 跨回合记忆卡住会静默关掉整条线（第 11 步）。
    workers_no = [r for r in turn.roles if isinstance(r, Worker)]
    gaps = _ring(turn)
    bounds = [len(gaps) * i // max(len(workers_no), 1) for i in range(len(workers_no) + 1)]
    segments = [gaps[bounds[i]: bounds[i + 1]] for i in range(len(workers_no))]
    worker_no = 0
    ore_taken: set[Pos] = set()
    paths: set[Pos] = set()

    for role in turn.roles:
        # **服任务中的开拓者：钉死。** 离开任务点周围一格任务立即作废，所以它连夜里都不回炮位。
        # 判据用载荷事实（`phase_task`），不是自己记的"谁领了任务"。放在循环最前面：与昼夜无关。
        if isinstance(role, Pioneer) and turn.phase_task:
            _answer_task(role, turn, cmds)
            continue

        # **要被关住的人先出来**：判据是 `leaving`，但**迈哪一步按"现在"的障碍算**
        # —— 墙还没砌满，缺口就是出路（按"假设砌满"算的话 `step_outside` 必返回 None，这一支永远空转）。
        if turn.is_day and role.id in leaving:
            step = step_outside(role.pos, box, turn.map.blocked | claimed, turn.map.size)
            if step is not None and _emit(cmds, role, actions.Move, step):
                claimed.add(step)
                continue

        # **有人被关在盒子里 ⇒ 工人去拆一格放人**（第 34 步，用户要求）。
        # 与上面那道闸门是**预防 vs 补救**的关系：闸门管"还没砌完时别把谁关进去"，
        # 这一支管"已经被关住了怎么办"（门让机器人堵死是闸门拦不住的）。放在收工闸门**之前**：
        # 放人比回炮位要紧，而且它自己带`day_rounds_left > HOLE_MIN_LEFT`这道时间门。
        if turn.is_day and _rescue(role, turn, cmds, claimed, box):
            continue

        if not turn.is_day:
            # **夜里**（第 42 步重排）：① 持基地券且基地残血（< 满血 1/4）⇒ 贴基地
            # `use` 升级（升级 + 回满血一次到位；基地要塌了就算机器人在门口也先升，
            # 当回合放弃开火）；② 视野里有机器人 ⇒ 回炮位开火；③ 怪清完 ⇒ **工人**
            # 出门近矿经济（近矿限制，build/remove 夜里非法、绝不发）；开拓者待命回炮位。
            if _upgrade_station(role, turn, cmds, claimed):
                continue
            if not turn.robots and isinstance(role, Worker):
                _mine_spare_ore(
                    role, turn, cmds, claimed, sites, ore_taken, paths, near=NIGHT_WANDER
                )
                continue
            _defend(role, turn, cmds, claimed, taken, assigned)
            continue

        # **白天收工**（第 33 步）：环砌完了 ⇒ 只在"离夜里第一波只剩回程步数"时才回家
        # （判据在 `_leave_for_the_post` 里）。**补墙优先于收工**，所以环没砌完时这一支不生效
        # —— 那时人继续砌/采石，而 `_stones_to_mine` 的 BFS 预算自己会拦住"来不及的远矿"，
        # 不会把人拖到天黑还在墙外。⚠️ 这一支**只发 move**，绝不能复用 `_defend`（会发 attack）。
        if not _ring(turn) and _leave_for_the_post(role, turn, cmds, claimed, taken):
            continue

        if isinstance(role, Pioneer):
            if turn.task_points:
                _take_task(role, turn, cmds, claimed)
            else:
                # 任务点全空 ⇒ **真空闲**：领"买券 → 用券"差事（第 40 步）。第 29 步那条
                # "开拓者去卖矿"因它没有矿而从未跑通（用户确认），就此退居兜底 ——
                # 接住它从任务/宝藏拿到可卖物的情形；没差事没货 ⇒ 待命（空指令合法）。
                if not _upgrade_line(role, turn, cmds, claimed):
                    _sell_ore(role, turn, cmds, claimed, frozenset())
            continue

        if not isinstance(role, Worker):
            continue  # 不该出现的角色（`roles.make` 已挡过一道）

        # **修墙 / 砌墙是最高优先级**（用户：每天的修墙都是最高优先级；第一天必须把墙建好）。
        # 先修半血墙，再砌新墙；砌不了（没石头 / 采不到 / 被闸门挡住）⇒ `_build_walls`
        # 返回 False，落到建武器、经济线 —— 工人不能什么都不做。
        if turn.is_day and _repair_line(role, turn, cmds, claimed, sites, repair_taken):
            continue

        segment = segments[worker_no] if worker_no < len(segments) else ()
        worker_no += 1
        target = segment[0] if segment else None
        if target is not None and _door_open(turn):
            target = None  # 白天中段：环上那个缺口当临时门，先别补（第 34 步）
        if target is not None and _build_walls(
            role, turn, cmds, claimed, sites,
            gated=bool(leaving), target=target,
            remaining=len(segment),
            ore_taken=ore_taken, paths=paths,
        ):
            continue

        slot = next(slots, None)
        if slot is not None and budget >= WEAPON_COST:
            kind, cell = slot
            budget -= WEAPON_COST  # **认领即预留**：宁可这回合少建，不可超支
            sites.add(cell)
            if role.pos.dist(cell) <= 1:
                if _emit(cmds, role, actions.Build, kind, cell):
                    continue
            else:
                walk = turn.map.blocked | claimed | sites | paths
                step = step_toward(role.pos, cell, walk, turn.map.size)
                if step is not None and _emit(cmds, role, actions.Move, step):
                    claimed.add(step)
                    continue

        # **经济线兜底**：修墙 / 砌墙 / 建武器都做不了 ⇒ 卖矿 → 升级 → 采闲矿。
        if not _sell_ore(role, turn, cmds, claimed, sites, paths):
            if not _upgrade_line(role, turn, cmds, claimed):
                _mine_spare_ore(role, turn, cmds, claimed, sites, ore_taken, paths)

    return cmds


def task_channel(turn: Turn) -> tuple[str, str]:
    """本回合任务的 `(prompt, executeCmd)` —— **响应顶层那两个字段的唯一来源**。

    合成一个函数是因为这条链唯一的不变量是"**两者互斥**"：拆开会把同一条链写两遍，
    任何一次单边修改都会造成"同一轮既提问又发命令"（LLM 拿过期结果作答 ⇒ 活锁）。
    本函数自己无状态（跨回合状态在 `AGENT` 实例上：SOP 与任务内会话两处）。

    判据**从上往下，先命中先返回**：

    1. **没任务，或名册里没有开拓者 ⇒ 什么都不发。** 沙盒仅任务期间可用。
       ⚠️ "开拓者还活着"是显式补的：`submitAnswer` 走 `roleCommandMap`，死了的人不在名册里
       ⇒ `_answer_task` 天生进不来；而 `executeCmd` 是响应顶层字段，不经过那道白送的闸门。
    2. **沙盒刚交作业（`cmd_result` 非空）⇒ 回灌结果、这轮绝不发命令。**
       ⚠️ **必须压在 3 前面**：`lastCmdResult` 文档明说了"未发命令时为空字符串"（不粘），
       而 `llmResp` 一个字没写 ⇒ 必须按**可能粘住**设计，否则同一条命令会被反复丢进沙盒。
    3. **回复是完整的工具调用、且工具给了命令 ⇒ 把命令交给判题器**（不提问）。
    4. **判题器说答案错了 ⇒ 带着"上次答错了"重问**（`errors` 里有 `code == 2` 且回复不是工具调用）。
       排在 3 之后：`code 2` 会连着报几轮，而 LLM 这时回了新的工具调用是进展，先让它跑。
    5. **回复是最终答案 ⇒ 本回合不发模型请求**，开拓者那边每回合在 `submitAnswer`。
       判据用 `answer_of` 而**不是**"取不出命令"：畸形的工具调用会被当成答案交上去，
       而 `_answer_task` 又跳过工具回复 ⇒ **两条通道同时哑火，永久空转**。
    6. **否则（第一次提问 / 畸形回复重问）⇒ 只把题目发出去问。**
       隐式子路径 ③′（完整工具调用但拿不到命令：`SOP2Prompt`、`python_exec`（第 45 步：
       本地执行、产出当回合就进会话 —— 重问的 prompt 窗口里 LLM 看得见，省一整个
       沙盒往返）、未知工具、空参数）也落在这里；
       它不会活锁 —— 任务期间 prompt 不限量不计数，出口有"LLM 改口给答案""纠错段""它自己写进去的 SOP 段"。

    **回合最末尾另有一道压缩闸门（第 43 步，用户拍板"回合最末尾、任务中、未生产
    模型请求才压缩"）**：判据链算完 `prompt` 还是空（只有 ③ 命令轮与 ⑤ 答案轮会这样）
    ⇒ 填上**压缩请求**。②/④/⑥ 的模型请求永远优先，压缩是这条链上优先级最低的租客；
    判据 ① 的两条早返回在闸门**之前** —— 没任务/没开拓者绝不压缩（任务线之外每游戏日
    只有 3 次额度）。压缩请求的回复（裸 `<summary>`）在链首就被路由进摘要、不进会话表，
    下一轮判据按"没回复"落 ⑥ 把任务对话推回去 —— 于是答案轮与压缩轮**交替**，互不阻塞。
    没开过会话 ⇒ `compression_request` 给 `""`，维持旧形状（降级安全）。

    工具调度只有 `AGENT.tool_call` 一个入口，副作用（SOP 沉淀）只发生在那一行，且写在判据之前
    ⇒ 走"回灌结果"那一轮 SOP 照样生效。`AGENT.hear`（把回复记进会话）也在这条链的开头：
    发命令/交答案那两轮没有 prompt，回复照样得进会话，否则回灌那一轮 LLM 看见的是
    "题目 → 莫名其妙的结果"，它自己要的命令凭空消失。⚠️ **骂的那一份必须与交的那一份
    出自同一个谓词**（`answer_of`）：交上去的是解包后的答案，骂的却是原文的话，
    LLM 会以为我们交了一堆标签。

    **本函数还负责打"本轮任务 / 上一轮模型回复 / CMD 执行结果"三样**（它们只存在于本回合的
    payload 里，黑盒下没有第二个观察窗）。⚠️ 这几行是**本项目第二条不在 `app` 名下的日志**
    （`LOGGER` 名是 `coregeek.game.planner`）⇒ 只盯 `coregeek.app` 的守卫看不见它们，
    硬约束 5 的字节预算用例必须收 root。组装出来的 `prompt` 那一行由 `app._log` 打 ——
    它拿到的是本函数**返回之后**的值。
    """
    if turn.phase_task or turn.llm_resp:
        # **必须记**：题目原文与 LLM 答了什么只存在于本回合的 payload 里。触发条件带上
        # `llm_resp`：任务刚结束那一回合 `phase_task` 已经空了，而那是唯一一次能看见
        # "判题器最后答了什么"的机会 —— 所以这一行**写在下面那道早返回之前**。
        LOGGER.info(
            "【本轮任务】：%s ｜ 【上一轮模型回复】：%s",
            _clip(turn.phase_task) or "无",
            _clip(turn.llm_resp) or "无",
        )

    if turn.cmd_result:
        # 沙盒回执**必须记**：回灌给 LLM 的就是它，"答案为什么不对"多半得从它里面看。
        # **只记结果、不记发出去的命令**：发命令那一回合 `llm_resp` 就是那次工具调用，
        # 已经印在上面那行的回复里了；而且这样"沙盒行数 = 实际跑过的命令数"。
        # 回灌给 LLM 是**全文**，这里才截断 —— 一个要正确性，一个要人眼看得下。
        LOGGER.info("【CMD命令执行结果】：「%s」", _clip(turn.cmd_result))

    reply = turn.llm_resp.strip()
    summary = is_summary_reply(reply)
    prices = is_prices_reply(reply)
    if summary is not None:
        # **压缩回复**（压缩请求的产物 —— 第 43 步起 ③/⑤ 落空的轮都发）：进摘要、
        # **不进会话表** —— 它不是 LLM 在任务上说过的话，进表会污染窗口、与【历史摘要】双份。
        # 粘住的重复路由一次 = 幂等。任务判据按"没回复"继续走（cmd_result 回灌等）。
        AGENT.adopt_summary(summary)
        reply = ""
    elif prices is not None:
        # **查价回复**（第 42 步：新闻查价的产物）：进价格期望表、不进会话表 ——
        # 与裸摘要同一条路由纪律（内容路由、永不当任务材料）。
        AGENT.adopt_price_hints(prices)
        reply = ""
    else:
        AGENT.hear(reply)  # 它自己说过的话得在会话里（发命令/交答案那轮没有 prompt，也得记）

    if not turn.phase_task:
        # **没任务 ⇒ 新闻查价**（第 42 步，判据①开口子）：同一份 news 指纹去重 ——
        # 任务线之外每游戏日只有 3 次额度。路由在上面已完成：查价回复到达的回合
        # 照样采纳、且不再重问（指纹已记）。没有 news / 问过 ⇒ 什么都不发。
        return AGENT.news_question(turn.news), ""
    if not any(isinstance(r, Pioneer) for r in turn.roles):
        return "", ""
    answer = answer_of(reply)  # 「该提交什么」与「该骂什么」是**同一份**
    call = tool_of(reply)
    command = AGENT.tool_call(*call) if call else ""  # 工具调度：副作用只发生在这一行
    #: 判题器**本轮**报的"答案不对"（`code 2`）—— 判据 ④ 的触发条件。
    rejected = any(e.code == 2 for e in turn.errors)
    #: 它自己的原话：黑盒里**唯一**能回答"错在哪一项"的东西。第 35 步起跟答案一起回灌，
    #: 在那之前它只进日志（`_clip(turn.errors)` 那行 INFO）。
    why = "；".join(e.description for e in turn.errors if e.code == 2 and e.description)
    #: ⚠️ **没答案就不纠错**（判据 ④ 的定义）：骂的那一份必须与交的那一份同源。
    #: 判题器没给描述（`why` 空）⇒ 退回第 34 步的行为：只骂答案本身，
    #: 不为"少了一句话"把整段纠错吞掉（那是判题器侧的信息缺失，不是我们该丢的信心）。
    retry = ""
    if rejected and answer:
        retry = f"{answer}\n【判题器反馈】：{why}" if why else answer

    if turn.cmd_result:  # ② 回灌结果、这轮绝不发命令（**必须压在 ③ 前**）
        prompt, cmd = AGENT.chat(turn.phase_task, result=turn.cmd_result, retry=retry), ""
    elif command:  # ③ 工具给了命令 ⇒ 交给沙盒；prompt 槽留给回合末尾的压缩闸门
        prompt, cmd = "", command
    elif retry:  # ④ 判题器说答案错了 ⇒ 带上"上次答错了"重问
        prompt, cmd = AGENT.chat(turn.phase_task, retry=retry), ""
    elif answer:  # ⑤ 我们已经拿到了答案 ⇒ 本回合任务线不产模型请求
        prompt, cmd = "", ""
    else:  # ⑥ 第一次提问 / 畸形或"不产出命令"的工具回复 ⇒ 只把题目问出去（**③′ 落在这里**）
        prompt, cmd = AGENT.chat(turn.phase_task), ""
    # **回合最末尾的压缩闸门**（第 43 步，用户拍板"回合最末尾、任务中、未生产模型请求
    # 才压缩"）：②/④/⑥ 的模型请求永远优先，压缩只填 ③/⑤ 落空的 prompt 槽 —— 这条链
    # 上优先级最低的租客。没开过会话 ⇒ `compression_request` 给 ""，维持旧形状（降级安全）。
    if prompt == "":
        prompt = AGENT.compression_request()
    return prompt, cmd


def _slots(turn: Turn) -> Iterator[tuple[str, Pos]]:
    """本回合可以开建的 `(武器类别, 落点)`，按优先级排；没名额就一个都不产出。

    三道门槛：**白天**（`build` 仅白天）、**份额** = `len(roles)`（武器数 < 角色数）、
    **落点是空的**（建在已有武器上会把它覆盖成 level1，25 金币打水漂还降级）。
    基地没了就没有可建造区 ⇒ 不建（故意的降级方向）。
    """
    if not turn.is_day:
        return
    station = turn.map.station
    if station is None:
        return

    have = {w.kind for w in turn.weapons}  # 名册在 `Turn.weapons`，网格里那份不用
    need = len(turn.roles) - len(turn.weapons)
    if need <= 0:
        return

    blocked = turn.map.blocked
    #: **先按种类配对、再滤**。反过来（先滤种类再 `zip` 落点）落点会整体前移 ——
    #: 后列那格被占时"少一座火箭"会把加特林塞进给火箭留的格子上，而落点与种类是绑死的。
    yield from [
        (kind, cell)
        for kind, cell in zip(WEAPONS_BY_SITE, weapon_sites(station, turn.map.size[0]))
        if kind not in have and cell not in blocked
    ][:need]


# ── 开拓者的任务线 ──────────────────────────────────────────────────
def _take_task(
    role: BaseRole, turn: Turn, cmds: dict[str, dict[str, Any]], claimed: set[Pos]
) -> None:
    """白天：走到最近一个**能接**的任务点旁边，贴着就 `acceptTask`。

    与 `build` / `collect` 同一条契约：任务点挡路，`step_toward` 天然停在贴着它的一格，
    而那正是"周围一格内"。只认锚点格就够（锚点本身就是任务点的一格）。

    一个能接的点都没有（都在冷却 / 已做完）⇒ 什么都不发，**不去冷却中的点蹲守**。
    ⚠️ 领到任务之后本函数就再也进不来了：`plan` 最前面那道分支会先一步接管。
    """
    if not turn.task_points:
        return
    # "已经贴着就领"与"走过去"分两步判：合成一步会在站在两个任务点中间时舍近求远。
    if min(role.pos.dist(p) for p in turn.task_points) <= 1:
        _emit(cmds, role, actions.AcceptTask)
        return
    # 并列按坐标排：先后不能取决于 payload 里的顺序，否则用例复现不了。
    target = min(turn.task_points, key=lambda p: (role.pos.dist(p), p))
    _step(role, target, turn, cmds, claimed)


def _answer_task(role: BaseRole, turn: Turn, cmds: dict[str, dict[str, Any]]) -> None:
    """服任务中：把手上的答案**原样**交上去。**这里从来不移动**（挪出任务点周围一格任务即作废）。

    答案 = `answer_of(llmResp)`：先挖掉工具块、认块外 `<answer>…</answer>` 里的内容，
    再不然**原文即答案**（工具块**内**的 `<answer>` 一律不算 —— 那多半是 SOP 正文里的示例）。
    **空答案不发**（可能被判成"字段缺失"，那是红线里的"指令非法"）。

    **每回合都交**：判题器按"提交过的通过率最高的答案"算分，反复提交是预期用法；
    开拓者被钉在这里也没有第二个动作可做 —— 这也是任务线不需要跨回合状态的全部理由。
    ⚠️ **工具调用绝不能当答案交上去**，而且交的这一份必须与 `task_channel` 判据 ④ 骂的那份同源。
    """
    answer = answer_of(turn.llm_resp)
    if answer:
        _emit(cmds, role, actions.SubmitAnswer, answer)


# ── 白天：采石 → 砌墙 ────────────────────────────────────────────────
def _build_walls(
    role: Worker,
    turn: Turn,
    cmds: dict[str, dict[str, Any]],
    claimed: set[Pos],
    sites: set[Pos],
    gated: bool = False,
    target: Pos | None = None,
    remaining: int = 0,
    ore_taken: set[Pos] | None = None,
    paths: set[Pos] | None = None,
) -> bool:
    """白天：先攒石头，再把墙砌到**黑板分给的那一格**上。

    返回 `True` = 本回合发了指令（采矿 / 挪开 / 砌墙 / 走向矿或墙）；
    `False` = 砌不了（没石头、采不到、被闸门挡住）⇒ 调用方接着走修墙 / 建武器 / 经济线，
    **工人不能什么都不做**（用户原话）。

    每回合独立判定，不存跨回合状态 —— 矿采没了、墙被别人砌了，下一回合都能自动跟着变。

    `target` / `remaining` = 黑板（`plan`）切段分给本工人的那一格与本段**还剩几格**
    （第 40 步：A 领前段、B 领后段沿环推进，两人不再都砍 `free[0]` 靠认领错开一格 ——
    那会让 B 的目标贴着 A 的目标，两人在同一段墙上挤）。

    `gated` = **这一回合不许砌**（砌下去会把谁关在墙里，见 `_trapped`）。
    返回 `False` 让调用方走其他差事，而不是原地待命（用户：工人不能什么都不做）。

    `ore_taken` / `paths` = 黑板的另两本账：**矿格认领**（本工人挑中即登记，别的工人
    这回合不再奔它 —— 旧口径"不认领矿"在两人都要石头时就是抢资源）与**路径预留**
    （动身前把 BFS 路径记进去，后处理的工人绕着走，防双双停住）。
    """
    if target is None:
        return False  # 没分到墙格 ⇒ 调用方走经济线
    ore_taken = set() if ore_taken is None else ore_taken
    paths = set() if paths is None else paths
    if gated:
        return True  # 砌下去会把人关住 ⇒ 待命（安全：别跑远，下回合缺口还在）
    #: **只认石矿**：墙只吃石头，铁/铜再多也砌不了墙。**排除本回合别人认领的矿格**
    #: （第 40 步）—— B 就近换一座，不跟着 A 奔同一座。
    #: ⚠️ **认领只发生在"真要采"之后**（第 44 步）：石头已够的工人（want=0）不该
    #: 占住矿格 —— 它这回合用不上，却会把缺石的同事挤去经济线，环白白慢一拍。
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
            if _emit(cmds, role, actions.Collect, mine):
                return True
        # 还没走到矿边：走一步，**动身成功才预留这条路**（"我在走这条线"才对别人成立；
        # 先预留再走会让工人绕开自己刚记下的路 —— 可绕时就地多绕三步，实测过）。
        # avoid 里的 `paths` 此时只含先处理工人的账，不含自己的。
        elif _step(role, mine, turn, cmds, claimed, sites | paths):
            _reserve_path(role, mine, turn, claimed, paths)
            return True

    if role.stone >= WALL_COST:
        # **认领要发生在动身之前**：等砌完再登记的话，另一个工人会在同一回合也奔着它去。
        sites.add(target)
        if role.pos == target:
            # ⚠️ **站在目标格上就先挪开一格，这一回合不砌**：人站在墙上时那一格在网格里
            # 只剩"worker"（单位铺在最后，墙被盖掉）⇒ 看不出砌过没有，而 `_ring` 的
            # "自己人算路过"又把它复活成候选 ⇒ 每回合对同一格 `build`（`dist == 0` 也是合法
            # 建造位），永远轮不到下一格、石头白花、`free` 永不为空（连卖矿那一支都进不去）。
            # 挪开一格两个方向都收敛：砌过的没人站着就现形；没砌过的下一回合从邻格稳稳砌上。
            if _step_aside(role, turn, cmds, claimed, sites):
                return True
        elif role.pos.dist(target) <= 1:
            # 与建武器同一条契约：`step_toward` 停在贴着目标的一格，那正是 `build` 的站位
            if _emit(cmds, role, actions.Build, WALL, target):
                return True
        elif _step(role, target, turn, cmds, claimed, sites | paths):
            _reserve_path(role, target, turn, claimed, paths)
            return True
    return False  # 没石头、采不到 ⇒ 调用方走其他差事（用户：工人不能什么都不做）


def _walled(turn: Turn) -> frozenset[Pos]:
    """"**假设墙砌满**"时的障碍集：现已挡路的照原样 + 全部 18 格墙。

    闸门问的是**将来** —— 现在走得出去不代表砌完还走得出去。

    ⚠️ **第 34 步起补救的通道有了**（`remove` + `_rescue`），但那**不构成撤销这道闸门的理由**：
    闸门是**预防**（零成本、砌墙的那一刻就避开），`_rescue` 是**补救**（1 回合 + 1 块不退的石头
    + 一整天的洞）。两者并存，各管各的 —— 别因为"反正能救"就把闸门拆了。

    ⚠️ **自己人算不算障碍，只看门那 2 格**（与 `_ring` 的"自己人一律算路过"故意相反，
    但也不是一律算障碍）：那 18 格墙里没有任何建筑 ⇒ **能堵门的只有单位**，
    所以"自己人站在格子上"这个问题**只在门口有意义**。环内那 16 格（基地 + 武器环）
    站着的自己人是**过路的**：那不是墙，他下一回合就走。
    ⚠️ **第 33 步实测的代价**（门从 6 格收到 2 格之后暴露出来的）：环一闭合，进出只剩
    环内那几条走廊，于是"一个工人正好站在走廊格上"变成常事。旧口径把他也算成墙 ⇒
    走廊另一头的工人被判成"砌满就出不去"，闸门 (2) 把他送出去一格；下一回合他不被围了、
    又走回来砌墙 —— **两人之间来回踱步，一整天不砌墙**（合成局面上 16 个回合 0 座）。
    只认门口那 2 格之后这条消失，而**真正会关人的局面照样接得住**（门那 2 格真被占住时
    一样判成被围，见 `test_a_colleague_in_the_door_still_holds_the_wall_back`）。
    """
    station = turn.map.station
    if station is None:
        return frozenset()
    blocked = turn.map.blocked
    #: 自己人站在门那 2 格上的，照旧算障碍；站在别处的从障碍里摘掉（见 docstring）
    door = set(door_cells(station, turn.map.size[0]))
    mine = {r.pos for r in turn.roles} - door
    return (blocked - mine) | set(wall_cells(station, turn.map.size[0]))


def _trapped(turn: Turn, box: frozenset[Pos]) -> frozenset[str]:
    """**砌满这一圈墙之后就走不出去了的**我方角色 id；没有就空集。

    判据是"整面墙"不是"某一格"：障碍集里永远有全部 18 格墙，真发生就是整面墙一起发生
    ⇒ 调用方要的是一个布尔量（该不该砌），而不是一串"可以砌的格子"。
    返回 id 是因为配套的闸门 (2) 得知道**谁**先出来，逐角色问 `step_outside` 才对
    "站在自己那格上的人"天然正确。

    ⚠️ 第 33 步把门从"整列 6 格"收成"背面中间 2 格" ⇒ 能堵门的单位从 6 个降到 2 个
    ⇒ 这条判据反而**更常真的命中**（以前要把 6 格全堵满才合得上，现在两个人就够）。
    """
    station = turn.map.station
    if station is None or not box:
        return frozenset()  # 没基地 ⇒ 既没有盒子也没有围墙，谁都关不住
    walled = _walled(turn)
    return frozenset(
        r.id
        for r in turn.roles
        # ⚠️ `r.pos in box` 这一半不能省：`step_outside` 对"本来就在外面"也返回 None，
        # 不看这一半会把**所有在盒外干活的角色**判成被关住（于是墙一格都不砌）。
        if r.pos in box and step_outside(r.pos, box, walled, turn.map.size) is None
    )


def _ring(turn: Turn) -> tuple[Pos, ...]:
    """按优先级排的围墙格；基地没了 ⇒ 空。

    既不在 `wall_cells` 里、也不挡路的格才算候选 —— 但**自己人站的那一格除外**。
    工人站在待砌的墙格上只是路过，若把它划掉，另一个工人的 `free[0]` 会整体后移、
    等那人一挪窝目标又变回来 —— 两个工人在两格之间对着改目标，一格都砌不上。

    这里只管"哪些格能砌"（几何 + 占用），"这一回合还砌不砌"是 `_trapped` 的事，两者正交。
    """
    station = turn.map.station
    if station is None:
        return ()
    #: 我方角色当前站的格（角色能走的都在这）
    mine = {r.pos for r in turn.roles}
    return tuple(
        c for c in wall_cells(station, turn.map.size[0]) if c not in turn.map.blocked or c in mine
    )


# ── 拆墙（第 34 步）：临时门 ─────────────────────────────────────────
def _after_first_day(turn: Turn) -> bool:
    """是不是**第 2 天及以后** —— 整套"临时门"机制成立的前提。

    ⚠️ **为什么必须有这一条**：环上"孤零零一个缺口"这一个状态，**既是"刚挖的洞"、
    也是"还差一格没砌"** —— 地图上逐字节同形，任何无状态判据都分不开。
    区分只能靠**时间**：第 1 天环在建，缺口是真的没砌（`_ring` / `_build_walls` 现在就是对的，
    一个字都别改）；**第 2 天起环在开局时必然是满的，于是环上一切缺口只可能来自我们自己的
    `remove`**。没有这一条，第 1 天砌到只剩中段某一格时那一格会被当成"洞"推迟到当天末尾，
    白天再也不补（合成局面上会直接挂掉 `test_a_worker_on_the_last_cell_still_finishes_the_ring`）。

    代价（记在 `code-task.md` 的不确定性里）：第 1 天没砌完的残局，次日起会被误读成"门"、
    推迟到当天末尾才补 —— 而末尾一定会补（见 `HOLE_PATCH_LEFT`）。
    """
    return turn.round_no > ROUNDS_PER_DAY


def _door_open(turn: Turn) -> bool:
    """白天中段：环上的缺口当**临时门**用，先别补（第 2 天起、离天黑还早）。

    补墙的时刻由它和 `HOLE_PATCH_LEFT` 一对窗口决定 —— "白天开着通行、天黑前封死"，
    与封口格 `(13,24)` 是同一个语义，只是从"排在建墙顺序最后"换成了"排到时间窗最后"。
    """
    return _after_first_day(turn) and turn.day_rounds_left > HOLE_PATCH_LEFT


def _dig(
    role: BaseRole,
    goal: Pos,
    turn: Turn,
    cmds: dict[str, dict[str, Any]],
    claimed: set[Pos],
    walk: Set[Pos],
) -> bool:
    """工人**贴着一格墙**、而拆了它能少走 `HOLE_MIN_SAVING` 步以上 ⇒ 这一回合拆它。

    钩在 `_step` 里而不是各个差事里：`_step` 是所有"走路"的公共出口（采矿 / 卖矿 / 顺路卖 /
    升级 / 砌墙 / 回炮位），一处就能覆盖全部差事，调用点一个字不用改。

    **"出不来"不用第二套逻辑**：出不去 ⇒ 到差事目标的步数是 **-1**，而这里对 `now < 0`
    的处理本来就是"拆了能到就更该拆"，两个触发条件在这一个函数里合流。

    ⚠️ **但实测过：`now < 0` 这一支今天是进不去的**（各差事 `_step` 之前都已经把
    "走不到的目标"滤掉了 —— `_mine_spare_ore` / `_sell_ore` / `_detour_sell` / `_upgrade_line`
    四处都有 `-1 ⇒ 这趟不去`），而且"人在盒里出不来"那种局面会先被 `plan` 里的 `_rescue`
    接管。它**仍然保留**，理由是 `_step` 的 `walk` 比各差事自己算的那份多了 `claimed` / `avoid`
    两样 ⇒ 差事算得通、走到 `_step` 时却不通是可能的。删掉它的代价是"工人安静地不动"
    （正是本步要治的那类症状），留着的代价只是一次条件判断。**别当它是死代码顺手删。**
    唯一不用它的是**被任务钉死的开拓者**（走 ① 支路，压根到不了这里）与非工人 —— 那由 `_rescue` 管。

    ⚠️ **候选只取当前已经贴着的墙格**（`Pos.dist <= 1`，`remove` 的站位要求就是这么定的）。
    刻意**不做**"走到最优的那一格再拆"：那会引入"在路上"的中间态（目标每回合重算 ⇒ 来回抖），
    而且省下的步数没有扣掉走过去的回合 ⇒ 系统性高估收益。沿墙走的过程里这种机会很多，不必提前跑。
    """
    if not turn.is_day or not isinstance(role, Worker):
        return False
    station = turn.map.station
    if station is None or not _after_first_day(turn):
        return False
    wall = wall_cells(station, turn.map.size[0])
    #: 环已砌满（⇒ 同时最多一个洞）；`claimed` 里出现墙格 ⇒ 本回合已经有人拆过了
    if _ring(turn) or claimed & set(wall):
        return False
    if role.stone < WALL_COST or turn.day_rounds_left <= HOLE_MIN_LEFT:
        return False

    size = turn.map.size
    now = steps_between(role.pos, goal, walk, size)
    best: tuple[int, Pos] | None = None
    for cell in wall:
        if role.pos.dist(cell) > 1:
            continue  # 直接排除：拆墙要贴着，走不到的那一格这一回合根本没得拆
        after = steps_between(role.pos, goal, walk - {cell}, size)
        if after < 0:
            continue  # 开了这个洞也还是到不了 ⇒ 白拆
        # `now < 0` = 现在压根走不到（出不去）⇒ 拆了能到就该拆，省下的步数没有意义
        if now >= 0 and now - after < HOLE_MIN_SAVING:
            continue
        if best is None or (after, cell) < best:
            best = (after, cell)
    if best is None:
        return False
    #: 登记被拆的那一格（它本来就在 `blocked` 里 ⇒ 对别人的寻路是空操作，但能挡住同回合的第二个工人）
    claimed.add(best[1])
    return _emit(cmds, role, actions.Remove, best[1])


def _stuck_inside(turn: Turn, box: frozenset[Pos]) -> tuple[BaseRole, ...]:
    """**现在真的走不出盒子**的人；没有就空元组。

    与 `_trapped` 的区别是**用"现在"的障碍**而不是"假设砌满"：闸门问的是将来（预防），
    这里问的是眼下（补救）。两者并存，各管各的。

    ⚠️ **自己人一律不算障碍**（除起点外把全部角色从障碍里摘掉）：同事站在门格上只是路过，
    下一回合就走 —— 把他算成障碍会**白拆一次**（1 回合 + 1 块不退的石头）。
    这正是第 33 步改 `_walled` 口径要治的那个活锁的同一条道理。
    """
    if not box:
        return ()
    walk = turn.map.blocked - {r.pos for r in turn.roles}
    size = turn.map.size
    return tuple(
        r for r in turn.roles if r.pos in box and step_outside(r.pos, box, walk, size) is None
    )


def _rescue(
    role: BaseRole,
    turn: Turn,
    cmds: dict[str, dict[str, Any]],
    claimed: set[Pos],
    box: frozenset[Pos],
) -> bool:
    """有人被关在盒子里 ⇒ **工人**去拆一格放人（第 34 步）。

    与 `_dig` 的分工：`_dig` 治"**自己**要去的地方到不了"，这里治"**别人**（或**自己**）出不来"。
    ⚠️ **工人被关住时也走这里，而且先走这里**（第 34 步真服务实测：门被两台机器人堵死、
    工人在盒内 ⇒ 它自己 `remove (13,23)`，压根没轮到差事）—— `_rescue` 在 `plan` 里排在
    白天所有差事之前。`_dig` 的 `now < 0` 那一支因此是**第二道**（留给"差事目标不通、
    但人还出得去"这种局面），不是唯一一道。**被任务钉死的开拓者**才非靠别人救不可
    —— 它走 ① 支路 `continue`，永远到不了这一段（用户拍板：开拓者被挡 ⇒ 工人去救）。

    开哪一格：**开了之后真能让某个被困的人迈出去、且离救援者最近的那一格**
    （并列取坐标序，可复现）。判据用"不开洞 + 本回合已认领的落脚格"，与 `_dig` 同一套账。
    ⚠️ 门被**机器人**堵死时 `walk` 不动那一格 ⇒ 命中；被**同事**堵住时 `_stuck_inside`
    先一步就把人放出来了（自己人不算障碍）⇒ 这里不命中 —— 那正是"不白拆一次"的意思。
    """
    if not turn.is_day or not isinstance(role, Worker) or not box:
        return False
    station = turn.map.station
    if station is None or not _after_first_day(turn):
        return False
    wall = wall_cells(station, turn.map.size[0])
    if _ring(turn) or claimed & set(wall):
        return False
    if role.stone < WALL_COST or turn.day_rounds_left <= HOLE_MIN_LEFT:
        return False
    stuck = _stuck_inside(turn, box)
    if not stuck:
        return False

    walk, size = turn.map.blocked | claimed, turn.map.size
    free = [
        (role.pos.dist(c), c)
        for c in wall
        if any(step_outside(v.pos, box, walk - {c}, size) is not None for v in stuck)
    ]
    if not free:
        return False
    site = min(free)[1]
    if role.pos.dist(site) <= 1:
        #: 登记被拆的那一格：挡住同回合的第二个工人（对寻路是空操作，它本来就在 `blocked` 里）
        claimed.add(site)
        return _emit(cmds, role, actions.Remove, site)
    #: 走 `_step` 会再进一次 `_dig`（同一个目标，多半不命中：省不到 5 步）—— 两条路都只是
    #: "朝那一格挪一格"，贴近了下一回合自然就拆。
    return _step(role, site, turn, cmds, claimed)


def _half_walls(turn: Turn) -> tuple[Pos, ...]:
    """血量不足一半的**已砌墙**（第 42 步修墙差事的目标表），按坐标序（可复现）。

    满血基准按等级查 `WALL_MAX_HP`（升级后回满血）。⚠️ health 缺失（-1）⇒ 未知 ⇒
    **不修** —— 不为一格读不出血量的墙白跑；已毁（0）的墙在 `model._walls` 就丢了
    （那是一格缺口，归 `_ring` 管重建）。
    """
    return tuple(
        w.pos
        for w in sorted(turn.walls, key=lambda w: w.pos)
        if 0 < w.health and w.health * 2 < WALL_MAX_HP.get(w.level, WALL_MAX_HP[1])
    )


def _repair_line(
    role: Worker,
    turn: Turn,
    cmds: dict[str, dict[str, Any]],
    claimed: set[Pos],
    sites: set[Pos],
    taken: set[Pos],
) -> bool:
    """半血墙的修复差事（第 42 步，用户拍板"**包优先、重建兜底**"）。
    返回 `True` = 这一轮归它了。

    与 `_upgrade_line` 同构的两阶段（拿没拿包看**背包**，跨夜保留）：

    ① **持包**：走到最近的半血墙（**认领在动身之前**，两个修墙工人不挤同一面），
       贴着就 `use WallFixer`（目标 = 墙坐标；墙回满血、不塌、1 回合 + 10 金 ——
       全面占优推倒重建的 2 石头 + 2 回合 + 洞开 2 回合）；
    ② **没包**：商店可达、价目里有它、金币够 ⇒ 走去商店 `Buy`；
    ③ **没包也买不了** ⇒ **重建兜底**：贴着半血墙且有石头 ⇒ `remove`
       （下一回合那格自然进 `_ring`、走建造）。⚠️ 天黑前砌不回来的洞等于白开
       —— 沿用 `_dig` 的时间门（`day_rounds_left > HOLE_PATCH_LEFT` 才拆）。

    **只在白天跑**（挂在白天支路上；修墙是防御工事的一部分，与 build 同一条昼夜口径）。
    """
    broken = _half_walls(turn)
    if not broken:
        return False
    walk, size = turn.map.blocked, turn.map.size
    budget = turn.day_rounds_left - TIME_MARGIN
    if WALLFIXER not in role.bag:
        price = turn.shop_prices.get(WALLFIXER, 0)
        hops = [(steps_between(role.pos, s, walk, size), s) for s in turn.map.shops]
        hops = [(d, s) for d, s in hops if d >= 0]
        to_shop, shop = min(hops) if hops else (-1, None)
        if shop is not None and price > 0 and turn.gold >= price and to_shop + 1 <= budget:
            if to_shop <= 1:
                return _emit(cmds, role, actions.Buy, WALLFIXER, 1)
            return _step(role, shop, turn, cmds, claimed, sites)
        # 买不了 ⇒ 重建兜底
        if role.stone >= WALL_COST and turn.day_rounds_left > HOLE_PATCH_LEFT:
            near = [p for p in broken if role.pos.dist(p) <= 1 and p not in taken]
            if near:
                taken.add(near[0])
                return _emit(cmds, role, actions.Remove, near[0])
        return False
    target = min(
        (p for p in broken if p not in taken),
        key=lambda p: (role.pos.dist(p), p),
        default=None,
    )
    if target is None:
        return False
    taken.add(target)
    if role.pos.dist(target) <= 1:
        return _emit(cmds, role, actions.Use, WALLFIXER, target)
    to_wall = steps_between(role.pos, target, walk, size)
    if to_wall < 0 or to_wall + 1 > budget:
        return False  # 来不及 ⇒ 待命，明天接着走
    return _step(role, target, turn, cmds, claimed, sites)


def _stones_to_mine(role: Worker, turn: Turn, target: Pos, mine: Pos | None, free: int) -> int:
    """这一趟还该采几块石头 —— 按**回合预算**每回合现算。

    记 `s` = 手里的石头、`k` = 还要采的块数，整件事的回合数：

        走到矿 + 采 k 块 + 从矿走回工地 + 砌 s+k 座的"挪一格 + 建造"
        = d_mine + d_wall + 2s − 1 + 3k        # 到达工地那一步已贴着首格，故 −1
                                            # ⇒ 每多采一块**净**花 3 回合

    令它 ≤ `白天还剩的回合 − TIME_MARGIN`，解出 k，再与"还差几格墙"取小
    （没有转移物品的指令，多采的石头给不了别人，只防采得离谱，不必精确分账）。

    **距离一律用 BFS 真实步数**（第 33 步）：回工地常常要绕整面围墙、从背面那 2 格门进来，
    切比雪夫会把 10+ 步说成 3 步，于是采到天黑都走不回来（实盘问题 ②）。`TIME_MARGIN` 现在是真余量。
    ⚠️ **-1（走不到）⇒ 一块都别采**：切比雪夫永远给不出这个值，是本步新增的失败态，
    往"少干一件事"的方向退化（安全：宁可这回合不动，不可把人留在墙外过夜）。
    """
    if mine is None:
        return 0
    walk, size = turn.map.blocked, turn.map.size
    to_mine = steps_between(role.pos, mine, walk, size)
    to_wall = steps_between(mine, target, walk, size)
    if to_mine < 0 or to_wall < 0:
        return 0
    budget = turn.day_rounds_left - TIME_MARGIN - to_mine - to_wall - 2 * role.stone + 1
    return max(0, min(budget // ROUNDS_PER_STONE, free - role.stone))


def _sell_ore(
    role: BaseRole,
    turn: Turn,
    cmds: dict[str, dict[str, Any]],
    claimed: set[Pos],
    sites: set[Pos],
    paths: set[Pos] | None = None,
) -> bool:
    """把小贩肯收的矿背过去换金币。**这一回合没去卖就返回 `False`**（调用方接着去采）。

    卖的可用角色是**全部**（§4.4）—— 工人（墙砌完后的主线）与开拓者（任务点全空时，
    第 29 步）都走这里。四条门，任一条不成立就 `False`：

    ① **有货**：`_best_load` 从 `SELLABLE` 里挑收购价最高的一种；
    ② **有小贩**：`Map.vendors` 空、或**一个都走不到**就无处可卖（第 33 步的 -1）；
    ③ **够本**：**已经不贴着才算** —— 货值 < 往返回合数（`2 × 步数`）就留在矿边接着采
       （已经站在小贩旁边时这趟路早付过了，不再拦）。⚠️ "1 金币 ≈ 1 回合"是拍的，唯一的调参旋钮；
    ④ **回得来**：`走到小贩 + 从小贩回基地 ≤ 白天剩余 − TIME_MARGIN`（夜里必须在炮位上）。

    **距离一律用 BFS 真实步数**（第 33 步）：小贩常在盒子外，回基地要绕背面那道门。
    ⚠️ **-1（走不到）一律当"这趟不去"**：切比雪夫永远给不出这个值，是本步新增的失败态。

    站位是 `sell` 要求的"小贩周围一格内"，与小贩格本身挡路正好对上（`step_toward` 自然停在一格外）。
    一回合只能发一条指令 ⇒ 一次只卖一种矿，`num` = 手上那种矿的全部件数（卖光）。
    """
    station = turn.map.station
    kind, num = _best_load(role, turn.vendor_prices)
    if not kind or station is None or not turn.map.vendors:
        return False
    walk, size = turn.map.blocked, turn.map.size
    # 并列按坐标排：先后不能取决于 payload 里的顺序。**走不到的小贩直接剔掉**（BFS -1）。
    hops = [(steps_between(role.pos, p, walk, size), p) for p in turn.map.vendors]
    hops = [(steps, pos) for steps, pos in hops if steps >= 0]
    if not hops:
        return False
    to_vendor, vendor = min(hops)
    if to_vendor <= 1:
        return _emit(cmds, role, actions.Sell, kind, num)
    value = turn.vendor_prices.get(kind, 0) * num
    if value < 2 * to_vendor:
        return False  # ③ 为这一堆货走这么远不划算，接着采
    back = steps_between(vendor, station, walk, size)
    if back < 0 or to_vendor + back > turn.day_rounds_left - TIME_MARGIN:
        return False  # ④ 去了就赶不回来
    moved = _step(role, vendor, turn, cmds, claimed, sites | (paths or set()))
    if moved and paths is not None:
        _reserve_path(role, vendor, turn, claimed, paths)  # 动身成功才预留（见 `_build_walls`）
    return moved


def _best_load(role: Worker, prices: Mapping[str, int]) -> tuple[str, int]:
    """挑这一趟卖哪种矿：收购价最高的，同价取件数多的；挑不出来 ⇒ `("", 0)`。

    **价 ≤ 0 或件数为 0 的矿跳过**：小贩不收（价目表里没有也算不收）的矿换不来金币。
    价目表为空 ⇒ 一件都不卖。名字参与比较只是为了让并列可复现。

    ⚠️ **石头保底留 1 块不卖**（第 33 步）：收工时手里得有石头才能把正面那个口封上
    （`wall_cells` 的最后一格），封不上就是整夜的一道门。只有 1 块 ⇒ 这一趟不卖石头
    （另外两种矿照卖；一件可卖的都没有 ⇒ 挑不出来）。规则是"墙砌完了才卖石头"，
    所以这 1 块**买不到墙**，它的用途只有封口。
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
    cmds: dict[str, dict[str, Any]],
    claimed: set[Pos],
    sites: set[Pos],
    ore_taken: set[Pos] | None = None,
    paths: set[Pos] | None = None,
    near: int = 0,
) -> None:
    """墙砌完了 ⇒ 白天不再闲着：**就近**采买得动、回得来的矿（第 29 步修闲置）。

    与旧版的差别：**先把"走得动、回得来"的矿筛出来、再挑** —— 旧版按价挑了最贵的、
    发现走不回来就整段放弃（"挖好石头就在家里等着"的根源）。回程参照 = **最近的武器位**
    （夜里要在炮前，机器人到进攻范围前必须站回去；没有武器才用基地）。
    ⚠️ **第 42 步起按"性价比"挑**：`价 × 新闻修正 ÷ (到矿 + 采一块 + 回炮位)` ——
    单位回合价值最高，**近的贱矿可能跑赢远的贵矿**（旧口径"价高优先"只看分子）。
    新闻修正是 `AGENT.price_hint`（第三处跨回合状态，退化 = 偏好偏一天，不碰红线）。
    **顺路交易**：先看`_detour_buy`（修墙缺包 / 升级缺券），再 `_detour_sell`。
    **夜里**（`near > 0`，第 42 步）：怪清完才出门，`max(到矿, 回炮位) ≤ near` 的
    **近矿限制** —— 机器人列表是视野过滤的，"清完"只是看不见，出得了事要回得了家。

    **距离一律用 BFS 真实步数**（第 33 步）。⚠️ **-1（走不到）的矿直接作废**。
    """
    ore_taken = set() if ore_taken is None else ore_taken
    paths = set() if paths is None else paths
    station = turn.map.station
    posts = [w.pos for w in turn.weapons] or ([station] if station else [])
    budget = turn.day_rounds_left - TIME_MARGIN
    walk, size = turn.map.blocked, turn.map.size
    #: 先筛可行（走过去 + 回得来），再按性价比挑。⚠️ BFS 是"命中目标即停"的，开销跟
    #: **距离**相关、不是整张图 —— 最坏 12 矿 × (1 + 3 炮) 次，实测每回合几十毫秒。
    feasible: dict[Pos, tuple[str, int, int]] = {}
    for p, kind in turn.map.ores.items():
        if p in ore_taken:
            continue  # 本回合已被人认领（第 40 步）
        out = steps_between(role.pos, p, walk, size)
        return_home = [steps_between(post, p, walk, size) for post in posts]
        back = min((d for d in return_home if d >= 0), default=-1)
        if out < 0 or back < 0:
            continue
        if near > 0:
            if max(out, back) > near:
                continue  # 夜里近矿限制（第 42 步）
        elif out + back > budget:
            continue  # 白天：这一趟赶不回来
        feasible[p] = (kind, out, back)
    best: tuple[float, int, Pos] | None = None
    for p, (kind, out, back) in feasible.items():
        value = turn.vendor_prices.get(kind, 0) * AGENT.price_hint(kind)
        if value <= 0:
            continue  # 小贩不收的矿不为它多走一步（与旧口径一致）
        key = (-(value / (out + back + 1)), out, p)
        if best is None or key < best:
            best = key
    mine = best[2] if best else None
    if station is None or mine is None:
        return
    ore_taken.add(mine)
    if _detour_buy(role, mine, turn, cmds, claimed):
        return
    if _detour_sell(role, turn, cmds, claimed, mine):
        return
    if role.pos.dist(mine) <= 1:
        _emit(cmds, role, actions.Collect, mine)
        return
    if _step(role, mine, turn, cmds, claimed, sites | paths):
        _reserve_path(role, mine, turn, claimed, paths)  # 动身成功才预留（见 `_build_walls`）


def _shopping_list(role: Worker, turn: Turn) -> str | None:
    """这一趟该顺路买什么（第 42 步，用户拍板"优先买武器升级券 + 修墙的"）⇒ 商品名；
    什么都不缺 ⇒ `None`。只在**买得起**时才列：钱不够绕过去也白绕。

    优先级：**WallFixer**（有半血墙且包里没包 —— 10 金回满血，防御面有薄弱格）>
    **升级链下一张券**（升级线要用；别人包里已有一张就不再买 —— 券在谁包里谁用，
    没有转移指令，囤两张是白花金币）。
    """
    prices = turn.shop_prices
    if (
        _half_walls(turn)
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
    role: Worker, goal: Pos, turn: Turn, cmds: dict[str, dict[str, Any]], claimed: set[Pos]
) -> bool:
    """去差事的路上**顺路买**：绕 2 格内有商店、且购物单上有东西 ⇒ 先朝商店迈一步。

    贴上商店的那回合 `Buy`（下一回合走 `use`/升级线），之后再继续去差事。
    与 `_detour_sell` 同一套绕路账（`via + after - direct ≤ DETOUR_MAX`）；
    ⚠️ -1（走不到）的绕法直接放弃。已经贴着差事目标就别绕了（这一回合该干活）。
    """
    want = _shopping_list(role, turn)
    if want is None or role.pos.dist(goal) <= 1:
        return False
    walk, size = turn.map.blocked, turn.map.size
    direct = steps_between(role.pos, goal, walk, size)
    if direct < 0:
        return False
    for shop in sorted(turn.map.shops):
        via = steps_between(role.pos, shop, walk, size)
        after = steps_between(shop, goal, walk, size)
        if via < 0 or after < 0:
            continue
        if via + after - direct <= DETOUR_MAX:
            return _step(role, shop, turn, cmds, claimed)
    return False


def _detour_sell(
    role: BaseRole, turn: Turn, cmds: dict[str, dict[str, Any]], claimed: set[Pos], mine: Pos
) -> bool:
    """去矿的路上**顺路卖矿**：绕去小贩比直走多花 ≤ `DETOUR_MAX` 格 ⇒ 先朝小贩迈一步。

    贴上小贩的那一回合同一条链里更早的 `_sell_ore` 自然把货出手（贴着跳过够本门），
    卖完没货、绕路条件消失，下一回合继续去矿。已经贴着矿就别绕了（这一回合该采）。

    **"有没有可卖的货"用 `_best_load` 判，不在这里重抄一遍**（第 33 步）：那边有
    "石头保底留 1 块"的口径，抄一遍就会出现"绕小贩去卖那 1 块石头、到了却不肯卖"
    —— 白绕一趟，且两处口径迟早分家。

    **距离一律用 BFS 真实步数**（第 33 步，与 `_sell_ore` 同口径）；绕路省下来的那几格
    只有在绕障之后才算数。⚠️ **-1（走不到）的绕法直接放弃**。
    """
    if role.pos.dist(mine) <= 1:
        return False
    if _best_load(role, turn.vendor_prices)[1] <= 0:
        return False
    walk, size = turn.map.blocked, turn.map.size
    direct = steps_between(role.pos, mine, walk, size)
    if direct < 0:
        return False
    for vendor in sorted(turn.map.vendors):
        via = steps_between(role.pos, vendor, walk, size)
        after = steps_between(vendor, mine, walk, size)
        if via < 0 or after < 0:
            continue
        if via + after - direct <= DETOUR_MAX:
            return _step(role, vendor, turn, cmds, claimed)
    return False


def _upgrade_line(
    role: BaseRole, turn: Turn, cmds: dict[str, dict[str, Any]], claimed: set[Pos]
) -> bool:
    """墙砌完后的**第二优先**（第 29 步，用户拍板"买得起就优先升级"）：买券 → 走到目标
    武器 → 用券。这一回合没发指令就返回 `False`（调用方接着去采矿）。

    **无状态**：拿没拿券看**背包**（买完金变少、包里多一张，两个阶段天然可分，跨夜也不丢
    —— 背包物品保留）。跑腿者 = **持券的那个角色**（券在谁包里谁用 —— 没有转移物品的
    指令；第 40 步起开拓者也能持券：它空闲时买下、被任务钉走就躺到下个空隙）；
    没人持券 ⇒ **真空闲的开拓者**（无可接任务，它白天本来就闲着 —— 第 40 步），
    再不然名册上第一个工人（别的工人照常卖/采）。**只在白天跑**（这条挂在白天的支路上；
    夜里 `_defend` 会把人接回炮位，明早预算重算、接着走）。

    **距离一律用 BFS 真实步数**（第 33 步）：商店/炮位都在盒子内外两侧，整趟要绕门。
    ⚠️ **-1（走不到）一律当天不去**，明天重算 —— 切比雪夫永远给不出这个值。
    """
    workers = [r for r in turn.roles if isinstance(r, Worker)]
    holder = next(
        (r for r in turn.roles if any(name in r.bag for name in VOUCHER.values())), None
    )
    #: 真空闲 = 无可接任务 —— 有任务在的话开拓者马上会被钉死，跑腿该让给工人。
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
    walk, size = turn.map.blocked, turn.map.size
    if voucher in role.bag:
        # 持券阶段：终点就是炮位，用完正好站岗 —— 不用留回程
        if role.pos.dist(weapon.pos) <= 1:
            return _emit(cmds, role, actions.Use, voucher, weapon.pos)
        to_weapon = steps_between(role.pos, weapon.pos, walk, size)
        if to_weapon < 0 or to_weapon + 1 > budget:
            return False  # 走不到 / 今天来不及 ⇒ 待命，明天接着走
        return _step(role, weapon.pos, turn, cmds, claimed)
    # 买券阶段：整趟 = 走到商店 + 买到武器 + 买/用两个动作回合
    price = turn.shop_prices.get(voucher, 0)
    if price <= 0 or turn.gold < price:
        return False
    # 并列按坐标排：先后不能取决于 payload 里的顺序。**走不到的商店直接剔掉**（BFS -1）。
    hops = [(steps_between(role.pos, s, walk, size), s) for s in turn.map.shops]
    hops = [(steps, pos) for steps, pos in hops if steps >= 0]
    if not hops:
        return False  # 没有商店、或者一个都走不到
    to_shop, shop = min(hops)
    if to_shop <= 1:
        return _emit(cmds, role, actions.Buy, voucher, 1)
    to_weapon = steps_between(shop, weapon.pos, walk, size)
    if to_weapon < 0 or to_shop + to_weapon + 2 > budget:
        return False
    return _step(role, shop, turn, cmds, claimed)


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
    """把"从 `role.pos` 走到 `goal` 的 BFS 路径"的中间格记进 `paths`（第 40 步的路径预留）。

    后处理的工人把 `paths` 当 `avoid` 传给 `_step` ⇒ 它选路时**整条让开**，而不是撞上
    前一个工人的**本回合这一格**才让 —— 盒子里走廊就那么几条，双双停住一回合是纯亏
    （任务书 §4.5.4：移动碰撞双双不动）。⚠️ **软约束**：`_step` 绕不开时退回硬障碍照走
    （擦肩好过卡死）；⚠️ 算路只用 `blocked | claimed`，**不含 `paths` 自己** —— 否则
    第二个工人的预留会绕开第一个的预留、越绕越远。图就 41×32，64 步是兜底上限。
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

    - `want_stone`（石头还不够砌完剩下的墙）⇒ **只认石矿，取最近的**（铁/铜砌不了墙）；
    - 否则 ⇒ 三种矿按小贩**收购价从高到低**，同价再取近的。价目表里没有的矿种按 0 算
      ⇒ 小贩不收的矿不值得为它多走一步（空价目表也就自然落成"谁也不采"）。

    价格逐回合从载荷读，**不写死"铜 > 铁 > 石头"**（那只是样例的价目，官方消息会让它波动）。
    并列按坐标排：先后不能取决于 payload 里的顺序，否则用例复现不了。
    不认领矿：两个工人挤同一座矿的不同邻格都能采，只有"冲进同一格"才是白扔动作（`claimed` 挡着）。
    """
    if want_stone:
        return min(
            (p for p, kind in ores.items() if kind == STONE),
            key=lambda p: (pos.dist(p), p),
            default=None,
        )
    #: `(取负的价, 距离, 坐标)` —— 价排第一，`min` 出来就是"价最高、同价取近的、再同取坐标最小"
    best = min(
        ((-prices.get(kind, 0), pos.dist(p), p) for p, kind in ores.items()),
        default=None,
    )
    # 价 0 ⇒ 小贩不收，不为它多走一步；一张空价目表也就自然落成"谁也不采"
    return best[2] if best is not None and best[0] < 0 else None


def _leave_for_the_post(
    role: BaseRole,
    turn: Turn,
    cmds: dict[str, dict[str, Any]],
    claimed: set[Pos],
    taken: set[Pos],
) -> bool:
    """白天收工（第 33 步）：**离夜里的第一波只剩回程步数了就回炮位**；这一回合到此为止 ⇒ `True`。

    为什么白天就得动身：机器人**在夜里第一个回合就全部出现**，而回炮位常常要**绕整面围墙、
    从背面那 2 格门进来、再横穿盒子** —— 在正面墙外干活时，直线三四步、BFS 十几步
    ⇒ 不提前出发，开局那几个回合就是白丢的（实盘问题 ②）。

    ⚠️ **与 `_defend` 的唯一区别是它不调 `_fire`**：`attack` 仅黑夜（§4.4），白天发就是
    **非法指令**、5 次出局。选炮口径与它一致（最近且没人认领、贴着就认领、不换炮）。

    ⚠️ **先看时间、再看位置**：还早 ⇒ 一律不打断白天的活。少了这一道，"到岗就待命"
    会让早上正好站在炮边的工人（升级线、卖矿都可能把人在那儿放下）**整天不动**。
    判据是"到最近那座炮的 BFS 步数 ≥ 白天还剩的回合 − 1"（`− 1` = 留 1 回合余量，拍的）；
    已经贴着那座炮时步数是 **0**，于是只有**白天最后一回合**才轮得到"在岗待命"。

    返回 `True` = 这一回合已由本函数处理（走了、或已在岗待命），调用方 `continue`；
    `False` = 还来得及干活（或没有可去的炮），照常走白天的活。
    """
    walk, size = turn.map.blocked, turn.map.size
    #: 最近、还没人认领、而且**真走得到**的那座炮 —— 一趟判定就够：最近的都赶不上，
    #: 更远的更赶不上。⚠️ BFS **-1（不可达）剔掉**，切比雪夫给不出这个值。
    #: 已经贴着某座 ⇒ 步数 0（`steps_between` 的约定），与"还差 3 步"是同一个刻度。
    hops = [
        (steps_between(role.pos, weapon.pos, walk, size), weapon.pos)
        for weapon in turn.weapons
        if weapon.pos not in taken
    ]
    hops = [(steps, pos) for steps, pos in hops if steps >= 0]
    if not hops:
        return False
    steps, post = min(hops)
    if steps < turn.day_rounds_left - 1:
        return False  # 还剩富裕回合 ⇒ 照常干活
    taken.add(post)  # 定下这座了：认领，免得另一个角色也奔这里（一人只能操一座）
    if steps == 0:
        return True  # 已经在岗 ⇒ 这一回合待命（什么都不发 = 合法空指令）
    _step(role, post, turn, cmds, claimed)  # ⚠️ 只发 move，绝不调 `_fire`
    return True


# ── 夜里：回炮位、开火 ──────────────────────────────────────────────
def _defend(
    role: BaseRole,
    turn: Turn,
    cmds: dict[str, dict[str, Any]],
    claimed: set[Pos],
    taken: set[Pos],
    assigned: dict[Pos, int],
) -> None:
    """夜里：走到最近的一座**还没被本回合别人认领**的武器旁，贴着就开火。所有角色都走这里。

    ⚠️ **回合号缺失（`round_no < 0`）时一发不发**：`is_day` 把缺失的回合号判成夜里，
    那个降级方向对"白天不许建造"安全，对 `attack` 就反过来了（白天开火非法）。

    选炮：最近且没人认领的，同距离按 `id` 排（并列不能取决于 payload 顺序）。
    贴着（切比雪夫 ≤1）就**认领并停在这里**，开不开火交给 `_fire`；**不换炮**
    —— 每回合重挑会让角色在炮位之间来回走。一人只能操一座，用 `taken` 去重。
    场上没有武器 / 都够不着 ⇒ 不动（空指令合法且不计异常）。
    """
    if turn.round_no < 0:
        return
    for weapon in sorted(turn.weapons, key=lambda w: (role.pos.dist(w.pos), w.id)):
        if weapon.pos in taken:
            continue
        if role.pos.dist(weapon.pos) <= 1:
            taken.add(weapon.pos)
            _fire(role, weapon, turn, cmds, assigned)  # 打不了就不发，但人已经站住了
            return
        step = step_toward(role.pos, weapon.pos, turn.map.blocked | claimed, turn.map.size)
        if step is None or not _emit(cmds, role, actions.Move, step):
            continue  # 走不到 / 发不出去 → 换下一座，别为一棵树放弃整片林子
        taken.add(weapon.pos)  # 认领发生在迈步之后：没走成才轮到下一座
        claimed.add(step)
        return


def _upgrade_station(
    role: BaseRole, turn: Turn, cmds: dict[str, dict[str, Any]], claimed: set[Pos]
) -> bool:
    """夜里**基地升级**（第 42 步，用户拍板）：持基地券 + 基地血量 < 满血 1/4
    （按等级查 `STATION_MAX_HP`：1500/3000/4500 表格实证）⇒ 贴基地 `use`。
    返回 `True` = 这一轮归它了（走了或用了）。

    **夜里人在盒内炮位、基地就在盒心** —— 站位天然满足"周围一格内"，最多走一两步。
    升级 + 回满血一次到位（L1→L2 且血回 3000），是要塌的基地最好的救兵；
    走过去/用券的那个回合放弃开火 —— 只在基地真残血时才值得。
    ⚠️ station_health 缺失（-1）⇒ 未知 ⇒ 不动（不轻举妄动）。
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
        return _emit(cmds, role, actions.Use, voucher, station)
    walk, size = turn.map.blocked | claimed, turn.map.size
    step = step_toward(role.pos, station, walk, size)
    if step is None:
        return False
    claimed.add(step)
    return _emit(cmds, role, actions.Move, step)


def _fire(
    role: BaseRole,
    weapon: Weapon,
    turn: Turn,
    cmds: dict[str, dict[str, Any]],
    assigned: dict[Pos, int],
) -> bool:
    """贴着炮了：按**最大伤害落点**开火（第 27 步的方针：**打死所有机器人**，
    撤掉了第 10 步的"补刀残血"）。打不了就什么都不发（空指令合法且不计异常）。

    - **冷却中不打**（`cooldown > 0`）。⚠️ **字段缺失时不算冷却**（解析成 -1）—— 否则整晚一炮不开；
    - **火箭**（`_rocket_site`）：落点任选（导弹指哪打哪）⇒ 中心 20 + 溅射 10 **全场**算账，
      取总分最高的落点 —— 落进机器人簇里、落在最肥的那台身上；
    - **加特林 / 电磁**（`_beam_site`）：弹道武器 ⇒ 落点打在某台**机器人身上**（终点必在
      弹道上 ⇒ 必命中；途中更近的先接住，那也是命中）。取**有效伤害**最高的，并列打近的；
    - **`assigned` 记账**：先开火的炮把估计伤害记在机器人身上，后开的按**剩余血**算 ——
      不挤同一个将死的目标。⚠️ 估计有偏差（加特林实际命中的是弹道上**最近**的那台，
      可能不是终点那台），代价只是次优、不会非法；
    - 机器人 `health` 缺失（-1）时按"还活着"算：打空处只是执行失败，而"一律不打"会让整晚一炮不开。
    """
    if weapon.cooldown > 0:
        return False
    if weapon.kind == "rocket":
        target = _rocket_site(weapon, turn.robots, turn.map.size, assigned)
        if target is None:
            return False
        _book_rocket(target, turn.robots, assigned)
    else:
        victim = _beam_site(weapon, turn.robots, assigned)
        if victim is None:
            return False
        target = victim.pos
        shot = GATLING_SHOT if weapon.kind == "gatling" else RAILGUN_ENERGY
        assigned[victim.pos] = assigned.get(victim.pos, 0) + shot
    # **key 是武器 id**，操控角色在报文的 `controllerId` 里
    return _emit(cmds, role, actions.Attack, str(role.id), target, key=str(weapon.id))


def _beam_site(
    weapon: Weapon, robots: tuple[Robot, ...], assigned: Mapping[Pos, int]
) -> Robot | None:
    """加特林/电磁的目标：**有效伤害**最高的那台（并列打近的、再并列按坐标序）。

    "有效伤害" = min(伤害, 剩余血)：L1 的 10 点对满血机器人（≥40 血）等额，差别只在
    将死者 —— 别把整发浪费在已被打得差不多的人身上（`assigned` 是本回合先开火的炮
    记的账）。够得着的目标全是将死的（有效 ≤ 0）⇒ 不打。
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
    """火箭的**最大伤害落点**：中心 20 + 周围 8 格溅射 10，取总分最高的格子。

    候选 = 机器人占的格与其 8 邻格（别的格子一分伤害都摸不到），且**落点**须在射程内
    —— 溅射可以够到**射程之外**的机器人（射程只管落点）。评分 = Σ min(伤害, 剩余血)
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
    """把火箭这一发的**估计伤害**记到账上（同回合后开的炮按剩余血挑目标）。"""
    for r in robots:
        if r.health == 0:
            continue
        hit = ROCKET_CENTER if r.pos == target else ROCKET_SPLASH if r.pos.dist(target) == 1 else 0
        if hit:
            assigned[r.pos] = assigned.get(r.pos, 0) + hit


# ── 发指令 ──────────────────────────────────────────────────────────
def _step(
    role: BaseRole,
    goal: Pos,
    turn: Turn,
    cmds: dict[str, dict[str, Any]],
    claimed: set[Pos],
    avoid: Set[Pos] = frozenset(),
) -> bool:
    """朝 `goal` 走一格并登记落脚格。走不到 / 发不出去返回 False。

    `avoid` = 额外要避开的格：工人传**建造格**（免得径直走到建造格**上**去，
    那样建完自己站在墙里）与**别人预留的路径**（第 40 步的 `paths` —— 整条让开，
    不等撞上才让）。⚠️ **avoid 是软的**：绕不开就退回硬障碍（`blocked | claimed`）
    重算照走 —— 让路的代价不能是原地卡死，擦肩好过双双停住。`claimed`（本回合
    别人已迈的落脚格）始终是硬的：两个身体进同一格是注定双双白扔的碰撞。
    ⚠️ **拆墙（第 34 步）钩在这里**：`_step` 是所有差事（采矿 / 卖矿 / 顺路卖 / 升级 / 砌墙 /
    回炮位）走路的**公共出口** ⇒ 一处覆盖全部调用点，调用点一个字不用改。
    代价是它也覆盖了开拓者与夜里的回炮位 —— 那两边由 `_dig` 自己的闸门（`is_day` + `Worker` +
    环满 + 窗口）挡住，**不在这里加分支**。
    """
    walk = turn.map.blocked | claimed | avoid
    if _dig(role, goal, turn, cmds, claimed, walk):
        return True  # 与其绕远，不如把这格的墙拆了（省 ≥ `HOLE_MIN_SAVING` 步）
    step = step_toward(role.pos, goal, walk, turn.map.size)
    if step is None and avoid:
        step = step_toward(role.pos, goal, turn.map.blocked | claimed, turn.map.size)
    if step is None or not _emit(cmds, role, actions.Move, step):
        return False
    claimed.add(step)
    return True


def _step_aside(
    role: BaseRole,
    turn: Turn,
    cmds: dict[str, dict[str, Any]],
    claimed: set[Pos],
    avoid: Set[Pos] = frozenset(),
) -> bool:
    """从**脚下**这一格挪到任一可走的邻格；八面都走不了（或指令发不出去）返回 False。

    与 `_step` 的区别是**没有目标**，只求离开脚下这一格（唯一的用途见 `_build_walls`）
    —— 所以不走 `step_toward`：它的契约是"贴着 goal 即到"，对任意邻格都返回 None。

    `avoid` 与 `_step` 同义（工人传本回合认领的建造格与炮位）：迈到那上面等于换个格子接着站。
    """
    walk = turn.map.blocked | claimed | avoid
    width, height = turn.map.size
    for d in STEPS:  # 方向顺序无所谓：任何一个可走的邻格都等价（挪开一步就够了）
        cell = Pos(role.pos.x + d.x, role.pos.y + d.y)
        if cell in walk or not (0 <= cell.x < width and 0 <= cell.y < height):
            continue
        if not _emit(cmds, role, actions.Move, cell):
            return False
        claimed.add(cell)
        return True
    return False


def _emit(
    cmds: dict[str, dict[str, Any]],
    role: BaseRole,
    cls: type[actions.BaseAction],
    *args: Any,
    key: str | None = None,
) -> bool:
    """造一条指令放进 `cmds`，成功返回 True。

    默认挂在 `str(role.id)` 下，**只有 `attack` 例外**（key 是武器 id，操控者在 `controllerId` 里）
    ⇒ 这里开一个 keyword-only 的口子，只有 `_fire` 那一处传 `key`。

    ⚠️ **越权只丢这一条并告警**，不连坐同回合其他角色 —— 抛出去会变成"每回合空指令
    ⇒ 全队冻结一整局"，现象与 `main3.py` 改名事故一样难排查（`CLAUDE.md` 硬约束 3）。
    """
    try:
        cmds[key if key is not None else str(role.id)] = cls(role.type_name, *args).to_wire()
    except PermissionError as exc:
        LOGGER.warning("拦下越权动作：角色 %s(%s) %s", role.id, role.type_name, exc)
        return False
    return True
