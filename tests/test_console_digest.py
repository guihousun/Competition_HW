"""Independent expectations for the bounded console digest (Issues 14-17).

Fixtures are hand-written summary rows, not output of the strategy or the
simulator. The digest is observation-only: these tests pin aggregation,
transition and conservation behaviour, and that console failure can never
change an HTTP response or the JSONL trace.

    python -m unittest discover -s tests -p test_console_digest.py -v
"""
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))
sys.path.insert(0, str(ROOT / "tools"))
from agent import console_digest as cd, diagnostics, server  # noqa: E402
import trace_tool  # noqa: E402


def row(round_no, **extra):
    data = {"kind": "response", "round": round_no, "phase": "day", "commands": 1,
            "action_counts": {"move": 1}, "empty_reason": None, "plan_ms": 1.0}
    data.update(extra)
    return data


def tokens(line):
    out = {}
    for part in line.split():
        if "=" in part:
            key, value = part.split("=", 1)
            out[key] = value
    return out


class AggregationTests(unittest.TestCase):
    def digest(self, **kwargs):
        return cd.ConsoleDigest(rollup_rounds=kwargs.pop("rollup_rounds", 3), **kwargs)

    def test_round_one_starts_a_stream(self):
        digest = self.digest()
        lines = digest.observe(row(1, base_hp=1500, workers_live=2, robots_visible=0))
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("digest "))
        self.assertIn(" start=1 ", lines[0])
        self.assertIn("base=1500", lines[0])

    def test_phase_change_is_emitted_once(self):
        digest = self.digest()
        digest.observe(row(1, phase="day"))
        night = digest.observe(row(2, phase="night", robots_visible=40))
        self.assertEqual([tokens(line).get("now") for line in night if " now=" in line], ["night"])
        again = digest.observe(row(3, phase="night"))
        self.assertFalse([line for line in again if " now=" in line])

    def test_periodic_rollup_aggregates_repeated_actions(self):
        digest = self.digest()
        lines = []
        for round_no in (1, 2, 3):
            lines += digest.observe(row(round_no, commands=2, action_counts={"move": 2},
                                        plan_ms=1.5, walls_live=round_no))
        rollups = [line for line in lines if line.startswith("digest rollup ")]
        self.assertEqual(len(rollups), 1)
        self.assertIn("actions=move:6", rollups[0])
        self.assertIn("turns=3", rollups[0])
        self.assertIn("walls=1..3", rollups[0])
        self.assertIn("cmds=6", rollups[0])

    def test_first_anomalies_are_emitted_once_each(self):
        digest = self.digest()
        lines = []
        lines += digest.observe(row(1, base_hp=1500, robots_visible=0))
        lines += digest.observe(row(2, commands=0, empty_reason="all_weapons_cooling",
                                    weapons_live=3, weapons_ready=0, base_hp=1490,
                                    action_results_false=2, robots_visible=5))
        lines += digest.observe(row(3, commands=0, empty_reason="all_weapons_cooling",
                                    weapons_live=3, weapons_ready=0, base_hp=1480,
                                    action_results_false=2, robots_visible=5))
        blob = "\n".join(lines)
        for name in ("first_empty", "first_action_false", "first_robots", "first_base_damage"):
            self.assertEqual(blob.count(name), 1, name)

    def test_base_thresholds_do_not_flood_every_hit(self):
        digest = self.digest()
        digest.observe(row(1, base_hp=1500))
        lines = []
        for round_no, hp in ((2, 1450), (3, 1400), (4, 1350), (5, 1000), (6, 999), (7, 998)):
            lines += digest.observe(row(round_no, base_hp=hp))
        lows = [line for line in lines if "base_low" in line]
        self.assertEqual(len(lows), 1, lows)     # only the 1000 crossing, once
        self.assertIn("hp=1000", lows[0])
        lines = digest.observe(row(8, base_hp=40))
        self.assertEqual(len([line for line in lines if "base_low" in line]), 1)

    def test_base_death_is_preserved_as_critical(self):
        digest = self.digest()
        digest.observe(row(1, base_hp=1500))
        lines = digest.observe(row(2, base_hp=0, base_health_state="observed_zero",
                                   commands=0, empty_reason="base_observed_dead"))
        self.assertTrue(any("base_observed_zero" in line for line in lines), lines)

    def test_repeated_errors_are_conserved_across_windows(self):
        digest = self.digest(rollup_rounds=2)
        lines = []
        total = 0
        for round_no in range(1, 7):
            count = round_no % 3
            total += count
            lines += digest.observe(row(round_no, judge_errors_total=count,
                                        errors_by_code={4: count} if count else {}))
        lines += digest.flush()
        seen = sum(int(tokens(line).get("errors", 0)) for line in lines
                   if line.startswith("digest rollup "))
        self.assertEqual(seen, total)

    def test_accept_and_submit_counts_survive_rollups(self):
        digest = self.digest(rollup_rounds=2)
        lines = []
        for round_no in range(1, 5):
            lines += digest.observe(row(round_no, action_counts={"acceptTask": 1, "submitAnswer": 2}))
        lines += digest.flush()
        accept = sum(int(tokens(line).get("accept", 0)) for line in lines
                     if line.startswith("digest rollup "))
        submit = sum(int(tokens(line).get("submit", 0)) for line in lines
                     if line.startswith("digest rollup "))
        self.assertEqual(accept, 4)
        self.assertEqual(submit, 8)


