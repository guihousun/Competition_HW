"""几何基元：坐标、方向、距离。**纯值对象，不依赖任何其它模块。**

任务书 §4.5.4：距离一律用切比雪夫 `max(|dx|, |dy|)`；角色可朝 8 个方向移动。
"""

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


def step_toward(pos: Pos, goal: Pos, blocked: Set[Pos]) -> Pos | None:
    """朝 goal 走一格：8 邻格里挑"没被占、且确实更近"的那格；走不动就返回 None。

    贪心而非寻路 —— 绕不过去的障碍会停在原地。等距离开始影响取舍（岗位点、矿区）时再换 BFS。
    """
    best: Pos | None = None
    best_dist = pos.dist(goal)
    for step in STEPS:
        nxt = Pos(pos.x + step.x, pos.y + step.y)
        if nxt in blocked:
            continue
        dist = nxt.dist(goal)
        if dist < best_dist:
            best, best_dist = nxt, dist
    return best


def base_cells(top_left: Pos) -> set[Pos]:
    """基地的 2×2 四格（接口文档 §1.3.1 注）。

    `pos` 给的是**左上角** ⇒ x 向右增、**y 向下减**。只标一格会让角色一头撞进基地里。
    """
    return {Pos(top_left.x + dx, top_left.y - dy) for dx in (0, 1) for dy in (0, 1)}
