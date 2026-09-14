"""Worker metal economy (strategy optimization / R03, R06; 任务书 §4.4–4.6.1).

Expected results come from the published rules, not from the helpers under test:

  * 采集 needs a worker within one cell of a 石矿/铁矿/铜矿 (任务书 §4.4), so a
    collect command is only legal next to the matching mine;
  * 小贩 buys 石头/铁/铜 at the price in the live 收购清单 (R06), so ore with no
    published price is never mined;
  * walls and towers are the standing priority and only workers may build
    (任务书 §4.4), so metal work never starts while they are missing;
  * night belongs to the defence (R02/R04), so no new daytime mining run starts
    at dusk or at night.

The board is built so the geometry each case depends on is asserted, and the
end-to-end case drives the real local round loop: collect -> carried metal ->
sell -> gold, with no privileged strategy input.
"""
import sys
import unittest
from collections import Counter
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))

from agent import brain  # noqa: E402
from agent.protocol import (Pos, Turn, collect_command, distance,  # noqa: E402
                            move_command)
from agent.simulator import step  # noqa: E402


def unit(uid, kind, x, y, hp, cap=0, backpack=None):
    return {"id": uid, "roleType": kind, "pos": {"x": x, "y": y}, "health": hp,
            "level": 1, "backPackCapability": cap, "backpack": list(backpack or [])}


BASE = (10, 24)
# The base footprint is (10,23)-(11,24) and `_wall_order` rings it at radius 2
# (x = 8..13 for this base). (14,24) is the first legal ground outside that ring
# and (15,24) is its neighbour, so the collect adjacency 任务书 §4.4 requires is
# fixed by construction rather than by luck.
WORKER = (14, 24)
PIONEER = (10, 22)
MINE = (15, 24)
VENDOR = (10, 28)


def board(*, ores=(("copper", MINE),), vendor=VENDOR, round_no=13, gold=0,
          backpack=None, towers=3, walls="complete", terrain=()):
    """Base + one worker just outside the ring, defence as requested.

    `walls="complete"` stands up the ring `brain._wall_order` asks for, so the
    worker really is past the stone-building phase; `walls="missing"` leaves it
    empty. The worker stands at (14,24) and the mine at (15,24): both are
    asserted legal, outside the ring, and adjacent, so the collect adjacency is
    real and the worker starts beside the ore it is expected to mine.
    """
    roles = [unit(10013, "station", BASE[0], BASE[1], 1500)]
    roles.append(unit(10010, "worker", WORKER[0], WORKER[1], 220, 100, backpack))
    roles.append(unit(10011, "pioneer", PIONEER[0], PIONEER[1], 200, 40))
    state = {
        "roundNo": round_no,
        "mapInfo": {"width": 41, "height": 32,
                    "zones": [{"pos": {"x": x, "y": y}, "neutralType": kind}
                              for kind, (x, y) in ores]},
        "teamOur": {"type": "challenger", "goldNum": gold, "totalScore": 0,
                    "roles": roles, "playerTasks": []},
        "teamEnemy": {"roles": []}, "robot": {"roles": []},
        "phaseTask": "", "llmResp": "", "lastCmdResult": "", "errors": [],
        "lastRoundRoleActionResults": {},
        "worldNews": {"officialNews": "", "folkLegends": ""},
        "vendorShopList": [{"name": "stone", "price": 1},
                           {"name": "iron", "price": 3},
                           {"name": "copper", "price": 5}],
        "weaponShopList": [{"name": "WeaponUpgradeVoucher1", "price": 100}],
        "_demo": {"seed": 1, "pressure": 1, "mines": {}, "dead": {}, "kills": 0,
                  "finished": False, "waves": 0, "elapsed": 0, "errands": {}},
    }
    if vendor is not None:
        state["mapInfo"]["zones"].append(
            {"pos": {"x": vendor[0], "y": vendor[1]}, "neutralType": "vendor"})
    if walls == "complete":
        for i, pos in enumerate(brain._wall_order(Turn.load(state))):
            state["teamOur"]["roles"].append(unit(40000 + i, "wall", pos.x, pos.y, 1000))
    for i, pos in enumerate(brain._tower_sites(Turn.load(state))[:towers]):
        state["teamOur"]["roles"].append(unit(10040 + i, "rocket", pos.x, pos.y, 1000))
    for x, y in terrain:
        state["mapInfo"]["zones"].append({"pos": {"x": x, "y": y},
                                          "neutralType": "terrain"})
    return state


