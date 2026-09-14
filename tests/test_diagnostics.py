"""Focused tests for the runtime diagnostics module and its HTTP hook.

Independent expectations: fixtures are hand-written observations, not output of
the strategy or the simulator.

    python -m unittest discover -s tests -p test_diagnostics.py -v
"""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))
from agent import diagnostics  # noqa: E402


def role(rid, kind, health=100, **extra):
    data = {"id": rid, "roleType": kind, "health": health, "pos": {"x": 1, "y": 1}}
    data.update(extra)
    return data


def observation(roles, **extra):
    payload = {"roundNo": 85,
               "teamOur": {"type": "challenger", "roles": roles},
               "robot": {"roles": []},
               "errors": []}
    payload.update(extra)
    return payload


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.records = []
        diagnostics.set_summary_emitter(self.records.append)
        self.addCleanup(diagnostics.set_summary_emitter, None)

    def summary(self, request, response, **kwargs):
        return diagnostics.build_summary(request, response, **kwargs)

    def test_missing_health_is_unknown_not_dead(self):
        roles = [role(10013, "station", 1500),
                 {"id": 10010, "roleType": "worker", "pos": {"x": 1, "y": 1}},   # no health key
                 role(10011, "pioneer", 200)]
        summary = self.summary(observation(roles), {"roleCommandMap": {}})
        self.assertIsNone(summary["workers_live"])          # unknown, not 0
        self.assertEqual(summary["health_unknown"], 1)
        self.assertEqual(summary["pioneers_live"], 1)
        self.assertEqual(summary["base_health_state"], "observed_positive")
        self.assertEqual(summary["empty_reason"], "unclassified",
                         "missing health must not be reported as death")

    def test_observed_zero_health_is_dead(self):
        roles = [role(10013, "station", 0), role(10010, "worker", 0), role(10011, "pioneer", 220)]
        summary = self.summary(observation(roles), {"roleCommandMap": {}})
        self.assertEqual(summary["base_health_state"], "observed_zero")
        self.assertEqual(summary["base_hp"], 0)
        self.assertEqual(summary["workers_live"], 0)
        self.assertEqual(summary["empty_reason"], "base_observed_dead")

    def test_nonfinite_health_is_unknown(self):
        roles = [role(10013, "station", 1500), role(10010, "worker", float("nan"))]
        summary = self.summary(observation(roles), {"roleCommandMap": {}})
        self.assertIsNone(summary["workers_live"])
        self.assertGreaterEqual(summary["health_unknown"], 1)

    def test_missing_robot_data_is_not_cleared_robots(self):
        payload = observation([role(10013, "station", 1500)])
        del payload["robot"]
        summary = self.summary(payload, {"roleCommandMap": {}})
        self.assertIsNone(summary["robots_visible"])

    def test_missing_cooldown_is_not_ready(self):
        roles = [role(10013, "station", 1500), role(10010, "worker", 220),
                 role(10020, "gatling", 1000)]          # no cooldown key
        summary = self.summary(observation(roles), {"roleCommandMap": {}})
        self.assertIsNone(summary["weapons_ready"])
        self.assertEqual(summary["cooldown_unknown"], 1)

    def test_observed_zero_cooldown_is_ready(self):
        roles = [role(10013, "station", 1500), role(10010, "worker", 220),
                 role(10020, "gatling", 1000, cooldown=0)]
        summary = self.summary(observation(roles), {"roleCommandMap": {}})
        self.assertEqual(summary["weapons_ready"], 1)
        self.assertEqual(summary["controllers"]["issued_attacks"], [])

    def test_channel_only_response_is_distinguished_from_truly_empty(self):
        payload = observation([role(10013, "station", 1500), role(10011, "pioneer", 200)])
        channel = self.summary(payload, {"roleCommandMap": {}, "prompt": "question"})
        self.assertTrue(channel["has_prompt"])
        self.assertEqual(channel["empty_reason"], "channel_only")
        empty = self.summary(payload, {"roleCommandMap": {}})
        self.assertFalse(empty["has_prompt"])
        self.assertNotEqual(empty["empty_reason"], "channel_only")

    def test_parse_failure_and_planner_exception_are_labelled(self):
        parsed = self.summary(None, {"roleCommandMap": {}}, invalid_input=True)
        self.assertEqual(parsed["empty_reason"], "invalid_input")
        self.assertIsNone(parsed["round"])
        planner = self.summary(observation([role(10013, "station", 1500)]),
                               {"roleCommandMap": {}}, decision_exception="internal_error")
        self.assertEqual(planner["empty_reason"], "decision_exception")
        self.assertEqual(planner["decision_error"], "internal_error")

    def test_failed_action_flags_and_protocol_errors_stay_separate(self):
        payload = observation([role(10013, "station", 1500)],
                              lastRoundRoleActionResults={"10010": False, "10011": True},
                              errors=[{"errorCode": 4, "description": "x"}])
        summary = self.summary(payload, {"roleCommandMap": {}})
        self.assertEqual(summary["action_results_false"], 1)
        self.assertEqual(summary["protocol_errors"], 1)

    def test_command_tally_keeps_unknown_actions_out_of_named_buckets(self):
        payload = observation([role(10013, "station", 1500)])
        response = {"roleCommandMap": {"1": {"action": "move", "targetPos": [{"x": 1, "y": 1}]},
                                       "2": {"action": "teleport"},
                                       "3": {"nope": True}}}
        summary = self.summary(payload, response)
        self.assertEqual(summary["commands"], 3)
        self.assertEqual(summary["action_counts"], {"move": 1})
        self.assertEqual(summary["unknown_actions"], 2)

    def test_secret_sentinel_never_reaches_the_summary(self):
        sentinel = "sk-SECRET-SENTINEL-0123456789"
        payload = observation([role(10013, "station", 1500)],
                              lastCmdResult=sentinel,
                              worldNews={"officialNews": sentinel},
                              phaseTask=sentinel)
        response = {"roleCommandMap": {"1": {"action": "move", "targetPos": [{"x": 1, "y": 1}]}},
                    "prompt": sentinel, "executeCmd": sentinel}
        summary = self.summary(payload, response)
        blob = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn(sentinel, blob)
        self.assertNotIn("lastCmdResult", blob)
        self.assertNotIn("phaseTask", blob)

    def test_summary_does_not_mutate_the_observation(self):
        payload = observation([role(10013, "station", 1500), role(10010, "worker", 220)])
        before = json.dumps(payload, sort_keys=True)
        self.summary(payload, {"roleCommandMap": {}})
        self.assertEqual(json.dumps(payload, sort_keys=True), before)

    def test_task_items_are_reported_as_inventory_hint_only(self):
        roles = [role(10013, "station", 1500), role(10011, "pioneer", 200,
                                                    backpack=["AcientTablet"])]
        summary = self.summary(observation(roles), {"roleCommandMap": {}})
        self.assertEqual(summary["task_items_carriers"], 1)
        self.assertNotIn("task_answers_ready", summary)

    def test_emit_is_fail_open(self):
        def broken(_line):
            raise RuntimeError("sink is down")

        diagnostics.set_summary_emitter(broken)
        diagnostics.emit_response_summary({"kind": "response"})   # must not raise
        diagnostics.response_summary({"roundNo": 1}, {"roleCommandMap": {}})  # must not raise


