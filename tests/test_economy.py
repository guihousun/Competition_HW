"""Economy, item and action-registry tests with independently hand-checked inputs.

Every expectation here is derived from the official text (docs/任务书.md §4.4/§4.6,
docs/接口文档.md §2) rather than from the implementation under test:

  * sell price comes from `vendorShopList`, not from a local table;
  * shop distance is 1, backpack and gold limits are enforced;
  * upgrade vouchers only apply to their own building kind and their own level,
    and a failed use must not consume the item;
  * wall removal refunds nothing (R03);
  * bomb/dizzy hit both teams inside the 3x3 and resolve before robot movement;
  * summon orders add robots to the opponent's next night and cap at 10/day.
"""
import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))

from agent import actions, market, turnactions  # noqa: E402
from agent.protocol import (  # noqa: E402
    BOMB, DIZZY_WEAPON, MEDICINE, Pos, SMALL_ROBOT_SUMMON_ORDER,
    STATION_UPGRADE_VOUCHER_1, WALL_FIXER, WEAPON_UPGRADE_VOUCHER_1,
    WEAPON_UPGRADE_VOUCHER_2,
)
from agent.scenarios import scenario  # noqa: E402
from agent.simulator import step  # noqa: E402


def state_with(*, gold=75, backpack=None, roles=None, zones=None, round_no=1):
    """A minimal observation shaped exactly like the official request."""
    base = {
        "roundNo": round_no,
        "mapInfo": {"width": 41, "height": 32,
                    "zones": zones if zones is not None else []},
        "teamOur": {"type": "challenger", "goldNum": gold, "totalScore": 0,
                    "roles": roles if roles is not None else []},
        "teamEnemy": {"roles": []},
        "robot": {"roles": []},
        "vendorShopList": [{"name": "stone", "price": 1},
                           {"name": "iron", "price": 3},
                           {"name": "copper", "price": 5}],
        "weaponShopList": [{"name": WEAPON_UPGRADE_VOUCHER_1, "price": 100},
                           {"name": WEAPON_UPGRADE_VOUCHER_2, "price": 150},
                           {"name": WALL_FIXER, "price": 10},
                           {"name": MEDICINE, "price": 10},
                           {"name": BOMB, "price": 100},
                           {"name": DIZZY_WEAPON, "price": 100},
                           {"name": SMALL_ROBOT_SUMMON_ORDER, "price": 20}],
    }
    if backpack is not None:
        base["teamOur"]["roles"] = [{
            "id": 10010, "roleType": "worker", "pos": {"x": 10, "y": 10},
            "health": 220, "level": 1, "backPackCapability": 100,
            "backpack": list(backpack)}]
    return base


