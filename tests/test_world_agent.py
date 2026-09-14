"""Public source -> model interpretation -> actual policy seams (R06/R07).

Model replies are scripted here; expectations are independent public examples.
No hidden simulator schedule or answer enters the coordinator.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Demo/CoreGeek/src"))
from agent import brain, planner
from agent.world_agent import WorldAgent
from test_tasks import observation
from test_metal_economy import board

NEWS = "今天铁矿可开采；明天开始停工两天，停工期间铁价上涨，之后恢复。"
SITE = "祭坛位于横坐标3、纵坐标4的交点，用品和时间未知。"
ITEMS = "同一祭坛需要AcientTablet和AncientScroll，各一份，时间尚未公布。"
WINDOW = "此前祭坛在第三天白昼开始的前30回合开启，地点与用品要求不变。"


class WorldAgentTests(unittest.TestCase):
    def setUp(self):
        self.state = planner.PlannerState()
        env = patch.dict(os.environ, {brain.WORLD_AGENT_ENV: "on", brain.TASK_AGENT_ENV: "off"})
        env.start()
        self.addCleanup(env.stop)

    def run_round(self, payload):
        self.state.note_round(payload["roundNo"])
        response = brain.plan_for_state(payload, self.state, judge_tasks=False)
        self.state.note_submission(response.prompt, response.execute, payload["roundNo"], bool(self.state.tasks.get("cycle")))
        self.state = planner.PlannerState.load(json.loads(json.dumps(self.state.dump())))
        self.assertFalse(self.state.team_agent.degraded)
        return response.build()

    def proof(self, owner, text):
        record = next(r for r in self.state.team_agent.world.sources[owner] if r["text"] == text)
        return [{"sourceId": record["id"], "quote": text}]

    def reply(self, owner, value):
        link = self.state.team_agent.world.links[owner]
        self.assertIsNotNone(link)
        return json.dumps({"request_id": link["token"], "events" if owner == "news" else "hypothesis": value}, ensure_ascii=False)

    def news_payload(self, round_no):
        payload = board(ores=(("iron", (15, 24)), ("copper", (15, 25))), round_no=round_no)
        payload.pop("_demo")
        payload["worldNews"]["officialNews"] = NEWS
        payload["vendorShopList"][1]["price"] = 9
        return payload

    def events(self):
        return [{"resource": "iron", "availability": "unavailable", "startDay": 2,
                 "endDay": 3, "priceDirection": "up", "evidence": self.proof("news", NEWS)}]

    def test_news_changes_actual_mining_targets_and_recovers_after_two_days(self):
        self.assertIn("prompt", self.run_round(self.news_payload(13)))
        payload = self.news_payload(14)
        payload["llmResp"] = self.reply("news", self.events())
        self.run_round(payload)
        for round_no, target in ((15, {"x": 15, "y": 24}), (143, {"x": 15, "y": 25}),
                                 (273, {"x": 15, "y": 25}), (403, {"x": 15, "y": 24})):
            payload = self.news_payload(round_no)
            reply = self.run_round(payload)
            command = reply["roleCommandMap"]["10010"]
            self.assertEqual(command["action"], "collect")
            self.assertEqual(command["targetPos"], [target])
            self.assertNotIn("prompt", reply, "unchanged publication is not reinterpreted on every day")
            self.assertEqual(brain._sellable_metals(payload)["iron"], 9, "live sale price stays authoritative")
        self.assertEqual(self.state.prompts_sent, 1)

    def test_unknown_price_magnitude_and_forged_quotes_are_rejected(self):
        self.run_round(self.news_payload(13))
        events = self.events()
        events[0]["price"] = 999
        payload = self.news_payload(14)
        payload["llmResp"] = self.reply("news", events)
        self.run_round(payload)
        self.assertEqual(self.state.team_agent.world.news_events, [])
        events = self.events()
        events[0]["evidence"][0]["quote"] = "a sentence never published"
        payload = self.news_payload(15)
        payload["llmResp"] = self.reply("news", events)
        self.run_round(payload)
        self.assertEqual(self.state.team_agent.world.news_events, [])
        self.assertEqual(self.state.team_agent.world.status["news"], "retry_limit")

    def partial(self, site=None, items=None, window=None):
        return {"site": site, "items": items,
                "opensAt": window[0] if window else None, "closesAt": window[1] if window else None,
                "uncertain": not (site and items and window), "evidence": {
                    "site": self.proof("treasure", SITE) if site else [],
                    "items": self.proof("treasure", ITEMS) if items else [],
                    "window": self.proof("treasure", WINDOW) if window else []}}

    def treasure_payload(self, round_no, text):
        payload = observation(round_no=round_no, role_pos=(4, 4), task_points=[])
        payload["mapInfo"]["zones"] = []
        payload["worldNews"]["folkLegends"] = text
        return payload

    def complete_treasure(self):
        self.run_round(self.treasure_payload(1, SITE))
        p = self.treasure_payload(2, SITE)
        p["llmResp"] = self.reply("treasure", self.partial(site={"x": 3, "y": 4}))
        self.run_round(p)
        self.assertFalse(self.state.team_agent.world.policy_view(2)["treasure"]["known"])
        self.run_round(self.treasure_payload(131, ITEMS))
        p = self.treasure_payload(132, ITEMS)
        p["llmResp"] = self.reply("treasure", self.partial(site={"x": 3, "y": 4}, items=["AcientTablet", "AncientScroll"]))
        self.run_round(p)
        self.assertFalse(self.state.team_agent.world.policy_view(132)["treasure"]["known"])
        response = self.run_round(self.treasure_payload(261, WINDOW))
        self.assertIn(SITE, response["prompt"])
        self.assertIn(ITEMS, response["prompt"])
        p = self.treasure_payload(262, WINDOW)
        p["llmResp"] = self.reply("treasure", self.partial(site={"x": 3, "y": 4}, items=["AcientTablet", "AncientScroll"], window=(261, 290)))
        p["teamOur"]["roles"][0]["backpack"] = ["AncientScroll", "AcientTablet"]
        return p, self.run_round(p)

    def test_three_day_clues_survive_json_and_produce_actual_summon(self):
        payload, response = self.complete_treasure()
        self.assertEqual(response["roleCommandMap"]["10011"], {
            "action": "summonTreasure", "targetPos": [{"x": 3, "y": 4}], "item": ["AcientTablet", "AncientScroll"]})
        self.assertEqual(len(self.state.team_agent.world.sources["treasure"]), 3)
        self.assertEqual(self.state.judge.llm_used_today, 1)
        payload["roundNo"] = 263
        payload["llmResp"] = ""
        payload["lastSummonTreasureResult"] = 1
        response = self.run_round(payload)
        self.assertNotEqual(response["roleCommandMap"].get("10011", {}).get("action"), "summonTreasure")
        self.assertTrue(self.state.team_agent.world.taken)

    def test_failed_summon_invalidates_guess_before_spending_again(self):
        payload, _ = self.complete_treasure()
        payload["roundNo"] = 263
        payload["llmResp"] = ""
        payload["lastSummonTreasureResult"] = 3
        response = self.run_round(payload)
        self.assertFalse(self.state.team_agent.world.policy_view(263)["treasure"]["known"])
        self.assertNotEqual(response["roleCommandMap"].get("10011", {}).get("action"), "summonTreasure")
        self.assertIn("prompt", response)
        self.assertIn('"code": 3', response["prompt"])

    def test_missing_inventory_cannot_be_bypassed_by_model_hypothesis(self):
        payload, _ = self.complete_treasure()
        payload["roundNo"] = 263
        payload["llmResp"] = ""
        payload["teamOur"]["roles"][0]["backpack"] = []
        response = self.run_round(payload)
        self.assertNotEqual(response["roleCommandMap"].get("10011", {}).get("action"), "summonTreasure")

    def test_private_future_data_never_enters_model_context(self):
        payload = self.treasure_payload(1, SITE)
        payload["_demo"] = {"future_clue": "PRIVATE-SECRET-UNPUBLISHED", "treasure": {"site": {"x": 40, "y": 31}}}
        response = self.run_round(payload)
        self.assertNotIn("PRIVATE-SECRET", response["prompt"])
        self.assertNotIn("future_clue", json.dumps(self.state.team_agent.world.dump()))
        self.assertFalse(self.state.team_agent.world.policy_view(1)["treasure"]["known"])

    def test_corrupted_json_or_false_source_cannot_restore_a_plan(self):
        self.run_round(self.news_payload(13))
        raw = self.state.team_agent.world.dump()
        raw["sources"]["news"][0]["text"] = "tampered"
        restored = WorldAgent.load(raw)
        self.assertTrue(restored.degraded)
        self.assertEqual(restored.policy_view(143)["unavailable"], [])

    def test_news_and_treasure_share_three_calls_then_fourth_waits(self):
        p = self.news_payload(13)
        self.run_round(p)
        p = self.news_payload(14)
        p["llmResp"] = self.reply("news", [])
        p["worldNews"]["officialNews"] = "第二条公开新闻。"
        self.assertIn("prompt", self.run_round(p))
        p = self.news_payload(15)
        p["worldNews"] = {"officialNews": "第二条公开新闻。", "folkLegends": SITE}
        p["llmResp"] = self.reply("news", [])
        self.assertIn("prompt", self.run_round(p))
        p = self.news_payload(16)
        p["worldNews"]["officialNews"] = "第三条公开新闻。"
        p["worldNews"]["folkLegends"] = SITE
        p["llmResp"] = self.reply("treasure", self.partial())
        response = self.run_round(p)
        self.assertEqual(self.state.judge.llm_used_today, 3)
        self.assertEqual(self.state.prompts_sent, 3)
        self.assertNotIn("prompt", response)
        self.assertTrue(response["roleCommandMap"], "ordinary roles continue when cognitive quota is spent")

    def test_same_text_keeps_first_observed_date_across_days(self):
        self.run_round(self.news_payload(13))
        p = self.news_payload(14)
        p["llmResp"] = self.reply("news", self.events())
        self.run_round(p)
        self.run_round(self.news_payload(143))
        records = self.state.team_agent.world.sources["news"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["firstRound"], 13)
        self.assertEqual(self.state.team_agent.world.policy_view(273)["unavailable"], ["iron"])
        self.assertEqual(self.state.team_agent.world.policy_view(403)["unavailable"], [])

    def test_new_day_correction_replaces_old_event_after_evidence_checked(self):
        self.run_round(self.news_payload(13))
        p = self.news_payload(14)
        p["llmResp"] = self.reply("news", self.events())
        self.run_round(p)
        corrected = "更正：今天第二天已修复铁矿，恢复开采。"
        p = self.news_payload(143)
        p["worldNews"]["officialNews"] = corrected
        self.run_round(p)
        p["roundNo"] = 144
        p["llmResp"] = self.reply("news", [{"resource": "iron", "availability": "available", "startDay": 2,
                                            "endDay": 10, "priceDirection": "unknown", "evidence": self.proof("news", corrected)}])
        response = self.run_round(p)
        self.assertEqual(response["roleCommandMap"]["10010"]["targetPos"], [{"x": 15, "y": 24}])

    def test_out_of_map_exact_parser_cannot_bypass_model_bounds(self):
        p = self.treasure_payload(1, "祭坛在 (90, 90) 附近，需献祭 AcientTablet，第 1-30 回合有效")
        response = self.run_round(p)
        self.assertFalse(self.state.team_agent.world.policy_view(1)["treasure"]["known"])
        self.assertNotEqual(response["roleCommandMap"].get("10011", {}).get("action"), "summonTreasure")

    def test_explicit_note_with_a_correction_is_not_treated_as_complete(self):
        p = self.treasure_payload(1, "祭坛在 (3, 4) 附近，需献祭 AcientTablet，第 1-30 回合有效。更正：祭坛已经搬迁。")
        response = self.run_round(p)
        self.assertIn("prompt", response)
        self.assertFalse(self.state.team_agent.world.policy_view(1)["treasure"]["known"])


if __name__ == "__main__":
    unittest.main()