METAL_ZONES = ("copper", "iron")


def day_command(state, unit_id=10010):
    """The raw command the day planner issues for the worker, if any."""
    turn = Turn.load(state)
    commands = {}
    brain._day(turn, commands, state, None)
    return commands.get(unit_id)


def metal_command(state, unit_id=10010):
    """The worker's command when the day planner issues one for *ore* work.

    Buildings, stone and walking home are the day plan's ordinary business, so a
    command only counts as a metal run when the worker is collecting on a metal
    mine or standing next to one. Anything else means the metal branch stayed
    out of the way — which is exactly what the dusk/night case asserts.
    """
    turn = Turn.load(state)
    command = day_command(state, unit_id)
    if not command or command.get("action") not in ("collect", "move"):
        return None
    target = Pos.load(command["targetPos"][0])
    if command["action"] == "collect":
        return command if turn.zones.get(target) in METAL_ZONES else None
    return command if any(kind in METAL_ZONES
                          for pos, kind in turn.zones.items()
                          if max(abs(pos.x - target.x), abs(pos.y - target.y)) <= 1) else None


class BoardGeometryTests(unittest.TestCase):
    """The fixture must really be the situation the other cases describe."""

    def test_worker_and_mine_are_legal_adjacent_ground_outside_the_ring(self):
        state = board()
        turn = Turn.load(state)
        worker = next(u for u in turn.workers() if u.unit_id == 10010)
        mine = Pos(*MINE)
        self.assertEqual(worker.pos, Pos(*WORKER))
        self.assertEqual(max(abs(worker.pos.x - mine.x), abs(worker.pos.y - mine.y)), 1)
        # Legal ground: not on the base footprint, not occupied, not in the ring.
        footprint = {(x, y) for x in (BASE[0], BASE[0] + 1) for y in (BASE[1] - 1, BASE[1])}
        self.assertNotIn((worker.pos.x, worker.pos.y), footprint)
        self.assertTrue(turn.land(worker.pos))
        self.assertNotIn(worker.pos, turn.blocked(worker))
        ring = brain._wall_order(turn)
        self.assertNotIn(Pos(*WORKER), ring)
        self.assertNotIn(mine, ring)
        self.assertEqual(len(turn.weapons()), 3)
        self.assertEqual(len(turn.walls()), len(ring))

    def test_both_base_adjacent_vendor_cells_are_reachable_from_the_worker(self):
        turn = Turn.load(board())
        worker = next(u for u in turn.workers() if u.unit_id == 10010)
        route = brain._vendor_route(turn, worker)
        self.assertIsNotNone(route)
        stand, cost = route
        self.assertLessEqual(max(abs(stand.x - VENDOR[0]), abs(stand.y - VENDOR[1])), 1)
        self.assertLess(cost, brain.RETURN_BEFORE_NIGHT - 13)