class ActionRegistryTests(unittest.TestCase):
    def test_all_official_actions_have_exactly_one_planner(self):
        self.assertEqual(actions.REGISTRY.missing(), ())
        self.assertEqual(set(actions.REGISTRY.actions()), set(actions.OFFICIAL_ACTIONS))
        self.assertEqual(len(actions.REGISTRY.actions()), 12)

    def test_duplicate_registration_is_refused_unless_explicit(self):
        registry = actions.default_registry()
        with self.assertRaises(actions.ActionError):
            registry.register("move", lambda **_: None)
        registry.register("move", lambda **_: None, replace=True)
        self.assertIsNone(registry.build("move"))
        with self.assertRaises(actions.ActionError):
            registry.register("notAnAction", lambda **_: None)

    def test_command_shape_matches_the_published_contract(self):
        self.assertEqual(actions.build("move", target=Pos(3, 4)),
                         {"action": "move", "targetPos": [{"x": 3, "y": 4}]})
        self.assertEqual(actions.build("sell", material="iron", amount=3),
                         {"action": "sell", "name": "iron", "num": 3})
        self.assertEqual(actions.build("use", item=WALL_FIXER, target=Pos(1, 2)),
                         {"action": "use", "name": WALL_FIXER,
                          "targetPos": [{"x": 1, "y": 2}]})
        attack = actions.build("attack", targets=[Pos(1, 1), Pos(2, 2)], controller=10010)
        self.assertEqual(attack["controllerId"], "10010")
        self.assertEqual(len(attack["targetPos"]), 2)
        self.assertEqual(actions.build("summonTreasure", target=Pos(0, 0),
                                       items=["AcientTablet"]),
                         {"action": "summonTreasure", "targetPos": [{"x": 0, "y": 0}],
                          "item": ["AcientTablet"]})
        self.assertEqual(actions.build("acceptTask"), {"action": "acceptTask"})
        self.assertEqual(actions.build("remove", target=Pos(9, 9)),
                         {"action": "remove", "targetPos": [{"x": 9, "y": 9}]})
        self.assertEqual(actions.build("drop", item="stone"),
                         {"action": "drop", "name": "stone"})

    def test_malformed_requests_are_rejected_before_reaching_the_wire(self):
        bad_calls = [
            ("unknown action", lambda: actions.build("teleport", target=Pos(1, 1))),
            ("missing target", lambda: actions.build("move")),
            ("non-official field", lambda: actions.build("attack", targets=[Pos(1, 1)],
                                                         controller=1, extra=1)),
            ("num on move", lambda: actions.build("move", target=Pos(1, 1), num=2)),
            ("zero amount", lambda: actions.build("buy", item=BOMB, amount=0)),
            ("bool amount", lambda: actions.build("buy", item=BOMB, amount=True)),
        ]
        for label, call in bad_calls:
            with self.subTest(label):
                with self.assertRaises(actions.ActionError):
                    call()

    def test_validation_keeps_official_fields_and_types(self):
        command = actions.build("buy", item=BOMB, amount=2)
        self.assertEqual(actions.validate(command), command)
        for broken in ({"action": "attack", "targetPos": [{"x": 1}], "controllerId": "1"},
                       {"action": "attack", "targetPos": [], "controllerId": "1"},
                       {"action": "attack", "targetPos": [{"x": 1, "y": 1}], "controllerId": 1},
                       {"action": "summonTreasure", "targetPos": [{"x": 1, "y": 1}]},
                       {"action": "sell", "name": "stone", "rogue": True}):
            with self.subTest(broken=broken):
                with self.assertRaises(actions.ActionError):
                    actions.validate(broken)

    def test_field_vocabulary_covers_the_published_actions(self):
        self.assertEqual(set(actions.SPECS), set(actions.OFFICIAL_ACTIONS))
        from agent.protocol import ERROR_CODES, VISION_DISTANCE
        self.assertEqual(ERROR_CODES[5], "LLM 额度超限")
        self.assertEqual(VISION_DISTANCE, 4)


