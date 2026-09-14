"""Local execution of economy and item actions (trade, upgrades, consumables).

Each function is an **atomic unit**: it either completes fully (state mutated and
an action record returned) or refuses with a reason and leaves the state exactly
as it was. Nothing here writes to the wire format — the strategy only ever sees
the official command builders in ``actions.py``.

Rule sources (docs/任务书.md, docs/DEVELOPMENT_RULES.md):
  R03 walls cost a stone, no refund on removal;  R06 shop distance, upgrade
  chains, prices, summon limit;  R02 items resolve before robot movement.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Callable

from .market import (
    SUMMON_ORDERS,
    TARGETED_ITEMS,
    VOUCHER_TARGETS,
    can_upgrade,
    count_item,
    vendor_prices,
)
from .protocol import (
    BOMB,
    BOMB_DAMAGE,
    BOMB_RADIUS,
    DIZZY_ROUNDS,
    DIZZY_WEAPON,
    MATERIALS,
    MEDICINE,
    STATION,
    TOWER_TYPES,
    WALL,
    WALL_FIXER,
    Pos,
    distance,
    station_footprint,
)

# Where an interaction may happen (任务书 §4.4: 周围一格内).
INTERACTION_RANGE = 1
# Health restored by an upgrade (任务书 §4.5.1, §4.6.3).
BUILDING_MAX_HEALTH = {
    "gatling": (1000, 1500, 2000),
    "railgun": (1000, 1500, 2000),
    "rocket": (1000, 1500, 2000),
    WALL: (1000, 1500, 2000),
    STATION: (1500, 3000, 4500),
}
CREW_MAX_HEALTH = {"worker": 220, "pioneer": 200}


class Refusal(Exception):
    """The action is legal to send but cannot take effect locally (R01)."""


def own_task_point(state: dict[str, Any], pos: Pos) -> dict[str, Any] | None:
    """The own-team task point whose one-cell ring contains `pos`.

    Only our own points count (任务书 §4.6.2); task point 2 spans two cells, so
    both are checked. Shared by the simulator and the task pipeline so the two
    can never disagree about where a task may be accepted from.
    """
    team = str((state.get("teamOur") or {}).get("type") or "")
    for zone in (state.get("mapInfo") or {}).get("zones") or ():
        kind = str(zone.get("neutralType", ""))
        if not (kind.startswith(team) and "TaskPoint" in kind):
            continue
        cell = Pos.load(zone["pos"])
        cells = (cell, Pos(cell.x + 1, cell.y)) if kind.endswith("TaskPoint2") else (cell,)
        if min(distance(pos, spot) for spot in cells) <= INTERACTION_RANGE:
            return zone
    return None


def _zones_of(state: dict[str, Any], kind: str) -> list[Pos]:
    return [
        Pos(int(zone["pos"]["x"]), int(zone["pos"]["y"]))
        for zone in (state.get("mapInfo") or {}).get("zones") or ()
        if zone.get("neutralType") == kind
    ]


def _alive_roles(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [role for role in state["teamOur"]["roles"] if int(role.get("health") or 0) > 0]


def _buildings(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [role for role in _alive_roles(state)
            if role.get("roleType") in (WALL, STATION) + TOWER_TYPES]


def _role_cells(role: dict[str, Any]) -> tuple[Pos, ...]:
    pos = Pos.load(role["pos"])
    return station_footprint(pos) if role.get("roleType") == STATION else (pos,)


def adjacent_to_zone(state: dict[str, Any], pos: Pos, kind: str) -> bool:
    """True when `pos` is within one cell of any zone of that kind."""
    return any(distance(pos, cell) <= INTERACTION_RANGE for cell in _zones_of(state, kind))


def find_zone(state: dict[str, Any], kind: str, *, distance_of: Callable[[Pos], int] | None = None) -> Pos | None:
    cells = _zones_of(state, kind)
    if not cells:
        return None
    if distance_of is None:
        return cells[0]
    return min(cells, key=lambda cell: (distance_of(cell), cell.x, cell.y))


def _building_at(state: dict[str, Any], target: Pos) -> dict[str, Any] | None:
    for role in _buildings(state):
        if target in _role_cells(role):
            return role
    return None


def _take_from_backpack(role: dict[str, Any], item: str, amount: int = 1) -> None:
    bag = role.setdefault("backpack", [])
    for _ in range(amount):
        if item not in bag:
            raise Refusal(f"背包中没有 {item}")
        bag.remove(item)


# --------------------------------------------------------------------------
# sell / buy
# --------------------------------------------------------------------------

def sell(state: dict[str, Any], role: dict[str, Any], *, material: str,
         amount: int = 1) -> dict[str, Any]:
    """Sell `amount` units of a material to the vendor next to the role."""
    if material not in MATERIALS:
        raise Refusal(f"小贩不收购 {material}")
    price = vendor_prices(state).get(material)
    if price is None:
        raise Refusal(f"小贩当前不收购 {material}")
    have = count_item(role, material)
    amount = int(amount)
    if amount < 1:
        raise Refusal("数量必须大于 0")
    if have < amount:
        raise Refusal(f"{material} 不足（持有 {have}）")
    pos = Pos.load(role["pos"])
    if not adjacent_to_zone(state, pos, "vendor"):
        raise Refusal("不在小贩周围一格内")
    _take_from_backpack(role, material, amount)
    gain = price * amount
    state["teamOur"]["goldNum"] = int(state["teamOur"].get("goldNum") or 0) + gain
    return {"a": "sell", "id": role["id"], "kind": material, "num": amount,
            "price": price, "gold": gain, "from": pos.dump(), "to": pos.dump(),
            "bag": len(role.get("backpack") or [])}


def buy(state: dict[str, Any], role: dict[str, Any], *, item: str,
        amount: int = 1) -> dict[str, Any]:
    """Buy `amount` units of a shop item into the role's backpack."""
    prices = {str(entry["name"]): int(entry["price"])
              for entry in (state.get("weaponShopList") or ())
              if "name" in entry and "price" in entry}
    price = prices.get(item)
    if price is None:
        raise Refusal(f"商店不出售 {item}")
    amount = int(amount)
    if amount < 1:
        raise Refusal("数量必须大于 0")
    pos = Pos.load(role["pos"])
    if not adjacent_to_zone(state, pos, "weaponShop"):
        raise Refusal("不在武器商店周围一格内")
    capacity = int(role.get("backPackCapability") or 0)
    bag = role.setdefault("backpack", [])
    if len(bag) + amount > capacity:
        raise Refusal(f"背包空间不足（{len(bag)}/{capacity}）")
    cost = price * amount
    gold = int(state["teamOur"].get("goldNum") or 0)
    if gold < cost:
        raise Refusal(f"金币不足（{gold} < {cost}）")
    state["teamOur"]["goldNum"] = gold - cost
    bag.extend([item] * amount)
    return {"a": "buy", "id": role["id"], "kind": item, "num": amount,
            "price": price, "gold": -cost, "from": pos.dump(), "to": pos.dump(),
            "bag": len(bag)}


