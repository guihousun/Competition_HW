"""几何基元：坐标、方向、距离、寻路、基地几何。**不依赖任何其它模块。**

距离一律用切比雪夫 `max(|dx|, |dy|)`；角色可朝 8 个方向移动。
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
    """朝 goal 走一格，**BFS 最短路**；已经在 goal 旁边、或压根走不到，返回 None。

    **终点是"贴着 goal 的一格"，不是 goal 本身** —— goal 通常是一格挡路的东西
    （矿、建筑、武器操控位），角色本来就只能站在它旁边。BFS 只在可通行格上展开，
    所以它不会把 `goal` 自己当成落脚点，无需特判。返回的是**从 pos 迈出的第一步**。

    `size` = `(width, height)`：BFS 会往远处探路，所以必须自己挡住地图外
    （越界会怎样文档没写，不走一定安全）；尺寸无效（≤0）时无格可走 ⇒ 单位不动。
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
    """朝 `box` **外面**走一格（BFS 最短路）；`pos` 已经在外面、或压根走不出去，返回 None。

    与 `step_toward` 同构，差别只在**终止条件**：那里是"贴着某一格"，这里是 `nxt not in box`。
    不能用 `step_toward` 顶替：「盒子外面」不是一个 goal；而且它的契约是"贴着 goal 即到"
    （`dist <= 1 ⇒ None`），拿它测"出不出得去"会把**正贴着门口、而门被堵住**的角色
    误判成"到不了"。

    `box` 是**格子集合**而不是矩形（由 `box_cells` 给）。
    ⚠️ "本来就在外面"与"走不出去"合流成一个 `None` 是**有意的**，代价是
    **"谁在盒子里面"必须由调用方自己筛**（`planner._trapped` 就踩过这个）。
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


def base_cells(top_left: Pos) -> set[Pos]:
    """基地的 2×2 四格。`pos` 给的是**左上角** ⇒ x 向右增、**y 向下减**。"""
    return {Pos(top_left.x + dx, top_left.y - dy) for dx in (0, 1) for dy in (0, 1)}


def box_cells(base: Pos) -> frozenset[Pos]:
    """整个防御盒子：**36 格** = 基地 4 + 武器环 12 + 围墙环 20。

    即 `x ∈ [bx-2, bx+3]` × `y ∈ [by-3, by+2]`。
    存在的唯一理由是给"会不会被墙关住"提供"里 / 外"的判据（`step_outside` 的 `box`）。
    """
    return frozenset(
        Pos(x, y)
        for x in range(base.x - 2, base.x + 4)
        for y in range(base.y - 3, base.y + 3)
    )


def weapon_cells(base: Pos) -> tuple[Pos, ...]:
    """可建造武器的 12 格 = 基地外圈 4×4 减去基地自身（`x ∈ [bx-1, bx+2]`、`y ∈ [by-2, by+1]`）。

    ⚠️ 公式的来源是图（`docs/pic/build_map.png`），任务书正文没有。
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

    `d` = 机器人来的方向：基地在地图左半 ⇒ `+1`（从 `+x` 来），右半 ⇒ `-1`。
    `far` / `near` = 基地朝机器人一侧 / 背向一侧的那条边（基地占 `[bx, bx+1]`）。
    按**基地坐标**判而不用 `teamOur.type`：换边后基地会挪，按坐标判会自动跟着翻。

    **只此一处**：`wall_cells` 与 `weapon_sites` 的正面/背面都从这里取。
    """
    d = 1 if base.x * 2 < width else -1
    return (d, base.x + 1, base.x) if d > 0 else (d, base.x, base.x + 1)


def wall_cells(base: Pos, width: int) -> tuple[Pos, ...]:
    """可砌围墙的 **14 格，按建造优先级排**（**背面整列一格都不砌**）。

    6×6 边框里既不属于基地 4 格、也不属于武器环 12 格的格子，共 20 格；砌其中 14 格：
    **正面列 6 + 顶行 4 + 底行 4**。正面/背面由 `_front_back` 给（左半基地 ⇒ 正面 `x = bx+3`）。
    顺序 = 正面列（正对基地的先砌）→ 顶行 → 底行。

    **背面整列 6 格（含上下两角）留成一道永远不砌的"门"**：环一闭合工人就进出不得
    （采不了矿、回不到环内操炮），而 `remove`（拆墙）没实现 ⇒ 关进去就是整场出不来。
    正面仍完整，钻进来的机器人紧贴着武器列与基地，等于直接撞在火力上。
    ⚠️ 门那 6 格里**没有任何建筑**，所以能堵门的只有**单位**。
    """
    d, far, _ = _front_back(base, width)
    front_x = far + 2 * d
    ys = list(range(base.y - 3, base.y + 3))  # 6 格
    xs = list(range(base.x - 2, base.x + 4))  # 6 格
    #: 基地纵深中心的 2 倍 —— 用整数比大小，避免浮点
    center = 2 * base.y - 1

    front = sorted(ys, key=lambda y: (abs(2 * y - center), y))
    order = [Pos(front_x, y) for y in front]
    # `xs[1:-1]` 正好排除两端的正面列与背面列；`reversed` = 从正面往背面铺
    for row_y in (ys[-1], ys[0]):
        order += [Pos(x, row_y) for x in reversed(xs[1:-1])]
    return tuple(order)


def weapon_sites(base: Pos, width: int) -> tuple[Pos, ...]:
    """三座武器的**落点，按建造顺序**：后列 1 格 + 前排两角。

    阵形要求**三个落点各自与基地的一角切比雪夫距离 1** —— 升级券与维修包必须在目标建筑
    周围一格内使用，`attack` 也要求角色站在炮旁 ⇒ 一个角色站在落点上，脚下的炮和旁边的基地
    一够就是两个。正/背面由 `_front_back` 给：

    - **后列 `(back_x, by-1)`**：贴基地下沿，离机器人最远，基地挡在它前面；
    - **前排两角 `(front_x, by-2)` 与 `(front_x, by+1)`**：主战线上的两个侧翼位。

    左半基地 `(10, 24)` ⇒ `((9, 23), (12, 22), (12, 25))`。三个都在 `weapon_cells` 里。
    """
    d, far, near = _front_back(base, width)
    back_x, front_x = near - d, far + d
    return (
        Pos(back_x, base.y - 1),
        Pos(front_x, base.y - 2),
        Pos(front_x, base.y + 1),
    )
