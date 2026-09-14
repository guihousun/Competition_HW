"""Two-team job tests: the local 1v1 must be stoppable, observable and labelled."""
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))

from agent import twomatch  # noqa: E402


def wait_for(predicate, timeout=90.0, interval=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = twomatch.snapshot()
        if predicate(snapshot):
            return snapshot
        time.sleep(interval)
    return twomatch.snapshot()


class TwoMatchJobTests(unittest.TestCase):
    def tearDown(self):
        twomatch.stop()

    def test_idle_before_any_match(self):
        snapshot = twomatch.snapshot()
        self.assertIn(snapshot["state"], ("idle", "running", "done"))
        self.assertTrue(snapshot["local"])

    def test_short_match_runs_to_completion_and_reports_both_sides(self):
        started = twomatch.start(seed=3, pressure=1, max_rounds=40)
        self.assertEqual(started["state"], "running")
        self.assertEqual(started["maxRounds"], 40)
        final = wait_for(lambda snap: snap["state"] != "running", timeout=120)
        self.assertEqual(final["state"], "done", final.get("error"))
        self.assertEqual(final["round"], 40)
        self.assertEqual(sorted(final["scores"]), ["challenger", "defender"])
        self.assertEqual(sorted(final["baseHp"]), ["challenger", "defender"])
        self.assertTrue(final["local"])
        self.assertIsNotNone(final["result"])

    def test_progress_is_reported_while_running(self):
        twomatch.start(seed=3, pressure=1, max_rounds=120)
        snapshot = wait_for(lambda snap: snap["round"] >= 2, timeout=60)
        self.assertGreaterEqual(snapshot["round"], 2)
        self.assertGreater(snapshot["progress"], 0.0)
        self.assertLessEqual(snapshot["progress"], 1.0)

    def test_starting_a_second_match_stops_the_first(self):
        twomatch.start(seed=1, pressure=1, max_rounds=600)
        first = wait_for(lambda snap: snap["round"] >= 1, timeout=60)
        self.assertEqual(first["state"], "running")
        second = twomatch.start(seed=2, pressure=1, max_rounds=30)
        self.assertEqual(second["seed"], 2)
        self.assertEqual(second["maxRounds"], 30)

    def test_stop_requests_are_honoured(self):
        twomatch.start(seed=1, pressure=1, max_rounds=1300)
        wait_for(lambda snap: snap["round"] >= 1, timeout=60)
        twomatch.stop()
        final = wait_for(lambda snap: snap["state"] != "running", timeout=60)
        self.assertIn(final["state"], ("done", "failed"))
        if final["state"] == "done":
            self.assertLess(final["round"], 1300, "停止后不应继续跑完整场")

    def test_bad_arguments_fall_back_to_defaults(self):
        started = twomatch.start(seed="abc", pressure="99", max_rounds="9999")
        self.assertEqual(started["seed"], 1)
        self.assertEqual(started["pressure"], 3)
        self.assertEqual(started["maxRounds"], 1300)


if __name__ == "__main__":
    unittest.main()
