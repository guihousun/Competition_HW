"""Robot behaviour tests (任务书 §4.7).

The rule that shapes every defence: "机器人会攻击阻挡其移动的单位（包括角色/建筑）".
A robot blocked by a wall attacks *that wall*, so a wall ring is a delaying shield
rather than an absolute one, and a wave can eventually break through to the base.

These use a small hand-built state so the expectation is computable by eye: one
small robot (5 damage, melee range 1), one wall directly between it and the base.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))

from agent.protocol import Pos, Turn, distance  # noqa: E402
from agent.simulator import ATTACKABLE_BUILDING_KINDS, step  # noqa: E402


def board(robot_pos=(6, 5), robot_kind="smallRobot", wall_pos=None, station=(10, 5)):
    """A night board: one station, optional wall, one robot to the west."""
    zones = [{"neutralType": "challengerBase", "pos": {"x": station[0], "y": station[1]}}]
    roles = [{"id": 10013, "roleType": "station", "health": 1500, "level": 1,
              "pos": {"x": station[0], "y": station[1]}}]
    if wall_pos is not None:
        # The base is a 2x2 footprint; the wall is placed clear of it.
        zones.append({"neutralType": "wall", "pos": {"x": wall_pos[0], "y": wall_pos[1]}})
        roles.append({"id": 40000, "roleType": "wall", "health": 1000, "level": 1,
                      "pos": {"x": wall_pos[0], "y": wall_pos[1]}})
    return {
        "roundNo": 80,  # night (rounds 71..130 of a day)
        "mapInfo": {"width": 41, "height": 32, "zones": zones},
        "teamOur": {"type": "challenger", "goldNum": 75, "totalScore": 0, "roles": roles},
        "teamEnemy": {"type": "defender", "roles": []},
        "robot": {"roles": [{"id": 307100, "roleType": robot_kind,
                             "health": 40, "pos": {"x": robot_pos[0], "y": robot_pos[1]},
                             "targetTeam": "challenger", "abnormalState": ""}]},
    }


def health_of(state, role_type):
    return [int(r["health"]) for r in state["teamOur"]["roles"]
            if r["roleType"] == role_type]


class BuildingAttackTests(unittest.TestCase):
    def test_robot_search_radius_is_not_a_ranged_attack(self):
        state = board(robot_pos=(6, 5), wall_pos=None)
        state['teamOur']['roles'].append({
            'id': 10010, 'roleType': 'worker', 'health': 100,
            'level': 1, 'pos': {'x': 8, 'y': 5},
        })
        first = step(state, external_response={'roleCommandMap': {}})
        self.assertEqual(first['frame'].get('robotAttacks'), [])
        self.assertTrue(first['frame'].get('robotMoves'))
        self.assertEqual(first['frame']['robotMoves'][0]['to'], {'x': 7, 'y': 4})
        second = step(first['state'], external_response={'roleCommandMap': {}})
        self.assertEqual(second['frame']['robotAttacks'][0]['victim'], 10010)

    def test_building_kinds_are_named_explicitly(self):
        self.assertIn("wall", ATTACKABLE_BUILDING_KINDS)
        self.assertIn("station", ATTACKABLE_BUILDING_KINDS)
        self.assertIn("gatling", ATTACKABLE_BUILDING_KINDS)
        self.assertNotIn("stone", ATTACKABLE_BUILDING_KINDS)
        self.assertNotIn("land", ATTACKABLE_BUILDING_KINDS)

    def test_robot_attacks_the_wall_that_blocks_it(self):
        """Small robot damage is 5, so one round must cost the wall exactly 5."""
        state = board(robot_pos=(6, 5), wall_pos=(7, 5))
        result = step(state)
        self.assertEqual(health_of(result["state"], "wall"), [995],
                         '被围墙挡住的机器人应当攻击这堵墙（伤害 5）')
        attack = [record for record in result["frame"].get("robotAttacks") or []
                  if record.get("building")]
        self.assertTrue(attack, '应当记录一条"攻击建筑"的机器人行为')
        self.assertEqual(attack[0]["damage"], 5)

    def test_robot_does_not_attack_across_a_clear_path(self):
        """With the way open, the robot closes in instead of hitting scenery."""
        state = board(robot_pos=(6, 5), wall_pos=None)
        result = step(state)
        self.assertEqual(health_of(result["state"], "wall"), [])
        moved = [record for record in result["frame"].get("robotMoves") or []]
        self.assertTrue(moved, '没有阻挡时机器人应当移动')

    def test_wall_absorbs_the_base_damage(self):
        state = board(robot_pos=(6, 5), wall_pos=(7, 5))
        result = step(state)
        self.assertEqual(health_of(result["state"], "station"), [1500],
                         '有墙挡着时基地不应掉血')

    def test_wall_damage_accumulates_each_night_round(self):
        """A small robot deals 5 per round; the wall keeps the damage across rounds."""
        state = board(robot_pos=(6, 5), wall_pos=(7, 5))
        for _ in range(4):
            state = step(state)["state"]
        self.assertEqual(health_of(state, "wall"), [980],
                         '四个夜晚回合应当累计 20 点伤害')

    def test_wall_is_destroyed_and_reported(self):
        """A wall at 20 health takes four 5-damage hits and is then destroyed.

        Night ends at dawn and clears the robots (任务书 §4.7.3), so a full-health
        wall cannot be broken inside one night by a single small robot — the test
        weakens the wall instead of waiting out 200 rounds.
        """
        state = board(robot_pos=(6, 5), wall_pos=(7, 5))
        for role in state["teamOur"]["roles"]:
            if role["roleType"] == "wall":
                role["health"] = 20
        destroyed = False
        for _ in range(4):
            result = step(state)
            state = result["state"]
            if any("被机器人摧毁" in event for event in result["events"]):
                destroyed = True
                break
        self.assertTrue(destroyed, '围墙血量耗尽时应当被摧毁并记录事件')
        self.assertEqual(health_of(state, "wall"), [0])

    def test_destroyed_wall_no_longer_blocks_movement(self):
        """A destroyed wall's cell becomes walkable ground.

        The robot may well route *around* the cleared cell — it only ever steps to
        a cell closer to its target — so the assertions are about the ground, not
        about the robot's chosen path.
        """
        state = board(robot_pos=(6, 5), wall_pos=(7, 5))
        for role in state["teamOur"]["roles"]:
            if role["roleType"] == "wall":
                role["health"] = 5  # one hit kills it
        state = step(state)["state"]
        self.assertEqual(health_of(state, "wall"), [0], '围墙应当已被摧毁')
        self.assertNotIn({"x": 7, "y": 5},
                         [zone["pos"] for zone in state["mapInfo"]["zones"]],
                         '被摧毁的围墙不应再占着地图格')
        turn = Turn.load(state)
        self.assertTrue(turn.land(Pos(7, 5)), '被摧毁的围墙那一格应当可以通行')
        self.assertNotIn(Pos(7, 5), turn.blocked(turn.robots[0]),
                         '被摧毁的围墙不应再阻挡移动')
        # And the robot keeps closing on the station instead of standing still.
        robot_before = Pos.load(state["robot"]["roles"][0]["pos"])
        station = Pos.load(state["teamOur"]["roles"][0]["pos"])
        state = step(state)["state"]
        robot_after = Pos.load(state["robot"]["roles"][0]["pos"])
        self.assertLess(distance(robot_after, station), distance(robot_before, station),
                        '围墙被打掉后机器人应当继续逼近基地')

    def test_dead_robot_does_nothing(self):
        state = board(robot_pos=(7, 5), wall_pos=(8, 5))
        state["robot"]["roles"][0]["health"] = 0
        result = step(state)
        self.assertEqual(health_of(result["state"], "wall"), [1000])

    def test_dizzy_robot_does_nothing(self):
        state = board(robot_pos=(7, 5), wall_pos=(8, 5))
        state["robot"]["roles"][0]["abnormalState"] = "dizzy"
        result = step(state)
        self.assertEqual(health_of(result["state"], "wall"), [1000])

    def test_day_rounds_have_no_robot_activity(self):
        state = board(robot_pos=(7, 5), wall_pos=(8, 5))
        state["roundNo"] = 10  # day
        result = step(state)
        self.assertEqual(health_of(result["state"], "wall"), [1000],
                         '白天机器人不应行动')
        self.assertEqual(result["frame"].get("robotAttacks"), [])

    def test_neutral_scenery_is_not_attacked(self):
        """A mine blocks movement, but §4.7.3 names units and buildings only."""
        state = board(robot_pos=(6, 5), wall_pos=None)
        state["mapInfo"]["zones"].append(
            {"neutralType": "stone", "pos": {"x": 7, "y": 5}})
        result = step(state)
        self.assertFalse([record for record in result["frame"].get("robotAttacks") or []
                          if record.get("building")],
                         '矿区不应被当作可攻击建筑')
        self.assertEqual(result["state"]["mapInfo"]["zones"][-1]["neutralType"], "stone")


if __name__ == "__main__":
    unittest.main()
