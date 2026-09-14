"""P1 end-to-end local judging: scripted model, actual simulator and virtual FS.

Known fixtures define independent expected results. Scripted replies prove the
integrated chain and failure recovery; real model capability is evaluated apart.
"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Demo/CoreGeek/src"))
from agent import brain, local_task_cases, local_task_sandbox, planner, scenarios, simulator

PLANS = {
    "weather-doc-v1-beijing": [
        ("run", "cat /brief/README.txt"), ("run", "python3 /svc/weather.py --city 北京"),
        ("answer", '{"city":"北京","temperature":23}')],
    "weather-doc-v2-shanghai": [
        ("run", "cat /manual/service.txt"), ("run", "python3 /service/query.py --place 上海"),
        ("answer", '{"temperature":17,"city":"上海"}')],
    "decimal-aggregation": [
        ("run", "cat /docs/accounts.txt"), ("run", "python3 /tools/sum.py --zone A"),
        ("answer", "0.3")],
}


def exercise(case_id, side, *, wrong_first=False):
    state = scenarios.scenario(90601, side)
    local_task_cases.install(state, [case_id])
    state["worldNews"] = {"officialNews": "", "folkLegends": ""}
    zone = next(z for z in state["mapInfo"]["zones"] if z["neutralType"] == side + "TaskPoint1")
    pioneer = next(r for r in state["teamOur"]["roles"] if r["roleType"] == "pioneer")
    pioneer["pos"] = {"x": zone["pos"]["x"], "y": zone["pos"]["y"] + (1 if zone["pos"]["y"] < 31 else -1)}
    plans = list(PLANS[case_id])
    if wrong_first:
        plans.insert(0, ("answer", "deliberately wrong"))
    calls = tools = 0
    trace = []
    for _ in range(24):
        response = simulator.step(state)
        state = response["state"]
        memory = state["_demo"]["planner"]
        channels = response["judgeRequest"]
        report = state["_demo"].get("task_report", {})
        trace.append({"round": response["frame"]["round"], "executed": response["executed"],
                      "channels": list(channels), "errors": state["errors"], "report": report})
        if "prompt" in channels:
            kind, value = plans[calls]
            calls += 1
            pending = memory.team_agent.task.pending
            state["llmResp"] = json.dumps({"request_id": pending["token"], "plan": {
                "kind": kind, "command" if kind == "run" else "answer": value,
                "reason": "脚本化模型用于链路验收", "evidence_ids": pending["evidence_ids"]}}, ensure_ascii=False)
        if "executeCmd" in channels:
            tools += 1
            fixture = local_task_sandbox.active_task_fixture(state)
            if fixture is None:
                raise AssertionError("command emitted without an actual active sandbox")
            state["lastCmdResult"] = local_task_sandbox.execute(channels["executeCmd"], fixture, active=True)
        state["_demo"]["planner"] = memory.dump()
        state = json.loads(json.dumps(state))
        if report.get("ended"):
            return {"state": state, "trace": trace, "report": report, "calls": calls, "tools": tools}
    raise AssertionError("Agent did not finish its local task in 24 rounds: " + json.dumps(trace, ensure_ascii=False))


class TeamAgentSimulatorTests(unittest.TestCase):
    def test_three_cases_both_sides_finish_through_actual_simulator_judging(self):
        with patch.dict(os.environ, {brain.TASK_AGENT_ENV: "on"}):
            for case_id in PLANS:
                for side in ("challenger", "defender"):
                    with self.subTest(case_id=case_id, side=side):
                        result = exercise(case_id, side)
                        self.assertEqual(result["report"]["ended"], "completed")
                        self.assertEqual(result["report"]["rewards"]["rate"], 1)
                        self.assertGreater(result["report"]["rewards"]["score"], 0)
                        self.assertEqual((result["calls"], result["tools"]), (3, 2))
                        self.assertEqual(result["state"]["_demo"]["planner"]["judge"]["llmUsedToday"], 0)
                        self.assertFalse(any(len(row["channels"]) > 1 for row in result["trace"]))

    def test_incorrect_answer_receives_error2_then_corrects_in_the_same_task(self):
        with patch.dict(os.environ, {brain.TASK_AGENT_ENV: "on"}):
            result = exercise("weather-doc-v2-shanghai", "defender", wrong_first=True)
        self.assertTrue(any(any(e["errorCode"] == 2 for e in row["errors"]) for row in result["trace"]))
        self.assertEqual(result["report"]["ended"], "completed")
        self.assertEqual(result["report"]["rewards"]["rate"], 1)
        accepts = sum(c.get("action") == "acceptTask" for row in result["trace"] for c in row["executed"].values())
        self.assertEqual(accepts, 1)
        self.assertEqual(result["calls"], 4)


if __name__ == "__main__":
    unittest.main()