# --------------------------------------------------------------------------
# remove (wall only, no material refund - R03)
# --------------------------------------------------------------------------

def remove_wall(state: dict[str, Any], role: dict[str, Any], *, target: Pos) -> dict[str, Any]:
    if role.get("roleType") != "worker":
        raise Refusal("只有工人能拆除围墙")
    pos = Pos.load(role["pos"])
    if distance(pos, target) > INTERACTION_RANGE:
        raise Refusal("目标不在周围一格内")
    victim = _building_at(state, target)
    if victim is None or victim.get("roleType") != WALL:
        raise Refusal("目标位置没有围墙")
    state["teamOur"]["roles"].remove(victim)
    return {"a": "remove", "id": role["id"], "kind": WALL, "removedId": victim["id"],
            "from": pos.dump(), "to": target.dump(), "refund": 0}


# --------------------------------------------------------------------------
# use: upgrades, repairs, healing, battle items, summons (R06)
# --------------------------------------------------------------------------

def _level_index(level: int) -> int:
    return max(0, min(int(level) - 1, 2))


def use(state: dict[str, Any], role: dict[str, Any], *, item: str,
        target: Pos | None, effects: dict[str, Any]) -> dict[str, Any]:
    """Use one backpack item. `effects` collects deferred battle/summon work.

    Battle items and summon orders are not applied here: they must resolve at
    the official moment (items before robot movement, summons in the next wave),
    so they are queued instead of executed immediately.
    """
    if count_item(role, item) <= 0:
        raise Refusal(f"背包中没有 {item}")
    pos = Pos.load(role["pos"])
    if item in TARGETED_ITEMS and target is None:
        raise Refusal(f"{item} 需要指定目标坐标")

    if item in VOUCHER_TARGETS:
        return _use_voucher(state, role, item, target)

    if item == WALL_FIXER:
        victim = _building_at(state, target)
        if victim is None or victim.get("roleType") != WALL:
            raise Refusal("目标位置没有围墙")
        if distance(pos, target) > INTERACTION_RANGE:
            raise Refusal("需要站在待修复围墙周围一格内")
        max_hp = BUILDING_MAX_HEALTH[WALL][_level_index(victim.get("level") or 1)]
        if int(victim.get("health") or 0) >= max_hp:
            raise Refusal("围墙已是满血，修复包不消耗")
        victim["health"] = max_hp
        _take_from_backpack(role, item)
        return {"a": "use", "id": role["id"], "kind": item, "targetId": victim["id"],
                "from": pos.dump(), "to": target.dump(), "health": max_hp}

    if item == MEDICINE:
        max_hp = CREW_MAX_HEALTH.get(role.get("roleType"))
        if not max_hp:
            raise Refusal(f"{role.get('roleType')} 不能使用生命药剂")
        if int(role.get("health") or 0) >= max_hp:
            raise Refusal("已经满血，生命药剂不消耗")
        role["health"] = max_hp
        _take_from_backpack(role, item)
        return {"a": "use", "id": role["id"], "kind": item, "health": max_hp,
                "from": pos.dump(), "to": pos.dump()}

    if item in (BOMB, DIZZY_WEAPON):
        # No use distance limit; affects robots of both teams only (R06).
        hits = []
        for robot in (state.get("robot") or {}).get("roles") or ():
            if int(robot.get("health") or 0) <= 0:
                continue
            robot_pos = Pos.load(robot["pos"])
            if distance(robot_pos, target) <= BOMB_RADIUS:
                hits.append(robot)
        if not hits:
            raise Refusal("3×3 范围内没有机器人")
        _take_from_backpack(role, item)
        effects.setdefault("battle_items", []).append({
            "item": item, "target": target.dump(), "by": role["id"],
            "robots": [robot["id"] for robot in hits],
        })
        return {"a": "use", "id": role["id"], "kind": item, "hits": len(hits),
                "from": pos.dump(), "to": target.dump()}

    if item in SUMMON_ORDERS:
        used = int(effects.get("summons_used_today") or 0)
        if used >= 10:
            raise Refusal("今天已使用 10 张召唤令")
        _take_from_backpack(role, item)
        effects["summons_used_today"] = used + 1
        effects.setdefault("summons", []).append({"order": item, "by": role["id"],
                                                  "kind": SUMMON_ORDERS[item]})
        return {"a": "use", "id": role["id"], "kind": item, "summon": SUMMON_ORDERS[item],
                "from": pos.dump(), "to": pos.dump()}

    raise Refusal(f"{item} 在本地方案中未实现使用效果")