class IdentityTests(unittest.TestCase):
    def test_verified_manifest_outranks_parent_git_commit(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "CoreGeek" / "Demo" / "CoreGeek"
            (entry / "src" / "agent").mkdir(parents=True)
            (entry / "src" / "agent" / "brain.py").write_text("TOWER_LOADOUT=('rocket',)\n", encoding="utf-8")
            for name in ("server.py", "protocol.py", "simulator.py"):
                (entry / "src" / "agent" / name).write_text("# fixture\n", encoding="utf-8")
            (entry / "main3.py").write_text("# fixture entry\n", encoding="utf-8")
            import hashlib
            files = []
            for rel in ("main3.py", "src/agent/brain.py", "src/agent/server.py",
                        "src/agent/protocol.py", "src/agent/simulator.py"):
                digest = hashlib.sha256((entry / rel).read_bytes()).hexdigest()
                files.append({"path": f"CoreGeek/Demo/CoreGeek/{rel}", "sha256": digest})
            commit = "b" * 40
            manifest = {"schema": diagnostics.MANIFEST_SCHEMA, "commit": commit,
                        "source_digest": hashlib.sha256(json.dumps(
                            {item["path"]: item["sha256"] for item in files},
                            sort_keys=True).encode()).hexdigest(),
                        "files": files}
            (root / "CoreGeek" / diagnostics.MANIFEST_NAME).write_text(
                json.dumps(manifest), encoding="utf-8")
            # A different repository commit must not be reported for the bundle.
            diagnostics.set_start_root(entry)
            self.addCleanup(diagnostics.set_start_root, None)
            record = diagnostics.startup_identity(entry="main3.py", root=entry)
            self.assertEqual(record["manifest"]["state"], "verified")
            self.assertEqual(record["code_commit"], commit)
            self.assertEqual(record["commit_source"], "manifest")

    def test_tampered_manifest_is_not_trusted(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "CoreGeek" / "Demo" / "CoreGeek"
            entry.mkdir(parents=True)
            (entry / "main3.py").write_text("x = 1\n", encoding="utf-8")
            (root / "CoreGeek" / diagnostics.MANIFEST_NAME).write_text(json.dumps({
                "schema": diagnostics.MANIFEST_SCHEMA, "commit": "c" * 40, "source_digest": "0" * 64,
                "files": [{"path": "CoreGeek/Demo/CoreGeek/main3.py", "sha256": "0" * 64}],
            }), encoding="utf-8")
            diagnostics.set_start_root(entry)
            self.addCleanup(diagnostics.set_start_root, None)
            record = diagnostics.startup_identity(entry="main3.py", root=entry)
            self.assertEqual(record["manifest"]["state"], "mismatch")
            self.assertIsNone(record["code_commit"])
            self.assertEqual(record["commit_source"], "unknown")

    def test_unsafe_manifest_paths_are_rejected_without_echo(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "CoreGeek"
            entry.mkdir()
            (entry / "main3.py").write_text("x = 1\n", encoding="utf-8")
            (entry / diagnostics.MANIFEST_NAME).write_text(json.dumps({
                "schema": diagnostics.MANIFEST_SCHEMA, "commit": "d" * 40, "source_digest": "0" * 64,
                "files": [{"path": "../../etc/passwd", "sha256": "0" * 64},
                          {"path": "C:/Windows/system32/cmd.exe", "sha256": "0" * 64},
                          {"path": "CoreGeek\\main3.py", "sha256": "0" * 64}],
            }), encoding="utf-8")
            diagnostics.set_start_root(entry)
            self.addCleanup(diagnostics.set_start_root, None)
            state = diagnostics.load_manifest(entry)
            self.assertEqual(state["state"], "mismatch")
            blob = json.dumps(state, ensure_ascii=False)
            self.assertNotIn("passwd", blob)
            self.assertNotIn("cmd.exe", blob)

    def test_missing_manifest_in_repo_checkout_uses_git_commit(self):
        diagnostics.set_start_root(ROOT / "Demo" / "CoreGeek")
        self.addCleanup(diagnostics.set_start_root, None)
        record = diagnostics.startup_identity(entry="main3.py", root=ROOT / "Demo" / "CoreGeek")
        self.assertEqual(record["manifest"]["state"], "absent")
        self.assertEqual(record["commit_source"], "git_checkout")
        self.assertRegex(record["code_commit"] or "", r"^[0-9a-f]{40}$")
        self.assertEqual(record["policy"]["tower_loadout"], ["rocket", "rocket", "rocket"])


if __name__ == "__main__":
    unittest.main()
