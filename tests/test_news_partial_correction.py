"""An explicitly uncertain correction cannot leave the old claim actionable."""
import json
from test_news_ledger import LedgerHarness
from agent.world_agent import WorldAgent
from agent.news_economy import sale_signals


class PartialCorrectionTests(LedgerHarness):
    def test_uncertain_end_weakens_only_the_possible_correction_interval(self):
        text = '游戏第1天，铁矿第2天到第5天停工。'
        agent, sid = self.agent_with(text, round_no=1)
        old = self.submit(agent, [self.news_event(sid, text, startDay=2, endDay=5)])[0]
        correction = '更正：铁矿从第3天起采矿情况未知，结束日期未知。'
        cid = self.add_source(agent, correction, 2)
        self.submit(agent, [self.news_event(cid, correction, availability='unknown', startDay=3, endDay=None,
            resolution={'kind':'corrected', 'targetId':old['id'], 'evidence':[{'sourceId':cid,'quote':correction}]})])
        for state in (agent, WorldAgent.load(json.loads(json.dumps(agent.dump())))):
            self.assertFalse(state.degraded)
            self.assertEqual(state.policy_view(131)['unavailable'], ['iron'])
            for round_no in (261, 391, 521):
                self.assertEqual(state.policy_view(round_no)['unavailable'], [])
            fact = next(f for f in state.news_view()['facts'] if f['id'] == old['id'])
            self.assertEqual(fact['effectiveDays'], [2])
            self.assertEqual(fact['possibleDays'], [2, 3, 4, 5])

    def test_unknown_correction_withholds_old_price_drop_advice(self):
        text = '游戏第1天，铜价第2天下降到1金币。'
        agent, sid = self.agent_with(text, round_no=1)
        old = self.submit(agent, [self.news_event(sid, text, resource='copper', availability='available',
            startDay=2, endDay=2, priceDirection='down', priceAmount=1, priceBasis='absolute')])[0]
        correction = '更正：铜价从第2天起情况未知，结束日期未知。'
        cid = self.add_source(agent, correction, 2)
        self.submit(agent, [self.news_event(cid, correction, resource='copper', availability='unknown',
            startDay=2, endDay=None, priceDirection='unknown', resolution={'kind':'corrected',
            'targetId':old['id'],'evidence':[{'sourceId':cid,'quote':correction}]})])
        self.assertEqual(sale_signals({'copper':5}, 1, **agent.economic_view(131)), {})
