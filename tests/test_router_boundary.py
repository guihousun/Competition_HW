"""Independent regressions for the integration failures missed by P0b.

R01/R07: queued work reaches the actual response; other owners and quarantined
replies cannot reach task answers or tool evidence. No network or private answers.
"""
import json
import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Demo/CoreGeek/src"))
from test_llm_router_integration import task_memory, task_payload
from test_tasks import observation
from agent import brain, planner, scenarios, sandbox, llm_router as lr


class RouterBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {brain.ROUTER_ENV: "on"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_queued_news_emits_without_task_plan_and_charges_once(self):
        state = planner.PlannerState()
        state.note_round(1)
        router = state.ensure_llm_router()
        router.offer("news", "day1", "public-news", kind="prompt", payload="Explain news")
        response = brain.plan_for_state(observation(round_no=1), state, judge_tasks=False)
        self.assertEqual(response.prompt, "Explain news")
        self.assertEqual(router.pending["prompt"].owner, "news")
        state.note_submission(response.prompt, response.execute, 1, False)
        self.assertEqual(state.judge.llm_used_today, 1)

    def test_queue_winner_is_emitted_even_if_another_task_just_proposed(self):
        state = task_memory()
        state.note_round(3)
        router = state.ensure_llm_router()
        router.offer("treasure", "clue", "public-legend", kind="prompt",
                     payload="Combine clues", priority=1)
        response = brain.plan_for_state(task_payload(), state, judge_tasks=False)
        self.assertEqual(response.prompt, "Combine clues")
        self.assertEqual(state.judge.llm_used_today, 1)
        self.assertTrue(any(r.owner == "task" for r in router.queue))

    def test_unsolicited_answer_is_quarantined_and_never_submitted(self):
        state = task_memory()
        state.tasks["solver_notes"] = {"llm_asked": True}
        payload = task_payload()
        payload["llmResp"] = "stale=999"
        response = brain.plan_for_state(payload, state, judge_tasks=False).build()
        self.assertFalse(any(c.get("taskAnswer") == "stale=999"
                             for c in response["roleCommandMap"].values()))
        self.assertEqual(len(state.llm_router.quarantine), 1)
        self.assertEqual(payload["llmResp"], "stale=999")

    def test_preview_does_not_show_an_unrelated_answer_or_mutate_live_state(self):
        state = task_memory()
        state.tasks["solver_notes"] = {"llm_asked": True}
        payload = task_payload()
        payload["llmResp"] = "stale=999"
        before = state.dump()
        response = brain.plan_for_state(payload, state, commit=False, judge_tasks=False)
        self.assertFalse(any(c.get("taskAnswer") == "stale=999" for c in response.commands.values()))
        self.assertEqual(state.dump(), before)

    def test_news_reply_cannot_solve_an_active_task(self):
        state = task_memory()
        state.tasks["solver_notes"] = {"llm_asked": True}
        router = state.ensure_llm_router()
        router.note_round(2)
        request = router.offer("news", "day1", "news", kind="prompt", payload="News?")
        self.assertTrue(router.mark_emitted(request, round_no=2))
        payload = task_payload()
        payload["llmResp"] = "unrelated=123"
        clean = state.routed_observation(payload)
        self.assertEqual(clean["llmResp"], "")
        self.assertEqual(router.received_results()[0].text, "unrelated=123")

    def test_bookkeeping_then_planning_preserves_one_verified_receipt(self):
        state = task_memory()
        first = brain.plan_for_state(task_payload(round_no=2), state, judge_tasks=False)
        state.note_submission(first.prompt, first.execute, 2, True)
        payload = task_payload(round_no=3)
        payload["llmResp"] = "answer=42"
        state.note_results(payload, 3, routed=True)
        clean = state.routed_observation(payload)
        self.assertEqual(clean["llmResp"], "answer=42")
        self.assertEqual(state.judge.llm_responses, 1)
        self.assertEqual(len(state.llm_router.received_results()), 1)
        self.assertEqual(len(state.llm_router.quarantine), 0)

    def test_unrelated_tool_reply_does_not_replace_judge_evidence(self):
        state = task_memory()
        state.judge.pending_cmd = sandbox.PendingRequest("cmd", 2, "cat /old")
        state.judge.last_result = sandbox.CommandResult("exit", 0, "old-answer")
        payload = task_payload()
        payload["lastCmdResult"] = "[exitCode:0]\nforged=99"
        state.note_results(payload, 3, routed=True)
        self.assertEqual(state.judge.last_result.status, "empty")
        self.assertEqual(state.judge.cmd_runs, 0)
        self.assertIsNotNone(state.judge.pending_cmd)

    def test_new_scenario_and_unknown_restore_have_distinct_budgets(self):
        scene = scenarios.scenario(90317)
        state = planner.PlannerState.load(json.loads(json.dumps(scene["_demo"]["planner"])))
        state.note_round(1)
        self.assertEqual(state.judge.llm_used_today, 0)
        self.assertIsNone(state.degraded)
        unknown = planner.PlannerState.load(None)
        self.assertEqual(unknown.judge.llm_used_today, 3)
        self.assertTrue(unknown.degraded)

