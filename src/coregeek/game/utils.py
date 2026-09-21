"""通用判定：拿一个 `Turn` 回答一个问题（谁能走 / 环砌在哪 / 谁会被关住 / 人手够不够）。

`core`/`day`/`night`/`planner` 的下层（都单向 import 本模块）—— 放这儿的判据一律是两边都要
问的；只有一个消费者的判据留在各自的模块里。

两种判据别混：`_passable` / `_stuck_inside` 问"现在怎么走"（自己人一律算路过），
`_walled` / `_trapped` 问"砌满之后怎么走"（自己人只在后方通道那几格算障碍）—— 一个补救人、
一个拦住人，口径故意不同。
"""

from .grid import Pos, door_cells, wall_cells, weapon_sites
from .path import step_outside
from .roles import BaseRole, Worker
from .world import ROUNDS_PER_DAY, Turn, Weapon


def _passable(turn: Turn) -> set[Pos]:
    """估算距离用的地形：`map.blocked` 剔掉我方角色站着的格子。

    与"这一回合实际怎么走"是两套口径（`_Queue.step` / `_walk_out` 必须把同事当硬障碍：撞上
    就是执行失败）。这里问的是"路有多远"：环砌满之后盒子里只剩一格宽的走廊，同事停在走廊上
    就会让估算判成不可达（-1）、整条经济线静默放弃。"""
    return turn.map.blocked - {r.pos for r in turn.roles}


def _sealed_back(turn: Turn) -> bool:
    """第 3 天起把背面两个角格补上（前两天的环只有 14 格，背面整列敞开）。

    判据只能用回合号：环上"没砌"与"砌了又被拆"在地图上同形。"""
    return turn.round_no > 2 * ROUNDS_PER_DAY


def _ring(turn: Turn) -> tuple[Pos, ...]:
    """按优先级排的围墙格；基地没了 ⇒ 空。

    既不在 `wall_cells` 里、也不挡路的格才算候选 —— 但自己人站的那一格除外：工人站在待砌的
    墙格上只是路过，若把它划掉，另一个工人的 `free[0]` 会整体后移、等那人一挪窝目标又变回来
    —— 两个工人在两格之间对着改目标，一格都砌不上。这里只管"哪些格能砌"，"这一回合还砌不
    砌"是 `_trapped` 的事。"""
    station = turn.map.station
    if station is None:
        return ()
    # 我方角色当前站的格（角色能走的都在这）
    mine = {r.pos for r in turn.roles}
    cells = wall_cells(station, turn.map.size[0], sealed=_sealed_back(turn))
    return tuple(c for c in cells if c not in turn.map.blocked or c in mine)


def _walled(turn: Turn) -> frozenset[Pos]:
    """"假设墙砌满"时的障碍集：现已挡路的照原样 + 那一圈墙（14 或 16 格）。

    闸门问的是将来。自己人算不算障碍只看后方通道那几格（与 `_ring` 的"自己人一律算路过"
    故意相反）：那圈墙里没有建筑，能堵门的只有单位，环内站着的自己人是过路的；一律算障碍
    会让走廊另一头的工人被判成"砌满就出不去"、两人来回踱步一整天不砌墙。
    守门：`test_a_colleague_in_the_door_still_holds_the_wall_back`。"""
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

    判据是"整面墙"不是"某一格"⇒ 调用方要的是一个布尔量；返回 id 是因为配套的闸门得知道
    谁先出来。"""
    station = turn.map.station
    if station is None or not box:
        return frozenset()  # 没基地 ⇒ 既没有盒子也没有围墙，谁都关不住
    walled = _walled(turn)
    return frozenset(
        r.id
        for r in turn.roles
        # `r.pos in box` 这一半不能省：`step_outside` 对"本来就在外面"也返回 None
        if r.pos in box and step_outside(r.pos, box, walled, turn.map.size) is None
    )


def _stuck_inside(turn: Turn, box: frozenset[Pos]) -> tuple[BaseRole, ...]:
    """现在真的走不出盒子的人；没有就空元组。与 `_trapped` 的区别是用"现在"的障碍而不是
    "假设砌满"：闸门是预防，这里是补救。自己人一律不算障碍 —— 同事站在门格上只是路过，
    把他算成障碍会白拆一次。"""
    if not box:
        return ()
    walk = _passable(turn)
    size = turn.map.size
    return tuple(
        r for r in turn.roles if r.pos in box and step_outside(r.pos, box, walk, size) is None
    )


def _weapon_groups(turn: Turn) -> tuple[tuple[Weapon, ...], ...]:
    """把武器分成操作组：同一组的武器由同一个角色操作。

    当前阵形 = 2 火箭（相邻）+ 1 加特林 ⇒ 两组。分组依据是 `weapon_sites` 的下标（0,1 =
    火箭对；2 = 加特林），不按场上已有武器的种类猜。某座还没建出来 ⇒ 那一组只含已建的。"""
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
    # 不在 `weapon_sites` 里的武器（测试手搭的位置）⇒ 单独成组，降级为"一人操一座"
    for w in turn.weapons:
        if w.pos not in grouped:
            groups.append((w,))
    return tuple(groups)


def _pioneer_mans_guns(turn: Turn) -> bool:
    """开拓者这一轮该不该上炮位：只有工人不够覆盖全部武器组时才补位。

    工人夜里除了操炮没别的活，而开拓者是任务线的主力 —— 工人阵亡（只可能在夜里）后人手
    不够了它才补位。昼夜同一个判据：白天用它决定收工回不回到炮位（`day.BackToPost`），夜里用它
    决定认不认领武器（`night.defend`）—— 两处必须同源，只改一处会让白天把人送进岗位、夜里又不
    认领，那格被占着、整组没人操。`_short_handed` 取用它。"""
    workers = sum(1 for r in turn.roles if isinstance(r, Worker))
    return workers < len(_weapon_groups(turn))


def _short_handed(turn: Turn) -> bool:
    """夜里操炮的人手够不够：不够 ⇒ 被任务钉死的开拓者也得弃任务回炮位（生存第一）。

    白天恒假：白天不能开火、回炮位没有意义（那一支只发 `move`）。判据与
    `_pioneer_mans_guns` 同源（一人只能操一组，少一人空一组），不带跨回合状态。"""
    return not turn.is_day and _pioneer_mans_guns(turn)
