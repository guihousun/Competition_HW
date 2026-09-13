"""几何基元：坐标、方向、距离、寻路。**不依赖任何其它模块。**

任务书 §4.5.4：距离一律用切比雪夫 `max(|dx|, |dy|)`；角色可朝 8 个方向移动。
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

    **终点是"贴着 goal 的一格"，不是 goal 本身。** goal 通常是一格挡路的东西
    （矿、建筑、武器操控位），角色本来就只能站在它旁边 —— 走到邻格即收工。
    BFS 只在可通行格上展开，所以它永远不会把 `goal` 自己当成落脚点，无需特判。

    `size` = `(width, height)`（接口文档 §1.2.1）：**BFS 会往远处探路，不像贪心只挪一格**，
    所以必须自己挡住地图外。任务书 L85 那张"阻挡移动"的清单里**没写地图边界**，
    越界会怎样是未知的——但 `(0,0)~(width-1,height-1)` 之外一律不走，一定安全。
    尺寸无效（≤0）时无格可走，返回 None ⇒ 单位不动。

    返回的是**从 pos 迈出的第一步**，不是整条路径。
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
            # 注意用新变量：`first` 是**这一格**的，内层循环里每个邻居各算各的。
            # 直接改 `first` 会让第二个邻居继承第一个的第一步。
            nxt_first = nxt if first is None else first
            if nxt.dist(goal) == 1:
                return nxt_first
            queue.append((nxt, nxt_first))
    return None


def base_cells(top_left: Pos) -> set[Pos]:
    """基地的 2×2 四格（接口文档 §1.3.1 注）。

    `pos` 给的是**左上角** ⇒ x 向右增、**y 向下减**。只标一格会让角色一头撞进基地里。
    """
    return {Pos(top_left.x + dx, top_left.y - dy) for dx in (0, 1) for dy in (0, 1)}


def weapon_cells(base: Pos) -> tuple[Pos, ...]:
    """可建造武器的 12 格 = 基地外圈 4×4 减去基地自身。

    ⚠️ **来源是图，不是正文**：任务书没有坐标公式，`docs/pic/build_map.png` 写着
    "中间黄色 2×2：基地 / 绿色 4×4 外圈：可建造武器 / 蓝色 6×6 外圈：可建造围墙"。
    基地 `pos` 是左上角、占 `y` 与 `y-1`，所以 4×4 环是 `x ∈ [bx-1, bx+2]`、
    `y ∈ [by-2, by+1]`，16 − 4 = 12 格。

    样例可作交叉验证：基地 `(10,24)` ⇒ 环含 `(9,24)` / `(9,25)` / `(10,25)`，
    正是样例那三座武器所在（**样例几何整体是手画的，不能当校准依据**，但环这一处对得上）。
    """
    own = base_cells(base)
    return tuple(
        Pos(x, y)
        for y in range(base.y - 2, base.y + 2)
        for x in range(base.x - 1, base.x + 3)
        if Pos(x, y) not in own
    )


def back_weapon_cells(base: Pos, width: int) -> tuple[Pos, ...]:
    """武器环里**背向机器人**的那一列（4 格），紧贴基地纵深的排前面。

    机器人从基地**面向地图中心**的那一侧水平逼近（`docs/pic/大致地图信息.png`：
    蓝方基地在左、刷新点在其右、箭头向左；红方镜像）。**任务书正文没写刷新点**，
    这是唯一依据。于是"基地后方" = 远离地图中心的那一列：
    **基地在地图左半 ⇒ 后方 `x = bx-1`；右半 ⇒ 后方 `x = bx+2`**。

    按**基地坐标**判而不用 `teamOur.type`：下半场换边后队伍身份不变、基地会挪，
    按坐标判会自动跟着翻。

    同列 4 格按"是否落在基地的纵向跨度内"排（紧贴基地的先建），并列按 y 升序。
    """
    back_x = base.x - 1 if base.x * 2 < width else base.x + 2
    own_ys = {base.y, base.y - 1}
    column = [c for c in weapon_cells(base) if c.x == back_x]
    return tuple(sorted(column, key=lambda c: (c.y not in own_ys, c.y)))
