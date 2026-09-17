"""几何基元：坐标、方向、距离、寻路、基地几何。不依赖任何其它模块。

两个距离口径，别混用：`Pos.dist` = 切比雪夫直线（选点用，不绕障、不是能走到的步数）；
`steps_between` = BFS 真实步数（回合预算用，不可达返回 -1，调用方必须接住）。角色可朝
8 个方向移动；无权重 ⇒ BFS 的步数就是切比雪夫下的最短路长度。
"""

from collections import deque
from collections.abc import Set
from typing import NamedTuple


class Pos(NamedTuple):
    x: int
    y: int

    def dist(self, other: "Pos") -> int:
        """切比雪夫距离。"""
        return max(abs(self.x - other.x), abs(self.y - other.y))


#: 8 个移动方向（不含原地）。
STEPS: tuple[Pos, ...] = tuple(
    Pos(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (dx, dy) != (0, 0)
)


def step_toward(pos: Pos, goal: Pos, blocked: Set[Pos], size: tuple[int, int]) -> Pos | None:
    """朝 goal 走一格（BFS 最短路）；已经贴着 goal、或压根走不到，返回 None。

    终点是"贴着 goal 的一格"，不是 goal 本身 —— goal 通常是挡路的（矿、建筑、武器操控
    位），角色本来就只能站在它旁边；BFS 只在可通行格上展开，无需特判。返回的是从 pos
    迈出的第一步。`size` = `(width, height)`：BFS 会往远处探路，必须自己挡住地图外
    （越界后果文档没写，不走一定安全）；尺寸无效（≤0）⇒ 单位不动。
    """
    if pos.dist(goal) <= 1:
        return None  # 已经贴着 goal（含 pos 就是 goal），再动反而走远
    width, height = size
    # 队列里存 `(当前格, 从 pos 迈出的第一步)`；pos 自己还没有"第一步"，故为 None
    queue: deque[tuple[Pos, Pos | None]] = deque([(pos, None)])
    seen = {pos}
    while queue:
        cell, first = queue.popleft()
        for step in STEPS:
            nxt = Pos(cell.x + step.x, cell.y + step.y)
            if nxt in seen or nxt in blocked:
                continue
            if not (0 <= nxt.x < width and 0 <= nxt.y < height):
                continue
            seen.add(nxt)
            # 用新变量：直接改 `first` 会让第二个邻居继承第一个的第一步。
            nxt_first = nxt if first is None else first
            if nxt.dist(goal) == 1:
                return nxt_first
            queue.append((nxt, nxt_first))
    return None


def step_onto(pos: Pos, goal: Pos, blocked: Set[Pos], size: tuple[int, int]) -> Pos | None:
    """朝 goal **自己**走一格（BFS 最短路）；`pos` 已经在 goal 上、或走不到，返回 None。

    与 `step_toward` 的唯一差别是终点：那里要停"贴着 goal 的一格"（goal 通常挡路），这里要
    停在 goal 上 —— 共用的武器操作位就是这种格子（那格是空地，得走上去才贴着两座炮）。
    两个函数不能互换：`step_toward` 到不了这种格子，`step_onto` 停不进挡路的 goal。
    """
    if pos == goal:
        return None  # 已经在上面（含"贴着"以外的情形：这里只能靠 == 判）
    width, height = size
    # 队列里存 `(当前格, 从 pos 迈出的第一步)`；pos 自己还没有"第一步"，故为 None
    queue: deque[tuple[Pos, Pos | None]] = deque([(pos, None)])
    seen = {pos}
    while queue:
        cell, first = queue.popleft()
        for step in STEPS:
            nxt = Pos(cell.x + step.x, cell.y + step.y)
            if nxt in seen or nxt in blocked:
                continue
            if not (0 <= nxt.x < width and 0 <= nxt.y < height):
                continue
            seen.add(nxt)
            # 用新变量：直接改 `first` 会让第二个邻居继承第一个的第一步。
            nxt_first = nxt if first is None else first
            if nxt == goal:
                return nxt_first
            queue.append((nxt, nxt_first))
    return None


def step_outside(pos: Pos, box: Set[Pos], blocked: Set[Pos], size: tuple[int, int]) -> Pos | None:
    """朝 `box` 外面走一格（BFS 最短路）；`pos` 已经在外面、或压根走不出去，返回 None。

    与 `step_toward` 同构，差别只在终止条件（这里判 `nxt not in box`）。不能用它顶替：
    「盒子外面」不是一个 goal，而且它"贴着 goal 即到"的契约会把正贴着门口而门被堵住的
    角色误判成"到不了"。`box` 是格子集合（由 `box_cells` 给）。"本来就在外面"与"走不
    出去"合流成一个 None 是有意的 ⇒ 谁在盒子里面必须由调用方筛。
    """
    if pos not in box:
        return None  # 本来就在外面
    width, height = size
    # 队列里存 `(当前格, 从 pos 迈出的第一步)`；pos 自己还没有"第一步"，故为 None
    queue: deque[tuple[Pos, Pos | None]] = deque([(pos, None)])
    seen = {pos}
    while queue:
        cell, first = queue.popleft()
        for step in STEPS:
            nxt = Pos(cell.x + step.x, cell.y + step.y)
            if nxt in seen or nxt in blocked:
                continue
            if not (0 <= nxt.x < width and 0 <= nxt.y < height):
                continue
            seen.add(nxt)
            # 与 `step_toward` 同一个坑：用新变量，别把上一格的第一步漏给下一个邻居
            nxt_first = nxt if first is None else first
            if nxt not in box:
                return nxt_first
            queue.append((nxt, nxt_first))
    return None


def steps_between(start: Pos, goal: Pos, blocked: Set[Pos], size: tuple[int, int]) -> int:
    """走到"贴着 goal 的一格"要几步 —— BFS 真实步数（绕障）；走不到返回 -1。

    与 `step_toward` 同一个终点约定（贴着 goal 一格、goal 自己当障碍），差别只在返回步数
    而不是第一步（`start` 已经贴着 goal ⇒ 0）。回炮位必须绕过整面围墙从背面的门进来，
    直线距离会低估得离谱 ⇒ 回合预算一律用它算。-1 是本函数独有的失败态（`Pos.dist`
    永远不会返回它），每个调用点都要自己接住：一律按"这趟不去了"处理（别把回程预算算成
    负数、把人留在墙外过夜）。
    """
    if start.dist(goal) <= 1:
        return 0  # 已经贴着 goal（含 start 就是 goal）
    width, height = size
    # 队列里存 `(当前格, 从 start 走了几步)` —— 这是与 `step_toward` 唯一的实现差别
    queue: deque[tuple[Pos, int]] = deque([(start, 0)])
    seen = {start}
    while queue:
        cell, depth = queue.popleft()
        for step in STEPS:
            nxt = Pos(cell.x + step.x, cell.y + step.y)
            if nxt in seen or nxt in blocked:
                continue
            if not (0 <= nxt.x < width and 0 <= nxt.y < height):
                continue
            seen.add(nxt)
            if nxt.dist(goal) == 1:
                return depth + 1
            queue.append((nxt, depth + 1))
    return -1


def base_cells(top_left: Pos) -> set[Pos]:
    """基地的 2×2 四格。`pos` 给的是左上角 ⇒ x 向右增、y 向下减。"""
    return {Pos(top_left.x + dx, top_left.y - dy) for dx in (0, 1) for dy in (0, 1)}


def box_cells(base: Pos) -> frozenset[Pos]:
    """整个防御盒子：36 格 = 基地 4 + 武器环 12 + 围墙环 20，即 `x ∈ [bx-2, bx+3]` ×
    `y ∈ [by-3, by+2]`。给"会不会被墙关住"提供里 / 外判据（`step_outside` 的 `box`）。
    """
    return frozenset(
        Pos(x, y)
        for x in range(base.x - 2, base.x + 4)
        for y in range(base.y - 3, base.y + 3)
    )


def weapon_cells(base: Pos) -> tuple[Pos, ...]:
    """可建造武器的 12 格 = 基地外圈 4×4 减去基地自身（`x ∈ [bx-1, bx+2]`、`y ∈ [by-2, by+1]`）。

    公式只存在于图里（`docs/pic/build_map.png`），任务书正文没有。算错 ⇒ build 落点非法、
    25 金白花。
    """
    own = base_cells(base)
    return tuple(
        Pos(x, y)
        for y in range(base.y - 2, base.y + 2)
        for x in range(base.x - 1, base.x + 3)
        if Pos(x, y) not in own
    )


def _front_back(base: Pos, width: int) -> tuple[int, int, int]:
    """`(d, far, near)` —— 机器人来的方向，以及基地朝它 / 背它的那两条边。

    基地在地图左半 ⇒ `d = +1`（机器人从 `+x` 来），右半 ⇒ `-1`；`far` / `near` = 基地朝
    机器人一侧 / 背向一侧的那条边（基地占 `[bx, bx+1]`）。按基地坐标判而不用
    `teamOur.type`：换边后基地会挪，坐标判自动跟着翻。正面/背面的唯一出处 ——
    `wall_cells` 与 `weapon_sites` 都从这里取。
    """
    d = 1 if base.x * 2 < width else -1
    return (d, base.x + 1, base.x) if d > 0 else (d, base.x, base.x + 1)


def wall_cells(base: Pos, width: int, *, sealed: bool = False) -> tuple[Pos, ...]:
    """可砌围墙的格，按建造顺序排。正面列 6 + 上下两行各 4 = 14 格；`sealed` 再把背面
    两个角格补上（16 格）—— 背面整列常年敞开，那是盒子唯一的进出口（`door_cells`）。

    正面/背面由 `_front_back` 给（左半基地 ⇒ 正面 x = bx+3、背面 x = bx-2）。顺序 =
    沿环走一圈：正面列 → 下行（正面往背面）→〔背面两角〕→ 上行（背面往正面）。正面列排
    第一位 ⇒ 回合不够时先砌的就是迎着机器人的那一面。
    """
    d, far, near = _front_back(base, width)
    front_x, back_x = far + 2 * d, near - 2 * d
    ys = list(range(base.y - 3, base.y + 3))  # 6 格
    xs = list(range(base.x - 2, base.x + 4))  # 6 格
    # 上下两行的中间 4 格（两端的角归正面列 / 背面列）
    side = xs[1:-1]

    order = [Pos(front_x, y) for y in ys]  # ① 正面列（迎着机器人）
    order += [Pos(x, ys[-1]) for x in reversed(side)]  # ② 下行：正面 → 背面
    if sealed:
        # ③ 背面两角：下角紧接 ② 的终点，再穿背后那条通道到上角，正好接上 ④
        order += [Pos(back_x, ys[-1]), Pos(back_x, ys[0])]
    order += [Pos(x, ys[0]) for x in side]  # ④ 上行：背面 → 正面
    return tuple(order)


def door_cells(base: Pos, width: int, *, sealed: bool = False) -> tuple[Pos, ...]:
    """后方通道：背面列里不砌的那些格（14 格时 6 个，16 格时 4 个），盒子唯一的进出口。

    通道里没有任何建筑 ⇒ 能堵门的只有单位 —— "自己人站在待砌格上算不算障碍"只在这里有意义
    （`planner._walled` 的判据来源）。
    """
    d, _far, near = _front_back(base, width)
    back_x = near - 2 * d
    walls = set(wall_cells(base, width, sealed=sealed))
    return tuple(
        Pos(back_x, y) for y in range(base.y - 3, base.y + 3) if Pos(back_x, y) not in walls
    )


def weapon_sites(base: Pos, width: int) -> tuple[Pos, ...]:
    """三座武器的落点，按建造顺序：前排相邻两格（2 火箭）+ 前排下一格（加特林）。

    两火箭相邻 ⇒ 一个角色站内侧那格 `(bx+1, by+1)`（左半基地即 `(11, 25)`）就能同时贴两座、
    利用火箭 3 回合冷却交替开火 ⇒ 2 个角色即可操 3 座武器。正/背面由 `_front_back` 给（左半
    front_x = bx+2）：左半基地 `(10,24)` ⇒ `((12,24), (12,25), (12,22))`，三格都在
    `weapon_cells` 的可建造环里（公式来自图片）。
    """
    d, far, near = _front_back(base, width)
    front_x = far + d
    return (
        Pos(front_x, base.y),       # rocket 1（前排、基地纵深中心行）
        Pos(front_x, base.y + 1),   # rocket 2（前排、上一格，与 rocket 1 相邻）
        Pos(front_x, base.y - 2),   # gatling（前排、下两格）
    )
