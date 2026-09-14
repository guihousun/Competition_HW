"""Independent P0b tests for the shared cognitive-channel router.

Covers the acceptance groups: shared 3-call allowance across owners, cross-day and
duplicate accounting, stale/same-round/unsolicited receipt handling, two-side
isolation, strict dump/load, structural correlation and model-plan validation.

    python -m unittest discover -s tests -p test_llm_router.py -v
"""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))

from agent import llm_router as lr  # noqa: E402
from agent import task_context as tc  # noqa: E402
from agent.sandbox import LLM_DAILY_QUOTA, JudgeState  # noqa: E402


def confirmed(generation="g", source="d", *, ok=True):
    return tc.TaskConfirmation(ok, generation, source, "confirmed" if ok else "no", {})


def router_with(used=0):
    judge = JudgeState()
    judge.llm_used_today = used
    return lr.LLMRouter(judge), judge


class BudgetSharingTests(unittest.TestCase):
    def test_news_and_treasure_share_the_three_call_allowance(self):
        router, judge = router_with()
        router.note_round(1)
        news1 = router.offer("news", "n1", "s", kind="prompt", payload="news one")
        self.assertIs(router.select({"roundNo": 1}), news1)
        self.assertTrue(router.mark_emitted(news1, round_no=1))

        self.assertEqual(router.ingest({"roundNo": 2, "llmResp": "a1"})["received"], 1)
        treasure = router.offer("treasure", "t1", "s", kind="prompt", payload="treasure one")
        self.assertIs(router.select({"roundNo": 2}), treasure)
        self.assertTrue(router.mark_emitted(treasure, round_no=2))

        self.assertEqual(router.ingest({"roundNo": 3, "llmResp": "a2"})["received"], 1)
        news2 = router.offer("news", "n2", "s", kind="prompt", payload="news two")
        self.assertIs(router.select({"roundNo": 3}), news2)
        self.assertTrue(router.mark_emitted(news2, round_no=3))

        self.assertEqual(router.ingest({"roundNo": 4, "llmResp": "a3"})["received"], 1)
        self.assertEqual(judge.llm_used_today, LLM_DAILY_QUOTA)
        extra = router.offer("news", "n3", "s", kind="prompt", payload="news three")
        self.assertIsNone(router.select({"roundNo": 4}),
                          "the fourth ordinary call must not be sent")
        self.assertEqual(judge.llm_used_today, LLM_DAILY_QUOTA)
        self.assertEqual(extra.status, "queued")

    def test_confirmed_task_request_still_allowed_and_ordinary_balance_unchanged(self):
        router, judge = router_with(used=LLM_DAILY_QUOTA)
        router.note_round(1)
        task = router.offer("task", "g", "d", kind="prompt", payload="solve the task")
        chosen = router.select({"roundNo": 1}, confirmed_task=confirmed("g"))
        self.assertIs(chosen, task)
        self.assertTrue(router.mark_emitted(task, round_no=1))
        self.assertEqual(task.budget_class, lr.BUDGET_TASK_EXEMPT)
        self.assertEqual(judge.llm_used_today, LLM_DAILY_QUOTA,
                         "a confirmed task call is exempt and does not change the balance")

    def test_caller_hint_cannot_confer_a_task_exemption(self):
        router, judge = router_with()
        router.note_round(1)
        router.offer("task", "g", "d", kind="prompt", payload="solve", in_task=True)
        self.assertIsNone(router.select({"roundNo": 1}, confirmed_task=None))
        self.assertEqual(judge.llm_used_today, 0)

    def test_invalid_task_work_is_dropped_not_launched(self):
        router, judge = router_with()
        router.note_round(1)
        offer = router.offer("task", "g", "d", kind="prompt", payload="solve")
        self.assertIsNone(router.select({"roundNo": 1}, confirmed_task=confirmed(ok=False)))
        self.assertEqual(offer.status, "obsolete")

    def test_cmd_does_not_consume_the_llm_allowance(self):
        router, judge = router_with(used=LLM_DAILY_QUOTA)
        router.note_round(1)
        cmd = router.offer("task", "g", "d", kind="cmd", payload="ls -la")
        self.assertIs(router.select({"roundNo": 1}, confirmed_task=confirmed("g")), cmd)
        self.assertTrue(router.mark_emitted(cmd, round_no=1))
        self.assertEqual(judge.llm_used_today, LLM_DAILY_QUOTA)

    def test_cmd_requires_a_confirmed_task(self):
        router, _judge = router_with()
        router.note_round(1)
        cmd = router.offer("task", "g", "d", kind="cmd", payload="ls")
        self.assertIsNone(router.select({"roundNo": 1}, confirmed_task=None))
        self.assertEqual(cmd.status, "obsolete")


