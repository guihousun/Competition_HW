from dataclasses import dataclass, field
from typing import Any, Sequence

DAY_ROUNDS = 70
NIGHT_ROUNDS = 60
ROUNDS_PER_DAY = DAY_ROUNDS + NIGHT_ROUNDS

WEAPON_BUILD_COST = 25
WALL_MATERIAL = "stone"
LAND = "land"
STATION = "station"
WALL = "wall"
WORKER = "worker"
PIONEER = "pioneer"
TOWER_TYPES = ("gatling", "railgun", "rocket")
MATERIALS = ("stone", "iron", "copper")
CONTROLLABLE_TYPES = (WORKER, PIONEER)
TWO_CELL_TASK_TYPES = ('challengerTaskPoint2', 'defenderTaskPoint2')
MAX_BUILDING_LEVEL = 3
# Weapon cooldown after firing, per type (任务书 §4.5.1: only the rocket has one).
WEAPON_COOLDOWN = {"gatling": 0, "railgun": 0, "rocket": 3}
DIZZY_ROUNDS = 5
BOMB_DAMAGE = 100
BOMB_RADIUS = 1
SUMMON_LIMIT_PER_DAY = 10
TOWER_RANGE_BY_LEVEL = {
    "gatling": (3, 5, 7),
    "railgun": (6, 8, 10),
    "rocket": (10, 15, 10**9),
}


@dataclass(frozen=True, slots=True)
class Pos:
    x: int
    y: int

    @classmethod
    def load(cls, raw: Any) -> "Pos":
        return cls(int(raw["x"]), int(raw["y"]))

    def dump(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y}


def distance(first: Pos, second: Pos) -> int:
    return max(abs(first.x - second.x), abs(first.y - second.y))


def station_footprint(pos: Pos) -> tuple[Pos, ...]:
    return (
        pos,
        Pos(pos.x + 1, pos.y),
        Pos(pos.x, pos.y - 1),
        Pos(pos.x + 1, pos.y - 1),
    )


@dataclass(frozen=True, slots=True)
class Unit:
    unit_id: int
    pos: Pos
    kind: str
    health: int
    level: int
    cooldown: int
    attack_range: int
    capacity: int | None
    backpack: tuple[str, ...]

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "Unit":
        raw_capacity = raw.get("backPackCapability")
        return cls(
            int(raw.get("id") or 0),
            Pos.load(raw["pos"]),
            str(raw["roleType"]),
            int(raw["health"]),
            int(raw.get("level") or 0),
            int(raw.get("cooldown") or 0),
            int(raw.get("attackRange") or 0),
            int(raw_capacity) if raw_capacity is not None else None,
            tuple(str(item) for item in raw.get("backpack") or ()),
        )

    @property
    def backpack_full(self) -> bool:
        if self.capacity is None:
            return False
        return len(self.backpack) >= self.capacity

    def range_of_attack(self) -> int:
        if self.attack_range > 0:
            return self.attack_range
        table = TOWER_RANGE_BY_LEVEL.get(self.kind)
        if table is None:
            return 0
        level = min(max(self.level, 1), len(table))
        return table[level - 1]


@dataclass(frozen=True, slots=True)
class Robot:
    robot_id: int
    pos: Pos
    health: int

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "Robot":
        return cls(int(raw["id"]), Pos.load(raw["pos"]), int(raw["health"]))


