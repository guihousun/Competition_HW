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