class AccountingEdgeTests(unittest.TestCase):
    def test_sent_at_130_reply_at_131_is_charged_once(self):
        router, judge = router_with()
        router.note_round(130)
        request = router.offer("news", "g", "s", kind="prompt", payload="q")
        self.assertIs(router.select({"roundNo": 130}), request)
        self.assertTrue(router.mark_emitted(request, round_no=130))
        self.assertEqual(judge.llm_used_today, 1)
        judge.note_round(131)                       # day 2 resets the allowance
        self.assertEqual(router.ingest({"roundNo": 131, "llmResp": "answer"})["received"], 1)
        self.assertEqual(router.sent_count, 1, "the send was counted once")
        self.assertEqual(judge.llm_used_today, 0, "a day-2 reply does not re-charge day 1")

    def test_repeated_input_does_not_double_charge_or_resend(self):
        router, judge = router_with()
        router.note_round(1)
        request = router.offer("news", "g", "s", kind="prompt", payload="q")
        self.assertIs(router.select({"roundNo": 1}), request)
        self.assertTrue(router.mark_emitted(request, round_no=1))
        again = router.offer("news", "g", "s", kind="prompt", payload="q")
        self.assertIs(again, request)
        self.assertIsNone(router.select({"roundNo": 1}))
        self.assertEqual(judge.llm_used_today, 1)
        self.assertFalse(router.mark_emitted(request, round_no=1))

    def test_mark_emitted_does_not_overwrite_an_in_flight_request(self):
        router, _judge = router_with()
        router.note_round(1)
        first = router.offer("news", "g1", "s", kind="prompt", payload="first")
        second = router.offer("news", "g2", "s", kind="prompt", payload="second")
        self.assertTrue(router.mark_emitted(first, round_no=1))
        self.assertFalse(router.mark_emitted(second, round_no=1),
                         "the second must not replace the request in flight")
        self.assertIs(router.pending["prompt"], first)

    def test_uncertain_reservation_stays_charged(self):
        router, judge = router_with()
        router.note_round(1)
        request = router.offer("news", "g", "s", kind="prompt", payload="q")
        router.select({"roundNo": 1})
        self.assertTrue(router.mark_uncertain(request))
        self.assertEqual(judge.llm_used_today, 1, "an uncertain write is reserved, not refunded")
        self.assertEqual(router.uncertain_reserved, 1)