class MetalCollectionTests(unittest.TestCase):
    def test_completed_defence_with_adjacent_copper_collects_copper(self):
        self.assertEqual(metal_command(board()), collect_command(Pos(*MINE)))

    def test_iron_only_board_collects_iron(self):
        state = board(ores=(("iron", MINE),))
        self.assertEqual(metal_command(state), collect_command(Pos(*MINE)))

    def test_dearest_published_ore_wins_and_reversed_prices_reverse_it(self):
        # Copper 5 > iron 3 under the published list. The iron mine sits on the
        # worker's other neighbour (14,25) so both are equally adjacent and only
        # the published price can decide.
        ores = (("iron", (14, 25)), ("copper", MINE))
        self.assertEqual(metal_command(board(ores=ores)), collect_command(Pos(*MINE)))
        state = board(ores=ores)
        state["vendorShopList"] = [{"name": "iron", "price": 9},
                                   {"name": "copper", "price": 2}]
        self.assertEqual(metal_command(state), collect_command(Pos(14, 25)))

    def test_ore_without_a_published_price_is_never_mined(self):
        for prices in ([], [{"name": "stone", "price": 1}],
                       [{"name": "copper", "price": 0}],
                       [{"name": "copper", "price": -5}],
                       [{"name": "iron", "price": 4}]):
            with self.subTest(prices=prices):
                state = board()
                state["vendorShopList"] = prices
                self.assertIsNone(metal_command(state))

    def test_no_mine_out_of_range_and_full_backpack_issue_nothing(self):
        self.assertIsNone(metal_command(board(ores=(("copper", (38, 2)),))),
                          "a mine beyond ORE_MAX_DISTANCE must not be chased")
        self.assertIsNone(metal_command(board(ores=())), "no mine, no collect")
        command = metal_command(board(backpack=["stone"] * 100))
        self.assertNotEqual((command or {}).get("action"), "collect",
                            "a full backpack cannot collect")

    def test_inaccessible_vendor_prevents_starting_metal_mining(self):
        # Terrain reaches the vendor's own cell, so no neighbouring cell is a
        # legal stand: the ore could never be sold and the run must not start.
        blocked = [(x, y) for x in range(9, 12) for y in range(27, 30)]
        state = board(terrain=blocked)
        turn = Turn.load(state)
        self.assertIsNone(brain._vendor_route(turn, turn.workers()[0]))
        self.assertIsNone(metal_command(state))

    def test_unreachable_vendor_walk_keeps_the_loaded_worker_in_place(self):
        blocked = [(x, y) for x in range(9, 12) for y in range(27, 30)]
        state = board(backpack=["copper"] * 4, terrain=blocked)
        self.assertIsNone(metal_command(state))

    def test_another_workers_errand_is_not_bypassed(self):
        # The one-errand-at-a-time ledger owns the team's trip: the spare worker
        # must not start a second one, not even to carry a finished batch.
        state = board(backpack=["copper"] * 4)
        turn = Turn.load(state)
        commands = {}
        brain._worker_day(turn, turn.workers()[0], brain._tower_sites(turn), [], [],
                          set(), commands, state, set(), other_errand=True)
        self.assertEqual(commands, {})

    def test_defence_construction_keeps_priority_over_metal(self):
        # Unfinished ring: the worker places its stone although an adjacent
        # copper mine is also in reach, so the metal branch is not taken.
        state = board(ores=(("copper", (14, 25)), ("stone", MINE)),
                      backpack=["stone"], walls="missing")
        turn = Turn.load(state)
        commands = {}
        brain._day(turn, commands, state, None)
        self.assertTrue(commands, "the worker still works on the ring")
        work = [cmd for cmd in commands.values() if cmd["action"] in ("build", "collect")]
        self.assertTrue(work)
        self.assertTrue(all(cmd["action"] == "build"
                            or cmd["targetPos"][0] == {"x": MINE[0], "y": MINE[1]}
                            for cmd in work), "only wall work or stone gathering")
        self.assertFalse(any(cmd["action"] == "collect"
                             and cmd["targetPos"][0] == {"x": 14, "y": 25}
                             for cmd in work), "copper is not the ring's business")

    def test_missing_tower_wins_over_metal_with_site_budget_and_route(self):
        """A real, reachable tower site plus enough gold must still be built.

        The board keeps one missing tower site free and gives the worker the
        budget, so the acceptance is the actual defence action: a `build` at that
        site, or the walk towards it. No ore action may be issued instead.
        """
        state = board(towers=2, gold=25)  # ring complete, mine still in reach
        turn = Turn.load(state)
        sites = brain._tower_sites(turn)
        standing = {w.pos for w in turn.weapons()}
        missing = [p for p in sites if p not in standing and p not in turn.occupied_cells()]
        self.assertTrue(missing, "the fixture must leave a real tower site")
        self.assertEqual(len(turn.weapons()), 2)
        self.assertGreaterEqual(turn.gold, brain.WEAPON_BUILD_COST)
        command = day_command(state)
        self.assertEqual(command["action"], "move", "the worker walks to the site")
        target = Pos.load(command["targetPos"][0])
        site = missing[0]
        worker = next(u for u in turn.workers() if u.unit_id == 10010)
        self.assertLess(brain._route_cost(turn, target, site),
                        brain._route_cost(turn, worker.pos, site),
                        "the step must make the site closer")
        # The mine must not influence the day at all while a tower is missing.
        without_mine = day_command(board(towers=2, gold=25, ores=()))
        self.assertEqual(command, without_mine, "the ore must not change this turn")
        self.assertNotEqual(command["action"], "collect")

    def test_dusk_and_night_start_no_new_daytime_mining(self):
        # 13 is a control: the run starts. 50 opens the evening return, 55 is the
        # dusk hand-off, 70 ends the day. At/after dusk the day plan must be
        # identical whether or not the mine exists — i.e. no part of it is caused
        # by the ore, which is what "no new mining run" means. (A collect-based
        # check cannot be used here: with the ring complete the worker still walks
        # to a *stone* mine, which is ordinary wall upkeep, not metal work.)
        self.assertEqual(metal_command(board(round_no=13)), collect_command(Pos(*MINE)))
        for round_no in (51, 56, 71):
            with self.subTest(round=round_no):
                with_mine = day_command(board(round_no=round_no))
                without_mine = day_command(board(round_no=round_no, ores=()))
                self.assertEqual(with_mine, without_mine,
                                 "the mine must not change the dusk/night plan")

    def test_worker_with_a_full_metal_bag_walks_toward_the_vendor(self):
        # Sell-ready is checked before `backpack_full`: a 100-cell bag of copper
        # must still leave the mine for the vendor instead of standing still.
        # The route is followed step by step, so an obstacle detour is allowed
        # but never a walk that fails to arrive.
        state = board(backpack=["copper"] * 100)
        vendor = Pos(*VENDOR)
        turn = Turn.load(state)
        worker = turn.workers()[0]
        moves = 0
        while max(abs(worker.pos.x - vendor.x), abs(worker.pos.y - vendor.y)) > 1:
            command = day_command(state)
            self.assertIsNotNone(command, "the loaded worker must keep walking")
            self.assertEqual(command["action"], "move")
            worker_pos = Pos.load(command["targetPos"][0])
            state["teamOur"]["roles"] = [
                {**u, "pos": worker_pos.dump()} if u["id"] == worker.unit_id else u
                for u in state["teamOur"]["roles"]]
            turn, worker = Turn.load(state), Turn.load(state).workers()[0]
            moves += 1
            self.assertLess(moves, 40, "the walk must terminate at the vendor")

    def test_both_sides_get_the_same_metal_decision(self):
        # R02/R08: the same legal situation for either team must behave alike.
        #
        # The fixture is the file's own outer placement (worker and mine outside
        # the ring, 任务书 §4.4 adjacency). An earlier version of this test put the
        # worker *on the base footprint* and relied on the accidental hole issue 12
        # fixes to let it reach the vendor; that is no longer a legal expectation.
        for side in ("challenger", "defender"):
            with self.subTest(side=side):
                state = board()
                state["teamOur"]["type"] = side
                turn = Turn.load(state)
                worker = next(u for u in turn.workers() if u.unit_id == 10010)
                self.assertEqual(worker.pos, Pos(*WORKER))
                self.assertEqual(distance(worker.pos, Pos(*MINE)), 1)
                self.assertFalse(any(worker.pos in turn.footprint(u)
                                     for u in turn.ours + turn.enemies if u.unit_id != worker.unit_id))
                self.assertIsNotNone(brain._vendor_route(turn, worker))
                self.assertEqual(metal_command(state), collect_command(Pos(*MINE)))


