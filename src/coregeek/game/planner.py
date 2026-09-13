"""决策入口：`Turn` → `roleCommandMap`，外加响应顶层的 `(prompt, executeCmd)`。**策略只写在这里。**

每个角色每回合只有一个动作（顺序见 `docs/策略指导.md`）：

- **白天·开拓者**：去最近一个能接的任务点 `acceptTask`；领到就被钉死（见 `plan`）。
- **白天·工人**：建武器 → 采石砌墙 → 卖矿 → 采最值钱的矿，一件不行才轮到下一件。
- **夜里·所有角色**（含开拓者，`attack` 的可用角色是"全部"）：认领一座武器走过去，贴着就开火。

返回 `{角色ID: 指令}`，key 用字符串（JSON 对象的 key 本来就是字符串）；
⚠️ **`attack` 是唯一的例外** —— 它的 key 是**武器 id**，操控者在 `controllerId` 里。

任务线不在这个返回值里：`task_channel(turn)` 单独产出 `(prompt, executeCmd)`。
"跟 LLM 说什么、怎么解析回复"在 `coregeek/agent/`；用的是包根那个单实例
`AGENT`（它带着全项目唯一一处跨回合状态：「沉淀的 SOP」）。
"""

import logging
from collections.abc import Iterator, Mapping, Set
from typing import Any

from ..agent import AGENT  # 与 LLM 说什么不在策略层
from ..agent.chat import answer_of, tool_of
from ..protocol import actions  # 指令只能经 Action 产出
from .grid import STEPS, Pos, box_cells, step_outside, step_toward, wall_cells, weapon_sites
from .map import COPPER, IRON, STONE
from .roles import BaseRole, Pioneer, Worker
from .world import Turn, Weapon

LOGGER = logging.getLogger(__name__)

#: 三座武器的**种类，下标与 `grid.weapon_sites()` 的落点一一对应**：后列火箭、前排两角加特林/电磁炮。
#: 按射程配（后列离机器人最远）；数量上限 = 角色数 = 3（§4.5.1 原文"每种 ≤3"与"全局 ≤3"矛盾，取保守的）。
WEAPONS_BY_SITE = ("rocket", "gatling", "railgun")

#: 建一座武器的金币（三种同价）。
WEAPON_COST = 25

#: 围墙的 `name` 与代价：石头×1，从**建造者自己的背包**扣（不是全队共享），拆了不返还。
WALL = "wall"
WALL_COST = 1

#: 一块石头的完整代价：采 1 回合 + 挪 1 回合 + 建 1 回合。
ROUNDS_PER_STONE = 3

#: 容错余量（回合）：留给"从矿走回工地、把石头砌完"，同时吸收距离估算的误差
#: —— 下面的距离一律用切比雪夫，绕障时会低估。
TIME_MARGIN = 5

#: 能卖给小贩的矿：三种（含多余的石头），顺序无关紧要，挑哪种由 `Turn.vendor_prices` 现算。
#: ⚠️ **石头只在"墙砌完了"那一支里才卖得出去** —— 调用点 `_build_walls` 已经保证了这一点。
SELLABLE = (STONE, IRON, COPPER)


