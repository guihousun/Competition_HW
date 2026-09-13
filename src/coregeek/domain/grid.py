"""格子与移动的原语。

**为什么 Pos 在 domain 而不是 protocol**：分层契约是 `protocol/ → domain/`，
若 `Pos` 定义在 protocol 里，domain 的几何/BFS 就得反向 import protocol，
分层当场失效（`tools/selfcheck.py` 的 import-lint 会把这条钉死）。
`Pos` 是纯值对象、不含任何线上 JSON 形态，放 domain 是唯一自洽的位置；
`protocol/model.py` 改为从这里 import 再 re-export，保证 `model.Pos` 仍可用。

✅ 已用官方 demo 的 `protocol.distance` / `grid.next_step` 交叉验证：
移动是 8 向、距离是**切比雪夫**；**对角穿缝合法**（两个斜对角的障碍不阻挡斜走），
所以 BFS 不需要"禁止切角"那套额外规则。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Pos", "NEIGHBOUR_STEPS", "STEP_TO_NAME"]

#: 8 向邻居的顺序与 demo `grid._STEPS` 一致（左上起顺时针），不要改
NEIGHBOUR_STEPS: tuple[tuple[int, int], ...] = (
    (-1, -1), (-1, 0), (-1, 1),
    (0, -1), (0, 1),
    (1, -1), (1, 0), (1, 1),
)

STEP_TO_NAME: dict[tuple[int, int], str] = {
    (0, -1): "N", (1, -1): "NE", (1, 0): "E", (1, 1): "SE",
    (0, 1): "S", (-1, 1): "SW", (-1, 0): "W", (-1, -1): "NW",
}


@dataclass(frozen=True, slots=True)
class Pos:
    x: int
    y: int

    def dump(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y}

    def __add__(self, other: tuple[int, int]) -> "Pos":
        return Pos(self.x + other[0], self.y + other[1])

    def step(self, dx: int, dy: int) -> "Pos":
        return Pos(self.x + dx, self.y + dy)

    def distance(self, other: "Pos") -> int:
        """切比雪夫距离（任务书 §4.5.4）。**不是**曼哈顿。"""
        return max(abs(self.x - other.x), abs(self.y - other.y))

    def neighbours(self) -> tuple["Pos", ...]:
        return tuple(Pos(self.x + dx, self.y + dy) for dx, dy in NEIGHBOUR_STEPS)

    def ring(self, radius: int) -> tuple["Pos", ...]:
        """到自己的切比雪夫距离**恰为** radius 的一圈格子。"""
        if radius <= 0:
            return (self,) if radius == 0 else ()
        out = [
            Pos(self.x + dx, self.y + dy)
            for dx in range(-radius, radius + 1)
            for dy in (-radius, radius)
        ]
        out += [
            Pos(self.x + dx, self.y + dy)
            for dy in range(-radius + 1, radius)
            for dx in (-radius, radius)
        ]
        return tuple(out)