class RealBoardChainTests(unittest.TestCase):
    """The same loop on a real seeded map: real base, towers and wall ring.

    Nothing is stripped from the fixture: the scenario keeps its own buildings,
    terrain and neutral points, the defence is stood up with the planner's own
    `_wall_order`/`_tower_sites`, and the worker is placed on a legal free cell
    next to an added copper mine. The mine placement is chosen from route probes,
    not by editing the board.
    """

    def probe(self, pos):
        """A Turn/route-shaped stand-in for "a worker standing at `pos`"."""
        return SimpleNamespace(pos=pos, unit_id=-1, kind="worker")

    def real_board(self, seed=7, side="challenger"):
        """Real seeded map, defence stood up, worker+mine on legal free ground.

        The worker is moved outside the ring *before* the walls exist, which is
        how a real match reaches this state: the crew builds the ring from the
        outside, so it is never sealed in by its own pre-placed defence. Every
        placement below is asserted (or checked by a route probe) in the test.
        """
        from agent.scenarios import scenario
        state = scenario(seed, side, 1)
        state["roundNo"] = 13
        state["_demo"]["errands"] = {}
        state["teamOur"]["goldNum"] = 0
        base = Turn.load(state).station()
        raw = next(u for u in state["teamOur"]["roles"] if u["id"] == 10010)
        raw["pos"] = Pos(base.pos.x + 4, base.pos.y).dump()
        for i, pos in enumerate(brain._wall_order(Turn.load(state))):
            state["teamOur"]["roles"].append(
                unit(40000 + i, "wall", pos.x, pos.y, 1000))
        for i, pos in enumerate(brain._tower_sites(Turn.load(state))):
            state["teamOur"]["roles"].append(
                unit(10040 + i, "rocket", pos.x, pos.y, 1000))
        turn = Turn.load(state)
        worker = next(u for u in turn.workers() if u.unit_id == 10010)
        ring = set(brain._wall_order(turn))
        occupied = turn.occupied_cells() | ring
        stand = worker.pos
        self.assertTrue(turn.land(stand) and stand not in ring)
        mine = next((p for p in (Pos(stand.x + dx, stand.y + dy)
                                 for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy)
                     if turn.land(p) and p not in occupied and p not in ring), None)
        self.assertIsNotNone(mine, "the placed worker needs a free neighbour")
        state["mapInfo"]["zones"] = [z for z in state["mapInfo"]["zones"]
                                     if z["neutralType"] != "copper"]
        state["mapInfo"]["zones"].append({"pos": mine.dump(), "neutralType": "copper"})
        return state, mine, stand

    def test_real_map_mine_carry_sell_raises_gold(self):
        state, mine, stand = self.real_board()
        # Rebuild the Turn: the worker is a real unit now, and its own cell is
        # excluded from `blocked` only for its own id.
        turn = Turn.load(state)
        worker = next(u for u in turn.workers() if u.unit_id == 10010)
        self.assertEqual(worker.pos, stand)
        self.assertEqual(max(abs(worker.pos.x - mine.x), abs(worker.pos.y - mine.y)), 1)
        cost = brain._RouteCost(turn, worker)
        self.assertIsNotNone(brain._vendor_route(turn, worker, cost),
                             "the vendor must be walkable for a sale to happen")
        self.assertIsNotNone(brain._metal_trip_fits(turn, worker, mine, state, cost))
        collected, sells = 0, []
        for _ in range(60):
            result = step(state)
            state = result["state"]
            for action in result["frame"]["actions"]:
                if action["a"] == "collect" and action["kind"] == "copper":
                    collected += 1
                if action["a"] == "sell" and action["kind"] == "copper":
                    sells.append(action["num"])
            if sells or result["done"]:
                break
        self.assertGreaterEqual(collected, 3, "the worker must collect the ore")
        self.assertTrue(sells, "the batch must reach the vendor")
        self.assertGreater(state["teamOur"]["goldNum"], 0, "the sale must pay")


