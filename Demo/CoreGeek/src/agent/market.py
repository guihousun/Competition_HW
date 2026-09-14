"""Official economy: prices, inventory, upgrade chains, summons.

Everything here is derived from observation fields, never from local constants
the judge might change:

  * selling prices come from ``vendorShopList`` (R06: the news moves them),
  * shop availability and prices come from ``weaponShopList``,
  * upgrade chains and item names follow 任务书 §4.6.3 exactly, including the
    published spelling ``AcientTablet``.

The module only *computes*; it never mutates game state. Mutation lives in the
local simulator, so the strategy and the local rules stay separable, and the
viewer can show the same numbers the strategy reasons about.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

from .protocol import (
    BOMB,
    BOMB_DAMAGE,
    BOMB_RADIUS,
    BOSS_ROBOT_SUMMON_ORDER,
    DIZZY_WEAPON,
    LARGE_ROBOT_SUMMON_ORDER,
    MATERIALS,
    MAX_BUILDING_LEVEL,
    MEDICINE,
    MIDDLE_ROBOT_SUMMON_ORDER,
    SHOP_PRICES,
    SMALL_ROBOT_SUMMON_ORDER,
    STATION,
    STATION_UPGRADE_VOUCHER_1,
    STATION_UPGRADE_VOUCHER_2,
    SUMMON_LIMIT_PER_DAY,
    TASK_ITEMS,
    TOWER_TYPES,
    WALL,
    WALL_FIXER,
    WALL_UPGRADE_VOUCHER_1,
    WALL_UPGRADE_VOUCHER_2,
    WEAPON_UPGRADE_VOUCHER_1,
    WEAPON_UPGRADE_VOUCHER_2,
)

# Which building kinds a voucher may legally target.
VOUCHER_TARGETS: dict[str, tuple[str, ...]] = {
    WEAPON_UPGRADE_VOUCHER_1: TOWER_TYPES,
    WEAPON_UPGRADE_VOUCHER_2: TOWER_TYPES,
    WALL_UPGRADE_VOUCHER_1: (WALL,),
    WALL_UPGRADE_VOUCHER_2: (WALL,),
    STATION_UPGRADE_VOUCHER_1: (STATION,),
    STATION_UPGRADE_VOUCHER_2: (STATION,),
}

# Level transition each voucher performs, so an illegal downgrade is refused.
VOUCHER_FROM_LEVEL: dict[str, int] = {
    WEAPON_UPGRADE_VOUCHER_1: 1, WEAPON_UPGRADE_VOUCHER_2: 2,
    WALL_UPGRADE_VOUCHER_1: 1, WALL_UPGRADE_VOUCHER_2: 2,
    STATION_UPGRADE_VOUCHER_1: 1, STATION_UPGRADE_VOUCHER_2: 2,
}

# Items that need a target coordinate when used (任务书 §4.6.3 notes).
TARGETED_ITEMS: frozenset[str] = frozenset({
    WEAPON_UPGRADE_VOUCHER_1, WEAPON_UPGRADE_VOUCHER_2,
    WALL_UPGRADE_VOUCHER_1, WALL_UPGRADE_VOUCHER_2,
    STATION_UPGRADE_VOUCHER_1, STATION_UPGRADE_VOUCHER_2,
    WALL_FIXER, DIZZY_WEAPON, BOMB,
})

# Only these items may be used while a robot is on the board meaningfully; the
# rest are economy/defence items.
BATTLE_ITEMS: frozenset[str] = frozenset({DIZZY_WEAPON, BOMB})

SUMMON_ORDERS: dict[str, str] = {
    SMALL_ROBOT_SUMMON_ORDER: "smallRobot",
    MIDDLE_ROBOT_SUMMON_ORDER: "middleRobot",
    LARGE_ROBOT_SUMMON_ORDER: "largeRobot",
    BOSS_ROBOT_SUMMON_ORDER: "bossRobot",
}

# Task items may only be spent on a treasure summon, never "used".
TASK_ITEM_SET = frozenset(TASK_ITEMS)


def backpack_of(role: dict[str, Any]) -> list[str]:
    return [str(item) for item in (role.get("backpack") or ())]


def count_item(role: dict[str, Any], name: str) -> int:
    return sum(1 for item in backpack_of(role) if item == name)


def capacity_left(role: dict[str, Any]) -> int:
    capacity = int(role.get("backPackCapability") or 0)
    return max(0, capacity - len(backpack_of(role)))


def can_carry(role: dict[str, Any], amount: int) -> bool:
    return capacity_left(role) >= int(amount)


def vendor_prices(state: dict[str, Any]) -> dict[str, int]:
    """Material -> current purchase price, straight from the observation."""
    prices: dict[str, int] = {}
    for entry in state.get("vendorShopList") or ():
        try:
            prices[str(entry["name"])] = int(entry["price"])
        except (KeyError, TypeError, ValueError):
            continue
    return prices


def shop_prices(state: dict[str, Any]) -> dict[str, int]:
    """Item -> current shop price, straight from the observation."""
    prices: dict[str, int] = {}
    for entry in state.get("weaponShopList") or ():
        try:
            prices[str(entry["name"])] = int(entry["price"])
        except (KeyError, TypeError, ValueError):
            continue
    return prices


def sellable_inventory(role: dict[str, Any], state: dict[str, Any]) -> dict[str, int]:
    """Materials in the backpack the vendor currently buys, with amounts."""
    prices = vendor_prices(state)
    counts: dict[str, int] = {}
    for item in backpack_of(role):
        if item in prices:
            counts[item] = counts.get(item, 0) + 1
    return counts


def sell_value(role: dict[str, Any], state: dict[str, Any]) -> int:
    prices = vendor_prices(state)
    return sum(prices.get(item, 0) for item in backpack_of(role))


def shop_catalog(state: dict[str, Any], gold: int) -> dict[str, dict[str, int | bool]]:
    """Everything the shop offers, marked with what the given gold can buy.

    Task items are included because the shop really sells them; whether buying
    one is useful is a strategy decision, not a rules decision.
    """
    catalog: dict[str, dict[str, int | bool]] = {}
    for name, price in shop_prices(state).items():
        catalog[name] = {
            "price": price,
            "affordable": gold >= price,
            "publishedPrice": SHOP_PRICES.get(name, price),
            "known": name in SHOP_PRICES or name in TASK_ITEM_SET,
        }
    return catalog


def buy_plan(role: dict[str, Any], state: dict[str, Any], name: str,
             gold: int) -> tuple[int, int] | None:
    """How many units of `name` this role can buy right now.

    Returns ``(amount, total_cost)`` or ``None`` when nothing can be bought.
    Both the shop price and the backpack space must allow it (R06).
    """
    price = shop_prices(state).get(name)
    if price is None or price <= 0:
        return None
    room = capacity_left(role)
    affordable = gold // price
    amount = int(min(room, affordable))
    if amount <= 0:
        return None
    return amount, amount * price


def upgrade_target_level(item: str) -> int:
    """The level a voucher produces, or 0 when the item is not a voucher."""
    return VOUCHER_FROM_LEVEL.get(item, 0) + 1 if item in VOUCHER_FROM_LEVEL else 0


def can_upgrade(item: str, building_kind: str, building_level: int) -> bool:
    """Whether `item` legally upgrades that building (任务书 §4.6.3)."""
    targets = VOUCHER_TARGETS.get(item)
    if not targets or building_kind not in targets:
        return False
    if int(building_level) >= MAX_BUILDING_LEVEL:
        return False
    return int(building_level) == VOUCHER_FROM_LEVEL[item]


def summon_load(orders: Iterable[str]) -> dict[str, int]:
    """Summon orders -> robots added to the opponent's next night."""
    load: dict[str, int] = {}
    for item in orders:
        kind = SUMMON_ORDERS.get(item)
        if kind:
            load[kind] = load.get(kind, 0) + 1
    return load


