"""Vision tests: the rule boundaries of 任务书 §4.3.

Hand-computed expectations:

* shared vision is Chebyshev distance 4, so a unit 4 cells away is visible and one
  5 cells away is not;
* enemy bases and walls are visible from anywhere (e.g. 40 cells away);
* robots are visible from anywhere;
* a hidden enemy must not appear anywhere in the observation handed to the policy.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))

from agent import vision  # noqa: E402
from agent.protocol import Pos, VISION_DISTANCE  # noqa: E402
from agent.scenarios import observation  # noqa: E402
from agent.simulator import step  # noqa: E402


def state_with(*, watch_pos=(10, 10), enemies=(), robots=()):
    """A minimal request shape with one station we own and given enemies."""
    return {
        "roundNo": 1,
        "mapInfo": {"width": 41, "height": 32, "zones": []},
        "teamOur": {
            "type": "challenger", "goldNum": 75, "totalScore": 0,
            "roles": [
                {"id": 10013, "roleType": "station", "pos": {"x": watch_pos[0], "y": watch_pos[1]},
                 "health": 1500, "level": 1},
            ],
        },
        "teamEnemy": {"roles": list(enemies)},
        "robot": {"roles": list(robots)},
    }


def enemy(role_type, x, y, unit_id=20010, health=1000):
    return {"id": unit_id, "roleType": role_type, "pos": {"x": x, "y": y}, "health": health}


class VisibilityTests(unittest.TestCase):
    def test_vision_distance_is_four_and_chebyshev(self):
        watchers = [Pos(10, 10)]
        self.assertEqual(VISION_DISTANCE, 4)
        # (14,14) is Chebyshev 4 → visible; (15,10) is 5 → hidden.
        self.assertTrue(vision.is_visible(enemy("worker", 14, 14), watchers))
        self.assertFalse(vision.is_visible(enemy("worker", 15, 10), watchers))
        # Diagonal 3 is inside; a Euclidean reading would also pass, so check the
        # square shape explicitly with (14,10) and (10,14).
        self.assertTrue(vision.is_visible(enemy("worker", 14, 10), watchers))
        self.assertTrue(vision.is_visible(enemy("worker", 10, 14), watchers))

    def test_enemy_base_and_wall_are_globally_visible(self):
        watchers = [Pos(0, 0)]
        self.assertTrue(vision.is_visible(enemy("station", 40, 31), watchers))
        self.assertTrue(vision.is_visible(enemy("wall", 40, 31), watchers))
        self.assertEqual(vision.visibility_reason(enemy("station", 40, 31), watchers),
                         vision.REASON_GLOBAL_TYPE)

    def test_robots_are_globally_visible(self):
        # Robots are not in teamEnemy normally; the rule is checked through the
        # same helper so the simulator can rely on one definition.
        watchers = [Pos(0, 0)]
        far_robot = {"id": 1, "roleType": "smallRobot", "pos": {"x": 40, "y": 31}, "health": 40}
        self.assertTrue(vision.is_visible(far_robot, watchers, kind_field="roleType"))

    def test_dead_friendlies_do_not_grant_vision(self):
        payload = state_with()
        payload["teamOur"]["roles"][0]["health"] = 0
        self.assertEqual(vision.friendly_cells(payload), [])

    def test_every_friendly_unit_is_a_watcher(self):
        payload = state_with(enemies=[enemy("worker", 20, 20)])
        payload["teamOur"]["roles"].append(
            {"id": 10010, "roleType": "worker", "pos": {"x": 20, "y": 24}, "health": 220})
        # Only the second unit is close enough to see (20,20): distance 4.
        self.assertTrue(vision.is_visible(payload["teamEnemy"]["roles"][0],
                                          vision.friendly_cells(payload)))


class FilterTests(unittest.TestCase):
    def test_hidden_enemy_is_removed_and_visible_one_kept(self):
        payload = state_with(enemies=[enemy("worker", 12, 10, unit_id=20010),
                                      enemy("worker", 30, 30, unit_id=20011)])
        trimmed = vision.filter_observation(payload)
        self.assertEqual([u["id"] for u in trimmed["teamEnemy"]["roles"]], [20010])

    def test_filter_does_not_mutate_the_input(self):
        payload = state_with(enemies=[enemy("worker", 30, 30)])
        vision.filter_observation(payload)
        self.assertEqual(len(payload["teamEnemy"]["roles"]), 1,
                         "过滤不能改动传入的状态")

    def test_observation_applies_vision(self):
        payload = state_with(enemies=[enemy("station", 40, 31, unit_id=20013),
                                      enemy("worker", 30, 30, unit_id=20010)])
        seen = observation(payload)
        self.assertEqual([u["id"] for u in seen["teamEnemy"]["roles"]], [20013],
                         "远处敌方基地可见、远处敌方角色不可见")

    def test_observation_still_hides_private_state(self):
        payload = state_with()
        payload["_demo"] = {"seed": 1}
        payload["_engine"] = {"lastCommands": {}}
        seen = observation(payload)
        self.assertNotIn("_demo", seen)
        self.assertNotIn("_engine", seen)

    def test_contact_report_counts_hidden_units(self):
        payload = state_with(enemies=[enemy("worker", 12, 10, unit_id=20010),
                                      enemy("worker", 30, 30, unit_id=20011),
                                      enemy("station", 40, 31, unit_id=20013)])
        report = vision.contact_report(payload)
        self.assertEqual(report["enemyTotal"], 3)
        self.assertEqual(report["enemyVisible"], 2, "近处角色 + 远处基地")
        self.assertEqual(report["enemyHidden"], 1)
        self.assertEqual(report["visionDistance"], 4)

    def test_simulator_keeps_hidden_enemies_out_of_the_policy_view(self):
        """A local round must not hand the strategy a god view."""
        payload = state_with(enemies=[enemy("station", 40, 31, unit_id=20013),
                                      enemy("worker", 30, 30, unit_id=20010)])
        result = step(payload)
        seen = observation(result["state"])
        ids = [u["id"] for u in seen["teamEnemy"]["roles"]]
        self.assertIn(20013, ids)
        self.assertNotIn(20010, ids)


class SideViewTests(unittest.TestCase):
    """One policy engine must be able to drive either team."""

    def world(self):
        return {
            "roundNo": 5,
            "mapInfo": {"width": 41, "height": 32, "zones": []},
            "teamOur": {"type": "challenger", "goldNum": 75, "totalScore": 3,
                        "roles": [{"id": 10013, "roleType": "station",
                                   "pos": {"x": 10, "y": 10}, "health": 1500, "level": 1},
                                  {"id": 10010, "roleType": "worker",
                                   "pos": {"x": 9, "y": 10}, "health": 220,
                                   "backPackCapability": 100, "backpack": []}]},
            "teamEnemy": {"type": "defender", "goldNum": 50, "totalScore": 7,
                          "roles": [{"id": 20013, "roleType": "station",
                                     "pos": {"x": 30, "y": 20}, "health": 1500, "level": 1},
                                    {"id": 20010, "roleType": "worker",
                                     "pos": {"x": 35, "y": 30}, "health": 220,
                                     "backPackCapability": 100, "backpack": []}]},
            "_demo": {"seed": 1},
        }

    def test_own_side_view_keeps_our_team_and_hides_private_state(self):
        view = vision.side_view(self.world(), "challenger")
        self.assertEqual(view["teamOur"]["goldNum"], 75)
        self.assertNotIn("_demo", view)
        # The far enemy worker is invisible, the enemy base is not.
        self.assertEqual([u["id"] for u in view["teamEnemy"]["roles"]], [20013])

    def test_other_side_view_swaps_the_teams(self):
        view = vision.side_view(self.world(), "defender")
        self.assertEqual(view["teamOur"]["goldNum"], 50)
        self.assertEqual(view["teamOur"]["totalScore"], 7)
        self.assertEqual(view["teamOur"]["type"], "defender")
        self.assertEqual(view["teamEnemy"]["type"], "challenger")
        # The defender sees the challenger base (global) but not its gold.
        self.assertEqual([u["roleType"] for u in view["teamEnemy"]["roles"]], ["station"])

    def test_both_sides_get_a_usable_turn(self):
        from agent.protocol import Turn
        from agent.brain import plan_for_state
        from agent.planner import PlannerState
        for side in ("challenger", "defender"):
            view = vision.side_view(self.world(), side)
            turn = Turn.load(view)
            self.assertEqual(turn.station().unit_id,
                             10013 if side == "challenger" else 20013)
            commands = plan_for_state(view, PlannerState(), judge_tasks=False).commands
            self.assertTrue(commands, f"{side} 应该能产出指令")


if __name__ == "__main__":
    unittest.main()
