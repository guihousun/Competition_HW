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


class ErrorAndEnrichmentTests(unittest.TestCase):
    """Independent expectations for error categorization and bounded evidence."""

    def summary(self, request, response=None):
        return diagnostics.build_summary(request, response or {"roleCommandMap": {}})

    def test_mixed_error_codes_are_categorized_exactly(self):
        payload = observation([role(10013, "station", 1500)],
                              errors=[{"errorCode": 1}, {"errorCode": 2}, {"errorCode": 2},
                                      {"errorCode": 3}, {"errorCode": 4}, {"errorCode": 5},
                                      {"errorCode": 0}])
        summary = self.summary(payload)
        self.assertEqual(summary["judge_errors_total"], 7)
        self.assertEqual(summary["errors_by_code"], {0: 1, 1: 1, 2: 2, 3: 1, 4: 1, 5: 1})
        self.assertEqual(summary["command_errors"], 1)
        self.assertEqual(summary["task_errors"], 3)
        self.assertEqual(summary["network_errors"], 1)
        self.assertEqual(summary["llm_quota_errors"], 1)
        self.assertEqual(summary["unknown_errors"], 1)
        self.assertEqual(summary["protocol_errors"], 1)

    def test_task_and_answer_failures_are_not_protocol_errors(self):
        payload = observation([role(10013, "station", 1500)],
                              errors=[{"errorCode": 1}, {"errorCode": 2}])
        summary = self.summary(payload)
        self.assertEqual(summary["task_errors"], 2)
        self.assertEqual(summary["protocol_errors"], 0,
                         "a task timeout/answer error is not a protocol violation")

    def test_malformed_error_values_count_as_unknown(self):
        payload = observation([role(10013, "station", 1500)],
                              errors=[{"errorCode": 4}, {"errorCode": "2"}, {"errorCode": None},
                                      {"errorCode": 7}, {"errorCode": True}, {"errorCode": {"x": 1}},
                                      {"description": "no code"}])
        summary = self.summary(payload)
        self.assertEqual(summary["judge_errors_total"], 7)
        self.assertEqual(summary["command_errors"], 1)
        self.assertEqual(summary["task_errors"], 0)
        self.assertEqual(summary["unknown_errors"], 6)

    def test_absent_or_non_list_errors_stay_unknown_not_zero(self):
        absent = observation([role(10013, "station", 1500)])
        del absent["errors"]
        non_list = observation([role(10013, "station", 1500)], errors="boom")
        null = observation([role(10013, "station", 1500)], errors=None)
        for payload in (absent, non_list, null):
            summary = self.summary(payload)
            self.assertIsNone(summary["judge_errors_total"])
            self.assertIsNone(summary["errors_by_code"])
            self.assertIsNone(summary["protocol_errors"])

    def test_empty_error_list_is_an_observed_zero(self):
        summary = self.summary(observation([role(10013, "station", 1500)]))
        self.assertEqual(summary["judge_errors_total"], 0)
        self.assertEqual(summary["protocol_errors"], 0)

    def test_bounded_evidence_from_the_actual_request(self):
        roles = [role(10013, "station", 1500),
                 role(10020, "gatling", 1000, cooldown=0),
                 role(10030, "railgun", 1000, cooldown=3),
                 role(10010, "worker", 220),
                 role(10011, "pioneer", 200)]
        payload = observation(roles,
                              robot={"roles": [
                                  {"id": 1, "roleType": "smallRobot", "health": 40,
                                   "targetTeam": "challenger"},
                                  {"id": 2, "roleType": "largeRobot", "health": 2000,
                                   "targetTeam": "defender"}]})
        payload["teamOur"].update({"goldNum": 75, "totalScore": 280})
        payload["lastRoundRoleActionResults"] = {"10010": False, "10011": True}
        summary = self.summary(payload)
        self.assertEqual(summary["gold"], 75)
        self.assertEqual(summary["score"], 280)
        self.assertEqual(summary["weapon_counts"], {"gatling": 1, "railgun": 1, "rocket": 0})
        self.assertEqual(summary["team_roles_live"], 5)
        self.assertEqual(summary["robots_visible"], 2)
        self.assertEqual(summary["robots_targeting_us"], 1,
                         "global robots are not threats; only targetTeam is a link")
        self.assertEqual(summary["action_results_false"], 1)

    def test_missing_target_team_stays_unknown(self):
        payload = observation([role(10013, "station", 1500)],
                              robot={"roles": [{"id": 1, "roleType": "smallRobot", "health": 40}]})
        self.assertIsNone(self.summary(payload)["robots_targeting_us"])

    def test_decision_report_is_optional_and_bounded(self):
        payload = observation([role(10013, "station", 1500)])
        self.assertNotIn("decision", self.summary(payload))
        summary = diagnostics.build_summary(
            payload, {"roleCommandMap": {}},
            decision={"supervisor": {"mode": "cautious", "reason": "base_low",
                                     "reserve_pioneer": True, "return_steps_lower_bound": 4,
                                     "relevant_robots": 4, "ready_workers": 2,
                                     "visible_pressure_hp": 35},
                      "task": {"phase": "moving", "cycle_active": True, "plan_kind": "goto",
                               "pending_command": "move", "pending_prompt": False,
                               "acceptance_status": {"phase": "moving",
                                                     "point": {"x": 14, "y": 17},
                                                     "retry_after": 2, "reason": "cooldown"}}})
        report = summary["decision"]
        self.assertEqual(report["supervisor"]["mode"], "cautious")
        self.assertEqual(report["supervisor"]["relevant_robots"], 4)
        self.assertEqual(report["task"]["phase"], "moving")
        self.assertEqual(report["task"]["acceptance_status"]["point"], {"x": 14, "y": 17})
        self.assertEqual(report["task"]["acceptance_status"]["phase"], "moving")
        blob = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("note", blob)
        self.assertNotIn("api_key", blob)
        self.assertNotIn("unknown_key", blob)


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


class IntegratedDecisionTests(unittest.TestCase):
    def test_boolean_pending_state_and_advice_survive(self):
        from agent.diagnostics import build_summary
        report={'supervisor':{'mode':'watch','reserve_pioneer':False,'recommended_reserve':True},
                'task':{'pending_command':False,'pending_prompt':True}}
        result=build_summary({}, {'roleCommandMap':{}}, decision=report)
        self.assertEqual(result['decision'],report)
