"""Actual brain/channel loop with scripted model replies and real virtual tools.

These test transport/control, not model intelligence. Expected answers are
hand-authored independently of the task-solving implementation.
"""
import json
import os
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Demo/CoreGeek/src"))
from agent import brain, planner, local_task_sandbox, local_task_cases, task_context
from agent.team_agent import TeamAgent
from test_tasks import observation


class TeamAgentIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {brain.TASK_AGENT_ENV: "on"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.state = planner.PlannerState()
        self.case = deepcopy(local_task_cases.CASES[0])
        self.round_no = 1

    def run_round(self, *, llm="", command="", errors=None, description=None, team="challenger"):
        payload = observation(round_no=self.round_no, team=team, role_pos=(6, 5),
                              phase_task=self.case["description"] if description is None else description,
                              llm_resp=llm, cmd_result=command, timeout_rounds=30)
        payload["errors"] = errors or []
        with patch.object(planner, "state_for", return_value=self.state):
            response = brain.respond(payload)
        self.state = planner.PlannerState.load(json.loads(json.dumps(self.state.dump())))
        self.round_no += 1
        return response

    def model_reply(self, kind, value):
        pending = self.state.team_agent.task.pending
        self.assertIsNotNone(pending)
        field = "command" if kind == "run" else "answer"
        return json.dumps({"request_id": pending["token"], "plan": {
            "kind": kind, field: value, "reason": "使用当前文档及实际返回数据",
            "evidence_ids": pending["evidence_ids"]}}, ensure_ascii=False)

    def test_document_query_answer_loop_preserves_json_and_learns_only_method(self):
        first = self.run_round()
        self.assertIn("prompt", first)
        self.assertLessEqual(len(first["prompt"]), task_context.PROMPT_LIMIT)
        self.assertNotIn("10011", first["roleCommandMap"], "pioneer holds task range")
        self.assertTrue(first["roleCommandMap"], "worker planning continues while model runs")
        for command in ("cat /brief/README.txt", "python3 /svc/weather.py --city 北京"):
            issued = self.run_round(llm=self.model_reply("run", command))
            self.assertEqual(issued.get("executeCmd"), command)
            result = local_task_sandbox.execute(command, self.case["sandbox_fixture"], active=True)
            following = self.run_round(command=result)
            self.assertIn("prompt", following)
            self.assertEqual(self.state.team_agent.evidence[-1]["text"], result.split("\n", 1)[1])
            self.assertIn(json.dumps(result.split("\n", 1)[1], ensure_ascii=False), following["prompt"])
        answer = '{"city":"北京","temperature":23}'
        submitted = self.run_round(llm=self.model_reply("answer", answer))
        self.assertEqual(submitted["roleCommandMap"]["10011"],
                         {"action": "submitAnswer", "taskAnswer": answer})
        self.assertEqual(self.state.team_agent.task.stage, "awaiting_judgement")
        self.assertEqual(self.state.team_agent.task.prompts, 3)
        self.assertEqual(self.state.team_agent.task.commands, 2)
        self.assertEqual(self.state.judge.llm_used_today, 0)
        self.assertEqual(len(self.state.team_agent.skills.entries), 1)
        saved = json.dumps(self.state.team_agent.skills.dump(), ensure_ascii=False)
        self.assertNotIn("北京", saved)
        self.assertNotIn("temperature", saved)
        self.run_round(description="")
        self.assertEqual(self.state.team_agent.task.stage, "ended")

    def test_wrong_answer_feedback_replans_instead_of_claiming_success(self):
        self.run_round()
        self.run_round(llm=self.model_reply("answer", "wrong"))
        repair = self.run_round(errors=[{"errorCode": 2, "description": "答案不完全正确"}])
        self.assertIn("prompt", repair)
        corrected = self.run_round(llm=self.model_reply("answer", '  exact\n'))
        self.assertEqual(corrected["roleCommandMap"]["10011"]["taskAnswer"], '  exact\n')
        self.assertEqual(self.state.team_agent.task.answers, 2)