def plan(turn: Turn) -> dict[str, dict[str, Any]]:
    cmds: dict[str, dict[str, Any]] = {}
    #: 本回合已被认领的落脚格（两个人冲同一格会双双停住，任务书 §4.5.4）
    claimed: set[Pos] = set()
    #: 已被认领的**建造格**（武器与围墙共用），与 `claimed` 分开
    sites: set[Pos] = set()
    #: 已被认领的**武器**（夜里一人只能操一座）
    taken: set[Pos] = set()
    budget = turn.gold
    slots = _slots(turn)
    #: 防御盒子的 36 格（空集 = 没基地 ⇒ 没有"里面"）
    box = box_cells(turn.map.station) if turn.map.station else frozenset()
    #: **砌满墙就会被关在盒子里的人**：闸门 (2) 按人放行，闸门 (1) 随 `gated` 传给 `_build_walls`
    leaving = _trapped(turn, box)

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

        if not turn.is_day:
            _defend(role, turn, cmds, claimed, taken)  # 夜里还没被钉住的角色回炮位
            continue

        if isinstance(role, Pioneer):
            _take_task(role, turn, cmds, claimed)
            continue

        if not isinstance(role, Worker):
            continue  # 不该出现的角色（`roles.make` 已挡过一道）

        slot = next(slots, None)
        if slot is not None and budget >= WEAPON_COST:
            kind, cell = slot
            budget -= WEAPON_COST  # **认领即预留**：宁可这回合少建，不可超支
            sites.add(cell)
            if role.pos.dist(cell) <= 1:
                # **站位即建造位**：`build` 要求目标在自身一格内，而 `step_toward`
                # 恰好把工人停在贴着目标的那一格 —— 不用先挪开再建。
                if _emit(cmds, role, actions.Build, kind, cell):
                    continue
            else:
                # `sites` 也当障碍：免得工人径直走到建造格**上**去（那样建完自己站在武器里）
                walk = turn.map.blocked | claimed | sites
                step = step_toward(role.pos, cell, walk, turn.map.size)
                if step is not None and _emit(cmds, role, actions.Move, step):
                    claimed.add(step)
                    continue
            # 建不了 / 走不到 → 落到下面，别杵着

        _build_walls(role, turn, cmds, claimed, sites, gated=bool(leaving))

    return cmds


