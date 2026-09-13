"""回合视图 `Turn` —— 一个回合的完整只读快照，以及由它派生的世界状态。

设计见 docs/design/code-design.md §3.2 第 2 条。

**为什么 `Turn` 在 domain**：同 `domain/entities.py` 的模块注释——它是策略层
唯一的输入对象，必须住在被依赖的那一侧。`protocol/model.py` 负责把它造出来。

这一层只放**纯函数**派生量（本回合的世界状态）。
需要**跨回合累积**的东西（波次曲线、对手 DPS、通过率）不放这里，
它们在 `domain/history.py`，由 `app.py` 每回合无条件喂一条记录。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .entities import (
    BUILDING_KINDS,
    MINE_KINDS,
    PIONEER,
    STATION,
    WALL,
    WEAPON_KINDS,
    WORKER,
    NativeError,
    PlayerTask,
    Robot,
    Role,
    ShopItem,
)
from .geometry import Box, box_from_station, front_from_positions, station_footprint
from .grid import Pos
from .path import Board

__all__ = [
    "Turn",
    "footprint_of",
    "blocked_cells",
    "board_for",
    "box_of",
    "wall_gaps",
    "wall_integrity",
    "carried",
    "in_range",
]

@dataclass(frozen=True, slots=True)
class Turn:
    """一个回合的完整只读视图。所有派生量都是纯函数。"""

    round_no: int
    width: int
    height: int
    # zones 必须是"一格 → 多格"：任务点 2 占两格（✅实测）
    zones: Mapping[str, tuple[Pos, ...]]
    zone_at: Mapping[Pos, str]
    our_type: str
    team_id: str
    team_name: str
    gold: int
    total_score: int
    player_tasks: tuple[PlayerTask, ...]
    our_roles: tuple[Role, ...]
    enemy_roles: tuple[Role, ...]
    robots: tuple[Robot, ...]
    phase_task: str
    action_results: Mapping[str, bool]
    treasure_result: int
    llm_resp: str
    cmd_result: str
    official_news: str
    folk_legends: str
    vendor_shop: tuple[ShopItem, ...]
    weapon_shop: tuple[ShopItem, ...]
    errors: tuple[NativeError, ...]
    anomalies: tuple[str, ...] = ()

    # ── 派生查询 ────────────────────────────────────────────────────
    @property
    def our_id(self) -> dict[int, Role]:
        return {r.id: r for r in self.our_roles}

    @property
    def weapons(self) -> tuple[Role, ...]:
        return tuple(r for r in self.our_roles if r.is_weapon)

    @property
    def controllables(self) -> tuple[Role, ...]:
        return tuple(r for r in self.our_roles if r.is_controllable)

    @property
    def workers(self) -> tuple[Role, ...]:
        return tuple(r for r in self.our_roles if r.role_type == WORKER)

    @property
    def pioneer(self) -> Role | None:
        for r in self.our_roles:
            if r.role_type == PIONEER:
                return r
        return None

    @property
    def station(self) -> Role | None:
        for r in self.our_roles:
            if r.role_type == STATION:
                return r
        return None

    @property
    def walls(self) -> tuple[Role, ...]:
        return tuple(r for r in self.our_roles if r.role_type == WALL)

    @property
    def our_wall_positions(self) -> frozenset[Pos]:
        """**只算活着的墙** —— 残骸不算已建成，否则破口会被当成完工（见 `our_wall_alive`）。"""
        return frozenset(r.pos for r in self.our_wall_alive)

    @property
    def enemy_walls(self) -> tuple[Role, ...]:
        return tuple(r for r in self.enemy_roles if r.role_type == WALL)

    def role(self, role_id: int) -> Role | None:
        return self.our_id.get(role_id)

    def action_ok(self, role_id: int) -> bool | None:
        """⚠️ `lastRoundRoleActionResults` 的 key 是**字符串**。

        用 int 查会全 miss，进而把"所有动作都失败"当成事实——这是竞态 #5。
        """
        v = self.action_results.get(str(role_id))
        return v if isinstance(v, bool) else None

    def task_points(self, kind_prefix: str) -> tuple[Pos, ...]:
        """按 `challenger` / `defender` 取两个任务点的**所有格子**。"""
        out: list[Pos] = []
        for name, cells in self.zones.items():
            if name.startswith(kind_prefix) and "TaskPoint" in name:
                out.extend(cells)
        return tuple(out)

    def mines(self, kinds: Iterable[str] = MINE_KINDS) -> tuple[Pos, ...]:
        want = set(kinds)
        out: list[Pos] = []
        for name, cells in self.zones.items():
            if name in want:
                out.extend(cells)
        return tuple(out)

    def mine_kind(self, pos: Pos) -> str | None:
        kind = self.zone_at.get(pos)
        return kind if kind in MINE_KINDS else None

    def cells_of(self, kind: str) -> tuple[Pos, ...]:
        return self.zones.get(kind, ())

    def in_bounds(self, pos: Pos) -> bool:
        return 0 <= pos.x < self.width and 0 <= pos.y < self.height

    @property
    def our_wall_alive(self) -> tuple[Role, ...]:
        """⚠️ 与 `walls` 的区别：**滤掉血量归零的残骸**。

        判题器是否会把已被摧毁的围墙从列表里摘掉**没有明文保证**。若不滤，
        我们会把一格空地当成已完工的墙 → 永远不去补 → 整晚开着破口。
        """
        return tuple(r for r in self.our_roles if r.role_type == WALL and r.alive)

    def neutral(self, kind: str) -> Pos | None:
        """取单格中立单位（小贩 / 武器商店）。占两格的一律不适用。"""
        cells = self.zones.get(kind, ())
        return cells[0] if cells else None

    @property
    def vendor_pos(self) -> Pos | None:
        """小贩的位置。`sell` 必须站在它周围一格内。"""
        return self.neutral("vendor")

    @property
    def shop_pos(self) -> Pos | None:
        """武器商店的位置。`buy` 必须站在它周围一格内。

        ⚠️ 名字**不能**叫 `weapon_shop` —— 那会遮蔽同名的 dataclass 字段
        （`weapon_shop: tuple[ShopItem, ...]`），property 会被 dataclass
        当成"带默认值的字段"，直接 `TypeError: non-default argument follows default`。
        """
        return self.neutral("weaponShop")

    def robot_threat(self, near: Pos, radius: int) -> tuple[Robot, ...]:
        """距离 `near` 不超过 `radius` 的存活机器人，按距离升序。

        ❓`targetTeam` 样例缺失（接口文档有、payload 没有），所以这里只做
        **几何**判定，不猜目标归属——见 code-design.md §2.2 情报通道降级。
        """
        out = [r for r in self.robots if r.alive and r.pos.distance(near) <= radius]
        return tuple(sorted(out, key=lambda r: (r.pos.distance(near), r.id)))


# ── 派生世界状态（纯函数；不跨回合，因此不进 history） ────────────────
def footprint_of(role: Role) -> tuple[Pos, ...]:
    """该角色占据的格子。基地是唯一的 2×2 —— 只排除它的 `pos` 会让角色撞进另外 3 格。"""
    if role.role_type == STATION:
        return station_footprint(role.pos)
    return (role.pos,)


def blocked_cells(turn: Turn, mover: Role | None = None) -> frozenset[Pos]:
    """本回合**不可通行**的格子全集（任务书 L85 的阻挡清单）。

    含：中立单位（矿区/小贩/武器商店/任务点）+ 双方建筑与角色 + 机器人。
    `mover` 自身占的格子被剔除——否则单位会被自己"堵住"，
    BFS 起点虽然仍能出发（`distance_field` 显式保证），但落脚点判定会全错。
    """
    cells: set[Pos] = set(turn.zone_at)
    for r in turn.our_roles:
        if r.alive:
            cells.update(footprint_of(r))
    for r in turn.enemy_roles:
        if r.alive:
            cells.update(footprint_of(r))
    for r in turn.robots:
        if r.alive:
            cells.add(r.pos)
    if mover is not None:
        cells.difference_update(footprint_of(mover))
    return frozenset(cells)


def board_for(turn: Turn, mover: Role | None = None) -> Board:
    return Board(turn.width, turn.height, blocked_cells(turn, mover))


def box_of(turn: Turn) -> Box | None:
    """己方防御盒子。基地还没出现在 payload 里时返回 None（此时无从推导）。

    正面优先用**观测到的机器人来向**（机器人是朝基地走的，分布本身即指向正面）。
    白天机器人不存在（任务书 L350：夜里第一个回合才统一出现），
    此时回落到 `geometry._infer_front` 的先验 —— 所以第 1 天武器与墙的方位
    完全由那条先验决定，这也正是它必须写对的原因。
    """
    station = turn.station
    if station is None:
        return None
    threat = tuple(r.pos for r in turn.robots if r.alive)
    return box_from_station(
        station.pos,
        turn.width,
        turn.height,
        turn.our_type,
        front=front_from_positions(station.pos, threat),
    )


def wall_gaps(turn: Turn, box: Box) -> tuple[Pos, ...]:
    """盒子上**还没建**的围墙格，按 `wall_order()`（正面 → 两侧 → 背面，门除外）。

    这是建墙任务的唯一输入：顺序完全由几何决定，与"谁去建"无关。
    """
    have = frozenset(r.pos for r in turn.our_wall_alive)
    return tuple(p for p in box.wall_order() if p not in have)


def wall_integrity(turn: Turn, box: Box, hp_fn=None) -> float:
    """墙体完好度 ∈ [0,1]：**按血量加权**，而不是数格子。

    只数格子会把"19 面墙全在、但正面 4 面只剩 100 血"算成 1.0，
    从而在最危险的时刻压制掉修复优先级。
    """
    total = len(box.wall_ring) - 1  # 门不建
    if total <= 0:
        return 1.0
    lvl_hp = {1: 1000, 2: 1500, 3: 2000}
    score = 0.0
    for r in turn.our_wall_alive:
        cap = lvl_hp.get(min(max(r.level, 1), 3), 1000)
        score += min(1.0, r.health / cap) if cap else 0.0
    return min(1.0, score / total)


def carried(turn: Turn) -> dict[str, int]:
    """我方**所有角色背包**中的物品总账。

    采购前必须查它：升级券不叠放，买了第二张而第一张还在背包里就是纯浪费金币
    （券不会消失，只是白花 100 金）。
    """
    tally: dict[str, int] = {}
    for r in turn.our_roles:
        for item in r.backpack:
            tally[item] = tally.get(item, 0) + 1
    return tally


def in_reach(a: Pos, b: Pos) -> bool:
    """`collect`/`build`/`sell`/`buy`/`use` 的"周围一格内"（切比雪夫 ≤ 1）。"""
    return a.distance(b) <= 1