@dataclass(frozen=True, slots=True)
class Turn:
    round_no: int
    is_day: bool
    gold: int
    width: int
    height: int
    zones: dict[Pos, str]
    ours: tuple[Unit, ...]
    robots: tuple[Robot, ...]
    enemies: tuple[Unit, ...] = ()
    _neutral_cells: frozenset[Pos] = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        # R02/R07: a point-2 anchor occupies two horizontal cells. Some public
        # snapshots explicitly list both; those cells are already the footprint,
        # so expanding each entry would invent a third blocked cell.
        cells = {pos for pos, kind in self.zones.items() if kind != LAND}
        for kind in TWO_CELL_TASK_TYPES:
            explicit = [pos for pos, value in self.zones.items() if value == kind]
            explicit_pair = (len(explicit) == 2 and explicit[0].y == explicit[1].y
                             and abs(explicit[0].x - explicit[1].x) == 1)
            if not explicit_pair:
                # An irregular/ambiguous group is not evidence that each anchor
                # lost its second cell. Keep conservative occupancy for every
                # reported anchor; do not silently turn possible obstacles into land.
                cells.update(Pos(pos.x + 1, pos.y) for pos in explicit)
        object.__setattr__(self, '_neutral_cells', frozenset(
            pos for pos in cells if 0 <= pos.x < self.width and 0 <= pos.y < self.height))

    def neutral_cells(self) -> frozenset[Pos]:
        """Occupied neutral footprint; the original zone map stays unmodified."""
        return self._neutral_cells

    @classmethod
    def load(cls, payload: dict[str, Any]) -> "Turn":
        round_no = int(payload["roundNo"])
        info = payload["mapInfo"]
        team = payload["teamOur"]
        return cls(
            round_no,
            (round_no - 1) % ROUNDS_PER_DAY < DAY_ROUNDS,
            int(team.get("goldNum") or 0),
            int(info["width"]),
            int(info["height"]),
            {
                Pos.load(zone["pos"]): str(zone["neutralType"])
                for zone in info.get("zones") or ()
            },
            tuple(Unit.load(role) for role in team.get("roles") or ()),
            tuple(
                Robot.load(robot)
                for robot in (payload.get("robot") or {}).get("roles") or ()
            ),
            tuple(Unit.load(role) for role in
                  (payload.get("teamEnemy") or {}).get("roles") or ()),
        )

    def station(self) -> Unit | None:
        for unit in self.ours:
            if unit.kind == STATION:
                return unit
        return None

    def alive(self, kinds: tuple[str, ...]) -> tuple[Unit, ...]:
        return tuple(
            unit for unit in self.ours
            if unit.kind in kinds and unit.health > 0
        )

    def controllable(self) -> tuple[Unit, ...]:
        return tuple(sorted(
            self.alive(CONTROLLABLE_TYPES), key=lambda unit: unit.unit_id,
        ))

    def workers(self) -> tuple[Unit, ...]:
        return tuple(sorted(
            self.alive((WORKER,)), key=lambda unit: unit.unit_id,
        ))

    def pioneer(self) -> Unit | None:
        """The living pioneer, if any: the only role that may touch tasks."""
        for unit in sorted(self.alive((PIONEER,)), key=lambda unit: unit.unit_id):
            return unit
        return None

    def weapons(self) -> tuple[Unit, ...]:
        return tuple(sorted(
            self.alive(TOWER_TYPES),
            key=lambda unit: (unit.pos.x, unit.pos.y),
        ))

    def walls(self) -> tuple[Unit, ...]:
        return self.alive((WALL,))

    def stone_mines(self) -> tuple[Pos, ...]:
        return tuple(
            pos for pos, kind in self.zones.items() if kind == WALL_MATERIAL
        )

    def footprint(self, unit: Unit) -> tuple[Pos, ...]:
        if unit.kind == STATION:
            return station_footprint(unit.pos)
        return (unit.pos,)

    def land(self, pos: Pos) -> bool:
        if not 0 <= pos.x < self.width or not 0 <= pos.y < self.height:
            return False
        return pos not in self._neutral_cells

    def occupied_cells(self) -> frozenset[Pos]:
        cells: set[Pos] = set()
        for unit in self.ours + self.enemies:
            if unit.health > 0:
                cells.update(self.footprint(unit))
        cells.update(robot.pos for robot in self.robots if robot.health > 0)
        return frozenset(cells)

    def blocked(self, moving: Unit) -> frozenset[Pos]:
        cells = set(self._neutral_cells)
        cells.update(self.occupied_cells())
        cells.discard(moving.pos)
        for robot in self.robots:
            if robot.health > 0:
                cells.add(robot.pos)
        return frozenset(cells)


def move_command(pos: Pos) -> dict[str, Any]:
    return {"action": "move", "targetPos": [pos.dump()]}


def collect_command(pos: Pos) -> dict[str, Any]:
    return {"action": "collect", "targetPos": [pos.dump()]}


def build_command(pos: Pos, name: str) -> dict[str, Any]:
    return {"action": "build", "targetPos": [pos.dump()], "name": name}


def attack_command(controller_id: int, pos: Pos) -> dict[str, Any]:
    return {
        "action": "attack",
        "targetPos": [pos.dump()],
        "controllerId": str(controller_id),
    }


# --------------------------------------------------------------------------
# Remaining official commands (接口文档 §2.2/§2.3). One builder per action so a
# strategy can never hand-assemble a malformed instruction, and every builder
# returns a fresh structure that is safe to mutate afterwards.
# --------------------------------------------------------------------------

def attack_command_multi(controller_id: int | str, positions: Sequence[Pos]) -> dict[str, Any]:
    """Gatling/rocket take one target per level; railgun always exactly one."""
    return {
        "action": "attack",
        "targetPos": [pos.dump() for pos in positions],
        "controllerId": str(controller_id),
    }


def sell_command(material: str, amount: int = 1, pos: Pos | None = None) -> dict[str, Any]:
    command: dict[str, Any] = {"action": "sell", "name": str(material),
                               "num": int(amount)}
    if pos is not None:
        command["targetPos"] = [pos.dump()]
    return command


