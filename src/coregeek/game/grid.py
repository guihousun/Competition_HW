"""几何基元：坐标、方向、基地几何。不依赖任何其它模块。

两个距离口径，别混用：`Pos.dist` = 切比雪夫直线（选点用，不绕障、不是能走到的步数）；
BFS 真实步数在 `path.py`（回合预算用，不可达返回 -1，调用方必须接住）。
"""

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
    （`utils._walled` 的判据来源）。
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
