"""固定 36 格防御盒子。

设计见 docs/design/code-design.md §12.1；几何已用 demo 自身的
`station_footprint` / `_tower_sites` / `_wall_order` 在样例基地 (10,24) 上实测验证：

    基地 footprint = (10,23) (10,24) (11,23) (11,24)   → pos 是**左上角**（x 最小、y 最大）
    武器环（4×4 去掉基地） = 12 格
    围墙环（6×6 去掉 4×4） = 20 格
    demo 的 _wall_order   = 19 格 = 20 格环 − 门 (13,22)

**一切坐标由己方 station 的 pos 推导，禁止绝对坐标**（跨半场换边后基地会挪）。

❓未确认：机器人出生区在哪一侧。默认先验是**背向地图中心**（朝最近边缘），
入夜观测到机器人后由 `front_from_positions` 覆盖，也可用 `FORCED_FRONT_SIDE` 写死。
三条支持证据见 `_infer_front` 的注释。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..infra import config
from .grid import Pos

__all__ = [
    "Box",
    "SIDES",
    "box_from_station",
    "front_from_positions",
    "station_footprint",
    "OppositeSide",
]


SIDES = ("-x", "+x", "-y", "+y")

#: ❓出生区未确认时的兜底：正面 = 朝地图中心的那一面。实盘确认后覆盖。
FORCED_FRONT_SIDE: dict[str, str] = {}


@dataclass(frozen=True, slots=True)
class Box:
    """一个基地的固定防御盒子。所有格子都是推导出来的，不可变。"""

    station: Pos
    #: 基地 2×2 占位（4 格）
    footprint: tuple[Pos, ...]
    #: 武器环 = 4×4 去掉基地（12 格）
    weapon_ring: tuple[Pos, ...]
    #: 围墙环 = 6×6 去掉 4×4（20 格）
    wall_ring: tuple[Pos, ...]
    #: 四个边的围墙格（各 6 格；四个角各归一条边，见 sides 的实现）
    sides: dict[str, tuple[Pos, ...]]
    #: 正面（朝向机器人出生区）
    front: str
    #: 门所在的格（背面边上的一格，不建墙）
    door: Pos

    @property
    def back(self) -> str:
        return _opposite(self.front)

    @property
    def wall_budget(self) -> int:
        return len(self.wall_ring) - 1  # 留一格门

    def is_wall_cell(self, pos: Pos) -> bool:
        return pos in self.wall_ring_set

    @property
    def wall_ring_set(self) -> frozenset[Pos]:
        return frozenset(self.wall_ring)

    def wall_order(self) -> tuple[Pos, ...]:
        """建墙顺序：正面 → 两侧 → 背面；**门排除在外**（背面最后才补，见 §6.3）。"""
        order: list[Pos] = []
        front = self.sides[self.front]
        back = self.sides[self.back]
        laterals = [s for s in SIDES if s not in (self.front, self.back)]
        order.extend(_rank_outward(front, self.station))
        for side in laterals:
            order.extend(_rank_outward(self.sides[side], self.station))
        order.extend(p for p in _rank_outward(back, self.station) if p != self.door)
        # 去重并保持顺序（四角可能被两条边同时认领）
        seen: set[Pos] = set()
        out: list[Pos] = []
        for p in order:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return tuple(out)

    def repair_order(self) -> tuple[Pos, ...]:
        """修复顺序：正面最优先，其次两侧，最后背面（与建墙同序）。"""
        return self.wall_order()

    def weapon_sites(self) -> tuple[Pos, ...]:
        """武器候选位：武器环上，按"离正面越近越优先"排序。

        电磁狙击炮射程远、贴正面偏中；火箭溅射、贴正面偏侧（§7.2）。
        """
        return _rank_outward(self.weapon_ring, self.station, toward=self.front)

    def standing_cells(self, site: Pos) -> tuple[Pos, ...]:
        """操控某座武器时角色可以站的格子（切比雪夫距离 ≤1，且不占该武器格）。"""
        return tuple(p for p in site.neighbours() if p != site)


def _opposite(side: str) -> str:
    return {"-x": "+x", "+x": "-x", "-y": "+y", "+y": "-y"}[side]


#: 每条边的**外法线**（指向盒子外）
NORMALS: dict[str, tuple[int, int]] = {
    "-x": (-1, 0),
    "+x": (1, 0),
    "-y": (0, -1),
    "+y": (0, 1),
}


def _rank_outward(
    cells: tuple[Pos, ...], station: Pos, toward: str | None = None
) -> tuple[Pos, ...]:
    """确定性排序（避免每次运行的顺序抖动）。

    `toward` 给定时，沿该边的外法线排序 → **最靠"正面"的排最前**；
    否则退回按 (x, y) 字典序。
    """
    if not cells:
        return ()
    if toward is None:
        return tuple(sorted(cells, key=lambda p: (p.x, p.y)))
    nx, ny = NORMALS[toward]
    # 外法线方向的投影越大 = 越靠外侧 = 越靠"正面"
    return tuple(sorted(cells, key=lambda p: (-(p.x * nx + p.y * ny), p.x, p.y)))


class OppositeSide(ValueError):
    pass


def station_footprint(pos: Pos) -> tuple[Pos, ...]:
    """基地 `pos`（左上角）占据的 2×2 格。

    单独抽出来是因为**判定"某格能不能站"时必须把基地的 4 格整体排除**：
    只排除 `station.pos` 一格会让角色一头撞进基地的另外 3 格，
    表现为"每回合都在撞墙但指令合法"（不记异常，所以极难发现）。
    ✅ 与 demo 的 `station_footprint` 在样例 (10,24) 上逐格比对一致。
    """
    return tuple(
        Pos(pos.x + dx, pos.y + dy) for dx in (0, 1) for dy in (-1, 0)
    )


def box_from_station(
    station: Pos, map_w: int, map_h: int, our_type: str, front: str | None = None
) -> Box:
    """由己方 station 的 pos 推导整个盒子。

    station.pos 是**左上角**（✅实测），因此：
        xm, xM = bx, bx+1
        ym, yM = by-1, by
        武器环 4×4 = x∈[xm-1, xM+1] × y∈[ym-1, yM+1]
        围墙环 6×6 = x∈[xm-2, xM+2] × y∈[ym-2, yM+2]

    `front` 的优先级：显式传入（观测值）> `FORCED_FRONT_SIDE` > 先验推断。
    """
    bx, by = station.x, station.y
    xm, xM = bx, bx + 1
    ym, yM = by - 1, by

    footprint = station_footprint(station)

    inner = {
        Pos(x, y)
        for x in range(xm - 1, xM + 2)
        for y in range(ym - 1, yM + 2)
    }
    weapon_ring = tuple(sorted(inner - set(footprint), key=lambda p: (p.x, p.y)))

    outer = {
        Pos(x, y)
        for x in range(xm - 2, xM + 3)
        for y in range(ym - 2, yM + 3)
    }
    wall_ring = tuple(sorted(outer - inner, key=lambda p: (p.x, p.y)))

    sides = _split_sides(wall_ring, xm, xM, ym, yM)

    front = front or FORCED_FRONT_SIDE.get(our_type) or _infer_front(map_w, map_h, bx, by)
    back = _opposite(front)
    door = _pick_door(sides[back])

    return Box(
        station=station,
        footprint=footprint,
        weapon_ring=weapon_ring,
        wall_ring=wall_ring,
        sides=sides,
        front=front,
        door=door,
    )


def _split_sides(
    wall_ring: tuple[Pos, ...], xm: int, xM: int, ym: int, yM: int
) -> dict[str, tuple[Pos, ...]]:
    """把 20 格围墙环切成四条边。

    每条边取**含两端拐角**的完整 6 格，因此四个角同时属于相邻两条边
    （4 条边合计 24 = 20 + 4 个重复的角）。调用方需自行去重——
    `wall_order()` 就是这么做的。这样每条边的格子数恒定，门的位置才好算。
    """
    x_lo, x_hi = xm - 2, xM + 2
    y_lo, y_hi = ym - 2, yM + 2
    sides: dict[str, list[Pos]] = {"-x": [], "+x": [], "-y": [], "+y": []}

    for pos in wall_ring:
        if pos.x == x_hi:
            sides["+x"].append(pos)
        if pos.x == x_lo:
            sides["-x"].append(pos)
        if pos.y == y_hi:
            sides["+y"].append(pos)
        if pos.y == y_lo:
            sides["-y"].append(pos)

    covered = {p for cells in sides.values() for p in cells}
    if covered != set(wall_ring):
        missing = set(wall_ring) - covered
        raise AssertionError(f"side split lost cells: {sorted((p.x, p.y) for p in missing)}")
    for name, cells in sides.items():
        if len(cells) != 6:
            raise AssertionError(f"side {name} has {len(cells)} cells, want 6")

    return {k: tuple(sorted(v, key=lambda p: (p.x, p.y))) for k, v in sides.items()}


def _pick_door(back_cells: tuple[Pos, ...]) -> Pos:
    """门开在背面边的**正中**（离两侧拐角最远，绕行代价最大）。"""
    if not back_cells:
        raise ValueError("back side has no cells")
    ordered = sorted(back_cells, key=lambda p: (p.x, p.y))
    return ordered[len(ordered) // 2]


def _infer_front(map_w: int, map_h: int, bx: int, by: int) -> str:
    """❓出生区未确认时的先验：正面 = **背向地图中心**（即朝最近的那条地图边缘）。

    ⚠️ 这与"直觉"（朝向地图中心）**相反**，是刻意的，有三条独立证据：

      1. demo 的 `_tower_sites` 按 `(x, y)` 升序取环上 3 格 → **永远落在 -x 那一列**；
      2. `docs/request.txt` 里 3 座武器全在 -x/-y 角：(9,24) (10,25) (9,25)，
         而基地在 (10,24)；
      3. 同一份样例里机器人出现在 (4,4)，相对基地 (10,24) 在左上方。

    ⚠️ 任务书 L350「机器人会在**夜晚第一个回合**统一出现」意味着白天看不到任何机器人，
    所以第 1 天的武器与墙只能靠这条先验定方位。入夜后 `front_from_positions`
    会用观测到的机器人来向覆盖它；确认后可用 `FORCED_FRONT_SIDE` 写死。
    """
    dx, dy = bx - map_w / 2.0, by - map_h / 2.0
    if abs(dx) >= abs(dy):
        return "-x" if dx <= 0 else "+x"
    return "-y" if dy <= 0 else "+y"


def front_from_positions(station: Pos, threat: Iterable[Pos]) -> str | None:
    """按**观测到的威胁位置**定正面。没有可用观测时返回 None（调用方退回先验）。

    取"基地 → 各威胁"的平均方向，再吸附到主轴。机器人是朝基地走的，
    所以它们的分布本身就指向正面；一旦观测到，这个判据比任何先验都可信。
    """
    pts = [p for p in threat]
    if not pts:
        return None
    sx = sum(p.x - station.x for p in pts)
    sy = sum(p.y - station.y for p in pts)
    if sx == 0 and sy == 0:
        return None  # 环绕基地：没有"主攻方向"可言
    if abs(sx) >= abs(sy):
        return "+x" if sx > 0 else "-x"
    return "+y" if sy > 0 else "-y"