def buy_command(item: str, amount: int = 1, pos: Pos | None = None) -> dict[str, Any]:
    command: dict[str, Any] = {"action": "buy", "name": str(item),
                               "num": int(amount)}
    if pos is not None:
        command["targetPos"] = [pos.dump()]
    return command


def remove_command(pos: Pos) -> dict[str, Any]:
    return {"action": "remove", "targetPos": [pos.dump()]}


def accept_task_command() -> dict[str, Any]:
    return {"action": "acceptTask"}


def submit_answer_command(answer: str) -> dict[str, Any]:
    return {"action": "submitAnswer", "taskAnswer": str(answer)}


def summon_treasure_command(pos: Pos, items: Sequence[str]) -> dict[str, Any]:
    return {"action": "summonTreasure", "targetPos": [pos.dump()],
            "item": [str(item) for item in items]}


def use_command(item: str, pos: Pos | None = None) -> dict[str, Any]:
    command: dict[str, Any] = {"action": "use", "name": str(item)}
    if pos is not None:
        command["targetPos"] = [pos.dump()]
    return command


def drop_command(item: str) -> dict[str, Any]:
    return {"action": "drop", "name": str(item)}


# --------------------------------------------------------------------------
# Official vocabularies that must not be "corrected" locally (R01/R06).
# --------------------------------------------------------------------------

OFFICIAL_ACTIONS: tuple[str, ...] = (
    "move", "attack", "sell", "buy", "build", "remove",
    "acceptTask", "submitAnswer", "summonTreasure", "use", "drop", "collect",
)

# Official item names exactly as published, including `AcientTablet` (R06).
WEAPON_UPGRADE_VOUCHER_1 = "WeaponUpgradeVoucher1"
WEAPON_UPGRADE_VOUCHER_2 = "WeaponUpgradeVoucher2"
WALL_UPGRADE_VOUCHER_1 = "WallUpgradeVoucher1"
WALL_UPGRADE_VOUCHER_2 = "WallUpgradeVoucher2"
STATION_UPGRADE_VOUCHER_1 = "StationUpgradeVoucher1"
STATION_UPGRADE_VOUCHER_2 = "StationUpgradeVoucher2"
WALL_FIXER = "WallFixer"
MEDICINE = "Medicine"
DIZZY_WEAPON = "DizzyWeapon"
BOMB = "Bomb"
SMALL_ROBOT_SUMMON_ORDER = "SmallRobotSummonOrder"
MIDDLE_ROBOT_SUMMON_ORDER = "MiddleRobotSummonOrder"
LARGE_ROBOT_SUMMON_ORDER = "LargeRobotSummonOrder"
BOSS_ROBOT_SUMMON_ORDER = "BossRobotSummonOrder"
TASK_ITEMS: tuple[str, ...] = (
    "AcientTablet", "StarSand", "FlameBreath", "FrostPotion", "ThornAmulet",
    "IronWhistle",
)
# Every 任务用品 costs the same (任务书 §4.6.3 末表: 金额 15).
TASK_ITEM_PRICE = 15

# Published shop prices (任务书 §4.6.3). Used only to sanity-check the live
# `weaponShopList`; the current price always comes from the observation.
SHOP_PRICES: dict[str, int] = {
    WEAPON_UPGRADE_VOUCHER_1: 100, WALL_UPGRADE_VOUCHER_1: 20,
    STATION_UPGRADE_VOUCHER_1: 100, WEAPON_UPGRADE_VOUCHER_2: 150,
    WALL_UPGRADE_VOUCHER_2: 30, STATION_UPGRADE_VOUCHER_2: 150,
    WALL_FIXER: 10, MEDICINE: 10, DIZZY_WEAPON: 100, BOMB: 100,
    SMALL_ROBOT_SUMMON_ORDER: 20, MIDDLE_ROBOT_SUMMON_ORDER: 30,
    LARGE_ROBOT_SUMMON_ORDER: 100, BOSS_ROBOT_SUMMON_ORDER: 200,
    # 任务用品 (任务书 §4.6.3 末表): every one costs 15 and is sold at the weapon
    # shop. They are only usable for a treasure summon — never "used" — and they
    # disappear when the summon happens (任务书 §5.2).
    **{item: TASK_ITEM_PRICE for item in TASK_ITEMS},
}

# Official error codes (接口文档 §1.7) for the developer panel.
ERROR_CODES: dict[int, str] = {
    0: "未知错误", 1: "任务超时", 2: "答案错误", 3: "网络错误",
    4: "指令错误", 5: "LLM 额度超限",
}

# Official vision distance shared by every friendly unit (任务书 §4.3).
VISION_DISTANCE = 4
# Robots, enemy bases and enemy walls are visible regardless of vision (R02).
GLOBAL_VISIBLE_TYPES: tuple[str, ...] = ("station", WALL)
