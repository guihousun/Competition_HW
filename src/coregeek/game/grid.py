"""几何基元：坐标、方向、距离、寻路、基地几何。不依赖任何其它模块。

两个距离口径，别混用：`Pos.dist` = 切比雪夫直线（"谁离得近"这类选点用，不绕障，
不是能走到的步数）；`steps_between` = BFS 真实步数（回合预算用，不可达返回 -1，
调用方必须接住）。角色可朝 8 个方向移动；无权重 ⇒ BFS 的步数就是切比雪夫下的最短路长度。
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

    终点是"贴着 goal 的一格"，不是 goal 本身 —— goal 通常是挡路的（矿、建筑、武器
    操控位），角色本来就只能站在它旁边；BFS 只在可通行格上展开，无需特判。
    返回的是从 pos 迈出的第一步。`size` = `(width, height)`：BFS 会往远处探路，
    必须自己挡住地图外（越界后果文档没写，不走一定安全）；尺寸无效（≤0）⇒ 单位不动。
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


def step_outside(pos: Pos, box: Set[Pos], blocked: Set[Pos], size: tuple[int, int]) -> Pos | None:
    """朝 `box` 外面走一格（BFS 最短路）；`pos` 已经在外面、或压根走不出去，返回 None。

    与 `step_toward` 同构，差别只在终止条件：这里判 `nxt not in box`。不能用
    `step_toward` 顶替：「盒子外面」不是一个 goal，而且它"贴着 goal 即到"的契约会把
    正贴着门口而门被堵住的角色误判成"到不了"。`box` 是格子集合（由 `box_cells` 给）。
    "本来就在外面"与"走不出去"合流成一个 None 是有意的 ⇒ 谁在盒子里面必须由调用方筛。
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

    与 `step_toward` 同一个终点约定（贴着 goal 一格、goal 自己当障碍），差别只在
    返回步数而不是第一步：`start` 已经贴着 goal ⇒ 0。回炮位必须绕过整面围墙从
    背面的门进来，直线距离会低估得离谱 ⇒ 回合预算一律用它算。
    -1 是本函数独有的失败态（`Pos.dist` 永远不会返回它），每个调用点都要自己接住：
    一律按"这趟不去了"处理（别把回程预算算成负数、把人留在墙外过夜）。
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
    """整个防御盒子：36 格 = 基地 4 + 武器环 12 + 围墙环 20，即 `x ∈ [bx-2, bx+3]` × `y ∈ [by-3, by+2]`。

    给"会不会被墙关住"提供里 / 外判据（`step_outside` 的 `box`）。
    """
    return frozenset(
        Pos(x, y)
        for x in range(base.x - 2, base.x + 4)
        for y in range(base.y - 3, base.y + 3)
    )


def weapon_cells(base: Pos) -> tuple[Pos, ...]:
    """可建造武器的 12 格 = 基地外圈 4×4 减去基地自身（`x ∈ [bx-1, bx+2]`、`y ∈ [by-2, by+1]`）。

    公式只存在于图里（`docs/pic/build_map.png`），任务书正文没有。算错 ⇒ build 落点非法、25 金白花。
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

    基地在地图左半 ⇒ `d = +1`（机器人从 `+x` 来），右半 ⇒ `-1`；`far` / `near` =
    基地朝机器人一侧 / 背向一侧的那条边（基地占 `[bx, bx+1]`）。按基地坐标判而不用
    `teamOur.type`：换边后基地会挪，坐标判自动跟着翻。正面/背面的唯一出处 ——
    `wall_cells` 与 `weapon_sites` 都从这里取。
    """
    d = 1 if base.x * 2 < width else -1
    return (d, base.x + 1, base.x) if d > 0 else (d, base.x, base.x + 1)


