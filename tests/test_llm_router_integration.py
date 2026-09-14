"""P0b integration: opt-in brain->router emission and disabled-path parity.

These tests drive the real planner/brain seam (no server, no network). The
disabled path must be byte-for-byte the deterministic strategy; the enabled path
may only change which cognitive channel is scheduled.

    python -m unittest discover -s tests -p test_llm_router_integration.py -v
"""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_tasks import observation  # noqa: E402
from agent import brain, llm_router as lr, planner, task_context as tc, tasks  # noqa: E402
from agent.sandbox import LLM_DAILY_QUOTA  # noqa: E402

ENABLED = {brain.ROUTER_ENV: "on"}
DISABLED = {brain.ROUTER_ENV: "off"}
LONG_QUESTION = "请查询第12号数据库的当前值并给出结论?" * 125


def task_memory(description=LONG_QUESTION, *, accepted=1, timeout=20, point=(6, 5)):
    state = planner.PlannerState()
    state.tasks = {"cycle": tasks.TaskCycle(point={"x": point[0], "y": point[1]},
                                            accepted_round=accepted, task_type="自进化类1",
                                            description=description, timeout_rounds=timeout),
                   "cooldown_until": 0, "solver_notes": {}}
    return state


def task_payload(round_no=3, description=LONG_QUESTION):
    return observation(round_no=round_no, role_pos=(6, 5), phase_task=description,
                       timeout_rounds=20)