def task_channel(turn: Turn) -> tuple[str, str]:
    """本回合任务的 `(prompt, executeCmd)` —— **响应顶层那两个字段的唯一来源**。

    合成一个函数是因为这条链唯一的不变量是"**两者互斥**"：拆开会把同一条链写两遍，
    任何一次单边修改都会造成"同一轮既提问又发命令"（LLM 拿过期结果作答 ⇒ 活锁）。
    本函数自己无状态（跨回合状态只有 `AGENT` 实例上那一处）。

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
    5. **回复是最终答案 ⇒ 什么都不发**，开拓者那边每回合在 `submitAnswer`。
       判据用 `answer_of` 而**不是**"取不出命令"：畸形的工具调用会被当成答案交上去，
       而 `_answer_task` 又跳过工具回复 ⇒ **两条通道同时哑火，永久空转**。
    6. **否则（第一次提问 / 畸形回复重问）⇒ 只把题目发出去问。**
       隐式子路径 ③′（完整工具调用但拿不到命令：`SOP2Prompt`、未知工具、空参数）也落在这里；
       它不会活锁 —— 任务期间 prompt 不限量不计数，出口有"LLM 改口给答案""纠错段""它自己写进去的 SOP 段"。

    工具调度只有 `AGENT.tool_call` 一个入口，副作用（SOP 沉淀）只发生在那一行，且写在判据之前
    ⇒ 走"回灌结果"那一轮 SOP 照样生效。⚠️ **骂的那一份必须与交的那一份出自同一个谓词**
    （`answer_of`）：交上去的是解包后的答案，骂的却是原文的话，LLM 会以为我们交了一堆标签。
    """
    if not turn.phase_task or not any(isinstance(r, Pioneer) for r in turn.roles):
        return "", ""

    reply = turn.llm_resp.strip()
    answer = answer_of(reply)  # 「该提交什么」与「该骂什么」是**同一份**
    call = tool_of(reply)
    command = AGENT.tool_call(*call) if call else ""  # 工具调度：副作用只发生在这一行
    retry = answer if any(e.code == 2 for e in turn.errors) else ""

    if turn.cmd_result:  # ② 回灌结果、这轮绝不发命令（**必须压在 ③ 前**）
        return AGENT.chat(turn.phase_task, result=turn.cmd_result, retry=retry), ""
    if command:  # ③ 工具给了命令 ⇒ 交给沙盒
        return "", command
    if retry:  # ④ 判题器说答案错了 ⇒ 带上"上次答错了"重问
        return AGENT.chat(turn.phase_task, retry=retry), ""
    if answer:  # ⑤ 我们已经拿到了答案 ⇒ 都不发
        return "", ""
    # ⑥ 第一次提问 / 畸形或"不产出命令"的工具回复 ⇒ 只把题目问出去（**③′ 落在这里**）
    return AGENT.chat(turn.phase_task), ""


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

    答案 = `answer_of(llmResp)`：`<answer>…</answer>` 里包着就取块内容，否则**原文即答案**。
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
) -> None:
    """白天：先攒石头，再把墙砌到优先级最高的那一格上；**砌完了就去卖矿、采最值钱的矿**。

    每回合独立判定，不存跨回合状态 —— 矿采没了、墙被别人砌了，下一回合都能自动跟着变。

    `gated` = **这一回合不许砌**（砌下去会把谁关在墙里，见 `_trapped`）。
    ⚠️ **"不许砌"和"砌完了"是两件事**：两者都会让 `free` 空掉，但含义相反 ——
    前者墙还没砌完，跑去采矿就是跑到地图另一头、几十回合回不来。所以闸门挡住的这一支原地待命。
    """
    free = [c for c in _ring(turn) if c not in sites]
    if not free:
        # 墙砌满了 ⇒ **先卖矿、卖不动再采矿**。卖矿没有位置独占性（各卖各的背包），不进 `sites`。
        if not _sell_ore(role, turn, cmds, claimed, sites):
            _mine_spare_ore(role, turn, cmds, claimed, sites)
        return
    if gated:
        return  # 还砌得动，但这回合砌下去就把人关住了 ⇒ 一格都不砌（待命，**不是**"砌完了"）
    target = free[0]  # `_ring` 已按建造优先级排好
    #: **只认石矿**：墙只吃石头，铁/铜再多也砌不了墙。
    mine = _pick_ore(role.pos, turn.map.ores, turn.vendor_prices, want_stone=True)
    want = _stones_to_mine(role, turn, target, mine, len(free))

    if want > 0 and mine is not None:
        if role.pos.dist(mine) <= 1:
            if _emit(cmds, role, actions.Collect, mine):
                return
        # 还没走到矿边：继续走。走不到（或采集被拦）就落到下面去砌墙，别杵着
        elif _step(role, mine, turn, cmds, claimed, sites):
            return

    if role.stone >= WALL_COST:
        # **认领要发生在动身之前**：等砌完再登记的话，两个工人会在同一回合都奔着 `target` 去。
        sites.add(target)
        if role.pos == target:
            # ⚠️ **站在目标格上就先挪开一格，这一回合不砌**：人站在墙上时那一格在网格里
            # 只剩"worker"（单位铺在最后，墙被盖掉）⇒ 看不出砌过没有，而 `_ring` 的
            # "自己人算路过"又把它复活成候选 ⇒ 每回合对同一格 `build`（`dist == 0` 也是合法
            # 建造位），永远轮不到下一格、石头白花、`free` 永不为空（连卖矿那一支都进不去）。
            # 挪开一格两个方向都收敛：砌过的没人站着就现形；没砌过的下一回合从邻格稳稳砌上。
            _step_aside(role, turn, cmds, claimed, sites)
            return
        if role.pos.dist(target) <= 1:
            # 与建武器同一条契约：`step_toward` 停在贴着目标的一格，那正是 `build` 的站位
            if _emit(cmds, role, actions.Build, WALL, target):
                return
        elif _step(role, target, turn, cmds, claimed, sites):
            return
    # 手里没石头、又没时间采了 ⇒ 什么都不发（空指令合法且不计异常）


def _walled(turn: Turn) -> frozenset[Pos]:
    """"**假设墙砌满**"时的障碍集：现已挡路的照原样 + 全部 14 格墙。

    闸门问的是**将来** —— 现在走得出去不代表砌完还走得出去，而 `remove`（拆墙）没实现。
    ⚠️ **自己人站的那格也算障碍**，与 `_ring` 的"自己人算路过"故意相反：
    `_ring` 问"这格要不要砌"，这里问"会不会有人出不来"。代价是宁可晚砌一回合。
    """
    station = turn.map.station
    if station is None:
        return frozenset()
    return turn.map.blocked | set(wall_cells(station, turn.map.size[0]))


