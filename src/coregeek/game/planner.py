"""决策入口：`Turn` → `roleCommandMap`。**策略只写在这里。**

工人每回合按 `docs/策略指导.md` 的优先级**择一**（每回合每个角色只能一个动作，任务书 L177）：

1. **建武器** —— 白天、武器数 < 角色数、金币够。位置优先放在**基地后方**那一列，
   让基地的 2×2 实体挡在武器与机器人之间（"建造的位置优先放在基地后面，让基地也能
   防守一下机器人的进攻"）。三座按 加特林 → 电磁狙击炮 → 火箭发射台。
2. **白天：采石砌墙** —— "工人最先建立武器，然后找石矿建墙，找石矿应该要去**最近的**，
   另外**必须在晚上到来前将墙建好，注意计算回合数**"。攒几块不写死，按**白天还剩的回合数**
   现算（见 `_stones_to_mine`）。墙砌在**面向机器人进攻的方向**（保护基地），背面留缺口。
3. **夜里：走到武器旁边待命** —— `attack` 还没落地，这一步只把"就位"做完（站位要求与
   `step_toward` 停在一格外的契约一致，`attack` 落地时只需在这里补一条 emit）。

开拓者这一步不发指令。

返回 `{角色ID: 指令}`，key 用**字符串** —— JSON 对象的 key 本来就是字符串。
"""

import logging
from collections.abc import Iterator
from typing import Any

from ..protocol import actions  # 唯一一条"由内往外"的依赖：指令只能经 Action 产出
from .grid import Pos, back_weapon_cells, step_toward, wall_cells
from .roles import BaseRole, Worker
from .world import Turn

LOGGER = logging.getLogger(__name__)

#: 三座武器的建造顺序（用户选定）。数量上限 = 角色数，正好 3 —— 与策略指导
#: "武器只有建立三个才有意义，建立多了没有意义"一致。
#: 任务书 §4.5.1 表格写"每种 ≤3"（合计 9 座）、补充说明写"**全局同时最多 3 座**"，
#: **原文自相矛盾**，取保守的那一个。
WEAPON_ORDER = ("gatling", "railgun", "rocket")

#: 建一座武器的金币（任务书 §4.5.1，三种同价）。
WEAPON_COST = 25

#: 围墙的 `name` 与代价。代价是**石头×1**，从**建造者自己的背包**扣（不是全队共享）；
#: 拆了不返还（任务书 L209）。
WALL = "wall"
WALL_COST = 1

#: 一块石头的完整代价：1 回合采集 + 1 回合挪到下一格墙边 + 1 回合建造。
ROUNDS_PER_STONE = 3

#: 留给"从矿走回工地、把石头砌完"的容错余量（用户指定 5 回合）。
#: 它同时吸收**距离估算的误差** —— 下面的距离一律用切比雪夫，绕障时会低估。
TIME_MARGIN = 5


def plan(turn: Turn) -> dict[str, dict[str, Any]]:
    cmds: dict[str, dict[str, Any]] = {}
    #: 本回合已被认领的落脚格。不设它，两个人冲同一个空格会按"目标点争夺"双双停住（任务书 §4.5.4）
    claimed: set[Pos] = set()
    #: 已被认领的**建造格**（武器与围墙共用）。与 `claimed` 分开：建造格是"要往里投料的位置"
    sites: set[Pos] = set()
    #: 已被认领的**武器**（夜里一人只能操一座）
    taken: set[Pos] = set()
    budget = turn.gold
    slots = _slots(turn)

    for role in turn.roles:
        if not isinstance(role, Worker):
            continue  # 开拓者这一步不动（见 code-task.md「不做什么」）

        slot = next(slots, None)
        if slot is not None and budget >= WEAPON_COST:
            kind, cell = slot
            budget -= WEAPON_COST  # **认领即预留**：宁可这回合少建，不可超支
            sites.add(cell)
            if role.pos.dist(cell) <= 1:
                # 「**站位即建造位**」：`build` 要求目标在自身一格内，而 `step_toward`
                # 恰好把工人停在贴着目标的那一格 —— 两条规则正好对上，不用先挪开再建。
                if _emit(cmds, role, actions.Build, kind, cell):
                    continue
            else:
                # 把 `sites` 也当障碍：免得工人径直走到建造格**上**去（那样建完自己站在武器里）
                walk = turn.map.blocked | claimed | sites
                step = step_toward(role.pos, cell, walk, turn.map.size)
                if step is not None and _emit(cmds, role, actions.Move, step):
                    claimed.add(step)
                    continue
            # 建不了 / 走不到 → 落到下面，别杵着

        if turn.is_day:
            _build_walls(role, turn, cmds, claimed, sites)
        else:
            _to_weapon(role, turn, cmds, claimed, taken)

    return cmds