class StreamTests(unittest.TestCase):
    def test_duplicate_round_is_not_double_counted(self):
        digest = cd.ConsoleDigest(rollup_rounds=5)
        digest.observe(row(1), stream="run:a")
        digest.observe(row(1), stream="run:a")          # identical content: duplicate
        lines = digest.observe(row(2), stream="run:a")
        rollups = [line for line in lines if line.startswith("digest rollup")]
        self.assertEqual(rollups, [])                    # no rollup yet
        final = digest.flush("run:a")
        self.assertIn("turns=2", final[0])

    def test_same_round_with_changed_payload_keeps_counts_and_anomaly(self):
        digest = cd.ConsoleDigest(rollup_rounds=5)
        digest.observe(row(1), stream="run:a")
        first = digest.observe(row(2, commands=0, empty_reason="all_weapons_cooling"),
                               stream="run:a")
        # Same round number, but a different request: it must not be dropped.
        second = digest.observe(row(2, commands=2, action_counts={"attack": 2},
                                    judge_errors_total=1, errors_by_code={4: 1}),
                                stream="run:a")
        self.assertTrue(any("first_empty" in line for line in first), first)
        self.assertFalse(any("first_empty" in line for line in second), second)
        final = digest.flush("run:a")
        rollup = final[0]
        self.assertIn("turns=3", rollup)
        self.assertIn("cmds=3", rollup)
        self.assertIn("actions=attack:2,move:2", rollup)
        self.assertIn("errors=1", rollup)

    def test_restart_flushes_pending_rollup_before_resetting(self):
        digest = cd.ConsoleDigest(rollup_rounds=20)
        for round_no in (50, 51):
            digest.observe(row(round_no, commands=2, action_counts={"move": 2}), stream="run:a")
        lines = digest.observe(row(2, commands=1), stream="run:a")
        flushed = [line for line in lines if line.startswith("digest rollup")]
        self.assertEqual(len(flushed), 1, lines)
        self.assertIn("final=1", flushed[0])
        self.assertIn("actions=move:4", flushed[0])
        self.assertIn("cmds=4", flushed[0])
        self.assertTrue(any("digest restart" in line for line in lines), lines)

    def test_restart_emits_the_stream_label_and_previous_round(self):
        digest = cd.ConsoleDigest(rollup_rounds=20)
        digest.observe(row(50), stream="run:a")
        lines = digest.observe(row(2), stream="run:a")
        restart = [line for line in lines if "digest restart" in line]
        self.assertEqual(len(restart), 1)
        self.assertIn("after=50", restart[0])
        self.assertIn("s=run/a", restart[0])

    def test_every_compact_line_carries_a_stream_label(self):
        digest = cd.ConsoleDigest(rollup_rounds=2)
        lines = []
        lines += digest.observe(row(1, base_hp=1500, phase="day"), stream="run:a")
        lines += digest.observe(row(1, base_hp=1500, phase="night"), stream="run:b")
        lines += digest.observe(row(3, base_hp=1400, commands=0,
                                    empty_reason="all_weapons_cooling"), stream="run:b")
        lines += digest.flush()
        self.assertTrue(lines)
        for line in lines:
            self.assertIn("s=run/", line, line)

    def test_parallel_streams_are_isolated(self):
        digest = cd.ConsoleDigest(rollup_rounds=2)
        digest.observe(row(1, action_counts={"move": 1}), stream="run:a")
        digest.observe(row(1, action_counts={"attack": 1}), stream="run:b")
        lines = digest.observe(row(2, action_counts={"move": 1}), stream="run:a")
        rollups = [line for line in lines if line.startswith("digest rollup ")]
        self.assertEqual(len(rollups), 1)
        self.assertIn("actions=move:2", rollups[0])

    def test_retained_streams_are_bounded(self):
        digest = cd.ConsoleDigest(rollup_rounds=20, max_streams=3)
        lines = []
        for index in range(5):
            lines += digest.observe(row(1), stream=f"run:{index}")
        self.assertLessEqual(digest.stream_count(), 3)
        self.assertTrue(any("final=1" in line for line in lines))

    def test_missing_fields_are_unknown_and_do_not_crash(self):
        digest = cd.ConsoleDigest(rollup_rounds=2)
        lines = digest.observe({"kind": "response", "round": 1})
        self.assertTrue(lines)
        lines += digest.observe({"round": 2})
        self.assertTrue(any("digest rollup " in line for line in lines))
        digest.observe("not a summary")
        digest.observe(None)


