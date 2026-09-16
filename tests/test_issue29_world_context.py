"""Independent public examples; scripted replies are not model/official PASS."""
from pathlib import Path
import unittest

import test_world_agent as support
from agent import brain, planner
from agent.world_agent import WorldAgent
from agent.world_context import APPEARANCES, item_context, prefix_limits
from test_metal_economy import board


class Issue29Tests(unittest.TestCase):
    setUp = support.WorldAgentTests.setUp
    run_round = support.WorldAgentTests.run_round
    reply = support.WorldAgentTests.reply
    proof = support.WorldAgentTests.proof
    treasure_payload = support.WorldAgentTests.treasure_payload

    def test_official_appearances_match_source_not_issue_recipe(self):
        doc = (Path(__file__).resolve().parents[1] / 'docs/任务书.md').read_text(encoding='utf-8')
        for name, appearance in APPEARANCES.items():
            self.assertIn(name, doc)
            self.assertIn(appearance, doc)
        context = item_context([{'name': 'NewUnknownRune', 'price': 21},
                                {'name': 'StarSand', 'price': 19},
                                {'name': 'WallFixer', 'price': 10}])
        self.assertEqual(context['observed_task_candidates'], [
            {'name': 'NewUnknownRune', 'price': 21}, {'name': 'StarSand', 'price': 19}])
        self.assertEqual(len(context['reference']), 6)
        self.assertNotIn('recipe', context)
        self.assertFalse(item_context(None)['catalog_received'])
        self.assertEqual(item_context([])['observed_task_candidates'], [])

    def test_catalog_is_bounded_and_rejects_unknown_price(self):
        stock = [{'name': 'Unknown' + str(i), 'price': True} for i in range(30)]
        got = item_context(stock)
        self.assertEqual(len(got['observed_task_candidates']), 16)
        self.assertEqual(got['omitted'], 14)
        self.assertTrue(all(row['price'] is None for row in got['observed_task_candidates']))
        self.assertTrue(all(row['price'] is True for row in stock))

    def test_sent_prompt_has_live_catalog_and_background_without_private_data(self):
        payload = self.treasure_payload(410, '有人见过装着会发光粉末的小袋，地点时间未知。')
        payload['weaponShopList'] = [{'name': 'StarSand', 'price': 22}]
        payload['_demo'] = {'recipe': 'PRIVATE_RECIPE'}
        reply = self.run_round(payload)
        prompt = reply['prompt']
        self.assertIn('"name": "StarSand", "price": 22', prompt)
        self.assertIn(APPEARANCES['StarSand'], prompt)
        self.assertIn('到格子的比例', prompt)
        self.assertNotIn('PRIVATE_RECIPE', prompt)
        self.assertFalse(self.state.team_agent.world.policy_view(410)['treasure']['known'])

    def test_short_sources_donate_prefix_budget_without_fake_reading(self):
        world = WorldAgent()
        texts = ['短消息一。', '短消息二。', '线索。' * 590 + '第五日白昼开启。']
        for n, text in zip((1, 131, 261), texts):
            sid = world.memories['treasure'].observe(text, n)
            world.sources['treasure'].append(dict(id=sid, firstRound=n, text=text, truncated=False))
        rows = world._source_view('treasure')
        self.assertEqual([r['text'] for r in rows], texts)
        self.assertTrue(all(r['memory']['reviewed_after_this_prompt'] for r in rows))
        self.assertFalse(world._reviewed('treasure'), 'preview is not actual send')
        limits = prefix_limits(['a' * 3000, 'b' * 3000, 'c' * 10])
        self.assertEqual(sum(limits), 2000)
        self.assertEqual(limits[2], 10)
        self.assertLessEqual(abs(limits[0] - limits[1]), 1)
        self.assertEqual(prefix_limits([]), [])

    def test_absolute_day_synonyms_without_publication_anchor(self):
        for text in ('唯待第五日白昼方可松动。', '第5日白天开启。', '第六天夜晚开启。'):
            with self.subTest(text=text):
                world = WorldAgent()
                sid = world.memories['treasure'].observe(text, 402)
                self.assertIsNone(world.memories['treasure'].origins[sid]['anchor_day'])
                self.assertTrue(world._dated('treasure', [{'sourceId': sid, 'quote': text}]))
        for text in ('明日白昼开启。', '需要五日准备。', '古人活了九十三岁。'):
            world = WorldAgent()
            sid = world.memories['treasure'].observe(text, 402)
            self.assertFalse(world._dated('treasure', [{'sourceId': sid, 'quote': text}]))

    def test_cross_day_filler_and_absolute_day_reach_actual_summon_both_sides(self):
        # Explicit grid units and closing statement remove the actual Issue's
        # unresolved km scale / end-window questions in this independent fixture.
        first = '祭坛位于横坐标3、纵坐标4，需献祭一份IronWhistle。'
        filler = '东边林场组织了巡逻队，今晚开始值夜。'
        final = '同一祭坛仅在第五日白昼开放，夜晚关闭。'
        for side in ('challenger', 'defender'):
            with self.subTest(side=side):
                self.state = planner.PlannerState()
                def payload(n, text):
                    p = self.treasure_payload(n, text)
                    p['teamOur']['type'] = side
                    p['teamEnemy']['type'] = 'defender' if side == 'challenger' else 'challenger'
                    p['teamOur']['roles'][0]['backpack'] = ['IronWhistle']
                    return p
                self.run_round(payload(1, first))
                self.run_round(payload(261, filler))
                response = self.run_round(payload(402, final))
                self.assertIn(first, response['prompt'])
                self.assertIn(filler, response['prompt'])
                h = {'site': {'x': 3, 'y': 4}, 'items': ['IronWhistle'],
                     'opensAt': 521, 'closesAt': 590, 'uncertain': False,
                     'unknowns': [], 'evidence': {'site': self.proof('treasure', first),
                       'items': self.proof('treasure', first), 'window': self.proof('treasure', final)}}
                p = payload(403, final)
                p['llmResp'] = self.reply('treasure', h)
                response = self.run_round(p)
                self.assertEqual(self.state.team_agent.world.status['treasure'], 'interpreted')
                self.assertNotEqual(response['roleCommandMap'].get('10011', {}).get('action'), 'summonTreasure')
                response = self.run_round(payload(521, final))
                self.assertEqual(response['roleCommandMap']['10011'], {
                    'action': 'summonTreasure', 'targetPos': [{'x': 3, 'y': 4}], 'item': ['IronWhistle']})
                p = payload(522, final)
                p['lastSummonTreasureResult'] = 1
                response = self.run_round(p)
                self.assertTrue(self.state.team_agent.world.taken)
                self.assertNotEqual(response['roleCommandMap'].get('10011', {}).get('action'), 'summonTreasure')

    def test_day2_notice_day3_4_outage_day5_recovery_actual_mining(self):
        before = '当前矿区正常。'
        notice = '铁矿明日停工，连续两天不可采，之后恢复。'
        def payload(n, text):
            p = board(ores=(('iron', (15, 24)), ('copper', (15, 25))), round_no=n)
            p.pop('_demo')
            p['worldNews']['officialNews'] = text
            p['vendorShopList'][1]['price'] = 9
            return p
        self.run_round(payload(130, before))
        p = payload(131, notice)
        p['llmResp'] = self.reply('news', [])
        self.run_round(p)
        events = [{'resource': 'iron', 'availability': 'unavailable', 'startDay': 3,
                   'endDay': 4, 'resumeDay': 5, 'priceDirection': 'unknown',
                   'evidence': self.proof('news', notice)}]
        p = payload(132, notice)
        p['llmResp'] = self.reply('news', events)
        self.run_round(p)
        for n, target in ((143, {'x': 15, 'y': 24}), (273, {'x': 15, 'y': 25}),
                          (403, {'x': 15, 'y': 25}), (533, {'x': 15, 'y': 24})):
            with self.subTest(round=n):
                p = payload(n, notice)
                response = self.run_round(p)
                self.assertEqual(response['roleCommandMap']['10010']['targetPos'], [target])
                self.assertEqual(brain._sellable_metals(p)['iron'], 9)


if __name__ == '__main__':
    unittest.main()