class ReceiptAttributionTests(unittest.TestCase):
    def test_unsolicited_reply_is_quarantined_without_breaking(self):
        router = lr.LLMRouter(JudgeState())
        info = router.ingest({"roundNo": 2, "llmResp": "old unsolicited reply"})
        self.assertEqual(info["quarantined"], 1)
        self.assertEqual(router.received_results(), [])

    def test_same_round_payload_is_not_a_new_result(self):
        router, _judge = router_with()
        router.note_round(5)
        request = router.offer("news", "g", "s", kind="prompt", payload="q")
        router.select({"roundNo": 5})
        router.mark_emitted(request, round_no=5)
        info = router.ingest({"roundNo": 5, "llmResp": "earlier answer"})
        self.assertEqual(info["same_round"], 1)
        self.assertIs(router.pending["prompt"], request)

    def test_empty_then_late_reply_expires(self):
        router, _judge = router_with()
        router.note_round(1)
        request = router.offer("news", "g", "s", kind="prompt", payload="q")
        router.select({"roundNo": 1})
        router.mark_emitted(request, round_no=1)
        self.assertEqual(router.ingest({"roundNo": 2, "llmResp": ""})["expired"], 0)
        info = router.ingest({"roundNo": 1 + lr.WAIT_ROUNDS + 1, "llmResp": ""})
        self.assertEqual(info["expired"], 1)
        self.assertIsNone(router.pending["prompt"])

    def test_reply_after_wait_is_quarantined_even_if_nonempty(self):
        router, _judge = router_with()
        router.note_round(1)
        request = router.offer("news", "g", "s", kind="prompt", payload="q")
        router.select({"roundNo": 1})
        router.mark_emitted(request, round_no=1)
        info = router.ingest({"roundNo": 99, "llmResp": "very late answer"})
        self.assertEqual(info["received"], 0)
        self.assertGreaterEqual(info["quarantined"], 1)
        self.assertEqual(router.received_results(), [])

    def test_duplicate_old_reply_after_delivery_is_quarantined(self):
        router, _judge = router_with()
        router.note_round(1)
        request = router.offer("news", "g", "s", kind="prompt", payload="q")
        router.select({"roundNo": 1})
        router.mark_emitted(request, round_no=1)
        self.assertEqual(router.ingest({"roundNo": 2, "llmResp": "answer"})["received"], 1)
        # The same text is not globally de-duplicated, but with no pending request
        # it cannot be attributed and is quarantined rather than reused.
        self.assertEqual(router.ingest({"roundNo": 3, "llmResp": "answer"})["quarantined"], 1)
        self.assertEqual(len(router.received_results()), 1)

    def test_stale_generation_reply_is_quarantined(self):
        router, _judge = router_with()
        router.note_round(5)
        request = router.offer("task", "g1", "d", kind="prompt", payload="q")
        router.select({"roundNo": 5}, confirmed_task=confirmed("g1"))
        router.mark_emitted(request, round_no=5)
        info = router.ingest({"roundNo": 6, "llmResp": "answer"},
                             confirmed_task=confirmed("g2"))
        self.assertEqual(info["stale"], 1)
        self.assertEqual(router.received_results(), [])

    def test_task_ending_quarantines_the_pending_reply(self):
        router, _judge = router_with()
        router.note_round(5)
        request = router.offer("task", "g", "d", kind="prompt", payload="q")
        router.select({"roundNo": 5}, confirmed_task=confirmed("g"))
        router.mark_emitted(request, round_no=5)
        info = router.ingest({"roundNo": 6, "llmResp": "answer"},
                             confirmed_task=confirmed(ok=False))
        self.assertEqual(info["stale"], 1)
        self.assertEqual(router.received_results(), [])

    def test_two_sides_do_not_share_receipts_or_quota(self):
        router_a, judge_a = router_with()
        router_b, judge_b = router_with()
        router_a.note_round(1)
        router_b.note_round(1)
        request = router_a.offer("news", "g", "s", kind="prompt", payload="side a")
        router_a.select({"roundNo": 1})
        router_a.mark_emitted(request, round_no=1)
        self.assertEqual(judge_a.llm_used_today, 1)
        self.assertEqual(judge_b.llm_used_today, 0)
        self.assertEqual(router_b.ingest({"roundNo": 2, "llmResp": "leak"})["received"], 0)
        self.assertEqual(router_a.ingest({"roundNo": 2, "llmResp": "a"})["received"], 1)