class ModeTests(unittest.TestCase):
    def tearDown(self):
        cd.reset()

    def test_unknown_mode_falls_back_to_compact(self):
        self.assertEqual(cd.console_mode("weird"), "compact")
        self.assertEqual(cd.console_mode(""), "compact")
        self.assertEqual(cd.console_mode("full"), "full")
        self.assertEqual(cd.console_mode("off"), "off")

    def test_full_mode_restores_the_json_line(self):
        records = []
        with patch.dict(os.environ, {cd.MODE_ENV: "full"}):
            lines = cd.emit_summary(row(7), emitter=records.append, stream="run:a")
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("response "))
        self.assertEqual(json.loads(lines[0][len("response "):])["round"], 7)
        self.assertEqual(records, lines)

    def test_off_mode_silences_only_the_console(self):
        records = []
        with patch.dict(os.environ, {cd.MODE_ENV: "off"}):
            lines = cd.emit_summary(row(7), emitter=records.append, stream="run:a")
        self.assertEqual(lines, [])
        self.assertEqual(records, [])

    def test_emit_summary_is_fail_open_with_a_broken_sink(self):
        def broken(_line):
            raise RuntimeError("sink is down")

        with patch.dict(os.environ, {cd.MODE_ENV: "compact"}):
            cd.emit_summary(row(1), emitter=broken, stream="run:a")   # must not raise
            cd.emit_summary(row(1), emitter=broken, stream="run:a")

    def test_console_failure_cannot_change_http_response(self):
        expected = {"roleCommandMap": {"1": {"action": "move", "targetPos": [{"x": 1, "y": 0}]}}}
        observation = {"roundNo": 1, "teamOur": {"type": "challenger", "roles": [
            {"id": 1, "roleType": "worker", "health": 220, "pos": {"x": 0, "y": 0}}]}}
        http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(server, "respond", return_value=expected), \
                 patch.object(cd, "emit_summary", side_effect=RuntimeError("console down")):
                request = Request(f"http://127.0.0.1:{http.server_port}/",
                                  data=json.dumps(observation).encode(),
                                  headers={"Content-Type": "application/json"})
                with urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.load(response), expected)
        finally:
            http.shutdown()
            http.server_close()
            thread.join(timeout=5)


