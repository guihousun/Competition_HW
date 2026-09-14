"""Independent deterministic contract tests for the LLM allowance and planner memory.

Official basis (接口文档 §1.7): 3 prompts per game day outside a task; while a task
is being executed the allowance does not apply and the call is **not** counted.
``consume_llm`` is an accounting primitive, not a refusal gate, so every test
consumes only after the ``llm_available`` guard says the call is allowed.

These tests are deterministic and offline: no network, no real LLM.

    python -m unittest discover -s tests -p test_llm_budget_contract.py -v
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))

from agent import sandbox  # noqa: E402
from agent.planner import PlannerState  # noqa: E402
from agent.tasks import Submission, TaskCycle  # noqa: E402


def guarded_consume(judge, in_task=False):
    """Consume one call only when the guard allows it; returns whether it ran."""
    if not judge.llm_available(in_task=in_task):
        return False
    judge.consume_llm(in_task=in_task)
    return True


class LlmBudgetTests(unittest.TestCase):
    def test_three_calls_available_and_the_fourth_is_guarded(self):
        judge = sandbox.JudgeState()
        self.assertEqual(sandbox.LLM_DAILY_QUOTA, 3)
        self.assertEqual([guarded_consume(judge) for _ in range(3)], [True, True, True])
        self.assertEqual(judge.llm_used_today, 3)
        self.assertFalse(guarded_consume(judge), "the 4th call must not pass the guard")
        self.assertEqual(judge.llm_used_today, 3,
                         "a guarded refusal must not consume, because consume_llm only accounts")

    def test_in_task_calls_stay_available_and_are_not_counted(self):
        judge = sandbox.JudgeState()
        for _ in range(6):
            self.assertTrue(guarded_consume(judge, in_task=True))
        self.assertEqual(judge.llm_used_today, 0, "in-task calls are exempt and uncounted")
        self.assertTrue(judge.llm_available(in_task=False), "the normal allowance is untouched")

    def test_in_task_calls_do_not_disturb_the_normal_balance(self):
        judge = sandbox.JudgeState()
        guarded_consume(judge)
        guarded_consume(judge)
        self.assertEqual(judge.llm_used_today, 2)
        for _ in range(10):
            guard_before = judge.llm_used_today
            guarded_consume(judge, in_task=True)
            self.assertEqual(judge.llm_used_today, guard_before)
        self.assertTrue(guarded_consume(judge), "one normal call must remain")
        self.assertFalse(guarded_consume(judge), "and then the allowance is spent")
        self.assertEqual(judge.llm_used_today, 3)

    def test_round_130_keeps_day_one_and_131_resets(self):
        judge = sandbox.JudgeState()
        guarded_consume(judge)
        guarded_consume(judge)
        judge.note_round(130)
        self.assertEqual(judge.llm_used_today, 2, "round 130 is still day 1")
        judge.note_round(131)
        self.assertEqual(judge.llm_used_today, 0, "round 131 starts day 2")
        self.assertEqual(judge.llm_day, 2)

    def test_two_planner_states_keep_independent_allowances(self):
        first = PlannerState()
        second = PlannerState()
        first.note_round(10)
        second.note_round(10)
        self.assertTrue(guarded_consume(first.judge))
        self.assertEqual(first.judge.llm_used_today, 1)
        self.assertEqual(second.judge.llm_used_today, 0,
                         "a second match must not inherit the first match's quota")
        # Rolling the day on one state must not touch the other.
        first.note_round(131)
        self.assertEqual(first.judge.llm_used_today, 0)
        self.assertEqual(second.judge.llm_used_today, 0)

    def test_a_new_match_round_restart_resets_the_allowance(self):
        state = PlannerState()
        state.note_round(1200)
        guarded_consume(state.judge)
        self.assertEqual(state.judge.llm_used_today, 1)
        state.note_round(1)                     # a benchmark loop starts a new match
        self.assertEqual(state.judge.llm_used_today, 0)


class PlannerMemoryContractTests(unittest.TestCase):
    """The debug transport round-trips the planner through JSON, so memory must survive."""

    @staticmethod
    def round_trip(state):
        return PlannerState.load(json.loads(json.dumps(state.dump(), ensure_ascii=False)))

    def test_pending_prompt_and_command_round_trip(self):
        state = PlannerState()
        state.note_round(5)
        state.note_submission("题目正文", "ls -la", 5, in_task=False)
        rebuilt = self.round_trip(state)
        prompt = rebuilt.judge.pending_prompt
        command = rebuilt.judge.pending_cmd
        self.assertIsNotNone(prompt)
        self.assertIsNotNone(command)
        self.assertEqual((prompt.kind, prompt.sent_round, prompt.payload, prompt.purpose),
                         ("prompt", 5, "题目正文", ""))
        self.assertEqual((command.kind, command.sent_round, command.payload, command.purpose),
                         ("cmd", 5, "ls -la", ""))
        self.assertEqual(rebuilt.judge.last_prompt, "题目正文")
        self.assertEqual(rebuilt.judge.last_command, "ls -la")
        self.assertEqual((rebuilt.prompts_sent, rebuilt.commands_sent), (1, 1))

    def test_current_task_text_and_submission_records_round_trip(self):
        state = PlannerState()
        state.note_round(48)
        cycle = TaskCycle(point={"x": 6, "y": 5}, accepted_round=48,
                          task_type="自进化类1", description="端口：8080 协议：tcp",
                          timeout_rounds=25)
        cycle.record(49, "端口=8080; 协议=tcp", "keyword-fill")
        cycle.record(52, "端口=8080; 协议=udp", "llm-ask")
        cycle.best_rate = 0.667
        state.tasks["cycle"] = cycle

        rebuilt = self.round_trip(state)
        restored = rebuilt.tasks["cycle"]
        self.assertEqual(restored.description, "端口：8080 协议：tcp")
        self.assertEqual(restored.task_type, "自进化类1")
        self.assertEqual(restored.point, {"x": 6, "y": 5})
        self.assertEqual([(item.round_no, item.answer, item.source) for item in restored.submissions],
                         [(49, "端口=8080; 协议=tcp", "keyword-fill"),
                          (52, "端口=8080; 协议=udp", "llm-ask")])
        self.assertEqual(restored.last_answer, "端口=8080; 协议=udp")
        self.assertEqual(restored.phase, "submitted")
        self.assertAlmostEqual(restored.best_rate, 0.667, places=3)

    def test_solver_notes_round_trip_through_json(self):
        state = PlannerState()
        state.note_round(3)
        notes = {"llm_asked": True, "attempts": 2,
                 "nested": {"a": [1, 2], "b": {"c": "d"}}}
        state.tasks["solver_notes"] = notes
        rebuilt = self.round_trip(state)
        self.assertEqual(rebuilt.tasks["solver_notes"], notes)

    def test_allowance_and_command_counters_round_trip(self):
        state = PlannerState()
        state.note_round(5)
        self.assertTrue(guarded_consume(state.judge))
        self.assertTrue(guarded_consume(state.judge))
        state.judge.cmd_runs = 3
        state.judge.cmd_failures = 1
        rebuilt = self.round_trip(state)
        self.assertEqual(rebuilt.judge.llm_used_today, 2)
        self.assertEqual(rebuilt.judge.llm_day, state.judge.llm_day)
        self.assertEqual((rebuilt.judge.cmd_runs, rebuilt.judge.cmd_failures), (3, 1))

    def test_same_round_result_is_not_mistaken_for_a_new_result(self):
        state = PlannerState()
        state.note_round(5)
        state.note_submission("q", "ls", 5, in_task=False)
        # The judge answers "last round"; a same-round payload is not that answer.
        summary = state.note_results({"llmResp": "answer", "lastCmdResult": "[exitCode:0]\nok"}, 5)
        self.assertEqual(summary, {})
        self.assertIsNotNone(state.judge.pending_prompt)
        self.assertIsNotNone(state.judge.pending_cmd)
        self.assertEqual((state.judge.llm_responses, state.judge.cmd_runs), (0, 0))
        # One round later the same payload is the real result.
        summary = state.note_results({"llmResp": "answer", "lastCmdResult": "[exitCode:0]\nok"}, 6)
        self.assertEqual(summary, {"llm": "answered", "cmd": "exit:0"})
        self.assertIsNone(state.judge.pending_prompt)
        self.assertIsNone(state.judge.pending_cmd)
        self.assertEqual((state.judge.llm_responses, state.judge.cmd_runs), (1, 1))
        # A further round must not count the already-consumed result again.
        self.assertEqual(state.note_results({"llmResp": "answer", "lastCmdResult": "[exitCode:0]\nok"}, 7), {})
        self.assertEqual((state.judge.llm_responses, state.judge.cmd_runs), (1, 1))

    def test_command_result_round_trips(self):
        state = PlannerState()
        state.note_round(5)
        state.note_submission(None, "exit 3", 5, in_task=True)
        state.note_results({"lastCmdResult": "[exitCode:3]\nboom"}, 6)
        rebuilt = self.round_trip(state)
        self.assertEqual(rebuilt.judge.last_result.status, "exit")
        self.assertEqual(rebuilt.judge.last_result.exit_code, 3)
        self.assertEqual(rebuilt.judge.last_result.output, "boom")
        self.assertEqual(rebuilt.judge.cmd_runs, 1)


if __name__ == "__main__":
    unittest.main()