def _use_voucher(state: dict[str, Any], role: dict[str, Any], item: str,
                 target: Pos | None) -> dict[str, Any]:
    victim = _building_at(state, target)
    if victim is None:
        raise Refusal("目标位置没有建筑")
    kind = str(victim.get("roleType"))
    level = int(victim.get("level") or 1)
    if kind not in VOUCHER_TARGETS[item]:
        raise Refusal(f"{item} 不能用于 {kind}")
    if not can_upgrade(item, kind, level):
        raise Refusal(f"{item} 不适用于 {kind} Lv{level}（升级券不消耗）")
    pos = Pos.load(role["pos"])
    if min(distance(pos, cell) for cell in _role_cells(victim)) > INTERACTION_RANGE:
        raise Refusal("需要站在目标建筑周围一格内")
    _take_from_backpack(role, item)
    new_level = level + 1
    victim["level"] = new_level
    victim["health"] = BUILDING_MAX_HEALTH[kind][_level_index(new_level)]
    return {"a": "upgrade", "id": role["id"], "kind": item, "targetId": victim["id"],
            "building": kind, "from": pos.dump(), "to": target.dump(),
            "level": new_level, "health": victim["health"]}


# --------------------------------------------------------------------------
# deferred effects, applied at the official moment
# --------------------------------------------------------------------------

