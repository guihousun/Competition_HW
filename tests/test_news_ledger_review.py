"""Parent-review counterexamples: independently specified news semantics."""
from copy import deepcopy
import json
import unittest
from test_news_ledger import LedgerHarness
from agent import news_ledger
from agent.world_agent import WorldAgent


class NewsLedgerReviewTests(LedgerHarness):
    def test_price_only_clause_does_not_cancel_a_known_outage(self):
        # Real-provider trial split one source into availability and price facts.
        text = '游戏第1天，铁矿第2天至第3天停止开采，第4天恢复；维修期间铁价上涨，幅度未公布。'
        for reverse in (False, True):
            agent, sid = self.agent_with(text, round_no=1)
            facts = [self.news_event(sid, text, startDay=2, endDay=3, resumeDay=4),
                     self.news_event(sid, text, availability='unknown', startDay=2, endDay=3,
                                     priceDirection='up', priceAmount=None, priceBasis='unknown')]
            self.submit(agent, list(reversed(facts)) if reverse else facts)
            for current in (agent, WorldAgent.load(json.loads(json.dumps(agent.dump())))):
                self.assertFalse(current.degraded)
                self.assertEqual(current.policy_view(131)['unavailable'], ['iron'])
                self.assertEqual(current.policy_view(261)['unavailable'], ['iron'])
                self.assertEqual(current.policy_view(391)['unavailable'], [])

    def test_price_only_clause_cannot_create_an_outage(self):
        text = '游戏第1天，第2天至第3天铁价上涨，幅度未公布。'
        agent, sid = self.agent_with(text, round_no=1)
        self.submit(agent, [self.news_event(sid, text, availability='unknown', startDay=2,
                         endDay=3, priceDirection='up', priceAmount=None, priceBasis='unknown')])
        self.assertEqual(agent.policy_view(131)['unavailable'], [])

    def test_price_only_clause_cannot_hide_a_real_availability_conflict(self):
        text = '游戏第1天，甲称第2天铁矿停产，乙称第2天铁矿照常；当天铁价上涨，幅度未公布。'
        agent, sid = self.agent_with(text, round_no=1)
        self.submit(agent, [self.news_event(sid, text, startDay=2, endDay=2),
            self.news_event(sid, text, availability='available', startDay=2, endDay=2),
            self.news_event(sid, text, availability='unknown', startDay=2, endDay=2,
                            priceDirection='up', priceAmount=None, priceBasis='unknown')])
        view = agent.policy_view(131)
        self.assertEqual(view['unavailable'], [])
        self.assertTrue(any('availability' in c['dimensions'] for c in view['conflicts']))

    def test_price_requires_exact_amount_not_relative_tolerance(self):
        self.assertFalse(news_ledger.verify_price(['铁价上涨到1000000金币'], 1000001, 'absolute'))
        self.assertTrue(news_ledger.verify_price(['铁价上涨到6.10金币'], 6.1, 'absolute'))

    def test_percentage_points_are_not_percent_and_negation_is_not_positive_evidence(self):
        self.assertFalse(news_ledger.verify_price(['铁价上涨20个百分点'], 20, 'percent'))
        self.assertFalse(news_ledger.verify_price(['铁价并未上涨到6金币'], 6, 'absolute'))
        self.assertFalse(news_ledger.verify_price(['iron price did not rise to 6 gold'], 6, 'absolute'))

    def test_large_integer_is_not_silently_rounded_or_an_exception(self):
        for amount in (9007199254740993, 10 ** 500):
            normalized, ok = news_ledger.normalize_amount(amount)
            if ok:
                self.assertEqual(normalized, amount)

    def test_explicit_resolution_still_requires_public_correction_evidence(self):
        text = '游戏第1天，明天铁矿停工两天。'
        agent, sid = self.agent_with(text, round_no=1)
        old = self.submit(agent, [self.news_event(sid, text, startDay=2, endDay=3)])[0]
        invalid = self.news_event(sid, text, availability='available', startDay=2, endDay=3,
            resolution={'kind': 'cancelled', 'targetId': old['id'], 'evidence': [{'sourceId': sid, 'quote': text}]})
        with self.assertRaises(ValueError):
            self.submit(agent, [invalid])

    def test_a_copper_correction_cannot_retire_an_iron_event(self):
        text = '游戏第1天，明天铁矿停工两天。'
        agent, sid = self.agent_with(text, round_no=1)
        old = self.submit(agent, [self.news_event(sid, text, startDay=2, endDay=3)])[0]
        corrected = '更正：第2天铜矿照常开采。'
        cid = self.add_source(agent, corrected, 2)
        invalid = self.news_event(cid, corrected, resource='copper', availability='available', startDay=2, endDay=3,
            resolution={'kind': 'corrected', 'targetId': old['id'],
                        'evidence': [{'sourceId': cid, 'quote': corrected}]})
        with self.assertRaises(ValueError):
            self.submit(agent, [invalid])

    def test_day3_recovery_does_not_erase_day2_outage(self):
        text = '游戏第1天，明天铁矿停工两天。'
        agent, sid = self.agent_with(text, round_no=1)
        self.submit(agent, [self.news_event(sid, text, startDay=2, endDay=3)])
        corrected = '更正：今天第三天已恢复铁矿开采。'
        cid = self.add_source(agent, corrected, 263)
        self.submit(agent, [self.news_event(cid, corrected, availability='available', startDay=3, endDay=10)])
        self.assertEqual(agent.policy_view(131)['unavailable'], ['iron'])
        self.assertEqual(agent.policy_view(261)['unavailable'], [])
        restored = WorldAgent.load(json.loads(json.dumps(agent.dump())))
        self.assertFalse(restored.degraded)
        self.assertEqual(restored.policy_view(131)['unavailable'], ['iron'])

    def test_price_conflict_is_retained_even_when_availability_agrees(self):
        text = '游戏第1天，明天铁价上涨到6金币。'
        agent, sid = self.agent_with(text, round_no=1)
        self.submit(agent, [self.news_event(sid, text, availability='available', startDay=2, endDay=2,
            priceDirection='up', priceAmount=6, priceBasis='absolute')])
        other = '游戏第1天，另一报道说明天铁价上涨到7金币。'
        oid = self.add_source(agent, other, 1)
        self.submit(agent, [self.news_event(oid, other, availability='available', startDay=2, endDay=2,
            priceDirection='up', priceAmount=7, priceBasis='absolute')])
        self.assertTrue(agent.policy_view(131)['conflicts'])
        self.assertEqual(agent.policy_view(131)['unavailable'], [])

    def test_eviction_cannot_turn_a_conflict_into_a_certain_ban(self):
        text = '游戏第1天，甲报称第1—10天铁矿停工，乙报称第1—10天铁矿照常。'
        agent, sid = self.agent_with(text, round_no=1)
        available = self.news_event(sid, text, availability='available', startDay=1, endDay=10)
        unavailable = self.news_event(sid, text, startDay=1, endDay=10)
        self.submit(agent, [available, unavailable])
        self.assertNotIn('iron', agent.policy_view(1)['unavailable'])
        filler = '游戏第1天，铜矿各个已公布时期照常开采。'
        fid = self.add_source(agent, filler, 1)
        intervals = [(start, end) for start in range(1, 11) for end in range(start, 11)][:23]
        for start in range(0, len(intervals), 8):
            self.submit(agent, [self.news_event(fid, filler, resource='copper', availability='available',
                startDay=left, endDay=right) for left, right in intervals[start:start+8]])
        self.assertNotIn('iron', agent.policy_view(1)['unavailable'])
        self.submit(agent, [unavailable])
        self.assertNotIn('iron', agent.policy_view(1)['unavailable'], 'one-sided replay cannot clear an eviction gap')

    def test_evicted_original_does_not_make_forged_quote_trusted(self):
        text = '游戏第1天，明天铁矿停工两天。'
        agent, sid = self.agent_with(text, round_no=1)
        self.submit(agent, [self.news_event(sid, text, startDay=2, endDay=3)])
        for i in range(13):
            self.add_source(agent, f'游戏第1天，第{i}份其他公开消息。', i+2)
            agent.sources['news'] = agent.sources['news'][-12:]
        raw = deepcopy(agent.dump())
        raw['news_events'][0]['evidence'][0]['quote'] = '从未发布过的伪造引文。'
        restored = WorldAgent.load(raw)
        self.assertTrue(restored.degraded or 'iron' not in restored.policy_view(131)['unavailable'])

    def test_known_resume_day_can_be_kept_when_other_endpoints_are_unknown(self):
        text = '游戏第4天，已确定铁矿第7天恢复，停工起始日未记载。'
        agent, sid = self.agent_with(text)
        self.submit(agent, [self.news_event(sid, text, resumeDay=7)])
        self.assertEqual(agent.news_events[0]['resumeDay'], 7)
        self.assertEqual(agent.policy_view(651)['unavailable'], [])


if __name__ == '__main__':
    unittest.main()