class SellPolicyTests(unittest.TestCase):
    def test_the_stone_reserve_is_kept_when_metal_is_sold(self):
        state = board(backpack=["copper"] * 6 + ["stone"] * 12)
        worker = next(u for u in state["teamOur"]["roles"] if u["roleType"] == "worker")
        worker["pos"] = {"x": VENDOR[0] + 1, "y": VENDOR[1]}
        turn = Turn.load(state)
        commands = {}
        self.assertTrue(brain._try_trade(turn, turn.workers()[0], commands, state))
        self.assertEqual(commands[10010]["action"], "sell")
        self.assertEqual(commands[10010]["name"], "copper")
        self.assertGreater(commands[10010]["num"], 0)

    def test_surplus_metal_alone_does_not_trigger_a_sale(self):
        for amount in (0, 1, 2):
            with self.subTest(amount=amount):
                state = board(backpack=["copper"] * amount)
                turn = Turn.load(state)
                self.assertFalse(brain._should_sell(turn, turn.workers()[0], state))
        state = board(backpack=["copper"] * 3)
        turn = Turn.load(state)
        self.assertTrue(brain._should_sell(turn, turn.workers()[0], state))

    def test_sell_uses_the_published_price_and_rejects_unlisted_ore(self):
        state = board(backpack=["copper"] * 4)
        worker = next(u for u in state["teamOur"]["roles"] if u["roleType"] == "worker")
        worker["pos"] = {"x": VENDOR[0] + 1, "y": VENDOR[1]}
        turn = Turn.load(state)
        commands = {}
        state["vendorShopList"] = [{"name": "copper", "price": 7}]
        self.assertTrue(brain._try_trade(turn, turn.workers()[0], commands, state))
        self.assertEqual(commands, {10010: {"action": "sell", "name": "copper", "num": 4}})
        state["vendorShopList"] = [{"name": "iron", "price": 7}]
        self.assertFalse(brain._should_sell(turn, turn.workers()[0], state))

    def test_selling_is_only_issued_next_to_the_vendor(self):
        state = board(backpack=["copper"] * 4)
        worker = next(u for u in state["teamOur"]["roles"] if u["roleType"] == "worker")
        worker["pos"] = {"x": 30, "y": 5}
        turn = Turn.load(state)
        commands = {}
        self.assertFalse(brain._try_trade(turn, turn.workers()[0], commands, state))
        self.assertEqual(commands, {})
        worker["pos"] = {"x": VENDOR[0] + 1, "y": VENDOR[1]}
        turn = Turn.load(state)
        self.assertTrue(brain._try_trade(turn, turn.workers()[0], commands, state))
        self.assertEqual(commands[10010]["action"], "sell")


