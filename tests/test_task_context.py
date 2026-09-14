"""Independent P0b tests for the bounded task context and public confirmation.

Fixtures are hand-written public observations. No network, no real LLM and no
private simulator state is used.

    python -m unittest discover -s tests -p test_task_context.py -v
"""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))

from agent import task_context as tc  # noqa: E402
from agent import tasks  # noqa: E402


def cycle(point=(6, 5), *, accepted=1, text="", timeout=20, phase="accepted", ended=0):
    item = tasks.TaskCycle(point={"x": point[0], "y": point[1]}, accepted_round=accepted,
                           description=text, timeout_rounds=timeout)
    item.phase = phase
    item.ended_round = ended
    return item


def observation(*, round_no=2, team="challenger", pioneer_pos=(6, 5), health=200,
                zones=None, player_tasks=None, phase_task="Task text", ready=True):
    if zones is None:
        zones = [{"pos": {"x": 6, "y": 5}, "neutralType": f"{team}TaskPoint1"}]
    pioneer = {"id": 10011, "roleType": "pioneer", "health": health,
               "pos": {"x": pioneer_pos[0], "y": pioneer_pos[1]}, "backpack": []}
    return {"roundNo": round_no, "phaseTask": phase_task,
            "mapInfo": {"width": 41, "height": 32, "zones": zones},
            "teamOur": {"type": team, "teamId": "t1", "goldNum": 75, "totalScore": 0,
                        "playerTasks": player_tasks if player_tasks is not None else [
                            {"taskType": "自进化类1", "taskPosition": {"x": 6, "y": 5},
                             "isValid": ready, "timeoutRounds": 20, "coldDownRounds": 0}],
                        "roles": [pioneer]},
            "teamEnemy": {"roles": []}, "robot": {"roles": []}, "errors": []}


class PublicConfirmationTests(unittest.TestCase):
    def test_published_text_in_range_and_own_zone_confirms(self):
        result = tc.public_task_confirmed(observation(), cycle(text="Task text"))
        self.assertTrue(result.confirmed)
        self.assertEqual(result.reason, "confirmed")
        self.assertTrue(result.generation)
        self.assertTrue(result.evidence["region"] in ("own_zone", "own_player_tasks"))

    def test_local_cycle_with_no_public_region_is_not_evidence(self):
        payload = observation(zones=[], player_tasks=[])
        result = tc.public_task_confirmed(payload, cycle(text="Task text"))
        self.assertFalse(result.confirmed)
        self.assertIn(result.reason, ("no_public_own_region", "anchor_not_own_region"))

    def test_enemy_zone_does_not_confirm(self):
        payload = observation(zones=[{"pos": {"x": 6, "y": 5},
                                      "neutralType": "defenderTaskPoint1"}],
                              player_tasks=[{"taskPosition": {"x": 6, "y": 5}}],
                              team="challenger")
        # Our own playerTasks list is attributable even next to an enemy zone, so
        # instead remove it to prove the enemy zone alone is not evidence.
        payload["teamOur"]["playerTasks"] = []
        result = tc.public_task_confirmed(payload, cycle(text="Task text"))
        self.assertFalse(result.confirmed)

    def test_missing_phase_text_never_confirms(self):
        result = tc.public_task_confirmed(observation(phase_task=""), cycle(text="Task text"))
        self.assertFalse(result.confirmed)

    def test_is_valid_false_does_not_end_a_published_task(self):
        result = tc.public_task_confirmed(observation(ready=False), cycle(text="Task text"))
        self.assertTrue(result.confirmed, "isValid only gates re-acceptance, not an active task")

    def test_known_timeout_expiry_is_not_confirmed(self):
        payload = observation(round_no=100)
        item = cycle(text="Task text", accepted=1, timeout=20)
        self.assertFalse(tc.public_task_confirmed(payload, item).confirmed)

    def test_unknown_timeout_is_neither_zero_nor_forever(self):
        payload = observation(round_no=500)
        item = cycle(text="Task text", accepted=1, timeout=0)
        result = tc.public_task_confirmed(payload, item)
        self.assertTrue(result.confirmed, "published text + in-range pioneer still confirm")
        self.assertFalse(result.evidence["timeout_known"])

    def test_pioneer_without_coordinates_is_not_in_range(self):
        payload = observation()
        payload["teamOur"]["roles"][0].pop("pos")
        self.assertFalse(tc.public_task_confirmed(payload, cycle(text="Task text")).confirmed)

    def test_pioneer_out_of_range_is_not_confirmed(self):
        payload = observation(pioneer_pos=(20, 20))
        self.assertFalse(tc.public_task_confirmed(payload, cycle(text="Task text")).confirmed)

    def test_dead_pioneer_is_not_confirmed(self):
        payload = observation(health=0)
        self.assertFalse(tc.public_task_confirmed(payload, cycle(text="Task text")).confirmed)


