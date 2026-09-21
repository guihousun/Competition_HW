"""Independent checks for finite self-evolution task opportunities.

These tests use hand-authored task endings and expected counters. They do not
derive the expected opportunity budget or cooldown result from taskworld's
implementation.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Demo/CoreGeek/src"))
from agent import taskworld
from agent.scenarios import scenario


class TaskWorldLimitTests(unittest.TestCase):
    def setUp(self):
        self.state = scenario(240917, "challenger")
        self.world = self.state["_demo"]["task_world"]
        self.kind = "challengerTaskPoint1"
        self.book = self.world["points"][self.kind]
        point = next(z for z in self.state["mapInfo"]["zones"]
                     if z["neutralType"] == self.kind)
        pioneer = next(r for r in self.state["teamOur"]["roles"]
                       if r["roleType"] == "pioneer")
        pioneer["pos"] = dict(point["pos"])
        self.pioneer_id = str(pioneer["id"])
        self.state["lastRoundRoleActionResults"] = {}
        self.state["_engine"] = {"lastCommands": {}}

    def test_new_world_records_current_observed_profile(self):
        rules = self.world["rules"]
        self.assertEqual(rules["profile"], taskworld.OBSERVED_PROFILE)
        self.assertEqual(rules["tasks_per_point"], 3)
        self.assertEqual(rules["timeout_rounds"], 15)
        self.assertEqual(rules["score_reward"], 80)
        self.assertEqual(rules["gold_reward"], 80)

    def test_legacy_profile_requires_explicit_opt_in(self):
        state = {"_demo": {"seed": 7,
                           "task_world_profile": taskworld.LEGACY_PROFILE}}
        world = taskworld.new_world(state)
        self.assertEqual(world["rules"]["profile"], taskworld.LEGACY_PROFILE)
        self.assertEqual(world["points"]["challengerTaskPoint1"]["tasks_left"], 30)

    def test_attach_does_not_replace_an_old_loaded_world(self):
        old_world = {"seed": 7, "points": {
            "challengerTaskPoint1": {"tasks_left": 11, "cooldown": 4,
                                      "active": None},
        }}
        self.state["_demo"]["task_world"] = old_world
        taskworld.attach(self.state)
        self.assertIs(self.state["_demo"]["task_world"], old_world)
        self.assertNotIn("rules", old_world)

    def end_one_task(self):
        """End one independently specified correct task at this point."""
        self.state["roundNo"] += 1
        self.world.pop("judged_round", None)
        self.book["active"] = {
            "type": "自进化类1", "accepted": self.state["roundNo"] - 1,
            "deadline": self.state["roundNo"] + 10, "timeout": 15,
            "description": "fixture task", "answer": "result=ok",
            "score": 80, "gold": 80, "best_rate": 0.0,
            "best_answer": "", "pending_answer": "result=ok",
        }
        report = taskworld.advance(self.state, self.world, [])
        self.assertEqual(report["ended"], "completed")
        return report

    def drain_refresh(self):
        for _ in range(taskworld.REFRESH_ROUNDS):
            self.state["roundNo"] += 1
            self.state["lastRoundRoleActionResults"] = {}
            self.state["_engine"]["lastCommands"] = {}
            taskworld.advance(self.state, self.world, [])

    def test_three_endings_exhaust_one_point(self):
        for ending in range(3):
            if self.book["cooldown"]:
                self.drain_refresh()
            self.end_one_task()
            self.assertEqual(self.book["tasks_left"], 2 - ending)

        self.assertEqual(self.book["tasks_left"], 0)
        self.assertEqual(self.book["cooldown"], taskworld.REFRESH_ROUNDS)
        self.state["roundNo"] += 1
        self.state["lastRoundRoleActionResults"] = {self.pioneer_id: True}
        self.state["_engine"]["lastCommands"] = {
            self.pioneer_id: {"action": "acceptTask"}}
        report = taskworld.advance(self.state, self.world, [])
        self.assertIsNone(report["accepted"])
        self.assertEqual(self.book["tasks_left"], 0)

    def test_thirty_round_cooldown_does_not_restore_opportunity(self):
        self.end_one_task()
        self.assertEqual(self.book["tasks_left"], 2)
        self.drain_refresh()
        self.assertEqual(self.book["cooldown"], 0)
        self.assertEqual(self.book["tasks_left"], 2)
        point = next(z for z in self.state["mapInfo"]["zones"]
                     if z["neutralType"] == self.kind)
        row = next(r for r in taskworld.player_tasks(
            self.state, self.world, "challenger")
                   if r["taskPosition"] == point["pos"])
        self.assertTrue(row["isValid"])

    def test_opportunity_consumption_is_per_point(self):
        self.end_one_task()
        other = self.world["points"]["challengerTaskPoint2"]
        self.assertEqual(other["tasks_left"], taskworld.TASKS_PER_POINT)
        self.assertEqual(other["cooldown"], 0)


if __name__ == "__main__":
    unittest.main()