class MarketTests(unittest.TestCase):
    def test_prices_come_from_the_observation(self):
        state = state_with(zones=[{"pos": {"x": 11, "y": 10}, "neutralType": "vendor"}])
        self.assertEqual(market.vendor_prices(state), {"stone": 1, "iron": 3, "copper": 5})
        # A news-driven price change must flow through, not be averaged away.
        state["vendorShopList"] = [{"name": "iron", "price": 9}]
        self.assertEqual(market.vendor_prices(state), {"iron": 9})

    def test_sell_value_ignores_items_the_vendor_does_not_buy(self):
        state = state_with(backpack=["iron", "iron", "stone", BOMB])
        role = state["teamOur"]["roles"][0]
        self.assertEqual(market.sell_value(role, state), 3 + 3 + 1)
        self.assertEqual(market.sellable_inventory(role, state), {"iron": 2, "stone": 1})

    def test_buy_plan_respects_gold_and_backpack_space(self):
        state = state_with(gold=250, backpack=[])
        role = state["teamOur"]["roles"][0]
        self.assertEqual(market.buy_plan(role, state, WEAPON_UPGRADE_VOUCHER_1, 250), (2, 200))
        self.assertEqual(market.buy_plan(role, state, WEAPON_UPGRADE_VOUCHER_1, 99), None)
        role["backPackCapability"] = 3
        role["backpack"] = ["stone", "stone"]
        self.assertEqual(market.buy_plan(role, state, WALL_FIXER, 100), (1, 10))
        role["backpack"] = ["stone", "stone", "stone"]
        self.assertEqual(market.buy_plan(role, state, WALL_FIXER, 100), None)

    def test_upgrade_chain_is_level_and_kind_specific(self):
        self.assertTrue(market.can_upgrade(WEAPON_UPGRADE_VOUCHER_1, "rocket", 1))
        self.assertFalse(market.can_upgrade(WEAPON_UPGRADE_VOUCHER_1, "rocket", 2))
        self.assertTrue(market.can_upgrade(WEAPON_UPGRADE_VOUCHER_2, "rocket", 2))
        self.assertFalse(market.can_upgrade(WEAPON_UPGRADE_VOUCHER_2, "rocket", 3))
        self.assertFalse(market.can_upgrade(WEAPON_UPGRADE_VOUCHER_1, "wall", 1))
        self.assertTrue(market.can_upgrade(STATION_UPGRADE_VOUCHER_1, "station", 1))
        self.assertFalse(market.can_upgrade(WALL_FIXER, "wall", 1))

    def test_summon_orders_are_capped_per_day(self):
        self.assertEqual(market.summon_amount_allowed(0, 3), 3)
        self.assertEqual(market.summon_amount_allowed(9, 3), 1)
        self.assertEqual(market.summon_amount_allowed(10, 3), 0)
        load = market.summon_load([SMALL_ROBOT_SUMMON_ORDER] * 2 + ["Bomb"])
        self.assertEqual(load, {"smallRobot": 2})

    def test_item_catalog_uses_official_spellings(self):
        from agent.protocol import SHOP_PRICES, TASK_ITEMS
        self.assertIn("AcientTablet", TASK_ITEMS)
        self.assertEqual(SHOP_PRICES[WEAPON_UPGRADE_VOUCHER_2], 150)
        self.assertEqual(SHOP_PRICES["BossRobotSummonOrder"], 200)
        state = state_with(gold=120)
        catalog = market.shop_catalog(state, 120)
        self.assertTrue(catalog[WEAPON_UPGRADE_VOUCHER_1]["affordable"])
        self.assertFalse(catalog[WEAPON_UPGRADE_VOUCHER_2]["affordable"])