def _trapped(turn: Turn, box: frozenset[Pos]) -> frozenset[str]:
    """**砌满这一圈墙之后就走不出去了的**我方角色 id；没有就空集。

    判据是"整面墙"不是"某一格"：障碍集里永远有全部 14 格墙，真发生就是整面墙一起发生
    ⇒ 调用方要的是一个布尔量（该不该砌），而不是一串"可以砌的格子"。
    返回 id 是因为配套的闸门 (2) 得知道**谁**先出来，逐角色问 `step_outside` 才对
    "站在自己那格上的人"天然正确。
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


def _stones_to_mine(role: Worker, turn: Turn, target: Pos, mine: Pos | None, free: int) -> int:
    """这一趟还该采几块石头 —— 按**回合预算**每回合现算。

    记 `s` = 手里的石头、`k` = 还要采的块数，整件事的回合数：

        走到矿 + 采 k 块 + 从矿走回工地 + 砌 s+k 座的"挪一格 + 建造"
        = d_mine + d_wall + 2s − 1 + 3k        # 到达工地那一步已贴着首格，故 −1
                                            # ⇒ 每多采一块**净**花 3 回合

    令它 ≤ `白天还剩的回合 − TIME_MARGIN`，解出 k，再与"还差几格墙"取小
    （没有转移物品的指令，多采的石头给不了别人，只防采得离谱，不必精确分账）。

    距离用切比雪夫（本游戏的移动度量），绕障时会低估，由 `TIME_MARGIN` 吸收。
    """
    if mine is None:
        return 0
    budget = (
        turn.day_rounds_left
        - TIME_MARGIN
        - role.pos.dist(mine)
        - mine.dist(target)
        - 2 * role.stone
        + 1
    )
    return max(0, min(budget // ROUNDS_PER_STONE, free - role.stone))


def _sell_ore(
    role: Worker, turn: Turn, cmds: dict[str, dict[str, Any]], claimed: set[Pos], sites: set[Pos]
) -> bool:
    """墙砌完了 ⇒ 把小贩肯收的矿背过去换金币。**这一回合没去卖就返回 `False`**（调用方接着去采矿）。

    四条门，任一条不成立就 `False`：

    ① **有货**：`_best_load` 从 `SELLABLE` 里挑收购价最高的一种；
    ② **有小贩**：`Map.vendors` 空就无处可卖；
    ③ **够本**：**已经不贴着才算** —— 货值 < 往返回合数（`2 × 距离`）就留在矿边接着采
       （已经站在小贩旁边时这趟路早付过了，不再拦）。⚠️ "1 金币 ≈ 1 回合"是拍的，唯一的调参旋钮；
    ④ **回得来**：`走到小贩 + 从小贩回基地 ≤ 白天剩余 − TIME_MARGIN`（夜里必须在炮位上）。

    站位是 `sell` 要求的"小贩周围一格内"，与小贩格本身挡路正好对上（`step_toward` 自然停在一格外）。
    一回合只能发一条指令 ⇒ 一次只卖一种矿，`num` = 手上那种矿的全部件数（卖光）。
    """
    station = turn.map.station
    kind, num = _best_load(role, turn.vendor_prices)
    if not kind or station is None or not turn.map.vendors:
        return False
    # 并列按坐标排：先后不能取决于 payload 里的顺序。
    vendor = min(turn.map.vendors, key=lambda p: (role.pos.dist(p), p))
    if role.pos.dist(vendor) <= 1:
        return _emit(cmds, role, actions.Sell, kind, num)
    value = turn.vendor_prices.get(kind, 0) * num
    if value < 2 * role.pos.dist(vendor):
        return False  # ③ 为这一堆货走这么远不划算，接着采
    if role.pos.dist(vendor) + vendor.dist(station) > turn.day_rounds_left - TIME_MARGIN:
        return False  # ④ 去了就赶不回来
    return _step(role, vendor, turn, cmds, claimed, sites)


def _best_load(role: Worker, prices: Mapping[str, int]) -> tuple[str, int]:
    """挑这一趟卖哪种矿：收购价最高的，同价取件数多的；挑不出来 ⇒ `("", 0)`。

    **价 ≤ 0 或件数为 0 的矿跳过**：小贩不收（价目表里没有也算不收）的矿换不来金币。
    价目表为空 ⇒ 一件都不卖。名字参与比较只是为了让并列可复现。
    """
    loads = [
        (prices.get(kind, 0), role.bag.get(kind, 0), kind)
        for kind in SELLABLE
        if prices.get(kind, 0) > 0 and role.bag.get(kind, 0) > 0
    ]
    if not loads:
        return "", 0
    _, num, kind = max(loads)
    return kind, num


def _mine_spare_ore(
    role: Worker, turn: Turn, cmds: dict[str, dict[str, Any]], claimed: set[Pos], sites: set[Pos]
) -> None:
    """墙砌完了 ⇒ 白天不再闲着，去采**收购价最高**的矿。

    这一趟没有工地可回，参照点换成**基地**（夜里的炮位就在基地四周）：不够走个来回就不动身。
    **拿不到收购价就哪儿也不去**。它只管采不管运 —— 运是 `_sell_ore`（在它之前先试一次）。
    """
    station = turn.map.station
    mine = _pick_ore(role.pos, turn.map.ores, turn.vendor_prices, want_stone=False)
    if station is None or mine is None:
        return
    if role.pos.dist(mine) + mine.dist(station) > turn.day_rounds_left - TIME_MARGIN:
        return
    if role.pos.dist(mine) <= 1:
        _emit(cmds, role, actions.Collect, mine)
        return
    _step(role, mine, turn, cmds, claimed, sites)


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


# ── 夜里：回炮位、开火 ──────────────────────────────────────────────
def _defend(
    role: BaseRole, turn: Turn, cmds: dict[str, dict[str, Any]], claimed: set[Pos], taken: set[Pos]
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
            _fire(role, weapon, turn, cmds)  # 打不了就不发，但人已经站住了
            return
        step = step_toward(role.pos, weapon.pos, turn.map.blocked | claimed, turn.map.size)
        if step is None or not _emit(cmds, role, actions.Move, step):
            continue  # 走不到 / 发不出去 → 换下一座，别为一棵树放弃整片林子
        taken.add(weapon.pos)  # 认领发生在迈步之后：没走成才轮到下一座
        claimed.add(step)
        return


def _fire(role: BaseRole, weapon: Weapon, turn: Turn, cmds: dict[str, dict[str, Any]]) -> bool:
    """贴着炮了：能打就打一发，返回发没发出去。**打不了就什么都不发**（空指令合法且不计异常）。

    - **冷却中不打**（`cooldown > 0`）。⚠️ **字段缺失时不算冷却**（解析成 -1）—— 否则整晚一炮不开；
    - **射程内没有机器人不打**（打空处是执行失败，白耗一次冷却，还看不出是"没敌人"还是"瞄错了"）；
    - 目标 = **射程内血最少的**（补刀优先，距离作平手判定）。
      ⚠️ **必须先按射程过滤、再取 min** —— 顺序写反就成了"拿全场最弱、但打不着的那个当目标"。
    - **不按"本回合已被别人瞄过"去重**：用户选的就是补刀 = 集火。
    - 机器人 `health` 缺失（-1）时按"还活着"算：打空处只是执行失败，而"一律不打"会让整晚一炮不开。
    """
    if weapon.cooldown > 0:
        return False
    reach = [
        r
        for r in turn.robots
        if r.health != 0 and weapon.pos.dist(r.pos) <= weapon.attack_range
    ]
    if not reach:
        return False
    target = min(reach, key=lambda r: (r.health, weapon.pos.dist(r.pos)))
    # **key 是武器 id**，操控角色在报文的 `controllerId` 里
    return _emit(cmds, role, actions.Attack, str(role.id), target.pos, key=str(weapon.id))


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
    那样建完自己站在墙里），开拓者用默认的空集。
    """
    walk = turn.map.blocked | claimed | avoid
    step = step_toward(role.pos, goal, walk, turn.map.size)
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