def _slots(turn: Turn) -> Iterator[tuple[str, Pos]]:
    """本回合可以开建的 `(武器类别, 落点)`，按优先级排；没名额就一个都不产出。

    三道门槛，缺一座都不该建：

    - **白天** —— `build` 仅白天可用（任务书 §4.4）。夜里 `build` 会被判无效。
    - **份额** = `len(roles)` —— 用户规则"武器数量 < 总角色数"（3 角色 ⇒ 3 座）。
    - **落点是空的** —— 建在已有武器上会把它**覆盖**成 level1（§4.5.1 补充说明），
      25 金币打水漂还降级；建在墙/矿上也必然失败。

    基地没了（`station is None`）就没有可建造区 ⇒ 不建。**这是故意的降级方向。**
    """
    if not turn.is_day:
        return
    station = turn.map.station
    if station is None:
        return

    have = turn.map.weapons
    need = len(turn.roles) - sum(len(positions) for positions in have.values())
    if need <= 0:
        return

    kinds = [k for k in WEAPON_ORDER if not have.get(k)]
    free = [c for c in back_weapon_cells(station, turn.map.size[0]) if c not in turn.map.blocked]
    yield from list(zip(kinds, free))[:need]


# ── 白天：采石 → 砌墙 ────────────────────────────────────────────────
def _build_walls(
    role: Worker, turn: Turn, cmds: dict[str, dict[str, Any]], claimed: set[Pos], sites: set[Pos]
) -> None:
    """白天：先攒石头，再把墙砌到优先级最高的那一格上。

    每回合独立判定，**不存任何跨回合状态** —— 所以矿采没了、墙被别人砌了、
    白天快过完了，下一回合都能自动跟着变。
    """
    free = [c for c in _ring(turn) if c not in sites]
    if not free:
        return  # 16 格砌满了：不再采（手里剩的石头留给第二天补墙）
    target = free[0]  # `_ring` 已按建造优先级排好
    mine = _nearest_stone(role.pos, turn.map.stones)
    want = _stones_to_mine(role, turn, target, mine, len(free))

    if want > 0 and mine is not None:
        if role.pos.dist(mine) <= 1:
            if _emit(cmds, role, actions.Collect, mine):
                return
        # 还没走到矿边：继续走。走不到（或采集被拦）就落到下面去砌墙，别杵着
        elif _step(role, mine, turn, cmds, claimed, sites):
            return

    if role.stone >= WALL_COST:
        # **认领要发生在动身之前**（与建武器同一条做法）：等到砌完再登记的话，
        # 两个工人会在同一回合里都奔着 `target` 去，白走一路、还多花一块石头。
        sites.add(target)
        if role.pos.dist(target) <= 1:
            # 与建武器同一条契约：`step_toward` 停在贴着目标的一格，那正是 `build` 的站位
            if _emit(cmds, role, actions.Build, WALL, target):
                return
        elif _step(role, target, turn, cmds, claimed, sites):
            return
    # 手里没石头、又没时间采了 ⇒ 什么都不发。空指令合法且不计异常（CLAUDE.md 硬约束 2）


def _ring(turn: Turn) -> tuple[Pos, ...]:
    """按优先级排的围墙格。基地没了 ⇒ 空（没有可建造区就不砌）。

    既不在 `wall_cells` 里、也不挡路的格才算候选 —— 但**自己人站的那一格例外**。

    工人站在待砌的墙格上只是**路过**（下一回合就走开），可那一格会因为有单位而入 `blocked`。
    若把它从表里划掉，另一个工人的 `free[0]` 就整体后移一格、掉头去砌更靠后的墙；
    等它一挪窝，目标又变回来，两人对着改目标来回踱步 —— **实测死循环：一格不砌，
    两个工人两格之间转到天黑**。已砌的墙 / 中立元素 / 机器人照旧排除（那些不是"路过"）。
    """
    station = turn.map.station
    if station is None:
        return ()
    #: 我方角色当前站的格（角色能走的都在这 —— 换边、多加角色都不用改）
    mine = {r.pos for r in turn.roles}
    return tuple(
        c for c in wall_cells(station, turn.map.size[0]) if c not in turn.map.blocked or c in mine
    )