class CorrelationTests(unittest.TestCase):
    def test_json_request_id_must_match_top_level(self):
        token = "n" + "a" * 15
        good = json.dumps({"request_id": token, "plan": {"kind": "answer", "answer": "x"}})
        self.assertTrue(lr.verify_json_token(good, token))
        mismatch = json.dumps({"request_id": "OLD", "plan": {"note": f"nonce={token}"}})
        self.assertFalse(lr.verify_json_token(mismatch, token))
        mentioned_only = json.dumps({"plan": {"note": f"nonce={token}"}})
        self.assertFalse(lr.verify_json_token(mentioned_only, token))

    def test_json_duplicate_keys_and_non_object_are_rejected(self):
        token = "n" + "b" * 15
        self.assertFalse(lr.verify_json_token('{"request_id":"%s","request_id":"x"}' % token, token))
        self.assertFalse(lr.verify_json_token("[1,2,3]", token))

    def test_json_nonce_key_also_correlates(self):
        token = "n" + "c" * 15
        self.assertTrue(lr.verify_json_token(json.dumps({"nonce": token}), token))

    def test_command_header_is_anchored(self):
        token = "n" + "d" * 15
        header = lr.COMMAND_HEADER_TEMPLATE.format(token)
        self.assertTrue(lr.verify_command_header(f"{header}\noutput", token))
        self.assertFalse(lr.verify_command_header(f"prefix\n{header}\noutput", token))

    def test_shape_decides_the_matcher_and_mismatch_is_quarantined(self):
        router, _judge = router_with()
        token = lr.LLMRouter.new_nonce()
        router.note_round(1)
        request = router.offer("news", "g", "s", kind="prompt", payload="q",
                               nonce=token, expected_result_shape="plan_json")
        router.select({"roundNo": 1})
        router.mark_emitted(request, round_no=1)
        bad = json.dumps({"request_id": "OTHER", "note": f"nonce={token}"})
        info = router.ingest({"roundNo": 2, "llmResp": bad})
        self.assertEqual(info["received"], 0)
        self.assertEqual(info["quarantined"], 1)

    def test_verified_json_result_is_delivered_with_genuine_digest(self):
        router, _judge = router_with()
        token = lr.LLMRouter.new_nonce()
        router.note_round(1)
        request = router.offer("news", "g", "s", kind="prompt", payload="q",
                               nonce=token, expected_result_shape="plan_json")
        router.select({"roundNo": 1})
        router.mark_emitted(request, round_no=1)
        good = json.dumps({"request_id": token, "plan": {"kind": "answer", "answer": "42"}})
        self.assertEqual(router.ingest({"roundNo": 2, "llmResp": good})["received"], 1)
        results = router.received_results("prompt")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].text, good)
        self.assertEqual(results[0].text_sha256, tc.sha256_text(good))

    def test_nonce_with_unsupported_shape_never_falls_back(self):
        router, _judge = router_with()
        token = lr.LLMRouter.new_nonce()
        router.note_round(1)
        request = router.offer("news", "g", "s", kind="prompt", payload="q", nonce=token,
                               expected_result_shape="text")
        router.select({"roundNo": 1})
        router.mark_emitted(request, round_no=1)
        info = router.ingest({"roundNo": 2, "llmResp": f"nonce={token}"})
        self.assertEqual(info["received"], 0)


class ModelPlanTests(unittest.TestCase):
    def test_valid_plan_is_accepted(self):
        plan = lr.LLMRouter.parse_model_plan(
            json.dumps({"kind": "run", "command": "cat data.txt", "reason": "inspect"}))
        self.assertEqual(plan["kind"], "run")
        self.assertEqual(plan["command"], "cat data.txt")

    def test_forged_official_fields_and_tools_are_rejected(self):
        for forged in ({"kind": "run", "roleCommandMap": {"1": {"action": "move"}}},
                       {"kind": "answer", "answer": "x", "executeCmd": "rm -rf /"},
                       {"kind": "run", "command": "x", "extra": "tool"},
                       {"kind": "teleport", "command": "x"},
                       {"kind": "run"}):
            self.assertIsNone(lr.LLMRouter.parse_model_plan(json.dumps(forged)), forged)

    def test_non_json_and_oversized_are_rejected(self):
        self.assertIsNone(lr.LLMRouter.parse_model_plan("not json"))
        self.assertIsNone(lr.LLMRouter.parse_model_plan({"kind": "run", "command": "x" * 50000}))