class DecisionReportTests(unittest.TestCase):
    def test_report_is_bounded_and_allowlisted(self):
        sentinel = "sk-SECRET-0123456789abcdef"
        secret = "task text that must never be echoed into compact evidence"
        summary = diagnostics.build_summary(
            {"roundNo": 5, "teamOur": {"type": "challenger", "roles": []}},
            {"roleCommandMap": {}},
            decision={"supervisor": {"mode": "cautious", "reason": "low_hp",
                                     "reserve_pioneer": True, "return_steps_lower_bound": 7,
                                     "relevant_robots": 4, "ready_workers": 2,
                                     "visible_pressure_hp": 35,
                                     "note": secret, "api_key": sentinel},
                      "task": {"phase": "moving", "cycle_active": True,
                               "plan_kind": "goto", "pending_command": "move",
                               "pending_prompt": False,
                               "acceptance_status": {"phase": "moving",
                                                     "point": {"x": 14, "y": 17},
                                                     "retry_after": 3, "reason": "cooldown",
                                                     "raw": secret},
                               "unknown_key": sentinel},
                      "unknown_top": sentinel})
        report = summary["decision"]
        self.assertEqual(report["supervisor"]["relevant_robots"], 4)
        self.assertEqual(report["supervisor"]["ready_workers"], 2)
        self.assertEqual(report["supervisor"]["visible_pressure_hp"], 35)
        self.assertIsInstance(report["supervisor"]["relevant_robots"], int)
        self.assertEqual(report["task"]["plan_kind"], "goto")
        self.assertIs(report["task"]["cycle_active"], True)
        self.assertEqual(report["task"]["pending_prompt"], False)
        self.assertEqual(report["task"]["acceptance_status"]["point"], {"x": 14, "y": 17})
        self.assertEqual(report["task"]["acceptance_status"]["reason"], "cooldown")
        blob = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn(sentinel, blob)
        self.assertNotIn(secret, blob)

    def test_relevant_robots_is_a_count_not_an_id_list(self):
        summary = diagnostics.build_summary(
            {"roundNo": 1, "teamOur": {"type": "challenger", "roles": []}},
            {"roleCommandMap": {}},
            decision={"supervisor": {"mode": "normal", "relevant_robots": 6}})
        self.assertEqual(summary["decision"]["supervisor"]["relevant_robots"], 6)
        bad = diagnostics.build_summary(
            {"roundNo": 1, "teamOur": {"type": "challenger", "roles": []}},
            {"roleCommandMap": {}},
            decision={"supervisor": {"mode": "normal", "relevant_robots": [1, 2, 3]}})
        self.assertNotIn("relevant_robots", bad["decision"]["supervisor"],
                         "an id list is not the accepted count interface")

    def test_absent_report_preserves_the_old_shape(self):
        summary = diagnostics.build_summary(
            {"roundNo": 5, "teamOur": {"type": "challenger", "roles": []}},
            {"roleCommandMap": {}})
        self.assertNotIn("decision", summary)

    def test_supervisor_and_task_transitions_are_compact_signals(self):
        digest = cd.ConsoleDigest(rollup_rounds=20)
        first = diagnostics.build_summary(
            {"roundNo": 1, "teamOur": {"type": "challenger", "roles": []}},
            {"roleCommandMap": {}},
            decision={"supervisor": {"mode": "normal", "reason": "default",
                                     "relevant_robots": 1},
                      "task": {"phase": "idle", "cycle_active": False,
                               "acceptance_status": {"phase": "idle"}}})
        second = diagnostics.build_summary(
            {"roundNo": 2, "teamOur": {"type": "challenger", "roles": []}},
            {"roleCommandMap": {}},
            decision={"supervisor": {"mode": "cautious", "reason": "low_hp",
                                     "relevant_robots": 3},
                      "task": {"phase": "moving", "cycle_active": True,
                               "plan_kind": "goto",
                               "acceptance_status": {"phase": "moving", "retry_after": 2}}})
        lines = digest.observe(first, stream="run:a") + digest.observe(second, stream="run:a")
        blob = "\n".join(lines)
        self.assertIn("digest supervisor", blob)
        self.assertIn("mode=cautious", blob)
        self.assertIn("relevant_robots=3", blob)
        self.assertIn("digest task", blob)
        self.assertIn("plan=goto", blob)
        self.assertIn("accept=moving", blob)
        self.assertIn("s=run/a", blob)
        # A later turn without a report must not crash or invent a transition.
        digest.observe(row(3, teamOur=None), stream="run:a")

    def test_report_with_nothing_usable_is_omitted(self):
        summary = diagnostics.build_summary(
            {"roundNo": 1, "teamOur": {"type": "challenger", "roles": []}},
            {"roleCommandMap": {}}, decision={"supervisor": {"mode": 5}, "task": "nope"})
        self.assertNotIn("decision", summary)