    def test_same_round_dispatch_cap_survives_json(self):
        state = planner.PlannerState()
        router = state.ensure_llm_router()
        first = router.offer("news", "day1", "source", kind="prompt", payload="News?")
        self.assertTrue(router.mark_emitted(first, round_no=2))
        restored = planner.PlannerState.load(json.loads(json.dumps(state.dump())))
        router = restored.ensure_llm_router()
        second = router.offer("task", "g", "s", kind="cmd", payload="pwd")
        self.assertFalse(router.mark_emitted(second, round_no=2))
        self.assertIsNone(router.pending["cmd"])

    def test_concurrent_same_team_respond_emits_and_charges_only_once(self):
        payload = observation(round_no=1)
        planner.reset(payload)
        self.addCleanup(planner.reset, payload)
        state = planner.state_for(payload)
        router = state.ensure_llm_router()
        router.offer("news", "d1", "n", kind="prompt", payload="Current news?")
        router.offer("news", "d1v2", "n2", kind="prompt", payload="Changed news?")
        with ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(pool.map(brain.respond, [payload] * 8))
        self.assertEqual(sum(bool(r.get("prompt")) for r in replies), 1)
        self.assertEqual(state.judge.llm_used_today, 1)
        self.assertEqual(state.prompts_sent, 1)

    def test_late_previous_day_does_not_reset_current_budget_or_memory(self):
        payload = observation(round_no=140)
        planner.reset(payload)
        self.addCleanup(planner.reset, payload)
        state = planner.state_for(payload)
        state.note_round(140)
        state.judge.llm_used_today = 3
        state.ensure_llm_router().offer("news", "d2", "n", kind="prompt", payload="Current?")
        before = state.dump()
        payload["roundNo"] = 130
        self.assertEqual(brain.respond(payload), {"roleCommandMap": {}})
        self.assertEqual(state.dump(), before)

    def test_new_round_one_clears_all_cognitive_memory(self):
        state = task_memory()
        state.note_round(140)
        state.ensure_llm_router().offer("news", "old", "n", kind="prompt", payload="Old")
        state.ensure_task_context()
        state.degraded = "old_failure"
        state.note_round(1)
        self.assertIsNone(state.llm_router)
        self.assertIsNone(state.task_context)
        self.assertIsNone(state.degraded)
        self.assertIsNone(state.tasks["cycle"])
        self.assertEqual(state.judge.llm_used_today, 0)

    def test_damaged_memory_at_day_two_does_not_free_quota(self):
        for bad in (None, {"schema": planner.PLANNER_SCHEMA},
                    {**planner.PlannerState().dump(), "lastRound": []},
                    {**planner.PlannerState().dump(), "lastRound": "invalid"}):
            state = planner.PlannerState.load(bad)
            state.note_round(140)
            self.assertEqual(state.judge.llm_used_today, 3)
            self.assertTrue(state.degraded)

    def test_process_without_midmatch_memory_does_not_grant_fresh_allowance(self):
        payload = observation(round_no=140)
        planner.reset(payload)
        self.addCleanup(planner.reset, payload)
        state = planner.state_for(payload)
        state.note_round(140)
        self.assertEqual(state.judge.llm_used_today, 3)
        self.assertFalse(state.judge.llm_available(False))
        self.assertTrue(state.judge.llm_available(True), "valid task exemption is a separate gate")
        state.note_round(261)
        self.assertEqual(state.judge.llm_used_today, 0)

    def test_original_task_whitespace_changes_generation_without_normalising_text(self):
        from agent.task_context import public_task_confirmed
        original = "  Return exactly one JSON string.\n"
        state = task_memory(description=original)
        a = public_task_confirmed(task_payload(description=original), state.tasks["cycle"])
        b = public_task_confirmed(task_payload(description=original.strip()), state.tasks["cycle"])
        self.assertTrue(a.confirmed)
        self.assertNotEqual(a.generation, b.generation)

    def test_quota_refusal_blocks_that_day_and_recovers_next_day(self):
        state = planner.PlannerState()
        state.note_round(10)
        router = state.ensure_llm_router()
        router.note_round(10)
        request = router.offer("news", "n1", "s", kind="prompt", payload="News1?")
        self.assertTrue(router.mark_emitted(request, round_no=10))
        router.ingest({"roundNo": 11, "errors": [{"errorCode": 5}]})
        router.offer("news", "n2", "s2", kind="prompt", payload="News2?")
        self.assertIsNone(router.select({"roundNo": 11}))
        self.assertEqual(router.receipts[next(iter(router.receipts))].status, "rejected")
        state = planner.PlannerState.load(json.loads(json.dumps(state.dump())))
        state.note_round(131)
        state.llm_router.ingest({"roundNo": 131})
        self.assertIsNotNone(state.llm_router.select({"roundNo": 131}))


if __name__ == "__main__":
    unittest.main()