class StrictPersistenceTests(unittest.TestCase):
    def test_roundtrip_preserves_counter_history_and_charge_marker(self):
        router, _judge = router_with()
        router.note_round(1)
        request = router.offer("news", "g", "s", kind="prompt", payload="q")
        router.select({"roundNo": 1})
        router.mark_emitted(request, round_no=1)
        restored = lr.LLMRouter(JudgeState())
        self.assertEqual(restored.load(json.loads(json.dumps(router.dump()))), "ok")
        self.assertEqual(restored._counter, router._counter)
        self.assertEqual(restored._charged_round, 1)
        self.assertIsNotNone(restored.pending["prompt"])
        self.assertEqual(restored.pending["prompt"].content_key, request.content_key)

    def test_roundtrip_does_not_resend_or_reuse_ids(self):
        router, _judge = router_with()
        router.note_round(1)
        request = router.offer("news", "g", "s", kind="prompt", payload="q")
        router.select({"roundNo": 1})
        router.mark_emitted(request, round_no=1)
        restored = lr.LLMRouter(JudgeState())
        restored.load(json.loads(json.dumps(router.dump())))
        same = restored.offer("news", "g", "s", kind="prompt", payload="q")
        self.assertEqual(same.request_id, request.request_id, "same content is not re-sent")
        self.assertIsNone(restored.select({"roundNo": 1}))
        fresh = restored.offer("news", "g", "s", kind="prompt", payload="different")
        self.assertNotEqual(fresh.request_id, request.request_id, "ids must not be reused")

    def test_degraded_state_survives_roundtrip(self):
        router, _judge = router_with()
        router.degraded = "unknown_schema"
        restored = lr.LLMRouter(JudgeState())
        self.assertEqual(restored.load(json.loads(json.dumps(router.dump()))), "unknown_schema")
        self.assertEqual(restored.degraded, "unknown_schema")

    def test_unknown_schema_degrades_and_grants_nothing(self):
        router, _judge = router_with()
        self.assertEqual(router.load({"schema": "newer/9"}), "unknown_schema")
        self.assertIsNone(router.offer("news", "g", "s", kind="prompt", payload="q"))

    def test_queue_cap_and_bool_types_are_strict(self):
        router, _judge = router_with()
        data = router.dump()
        data["queue"] = [{} for _ in range(lr.MAX_QUEUE + 1)]
        self.assertEqual(router.load(data), "malformed")
        data = router.dump()
        data["allow_prompt"] = "false"
        self.assertEqual(router.load(data), "malformed")

    def test_oversized_request_state_is_rejected(self):
        router, _judge = router_with()
        data = router.dump()
        data["pending_prompt"] = {"request_id": "r", "owner": "news", "generation": "g",
                                  "source_digest": "s", "kind": "prompt",
                                  "payload": "x" * (lr.PROMPT_PAYLOAD_LIMIT + 1),
                                  "status": "waiting", "charged": True,
                                  "expected_result_shape": "text"}
        self.assertNotEqual(router.load(data), "ok")
        self.assertIsNone(router.pending["prompt"])


class PayloadBoundTests(unittest.TestCase):
    def test_oversized_prompt_is_rejected_not_chopped(self):
        router, _judge = router_with()
        self.assertIsNone(router.offer("news", "g", "s", kind="prompt",
                                       payload="x" * (lr.PROMPT_PAYLOAD_LIMIT + 1)))

    def test_oversized_command_is_rejected(self):
        router, _judge = router_with()
        self.assertIsNone(router.offer("task", "g", "s", kind="cmd",
                                       payload="y" * (lr.COMMAND_PAYLOAD_LIMIT + 1)))

    def test_prompt_limit_is_larger_than_command_limit(self):
        self.assertGreater(lr.PROMPT_PAYLOAD_LIMIT, lr.COMMAND_PAYLOAD_LIMIT)
        self.assertLessEqual(lr.COMMAND_PAYLOAD_LIMIT, tc.PROMPT_LIMIT)


if __name__ == "__main__":
    unittest.main()