class EmptyClassificationTests(unittest.TestCase):
    def observation(self, **team):
        base = {"type": "challenger", "roles": []}
        base.update(team)
        return {"roundNo": 85, "teamOur": base, "robot": {"roles": []}, "errors": []}

    def summary(self, payload):
        return diagnostics.build_summary(payload, {"roleCommandMap": {}})

    def test_all_weapons_cooling(self):
        roles = [{"id": 1, "roleType": "station", "health": 1500, "pos": {"x": 0, "y": 0}},
                 {"id": 2, "roleType": "worker", "health": 220, "pos": {"x": 0, "y": 1}},
                 {"id": 3, "roleType": "rocket", "health": 1000, "cooldown": 3,
                  "pos": {"x": 0, "y": 2}}]
        payload = self.observation(roles=roles)
        payload["robot"] = {"roles": [{"id": 9, "roleType": "smallRobot", "health": 40}]}
        self.assertEqual(self.summary(payload)["empty_reason"], "all_weapons_cooling")

    def test_ready_weapons_with_observed_robots(self):
        roles = [{"id": 1, "roleType": "station", "health": 1500, "pos": {"x": 0, "y": 0}},
                 {"id": 2, "roleType": "worker", "health": 220, "pos": {"x": 0, "y": 1}},
                 {"id": 3, "roleType": "rocket", "health": 1000, "cooldown": 0,
                  "pos": {"x": 0, "y": 2}}]
        payload = self.observation(roles=roles)
        payload["robot"] = {"roles": [{"id": 9, "roleType": "smallRobot", "health": 40}]}
        self.assertEqual(self.summary(payload)["empty_reason"], "ready_weapons_no_attack")

    def test_observed_no_robots(self):
        roles = [{"id": 1, "roleType": "station", "health": 1500, "pos": {"x": 0, "y": 0}},
                 {"id": 2, "roleType": "worker", "health": 220, "pos": {"x": 0, "y": 1}},
                 {"id": 3, "roleType": "rocket", "health": 1000, "cooldown": 0,
                  "pos": {"x": 0, "y": 2}}]
        payload = self.observation(roles=roles)
        payload["robot"] = {"roles": []}
        self.assertEqual(self.summary(payload)["empty_reason"], "observed_no_robots")

    def test_unknown_cooldown_does_not_fabricate_a_reason(self):
        roles = [{"id": 1, "roleType": "station", "health": 1500, "pos": {"x": 0, "y": 0}},
                 {"id": 2, "roleType": "worker", "health": 220, "pos": {"x": 0, "y": 1}},
                 {"id": 3, "roleType": "rocket", "health": 1000, "pos": {"x": 0, "y": 2}}]
        payload = self.observation(roles=roles)
        payload["robot"] = {"roles": [{"id": 9, "roleType": "smallRobot", "health": 40}]}
        self.assertEqual(self.summary(payload)["empty_reason"], "unclassified")


