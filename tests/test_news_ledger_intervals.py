"""Additional independent parent audit: temporal scope and exact decimal text."""
import json
from test_news_ledger import LedgerHarness
from agent import news_ledger
from agent.world_agent import WorldAgent


class IntervalAudit(LedgerHarness):
    def test_integer_and_fractional_quotes_do_not_round_into_other_values(self):
        self.assertFalse(news_ledger.verify_price(['铁价上涨到9007199254740993金币'], 9007199254740992, 'absolute'))
        self.assertTrue(news_ledger.verify_price(['铁价上涨到9007199254740993金币'], 9007199254740993, 'absolute'))
        self.assertFalse(news_ledger.verify_price(['铁价上涨到6.1000000000000001金币'], 6.1, 'absolute'))

    def test_one_day_correction_leaves_later_unaffected_days(self):
        text = '游戏第1天，铁矿第2天到第5天停工。'
        agent, sid = self.agent_with(text, round_no=1)
        self.submit(agent, [self.news_event(sid, text, startDay=2, endDay=5)])
        correction = '更正：铁矿仅第3天可以开采，第2天、第4天和第5天仍停工。'
        cid = self.add_source(agent, correction, 3)
        self.submit(agent, [self.news_event(cid, correction, availability='available', startDay=3, endDay=3)])
        self.assertEqual(agent.policy_view(131)['unavailable'], ['iron'])
        self.assertEqual(agent.policy_view(261)['unavailable'], [])
        self.assertEqual(agent.policy_view(391)['unavailable'], ['iron'])
        restored = WorldAgent.load(json.loads(json.dumps(agent.dump())))
        self.assertFalse(restored.degraded)
        self.assertEqual(restored.policy_view(521)['unavailable'], ['iron'])

    def test_corrected_pair_is_not_an_unresolved_conflict_in_prompt(self):
        text = '游戏第1天，第2天到第3天铁矿停工。'
        agent, sid = self.agent_with(text, round_no=1)
        self.submit(agent, [self.news_event(sid, text, startDay=2, endDay=3)])
        correction = '更正：游戏第3天起铁矿恢复开采。'
        cid = self.add_source(agent, correction, 263)
        self.submit(agent, [self.news_event(cid, correction, availability='available', startDay=3, endDay=10)])
        self.assertEqual(agent._ledger_conflicts(), [])

    def test_partial_intervals_do_not_crash_overlap_and_remain_uncertain(self):
        text = '游戏第1天，铁矿从第2天开始停工，结束日未知。'
        agent, sid = self.agent_with(text, round_no=1)
        self.submit(agent, [self.news_event(sid, text, startDay=2, endDay=None)])
        other = '更正：铁矿第3天可开采。'
        oid = self.add_source(agent, other, 3)
        self.submit(agent, [self.news_event(oid, other, availability='available', startDay=3, endDay=3)])
        self.assertEqual(agent.policy_view(391)['unavailable'], [])
        self.assertFalse(WorldAgent.load(json.loads(json.dumps(agent.dump()))).degraded)