class TurnActionTests(unittest.TestCase):
    def setUp(self):
        self.state = state_with(
            gold=0, backpack=["stone", "iron", "iron"],
            zones=[{"pos": {"x": 11, "y": 10}, "neutralType": "vendor"},
                   {"pos": {"x": 10, "y": 11}, "neutralType": "weaponShop"}])
        self.role = self.state["teamOur"]["roles"][0]

    def test_sell_uses_the_live_price_and_updates_gold(self):
        record = turnactions.sell(self.state, self.role, material="iron", amount=2)
        self.assertEqual(record["gold"], 6)
        self.assertEqual(self.state["teamOur"]["goldNum"], 6)
        self.assertEqual(self.role["backpack"], ["stone"])

    def test_sell_refuses_without_adjacency_and_changes_nothing(self):
        before = deepcopy(self.state)
        self.role["pos"] = {"x": 30, "y": 30}
        with self.assertRaises(turnactions.Refusal):
            turnactions.sell(self.state, self.role, material="iron", amount=1)
        self.role["pos"] = before["teamOur"]["roles"][0]["pos"]
        self.assertEqual(self.state, before)

    def test_sell_refuses_more_than_held(self):
        with self.assertRaises(turnactions.Refusal):
            turnactions.sell(self.state, self.role, material="iron", amount=3)
        self.assertEqual(self.role["backpack"].count("iron"), 2)

    def test_buy_charges_gold_and_fills_backpack(self):
        self.state["teamOur"]["goldNum"] = 250
        record = turnactions.buy(self.state, self.role, item=WALL_FIXER, amount=2)
        self.assertEqual(record["gold"], -20)
        self.assertEqual(self.state["teamOur"]["goldNum"], 230)
        self.assertEqual(self.role["backpack"].count(WALL_FIXER), 2)

    def test_buy_refuses_when_gold_or_space_is_missing(self):
        self.state["teamOur"]["goldNum"] = 5
        with self.assertRaises(turnactions.Refusal):
            turnactions.buy(self.state, self.role, item=WALL_FIXER, amount=1)
        self.state["teamOur"]["goldNum"] = 999
        self.role["backPackCapability"] = 3
        with self.assertRaises(turnactions.Refusal):
            turnactions.buy(self.state, self.role, item=WALL_FIXER, amount=1)

    def test_upgrade_voucher_raises_level_and_restores_full_health(self):
        tower = {"id": 10040, "roleType": "rocket", "pos": {"x": 12, "y": 10},
                 "health": 300, "level": 1}
        self.state["teamOur"]["roles"].append(tower)
        # 任务书 §4.6.3: the voucher must be used next to the target building.
        self.role["pos"] = {"x": 11, "y": 10}
        self.role["backpack"].append(WEAPON_UPGRADE_VOUCHER_1)
        record = turnactions.use(self.state, self.role, item=WEAPON_UPGRADE_VOUCHER_1,
                                 target=Pos(12, 10), effects={})
        self.assertEqual(record["level"], 2)
        self.assertEqual(tower["level"], 2)
        self.assertEqual(tower["health"], 1500)
        self.assertNotIn(WEAPON_UPGRADE_VOUCHER_1, self.role["backpack"])

    def test_upgrade_refuses_when_not_adjacent(self):
        tower = {"id": 10040, "roleType": "rocket", "pos": {"x": 12, "y": 10},
                 "health": 1000, "level": 1}
        self.state["teamOur"]["roles"].append(tower)
        self.role["backpack"].append(WEAPON_UPGRADE_VOUCHER_1)
        with self.assertRaises(turnactions.Refusal):
            turnactions.use(self.state, self.role, item=WEAPON_UPGRADE_VOUCHER_1,
                            target=Pos(12, 10), effects={})
        self.assertIn(WEAPON_UPGRADE_VOUCHER_1, self.role["backpack"])
        self.assertEqual(tower["level"], 1)

    def test_failed_upgrade_does_not_consume_the_voucher(self):
        tower = {"id": 10040, "roleType": "rocket", "pos": {"x": 12, "y": 10},
                 "health": 1000, "level": 3}
        self.state["teamOur"]["roles"].append(tower)
        self.role["backpack"].append(WEAPON_UPGRADE_VOUCHER_2)
        with self.assertRaises(turnactions.Refusal):
            turnactions.use(self.state, self.role, item=WEAPON_UPGRADE_VOUCHER_2,
                            target=Pos(12, 10), effects={})
        self.assertIn(WEAPON_UPGRADE_VOUCHER_2, self.role["backpack"])
        self.assertEqual(tower["level"], 3)

    def test_upgrade_requires_standing_next_to_the_target(self):
        tower = {"id": 10040, "roleType": "rocket", "pos": {"x": 20, "y": 20},
                 "health": 1000, "level": 1}
        self.state["teamOur"]["roles"].append(tower)
        self.role["backpack"].append(WEAPON_UPGRADE_VOUCHER_1)
        with self.assertRaises(turnactions.Refusal):
            turnactions.use(self.state, self.role, item=WEAPON_UPGRADE_VOUCHER_1,
                            target=Pos(20, 20), effects={})

    def test_medicine_heals_but_is_not_wasted_at_full_health(self):
        self.role["backpack"].append(MEDICINE)
        self.role["health"] = 100
        turnactions.use(self.state, self.role, item=MEDICINE, target=None, effects={})
        self.assertEqual(self.role["health"], 220)
        self.role["backpack"].append(MEDICINE)
        with self.assertRaises(turnactions.Refusal):
            turnactions.use(self.state, self.role, item=MEDICINE, target=None, effects={})
        self.assertEqual(self.role["backpack"].count(MEDICINE), 1)

    def test_remove_wall_refunds_no_material(self):
        wall = {"id": 40000, "roleType": "wall", "pos": {"x": 11, "y": 11},
                "health": 1000, "level": 1}
        self.state["teamOur"]["roles"].append(wall)
        self.role["pos"] = {"x": 10, "y": 11}
        stones_before = self.role["backpack"].count("stone")
        record = turnactions.remove_wall(self.state, self.role, target=Pos(11, 11))
        # 任务书 §4.5.1: razing a wall does not return the stone it cost.
        self.assertEqual(record["refund"], 0)
        self.assertNotIn(wall, self.state["teamOur"]["roles"])
        self.assertEqual(self.role["backpack"].count("stone"), stones_before)

    def test_bomb_and_dizzy_queue_effects_and_do_not_touch_buildings(self):
        robots = [{"id": 1, "roleType": "smallRobot", "pos": {"x": 20, "y": 20},
                   "health": 40, "targetTeam": "challenger", "abnormalState": ""},
                  {"id": 2, "roleType": "middleRobot", "pos": {"x": 25, "y": 25},
                   "health": 60, "targetTeam": "challenger", "abnormalState": ""},
                  {"id": 3, "roleType": "smallRobot", "pos": {"x": 30, "y": 30},
                   "health": 40, "targetTeam": "challenger", "abnormalState": ""}]
        self.state["robot"]["roles"] = deepcopy(robots)
        self.role["backpack"].extend([BOMB, DIZZY_WEAPON])
        effects = {}
        turnactions.use(self.state, self.role, item=BOMB, target=Pos(20, 20), effects=effects)
        # Nothing happens yet: items resolve before robot movement, later.
        self.assertEqual(self.state["robot"]["roles"][0]["health"], 40)
        turnactions.use(self.state, self.role, item=DIZZY_WEAPON, target=Pos(25, 25), effects=effects)
        records = turnactions.apply_battle_items(self.state, effects)
        self.assertEqual(self.state["robot"]["roles"][0]["health"], 0)
        self.assertEqual(self.state["robot"]["roles"][1]["abnormalState"], "dizzy")
        self.assertEqual(self.state["robot"]["roles"][2]["health"], 40)
        self.assertEqual([r["a"] for r in records], ["bomb", "dizzy"])
        for dropped in turnactions.tick_dizzy(self.state, freshly_dizzy={2}):
            self.fail(f"a robot dizzied this round must keep its full window: {dropped}")
        # 任务书 §4.6.3: 眩晕 5 回合. The dizzy round plus four more still-dizzy
        # rounds, then the fifth tick clears it.
        for _ in range(4):
            ended = turnactions.tick_dizzy(self.state)
            self.assertEqual(ended, [], "the robot must stay dizzy for 5 rounds")
        ended = turnactions.tick_dizzy(self.state)
        self.assertEqual([record["robot"] for record in ended], [2])
        self.assertEqual(self.state["robot"]["roles"][1]["abnormalState"], "")
        self.assertEqual(self.state["robot"]["roles"][1]["health"], 60)

    def test_battle_item_without_targets_is_refused_and_kept(self):
        self.role["backpack"].append(BOMB)
        with self.assertRaises(turnactions.Refusal):
            turnactions.use(self.state, self.role, item=BOMB, target=Pos(1, 1), effects={})
        self.assertIn(BOMB, self.role["backpack"])

    def test_summon_order_lands_on_the_opponent(self):
        self.state["teamOur"]["goldNum"] = 100
        turnactions.buy(self.state, self.role, item=SMALL_ROBOT_SUMMON_ORDER, amount=1)
        effects = {}
        turnactions.use(self.state, self.role, item=SMALL_ROBOT_SUMMON_ORDER,
                        target=None, effects=effects)
        self.assertEqual(turnactions.summon_load(effects, "challenger"),
                         {"defender": {"smallRobot": 1}})
        self.assertEqual(effects["summons_used_today"], 1)

    def test_summon_limit_is_ten_per_day(self):
        effects = {"summons_used_today": 10}
        self.role["backpack"].append(SMALL_ROBOT_SUMMON_ORDER)
        with self.assertRaises(turnactions.Refusal):
            turnactions.use(self.state, self.role, item=SMALL_ROBOT_SUMMON_ORDER,
                            target=None, effects=effects)
        self.assertEqual(len(effects.get("summons") or []), 0)