class TraceIndependenceTests(unittest.TestCase):
    def test_every_turn_is_still_recorded_when_the_console_is_off(self):
        import tempfile
        from agent import telemetry as t

        with tempfile.TemporaryDirectory() as temp:
            recorder = t.Recorder(Path(temp) / "run", {"test": "console-off"})
            observation = {"roundNo": 1, "mapInfo": {"width": 41, "height": 32, "zones": []},
                           "teamOur": {"type": "challenger", "goldNum": 75, "roles": [
                               {"id": 1, "roleType": "station", "health": 1500, "level": 1,
                                "pos": {"x": 9, "y": 22}}]},
                           "teamEnemy": {"roles": []}, "robot": {"roles": []}}
            with patch.dict(os.environ, {cd.MODE_ENV: "off"}):
                for round_no in range(1, 6):
                    payload = dict(observation, roundNo=round_no)
                    recorder.submit(recorder.begin(), json.dumps(payload).encode(),
                                    b'{"roleCommandMap":{}}')
            self.assertTrue(recorder.close())
            rows = [record for record in trace_tool.records(recorder.directory)
                    if record.get("event") == "turn"]
            self.assertEqual(len(rows), 5)


if __name__ == "__main__":
    unittest.main()


class IntegratedWindowTests(unittest.TestCase):
    def test_window_ranges_and_boundary_damage_are_exact(self):
        from agent.console_digest import ConsoleDigest
        digest=ConsoleDigest(rollup_rounds=2)
        lines=[]
        for n,hp in [(1,1500),(2,1500),(3,1400),(4,1350),(5,1300)]:
            lines+=digest.observe({'round':n,'event':f'run:{n}','base_hp':hp})
        lines+=digest.flush()
        rollups=[line for line in lines if 'rollup' in line]
        self.assertIn('r=1..2 ',rollups[0]);self.assertIn('r=3..4 ',rollups[1]);self.assertIn('r=5 ',rollups[2])
        self.assertIn('dmg=150 ',rollups[1]+' ');self.assertIn('dmg=50 ',rollups[2]+' ')

    def test_walking_distance_does_not_emit_a_supervisor_line_each_round(self):
        from agent.console_digest import ConsoleDigest
        digest=ConsoleDigest();lines=[]
        for n in range(1,16):
            lines+=digest.observe({'round':n,'decision':{'supervisor':{'mode':'prepare',
                'reason':'return_before_night','reserve_pioneer':True,'return_steps_lower_bound':20-n}}})
        self.assertEqual(sum('supervisor' in line for line in lines),1)

    def test_periodic_summary_keeps_economy_weapon_levels_and_controller_ids(self):
        from agent.console_digest import ConsoleDigest
        digest=ConsoleDigest(rollup_rounds=1)
        lines=digest.observe({'round':1,'gold':123,'score':456,'weapon_levels':{'rocket.L2':3},
            'controllers':{'issued_attacks':[{'controller':10011}]}})
        line=next(line for line in lines if 'rollup' in line)
        for value in ('gold=123','score=456','weapons=rocket.L2:3','controllers=10011:1'):
            self.assertIn(value,line)


class LateCompletionTests(unittest.TestCase):
    def test_late_http_summary_does_not_fake_restart_or_hp_drop(self):
        from agent.console_digest import ConsoleDigest
        digest=ConsoleDigest(rollup_rounds=10)
        lines=[]
        for n,hp in ((1,1500),(3,1400),(2,1500),(4,1300)):
            lines+=digest.observe({'round':n,'event':f'run:{n}','base_hp':hp,
                                  'judge_errors_total':1,'action_counts':{'acceptTask':1}})
        lines+=digest.flush()
        self.assertFalse(any('restart' in line for line in lines))
        rollup=next(line for line in lines if 'rollup' in line)
        for field in ('turns=4','late=1','dmg=200','errors=4','accept=4'):
            self.assertIn(field,rollup)