class ContextBuildTests(unittest.TestCase):
    def test_long_fact_text_is_clipped_and_recorded(self):
        envelope = tc.build_context("task", "g", "d", task_text="t",
                                    facts=[{"text": "x" * 100000, "kind": "inferred"}])
        self.assertLessEqual(len(envelope.facts[0]["text"]), tc.ITEM_TEXT_LIMIT)
        self.assertEqual(envelope.truncation["facts_dropped"], 0)

    def test_long_task_text_is_clipped(self):
        envelope = tc.build_context("task", "g", "d", task_text="y" * 100000)
        self.assertEqual(len(envelope.task_text), tc.TEXT_LIMIT)
        self.assertTrue(envelope.task_text_truncated)
        self.assertTrue(envelope.check()["task_text_truncated"])

    def test_item_counts_are_bounded(self):
        envelope = tc.build_context("task", "g", "d", facts=[{"text": "f"}] * 100)
        self.assertLessEqual(len(envelope.facts), tc.FACT_LIMIT)
        self.assertEqual(envelope.truncation["facts_dropped"], 100 - tc.FACT_LIMIT)

    def test_render_prompt_reserves_header_and_nonce_room(self):
        envelope = tc.build_context("task", "g", "d", task_text="z" * 3000)
        prompt = tc.render_prompt(envelope, instruction="只输出答案", nonce="n" + "a" * 15)
        self.assertLessEqual(len(prompt), tc.PROMPT_LIMIT)
        self.assertTrue(prompt.startswith("只输出答案"))
        self.assertIn("request_id: n", prompt)
        self.assertIn(f"generation={envelope.generation}", prompt)

    def test_prompt_view_cap_includes_footer(self):
        envelope = tc.build_context("task", "g", "d", task_text="z" * 3000)
        view = envelope.prompt_view(120)
        self.assertLessEqual(len(view), 120)


class PromptRenderingTests(unittest.TestCase):
    def test_truncated_context_without_trace_says_unavailable(self):
        envelope = tc.build_context("task", "g", "d", task_text="x" * (tc.TEXT_LIMIT + 50))
        view = envelope.prompt_view(6000)
        self.assertIn("不可用", view)
        self.assertNotIn("本地trace", view)

    def test_verified_trace_reference_is_named(self):
        envelope = tc.build_context("task", "g", "d", task_text="x" * (tc.TEXT_LIMIT + 50),
                                    trace_refs=[{"event_id": "run:7", "round": 3}])
        view = envelope.prompt_view(6000)
        self.assertIn("run:7", view)

    def test_fact_kind_and_conflict_are_preserved(self):
        envelope = tc.build_context(
            "task", "g", "d", task_text="t",
            facts=[{"text": "a", "kind": "inferred", "round": 2},
                   {"text": "b", "kind": "observed", "conflict": True, "round": 3},
                   {"text": "c", "round": 4}])
        view = envelope.prompt_view(6000)
        self.assertIn("[inferred]", view)
        self.assertIn("[observed][冲突]", view)
        self.assertIn("[unclassified]", view)

    def test_metadata_and_display_agree(self):
        envelope = tc.build_context("task", "g", "d", task_text="t",
                                    facts=[{"text": "a", "kind": "assumed"}])
        dumped = json.loads(json.dumps(envelope.dump()))
        restored, reason = tc.ContextEnvelope.load(dumped)
        self.assertEqual(reason, "ok")
        self.assertIn("[assumed]", restored.prompt_view(6000))