def apply_battle_items(state: dict[str, Any], effects: dict[str, Any]) -> list[dict[str, Any]]:
    """Dizzy/bomb resolve before robot movement (任务书 §4.6.3 note)."""
    records: list[dict[str, Any]] = []
    robots = {robot["id"]: robot for robot in (state.get("robot") or {}).get("roles") or ()}
    for queued in effects.get("battle_items") or ():
        item = queued["item"]
        for robot_id in queued["robots"]:
            robot = robots.get(robot_id)
            if robot is None or int(robot.get("health") or 0) <= 0:
                continue
            if item == BOMB:
                before = int(robot["health"])
                robot["health"] = max(0, before - BOMB_DAMAGE)
                records.append({"a": "bomb", "item": item, "robot": robot_id,
                                "damage": before - int(robot["health"]),
                                "pos": robot["pos"], "by": queued["by"]})
            else:
                robot["abnormalState"] = "dizzy"
                robot["dizzyRounds"] = DIZZY_ROUNDS
                records.append({"a": "dizzy", "item": item, "robot": robot_id,
                                "rounds": DIZZY_ROUNDS, "pos": robot["pos"],
                                "by": queued["by"]})
    return records


def tick_dizzy(state: dict[str, Any], *, freshly_dizzy: set[int] | None = None) -> list[dict[str, Any]]:
    """Count down眩晕 rounds at the end of the local round.

    Robots dizzied *this* round keep their full window, so ``freshly_dizzy``
    (the ids just hit) are skipped for this tick and start counting next round.
    """
    skip = freshly_dizzy or set()
    records: list[dict[str, Any]] = []
    for robot in (state.get("robot") or {}).get("roles") or ():
        if robot.get("abnormalState") != "dizzy" or robot.get("id") in skip:
            continue
        rounds = int(robot.get("dizzyRounds") or 0) - 1
        robot["dizzyRounds"] = max(0, rounds)
        if rounds <= 0:
            robot["abnormalState"] = ""
            records.append({"a": "dizzy_end", "robot": robot["id"], "pos": robot["pos"]})
    return records


def describe_effect(record: dict[str, Any]) -> str:
    """One-line human summary of an applied action record (logs and viewer)."""
    kind = record.get("a")
    if kind == "upgrade":
        return f"{record.get('building')} → Lv{record.get('level')}，血量恢复至 {record.get('health')}"
    if kind == "sell":
        return f"{record.get('num')}×{record.get('kind')} 单价 {record.get('price')}，+{record.get('gold')} 金币"
    if kind == "buy":
        return f"{record.get('num')}×{record.get('kind')} 单价 {record.get('price')}，-{abs(int(record.get('gold') or 0))} 金币"
    if kind == "remove":
        return "围墙已拆除，不退还石头"
    if kind in ("bomb", "dizzy"):
        return f"命中 {record.get('hits', 1)} 个机器人"
    if kind == "summon":
        return f"下一次夜晚给对方增加 {record.get('summon')}"
    if kind == "use":
        return f"目标 {record.get('targetId', record.get('id'))}"
    return str(kind)


def summon_load(effects: dict[str, Any], our_team: str) -> dict[str, dict[str, int]]:
    """Turn queued summon orders into extra robots for the opponent's next night.

    The order is bought by us but lands on the opponent (任务书 §4.6.3), so the
    target team is the opposite of ``our_team``.
    """
    enemy_team = "defender" if our_team == "challenger" else "challenger"
    load: Counter[str] = Counter()
    for summon in effects.get("summons") or ():
        load[summon["kind"]] += 1
    return {enemy_team: dict(load)} if load else {}
