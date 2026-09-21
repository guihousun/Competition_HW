"""Focused checks for the public-observation manual strategy lab."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import manual_strategy_lab as lab  # noqa: E402


class ManualStrategyLabTests(unittest.TestCase):
    def session(self):
        directory = tempfile.TemporaryDirectory()
        lab.init_session(directory.name)
        self.addCleanup(directory.cleanup)
        return directory

    def test_init_uses_public_projection_and_explicit_fixture_contract(self):
        directory = self.session()
        result = lab.observe_session(directory.name)
        observation = result["observation"]
        encoded = json.dumps(observation, ensure_ascii=False)
        self.assertNotIn("_demo", encoded)
        self.assertNotIn("_engine", encoded)
        self.assertNotIn("sandbox_fixture", encoded)
        self.assertEqual(result["summary"]["difficulty"], "medium")
        self.assertEqual(result["summary"]["seed"], 311)
        tasks = observation["teamOur"]["playerTasks"]
        self.assertEqual([(t["scoreReward"], t["goldReward"], t["timeoutRounds"])
                          for t in tasks], [(80, 80, 15), (80, 80, 15)])
        self.assertEqual(result["summary"]["task_fixture"]["outcomes"],
                         {"type1": "failed", "type2": "two_of_three"})
        self.assertFalse(result["summary"]["official_pass"])

    def test_step_records_public_observation_action_and_partial_audit(self):
        directory = self.session()
        result = lab.step_session(directory.name, {"999": {"action": "move"}})
        self.assertTrue(result["feedback"]["audit_errors"])
        self.assertIn("unknown/dead issuer", result["feedback"]["audit_errors"])
        trace = (Path(directory.name) / "trace.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("_demo", trace)
        self.assertNotIn("answer", trace)
        row = json.loads(trace)
        self.assertEqual(row["action"]["999"]["action"], "move")
        self.assertIn("action_results", row["feedback"])
        self.assertEqual(result["observation"]["roundNo"], 2)

    def test_handler_receives_only_public_observation_and_is_bounded(self):
        directory = self.session()
        handler_file = Path(directory.name) / "handler.py"
        handler_file.write_text(
            "def choose(observation):\n"
            "    assert '_demo' not in observation\n"
            "    return {}\n", encoding="utf-8")
        result = lab.run_session(directory.name, f"{handler_file}:choose", 2)
        self.assertEqual(result["summary"]["rounds"], 2)
        self.assertEqual(result["observation"]["roundNo"], 3)

    def test_source_freeze_rejects_changed_implementation(self):
        directory = self.session()
        changed = dict(lab.source_hashes())
        changed["benchmark.py"] = "changed"
        with patch.object(lab, "source_hashes", return_value=changed):
            with self.assertRaisesRegex(RuntimeError, "source changed"):
                lab.observe_session(directory.name)

    def test_fixed_holdout_and_both_sides(self):
        for side in ("challenger", "defender"):
            state = lab._new_state(lab.HOLDOUT_SEED, side)
            self.assertEqual(state["_demo"]["profile"], "observed-seven-days")
            self.assertEqual(state["_demo"]["map_layout"]["id"], "attack-map-observed-v1")
            self.assertEqual(state["teamOur"]["type"], side)


if __name__ == "__main__":
    unittest.main()