def summon_amount_allowed(orders_used_today: int, requested: int) -> int:
    """At most 10 summon orders per day (任务书 §4.6.3)."""
    remaining = max(0, SUMMON_LIMIT_PER_DAY - int(orders_used_today))
    return max(0, min(int(requested), remaining))


def bomb_targets(state: dict[str, Any], center: Any, *, distance) -> list[dict[str, Any]]:
    """Robots inside the 3x3 blast, both teams included (R06)."""
    hits = []
    for robot in (state.get("robot") or {}).get("roles") or ():
        if int(robot.get("health") or 0) <= 0:
            continue
        pos = robot.get("pos") or {}
        cell = type(center)(int(pos.get("x", -99)), int(pos.get("y", -99)))
        if distance(cell, center) <= BOMB_RADIUS:
            hits.append(robot)
    return hits


def bomb_damage_map(state: dict[str, Any], center: Any, *, distance) -> dict[int, int]:
    return {int(robot["id"]): BOMB_DAMAGE for robot in bomb_targets(state, center, distance=distance)}


def dizzy_targets(state: dict[str, Any], center: Any, *, distance) -> list[dict[str, Any]]:
    return [robot for robot in bomb_targets(state, center, distance=distance)
            if robot.get("abnormalState") != "dizzy"]


def describe_item(name: str) -> str:
    """Human-readable item label for logs and the developer panel."""
    return {
        WEAPON_UPGRADE_VOUCHER_1: "武器升级券1（Lv1→Lv2）",
        WEAPON_UPGRADE_VOUCHER_2: "武器升级券2（Lv2→Lv3）",
        WALL_UPGRADE_VOUCHER_1: "围墙升级券1（Lv1→Lv2）",
        WALL_UPGRADE_VOUCHER_2: "围墙升级券2（Lv2→Lv3）",
        STATION_UPGRADE_VOUCHER_1: "基地升级券1（Lv1→Lv2）",
        STATION_UPGRADE_VOUCHER_2: "基地升级券2（Lv2→Lv3）",
        WALL_FIXER: "围墙修复包",
        MEDICINE: "生命药剂",
        DIZZY_WEAPON: "眩晕法宝",
        BOMB: "范围炸弹",
        SMALL_ROBOT_SUMMON_ORDER: "小型机器人召唤令",
        MIDDLE_ROBOT_SUMMON_ORDER: "中型机器人召唤令",
        LARGE_ROBOT_SUMMON_ORDER: "大型机器人召唤令",
        BOSS_ROBOT_SUMMON_ORDER: "BOSS 召唤令",
    }.get(name, name)


def is_material(item: str) -> bool:
    return item in MATERIALS