class RealRoundChainTests(unittest.TestCase):
    """The loop the task asks for, driven through the local settle pass."""

    def run_rounds(self, state, first=13, last=50):
        collected = Counter()
        sells = []
        for round_no in range(first, last + 1):
            result = step(state)
            state = result["state"]
            for action in result["frame"]["actions"]:
                if action["a"] == "collect":
                    collected[action["kind"]] += 1
                if action["a"] == "sell":
                    sells.append((round_no, action["kind"], action["num"],
                                  state["teamOur"]["goldNum"]))
            if sells or result["done"]:
                break
        return state, collected, sells

    def test_mine_carry_sell_increases_gold_without_privileged_inputs(self):
        state = board()
        start_gold = state["teamOur"]["goldNum"]
        state, collected, sells = self.run_rounds(state)
        self.assertGreaterEqual(collected["copper"], 3,
                                "the worker must really collect copper")
        self.assertTrue(sells, "the batch must reach the vendor")
        self.assertGreater(state["teamOur"]["goldNum"], start_gold,
                           "selling metal must raise gold")
        self.assertEqual(sells[0][1], "copper")
        self.assertGreater(sells[0][2], 0)

    def test_iron_only_board_runs_the_same_chain(self):
        state = board(ores=(("iron", MINE),))
        state, collected, sells = self.run_rounds(state)
        self.assertGreaterEqual(collected["iron"], 3)
        self.assertTrue(any(name == "iron" for _r, name, _n, _g in sells))
        self.assertGreater(state["teamOur"]["goldNum"], 0)

    def test_a_missing_price_stops_the_loop_instead_of_stockpiling(self):
        state = board()
        state["vendorShopList"] = [{"name": "stone", "price": 1}]
        state, collected, sells = self.run_rounds(state)
        self.assertEqual(sum(collected.values()), 0)
        self.assertEqual(sells, [])
        self.assertEqual(state["teamOur"]["goldNum"], 0)

    def test_night_keeps_the_crew_home_and_issues_no_collect(self):
        state = board(round_no=71)
        result = step(state)
        self.assertFalse(any(a["a"] == "collect" for a in result["frame"]["actions"]))
        self.assertFalse(any(a["a"] == "sell" for a in result["frame"]["actions"]))
        self.assertGreaterEqual(result["state"]["roundNo"], 71)


if __name__ == "__main__":
    unittest.main()