def wall_cells(base: Pos, width: int) -> tuple[Pos, ...]:
    """可砌围墙的 18 格，按建造顺序排。6×6 边框 20 格里砌 18：正面列 6（含封口格）
    + 两侧行各 5（含背面两角）+ 背面 2；正面/背面由 `_front_back` 给（左半基地 ⇒
    正面 x = bx+3、背面 x = bx-2）。

    顺序 = 沿环走一圈：正面列 → 一侧行 → 背面（先砌中间 2 格、穿过门）→ 另一侧行
    → 封口格。走一圈绕行量最小 —— 工人一天只有 70 回合，别的排法砌不满 18 格、
    封口格当天轮不到。
    背面中间 2 格（`door_cells`）永远是门：环闭合后盒子唯一的进出口；门里没有
    任何建筑 ⇒ 能堵门的只有单位。封口格 = 正面列正中 `(front_x, by)`、排最后
    ⇒ 白天开着方便通行、天黑前砌上封死。
    """
    d, far, near = _front_back(base, width)
    front_x, back_x = far + 2 * d, near - 2 * d
    ys = list(range(base.y - 3, base.y + 3))  # 6 格
    xs = list(range(base.x - 2, base.x + 4))  # 6 格
    #: 侧面两条的中间 4 格（两端的角归正面列 / 背面列）
    side = xs[1:-1]

    #: 白天开口、天黑前封上的那一格（正面列正中）
    seal = Pos(front_x, base.y)
    # ① 正面列（迎着机器人）：从一端扫到另一端（跳过封口格），含上下两角。
    #    终点是 `ys[-1]` 那个角，正好接上 ② 的第一格。
    order = [Pos(front_x, y) for y in ys if Pos(front_x, y) != seal]
    # ② `ys[-1]` 那一行：从正面往背面铺，末了补上背面那个角
    order += [Pos(x, ys[-1]) for x in reversed(side)] + [Pos(back_x, ys[-1])]
    # ③ 背面从那个角一路下来：先砌中间 2 格之一（门就由这 2 格围出），
    #    再穿过门、砌另一格，最后落到底行那个角
    order += [Pos(back_x, base.y + 1), Pos(back_x, base.y - 2), Pos(back_x, ys[0])]
    # ④ 另一行：从背面往正面铺，终点紧挨封口格
    order += [Pos(x, ys[0]) for x in side]
    # ⑤ 封口格排最后 ⇒ 白天最后才砌它
    return tuple(order + [seal])


def door_cells(base: Pos, width: int) -> tuple[Pos, ...]:
    """永远不砌的 2 格：背面列正中（基地纵深中心那一行及其下一行），盒子唯一的进出口。

    门里没有任何建筑 ⇒ 能堵门的只有单位 —— "自己人站在待砌格上算不算障碍"只在
    门口有意义（`planner._walled` 的判据来源）。
    """
    d, _far, near = _front_back(base, width)
    back_x = near - 2 * d
    return (Pos(back_x, base.y), Pos(back_x, base.y - 1))


def weapon_sites(base: Pos, width: int) -> tuple[Pos, ...]:
    """三座武器的落点，按建造顺序：前排相邻两格（2 火箭）+ 前排下一格（加特林）。

    两火箭相邻 ⇒ 一个角色站内侧那格 `(bx+1, by+1)`（左半基地即 `(11, 25)`）就能同时
    贴两座、利用火箭 3 回合冷却交替开火 ⇒ 2 个角色即可操 3 座武器。
    正/背面由 `_front_back` 给（左半基地 front_x = bx+2）。左半基地 `(10,24)` ⇒
    `((12,24), (12,25), (12,22))`，三格都在 `weapon_cells` 的可建造环里（公式来自图片）。
    """
    d, far, near = _front_back(base, width)
    front_x = far + d
    return (
        Pos(front_x, base.y),       # rocket 1（前排、基地纵深中心行）
        Pos(front_x, base.y + 1),   # rocket 2（前排、上一格，与 rocket 1 相邻）
        Pos(front_x, base.y - 2),   # gatling（前排、下两格）
    )