class SimulatorEconomyTests(unittest.TestCase):
    """The same rules, exercised through a real settle pass."""

    def build(self):
        state = scenario(5, "challenger", 1)
        worker = next(role for role in state["teamOur"]["roles"]
                      if role["roleType"] == "worker")
        # Put the worker next to the vendor that the seeded map already has.
        vendor = next(zone for zone in state["mapInfo"]["zones"]
                      if zone["neutralType"] == "vendor")
        worker["pos"] = {"x": vendor["pos"]["x"] + 1, "y": vendor["pos"]["y"]}
        worker["backpack"] = ["stone", "stone", "iron"]
        state["teamOur"]["goldNum"] = 0
        return state, worker

    def test_sell_command_through_step(self):
        state, worker = self.build()
        commands = {str(worker["id"]): {"action": "sell", "name": "iron", "num": 1}}
        result = step(state, commands)
        self.assertTrue(result["state"]["lastRoundRoleActionResults"][str(worker["id"])])
        self.assertEqual(result["state"]["teamOur"]["goldNum"], 3)
        self.assertEqual(result["frame"]["actions"][0]["a"], "sell")

    def test_failed_sell_is_reported_and_changes_nothing(self):
        state, worker = self.build()
        worker["pos"] = {"x": 39, "y": 31}
        commands = {str(worker["id"]): {"action": "sell", "name": "iron", "num": 1}}
        result = step(state, commands)
        self.assertFalse(result["state"]["lastRoundRoleActionResults"][str(worker["id"])])
        self.assertEqual(result["state"]["teamOur"]["goldNum"], 0)
        self.assertIn(str(worker["id"]), result["frame"]["skipped"])
        self.assertTrue(any("贩卖未执行" in event for event in result["events"]))

    def test_upgrade_through_step_changes_level_and_health(self):
        state, worker = self.build()
        # A fresh scenario has no towers yet; place one so the upgrade is real.
        tower = {"id": 10040, "roleType": "rocket", "pos": {"x": 20, "y": 20},
                 "health": 200, "level": 1, "backpack": []}
        state["teamOur"]["roles"].append(tower)
        worker["pos"] = {"x": 19, "y": 20}
        worker["backpack"] = [WEAPON_UPGRADE_VOUCHER_1]
        commands = {str(worker["id"]): {"action": "use", "name": WEAPON_UPGRADE_VOUCHER_1,
                                       "targetPos": [dict(tower["pos"])]}}
        result = step(state, commands)
        updated = next(role for role in result["state"]["teamOur"]["roles"]
                       if role["id"] == tower["id"])
        self.assertEqual(updated["level"], 2)
        self.assertEqual(updated["health"], 1500)
        self.assertEqual(result["frame"]["actions"][0]["a"], "upgrade")

    def test_attack_still_works_after_the_new_branches(self):
        state = scenario(11, "challenger", 1)
        state["roundNo"] = 71
        tower = {"id": 10040, "roleType": "rocket", "pos": {"x": 20, "y": 20},
                 "health": 1000, "level": 1, "cooldown": 0, "backpack": []}
        state["teamOur"]["roles"].append(tower)
        worker = next(role for role in state["teamOur"]["roles"]
                      if role["roleType"] == "worker")
        worker["pos"] = {"x": 19, "y": 20}
        target = {"x": 22, "y": 20}
        state["robot"]["roles"] = [{"id": 1, "roleType": "smallRobot", "pos": target,
                                    "health": 40, "targetTeam": "challenger",
                                    "abnormalState": ""}]
        commands = {str(tower["id"]): {"action": "attack", "controllerId": str(worker["id"]),
                                       "targetPos": [target]}}
        result = step(state, commands)
        self.assertEqual(result["state"]["robot"]["roles"][0]["health"], 20)
        self.assertEqual(result["state"]["teamOur"]["totalScore"], 0)


def adjacent(pos, state):
    """A free walkable cell next to the target, avoiding occupied cells."""
    occupied = set()
    for role in state["teamOur"]["roles"] + state["teamEnemy"]["roles"]:
        if role["health"] <= 0:
            continue
        occupied.add((role["pos"]["x"], role["pos"]["y"]))
        if role["roleType"] == "station":
            occupied.add((role["pos"]["x"] + 1, role["pos"]["y"] - 1))
    for dx in (1, -1, 0):
        for dy in (1, -1, 0):
            if dx == dy == 0:
                continue
            cell = {"x": pos["x"] + dx, "y": pos["y"] + dy}
            if 0 <= cell["x"] < 41 and 0 <= cell["y"] < 32 and (cell["x"], cell["y"]) not in occupied:
                return cell
    return {"x": pos["x"], "y": pos["y"]}


if __name__ == "__main__":
    unittest.main()