def _stones_to_mine(role: Worker, turn: Turn, target: Pos, mine: Pos | None, free: int) -> int:
    """这一趟还该采几块石头 —— 用户给的**回合预算**模型，每回合现算。

    用户的原话：「实时计算白天还有多少回合（赶路回合数+收集回合数+建墙回合数），
    当时间满足的时候尽可能多的采矿，减少来回的回合消耗，给自己留下 5 个回合的容错余量
    回家建墙，注意建墙的回合数也要算在回合数消耗中」。

    记 `s` = 手里已有的石头、`k` = 还要采的块数，把整件事的回合数写出来：

        走到矿(pos→mine) + 采 k 块 + 从矿走回工地(mine→target) + 砌 s+k 座的"挪一格 + 建造"
        = d_mine + k + d_wall + (2(s+k) − 1)      # 到达工地那一步已贴着首格，故 −1
        = d_mine + d_wall + 2s − 1 + 3k           # ⇒ 每多采一块**净**花 3 回合

    令它 ≤ `白天还剩的回合 − 5`，解出 k。

    距离用**切比雪夫**：那是本游戏的移动度量（8 方向、每步 1 回合），空地上精确，
    绕障时会低估 —— `TIME_MARGIN` 就是用来吸收这个误差的。不为此改用 BFS 距离：
    `step_toward` 不返回路径长度，为一个估算去改它的契约不划算。

    最后与"还差几格墙"取小：**没有转移物品的指令**，多采的石头给不了别的工人，
    只能压在自己背包里等第二天补墙 —— 所以只防"采得离谱"，不必精确分账。
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


def _nearest_stone(pos: Pos, stones: frozenset[Pos]) -> Pos | None:
    """最近的石矿（**只要石头** —— 铁/铜不计入 `Map.stones`，墙不吃它们）。

    策略指导：「找石矿应该要去**最近的**」。

    不认领矿：两个工人挤同一座矿的**不同邻格都能采**，只有"冲进同一格"才是白扔动作，
    而那件事已经由 `claimed` 挡住了。
    """
    return min(stones, key=pos.dist, default=None)


# ── 夜里：走到武器旁边待命 ──────────────────────────────────────────
def _to_weapon(
    role: Worker, turn: Turn, cmds: dict[str, dict[str, Any]], claimed: set[Pos], taken: set[Pos]
) -> None:
    """夜里：走到最近的一座**还没被本回合别的工人认领**的武器旁边。

    一人只能操一座武器，所以用 `taken` 去重 —— 两个工人挤同一座，等于有一个白站。
    贴着武器（切比雪夫 ≤ 1）就停手：`attack` 的站位要求正是这个距离，
    下一步只需在这里补一条 emit。

    场上没有武器 ⇒ 不动（空指令合法）。
    """
    for cell in sorted(_weapons(turn) - taken, key=role.pos.dist):
        if role.pos.dist(cell) <= 1:
            taken.add(cell)
            return  # 已就位。`attack` 落地前不发任何指令
        step = step_toward(role.pos, cell, turn.map.blocked | claimed, turn.map.size)
        if step is None or not _emit(cmds, role, actions.Move, step):
            continue  # 走不到 / 发不出去 → 换下一座，别为一棵树放弃整片林子
        taken.add(cell)  # 认领发生在迈步之后：没走成才轮到下一座
        claimed.add(step)
        return


def _weapons(turn: Turn) -> set[Pos]:
    """我方全部武器的落点（不分类别 —— 夜里只关心"哪一座最近"）。"""
    return {cell for positions in turn.map.weapons.values() for cell in positions}


# ── 发指令 ──────────────────────────────────────────────────────────
def _step(
    role: BaseRole,
    goal: Pos,
    turn: Turn,
    cmds: dict[str, dict[str, Any]],
    claimed: set[Pos],
    sites: set[Pos],
) -> bool:
    """朝 `goal` 走一格并登记落脚格。走不到 / 发不出去返回 False。

    `sites` 也算障碍：免得工人径直走到建造格**上**去（那样建完自己站在墙里）。
    """
    walk = turn.map.blocked | claimed | sites
    step = step_toward(role.pos, goal, walk, turn.map.size)
    if step is None or not _emit(cmds, role, actions.Move, step):
        return False
    claimed.add(step)
    return True


def _emit(
    cmds: dict[str, dict[str, Any]], role: BaseRole, cls: type[actions.BaseAction], *args: Any
) -> bool:
    """造一条指令放进 `cmds`，成功返回 True。

    **越权只丢这一条并告警**，不连坐同回合其他角色 —— 若越权是系统性的，抛出去会变成
    "每回合空指令 → 全队冻结一整局"，现象与 `main3.py` 改名事故一样难排查
    （`CLAUDE.md` 硬约束 3）。`role_type` 是 Action 唯一能自证的权限，所以闸门只能在这里。
    """
    try:
        cmds[str(role.id)] = cls(role.type_name, *args).to_wire()
    except PermissionError as exc:
        LOGGER.warning("拦下越权动作：角色 %s(%s) %s", role.id, role.type_name, exc)
        return False
    return True