    def test_lost_or_wrong_token_reply_never_falls_back_to_legacy_answer(self):
        self.run_round()
        reply = self.model_reply("answer", "wrong")
        reply = reply.replace(self.state.team_agent.task.pending["token"], "incorrect-token")
        result = self.run_round(llm=reply)
        self.assertNotIn("10011", result["roleCommandMap"])
        self.assertNotIn("prompt", result)
        self.run_round()
        result = self.run_round()
        self.assertIn("prompt", result, "expired request can replan within bounded attempts")
        self.assertEqual(self.state.team_agent.task.answers, 0)

    def test_higher_priority_news_does_not_acknowledge_queued_task(self):
        router = self.state.ensure_llm_router()
        router.offer("news", "day1", "news", kind="prompt", payload="Interpret news", priority=1)
        response = self.run_round()
        self.assertEqual(response["prompt"], "Interpret news")
        self.assertEqual(self.state.team_agent.task.prompts, 0)
        next_response = self.run_round(llm="ordinary news text")
        self.assertIn("request_id", next_response["prompt"])
        self.assertEqual(self.state.team_agent.task.prompts, 1)
        self.assertEqual(self.state.judge.llm_used_today, 1)

    def test_give_up_holds_official_task_without_refreshing_soft_budget(self):
        self.run_round()
        pending = self.state.team_agent.task.pending
        reply = json.dumps({"request_id": pending["token"], "plan": {
            "kind": "give_up", "reason": "没有可用文档", "evidence_ids": pending["evidence_ids"]}})
        self.run_round(llm=reply)
        response = self.run_round()
        self.assertNotIn("prompt", response)
        self.assertNotIn("10011", response["roleCommandMap"])
        self.assertIsNotNone(self.state.tasks["cycle"])
        self.assertEqual(self.state.team_agent.task.prompts, 1)
        self.assertEqual(self.state.team_agent.task.stage, "stopped")

    def test_keyword_solver_remains_model_free(self):
        from agent.tasks import solver_keyword_fill, SolverContext, TaskCycle
        # Use an existing deterministic fixture shape, not a model shortcut.
        text = '阅读资料并回答。资料：代号为 Aurora。请填写：代号=？'
        ctx = SolverContext(TaskCycle({"x": 6, "y": 5}, 1, description=text),
                            1, text, "", "", "empty", {}, (), True)
        self.assertIsNotNone(solver_keyword_fill(ctx))
        response = self.run_round(description=text)
        self.assertNotIn("prompt", response)
        self.assertIsNone(self.state.team_agent)

    def test_foreign_or_tampered_memory_cannot_provide_evidence(self):
        self.run_round()
        data = self.state.team_agent.dump()
        self.assertTrue(TeamAgent.load(data, "another-team").degraded)
        data["evidence"] = [{"id": "forged"}]
        self.assertTrue(TeamAgent.load(data, data["owner"]).degraded)

    def test_preview_does_not_consume_or_queue_new_agent_work(self):
        self.run_round()
        before = self.state.dump()
        payload = observation(round_no=2, role_pos=(6, 5), phase_task=self.case["description"])
        response = brain.plan_for_state(payload, self.state, commit=False, judge_tasks=False)
        self.assertEqual(self.state.dump(), before)
        self.assertIsNone(response.prompt)
        self.assertIsNone(response.execute)

    def test_deadline_round_cannot_confirm_an_exempt_task(self):
        from agent.tasks import TaskCycle
        cycle = TaskCycle({"x": 6, "y": 5}, 1, description=self.case["description"], timeout_rounds=3)
        payload = observation(round_no=4, role_pos=(6, 5), phase_task=cycle.description)
        self.assertFalse(task_context.public_task_confirmed(payload, cycle).confirmed)


if __name__ == "__main__":
    unittest.main()