class StrictLoadTests(unittest.TestCase):
    def envelope_dump(self, **overrides):
        envelope = tc.build_context("task", "g", "d", task_text="hello")
        data = envelope.dump()
        data.update(overrides)
        return data

    def test_unknown_schema_degrades(self):
        envelope, reason = tc.ContextEnvelope.load(self.envelope_dump(schema="newer/9"))
        self.assertIsNone(envelope)
        self.assertEqual(reason, "unknown_schema")

    def test_oversized_text_is_rejected(self):
        envelope, reason = tc.ContextEnvelope.load(self.envelope_dump(task_text="x" * 100000))
        self.assertIsNone(envelope)
        self.assertEqual(reason, "task_text_out_of_bounds")

    def test_truthy_string_flag_is_rejected(self):
        envelope, reason = tc.ContextEnvelope.load(self.envelope_dump(task_text_truncated="false"))
        self.assertIsNone(envelope)
        self.assertEqual(reason, "task_text_truncated_not_bool")

    def test_hash_mismatch_is_rejected(self):
        envelope, reason = tc.ContextEnvelope.load(self.envelope_dump(task_text_sha256="0" * 64))
        self.assertIsNone(envelope)
        self.assertEqual(reason, "task_text_sha256_mismatch")

    def test_too_many_facts_is_rejected(self):
        envelope, reason = tc.ContextEnvelope.load(self.envelope_dump(facts=[{"text": "x"}] * 100))
        self.assertIsNone(envelope)
        self.assertEqual(reason, "facts_too_many")

    def test_malformed_entry_is_rejected(self):
        envelope, reason = tc.ContextEnvelope.load(self.envelope_dump(facts=["not a dict"]))
        self.assertIsNone(envelope)
        self.assertEqual(reason, "facts_malformed_entry")

    def test_oversized_nested_item_is_rejected(self):
        envelope, reason = tc.ContextEnvelope.load(
            self.envelope_dump(facts=[{"text": "x" * 100000}]))
        self.assertIsNone(envelope)
        self.assertEqual(reason, "facts_malformed_entry")

    def test_roundtrip_preserves_bounded_fields(self):
        original = tc.build_context("task", "g", "d", task_text="hello",
                                    answer_contract={"fields": "a=1"},
                                    facts=[{"text": "f", "kind": "observed", "round": 1}],
                                    open_questions=["q"], trace_refs=[{"event_id": "e"}])
        restored, reason = tc.ContextEnvelope.load(json.loads(json.dumps(original.dump())))
        self.assertEqual(reason, "ok")
        self.assertEqual(restored.task_text, "hello")
        self.assertEqual(restored.answer_contract, {"fields": "a=1"})
        self.assertEqual(restored.facts[0]["kind"], "observed")
        self.assertEqual(restored.trace_refs[0]["event_id"], "e")


class ContextStoreTests(unittest.TestCase):
    def test_store_is_bounded_and_reports_loss(self):
        store = tc.ContextStore(limit=2)
        for index in range(4):
            store.put(tc.build_context("task", f"g{index}", "d", task_text="t"))
        self.assertEqual(len(store.dump()["items"]), 2)
        self.assertEqual(store.dropped, 2)

    def test_store_roundtrip_and_unknown_schema(self):
        store = tc.ContextStore()
        store.put(tc.build_context("task", "g", "d", task_text="text",
                                   answer_contract={"x": "y"}))
        restored = tc.ContextStore()
        self.assertEqual(restored.load(json.loads(json.dumps(store.dump()))), "ok")
        self.assertEqual(restored.get("task", "g").task_text, "text")
        broken = tc.ContextStore()
        self.assertEqual(broken.load({"schema": "newer"}), "unknown_schema")
        self.assertIsNone(broken.get("task", "g"))


if __name__ == "__main__":
    unittest.main()