class DisabledPathTests(unittest.TestCase):
    def test_router_is_off_by_default_and_state_is_untouched(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(brain.llm_router_enabled())
            state = task_memory()
            response = brain.plan_for_state(task_payload(), state, judge_tasks=False)
        self.assertIsNone(state.llm_router)
        self.assertIsNone(state.task_context)
        self.assertIn("roleCommandMap", response.build())

    def test_disabled_response_is_the_legacy_prompt(self):
        state = task_memory()
        with patch.dict(os.environ, DISABLED):
            response = brain.plan_for_state(task_payload(), state, judge_tasks=False)
        self.assertIsNotNone(response.prompt)
        self.assertIn(LONG_QUESTION[:40], response.prompt)
        self.assertIsNone(state.llm_router)

    def test_role_command_map_is_identical_enabled_and_disabled(self):
        with patch.dict(os.environ, DISABLED):
            disabled = brain.plan_for_state(task_payload(), task_memory(), judge_tasks=False)
            baseline = brain.plan_for_state(task_payload(), task_memory(), judge_tasks=False)
        with patch.dict(os.environ, ENABLED):
            enabled = brain.plan_for_state(task_payload(), task_memory(), judge_tasks=False)
        self.assertEqual(enabled.commands, disabled.commands)
        self.assertEqual(enabled.commands, baseline.commands)
        self.assertEqual(set(enabled.commands), set(disabled.commands),
                         "the opt-in must not add or drop role actions")


class EnabledEmissionTests(unittest.TestCase):
    def test_substantial_context_is_emitted_through_brain(self):
        state = task_memory()
        with patch.dict(os.environ, ENABLED):
            response = brain.plan_for_state(task_payload(), state, judge_tasks=False)
        router = state.llm_router
        self.assertIsNotNone(router)
        pending = router.pending["prompt"]
        self.assertIsNotNone(pending, "the confirmed task prompt must be in flight")
        self.assertGreater(len(pending.payload), 400,
                           "a substantial context must actually be offered")
        self.assertLessEqual(len(pending.payload), lr.PROMPT_PAYLOAD_LIMIT)
        self.assertIn("generation=", pending.payload)
        self.assertIn("请按题目要求作答", pending.payload)
        self.assertEqual(response.prompt, pending.payload)
        self.assertEqual(pending.budget_class, lr.BUDGET_TASK_EXEMPT)
        self.assertEqual(state.judge.llm_used_today, 0,
                         "a confirmed task call is exempt from the ordinary quota")
        # The context is persisted for a later dump/load.
        envelope = state.task_context.get("task", pending.generation)
        self.assertIsNotNone(envelope)
        self.assertEqual(envelope.task_text, LONG_QUESTION)

    def test_at_most_one_channel_and_legal_response_keys(self):
        state = task_memory()
        with patch.dict(os.environ, ENABLED):
            response = brain.plan_for_state(task_payload(), state, judge_tasks=False)
        built = response.build()
        self.assertTrue(set(built) <= {"roleCommandMap", "prompt", "executeCmd"})
        self.assertNotEqual(bool(built.get("prompt")), bool(built.get("executeCmd")),
                            "prompt and executeCmd are exclusive here")

    def test_preview_creates_no_pending_and_charges_nothing(self):
        state = task_memory()
        with patch.dict(os.environ, ENABLED):
            brain.plan_for_state(task_payload(), state, commit=False, judge_tasks=False)
        self.assertIsNone(state.llm_router)
        self.assertIsNone(state.task_context)
        self.assertEqual(state.judge.llm_used_today, 0)

    def test_router_fault_still_returns_a_legal_response(self):
        state = task_memory()
        with patch.dict(os.environ, ENABLED), \
                patch.object(lr.LLMRouter, "offer", side_effect=RuntimeError("boom")):
            response = brain.plan_for_state(task_payload(), state, judge_tasks=False)
        built = response.build()
        self.assertIn("roleCommandMap", built)
        self.assertTrue(set(built) <= {"roleCommandMap", "prompt", "executeCmd"})
        self.assertNotIn("prompt", built)
        self.assertEqual(state.judge.llm_used_today, 0)

    def test_unconfirmed_task_never_emits_a_prompt(self):
        # A local cycle with no published phaseTask text is not confirmation.
        payload = task_payload()
        payload["phaseTask"] = ""
        state = task_memory()
        with patch.dict(os.environ, ENABLED):
            response = brain.plan_for_state(payload, state, judge_tasks=False)
        self.assertIsNone(response.prompt)
        self.assertIsNone(state.llm_router.pending["prompt"] if state.llm_router else None)


class PlannerPersistenceTests(unittest.TestCase):
    def test_roundtrip_keeps_router_context_and_text(self):
        state = task_memory()
        with patch.dict(os.environ, ENABLED):
            brain.plan_for_state(task_payload(), state, judge_tasks=False)
        rebuilt = planner.PlannerState.load(json.loads(json.dumps(state.dump())))
        self.assertIsNotNone(rebuilt.llm_router)
        self.assertIsNotNone(rebuilt.llm_router.pending["prompt"])
        self.assertIsNotNone(rebuilt.task_context)
        generation = rebuilt.llm_router.pending["prompt"].generation
        envelope = rebuilt.task_context.get("task", generation)
        self.assertIsNotNone(envelope)
        self.assertEqual(envelope.task_text, LONG_QUESTION)
        self.assertIsNone(rebuilt.degraded)

    def test_unknown_planner_schema_degrades_and_never_frees_quota(self):
        data = task_memory().dump()
        data["schema"] = "newer/9"
        rebuilt = planner.PlannerState.load(data)
        self.assertEqual(rebuilt.degraded, "unknown_schema")
        self.assertEqual(rebuilt.judge.llm_used_today, LLM_DAILY_QUOTA,
                         "an unknown schema must not reset the quota to free")
        self.assertIsNone(rebuilt.tasks["cycle"])

    def test_broken_router_context_degrades_explicitly(self):
        state = task_memory()
        with patch.dict(os.environ, ENABLED):
            brain.plan_for_state(task_payload(), state, judge_tasks=False)
        data = json.loads(json.dumps(state.dump()))
        data["llmRouter"]["schema"] = "newer/9"
        data["taskContext"]["schema"] = "newer/9"
        rebuilt = planner.PlannerState.load(data)
        self.assertTrue(rebuilt.degraded)
        self.assertIsNone(rebuilt.llm_router.pending["prompt"])
        self.assertIsNone(rebuilt.task_context.get("task",
                                                   state.llm_router.pending["prompt"].generation))


if __name__ == "__main__":
    unittest.main()
