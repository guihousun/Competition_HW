"""Issue #41 evidence adapter; scripted model replies are NOT model success.

The first four texts are excerpts/paraphrases of the owner's analysis, not
complete platform news. Explicit coordinates/closing times below are labelled
synthetic controls: they do not fill missing official evidence silently.
"""
import unittest

import test_world_agent as support
from agent import planner


EXCERPTS = (
    '西部有一石门，门需三钥。',
    '商店老板收到了灰白色、刻有文字的石板，银白色、黑暗中自行发光的粉末，以及水晶瓶中的橙红色发光雾。',
    '真言之印需以铭文石板叩问；明光之印需以不灭之光显形；焚天之印需以纯净之火灼烧。',
    '在原点之北三公里、之东三公里附近发现石门。这等三封石门，唯待第五日白昼方可松动。',
)
MATERIALS = ['AcientTablet', 'StarSand', 'FlameBreath']


class Issue41ReplayTests(unittest.TestCase):
    setUp = support.WorldAgentTests.setUp
    run_round = support.WorldAgentTests.run_round
    reply = support.WorldAgentTests.reply
    proof = support.WorldAgentTests.proof
    treasure_payload = support.WorldAgentTests.treasure_payload

    def payload(self, n, text, *, side='challenger', site=(3, 3), bag=()):
        p = self.treasure_payload(n, text)
        p['teamOur']['type'] = side
        p['teamEnemy']['type'] = 'defender' if side == 'challenger' else 'challenger'
        pioneer = p['teamOur']['roles'][0]
        pioneer['pos'] = {'x': site[0]+1, 'y': site[1]}
        pioneer['backpack'] = list(bag)
        p['weaponShopList'] = [{'name': item, 'price': 15} for item in MATERIALS]
        return p

    def read_days(self, *, side='challenger', site=(3, 3), bag=(), fourth=None):
        texts = EXCERPTS[:3] + (fourth or EXCERPTS[3],)
        response = None
        for index, (n, text) in enumerate(zip((1, 131, 261, 402), texts)):
            response = self.run_round(self.payload(n, text, side=side, site=site, bag=bag))
            observed = self.state.team_agent.world.sources['treasure']
            self.assertEqual(len(observed), index+1)
            self.assertNotIn(texts[-1], [r['text'] for r in observed[:-1]])
            if index < 3:
                self.assertNotIn(EXCERPTS[3], response.get('prompt', ''))
        return response, texts

    def test_issue_excerpts_reach_model_with_live_catalog_without_fixed_answer(self):
        response, texts = self.read_days()
        prompt = response['prompt']
        for text in texts:
            self.assertIn(text, prompt)
        for item in MATERIALS:
            self.assertIn(item, prompt)
        self.assertIn('到格子的比例', prompt)
        self.assertIn('不能假造截止日', prompt)
        self.assertFalse(self.state.team_agent.world.policy_view(402)['treasure']['known'])
        self.assertFalse(any(c.get('action') == 'summonTreasure' for c in response['roleCommandMap'].values()))

    def test_explicit_missing_parts_remain_unresolved_not_silently_filled(self):
        _, texts = self.read_days()
        h = {'site': None, 'items': MATERIALS, 'opensAt': None, 'closesAt': None,
             'uncertain': True, 'unknowns': ['site', 'window'],
             'evidence': {'site': [], 'items': self.proof('treasure', texts[1]) + self.proof('treasure', texts[2]), 'window': []}}
        p = self.payload(403, texts[3], bag=MATERIALS)
        p['llmResp'] = self.reply('treasure', h)
        self.run_round(p)
        for n in (521, 522):
            response = self.run_round(self.payload(n, texts[3], bag=MATERIALS))
            self.assertFalse(self.state.team_agent.world.policy_view(n)['treasure']['known'])
            self.assertFalse(any(c.get('action') == 'summonTreasure' for c in response['roleCommandMap'].values()))

    def test_synthetic_clarification_replays_to_correct_day_and_site_both_sides(self):
        # Control variations test execution and generality, not the model's
        # ability to infer the solution from the owner's incomplete excerpts.
        for side, site, day, items in (
            ('challenger', (3,3), 5, MATERIALS),
            ('defender', (3,3), 5, MATERIALS),
            ('challenger', (12,7), 6, ['FrostPotion', 'IronWhistle']),
            ('defender', (12,7), 6, ['FrostPotion', 'IronWhistle']),
        ):
            with self.subTest(side=side, site=site, day=day):
                self.state = planner.PlannerState()
                clarification = (f'合成验收补充：石门位于横坐标{site[0]}、纵坐标{site[1]}；'
                    f'本门只在第{day}天白天开放，夜晚关闭；献祭' + '、'.join(items) + '，各一份。')
                _, texts = self.read_days(side=side, site=site, bag=items, fourth=clarification)
                start = (day-1)*130+1
                proof = self.proof('treasure', clarification)
                h = {'site': dict(x=site[0],y=site[1]), 'items': items,
                     'opensAt': start, 'closesAt': start+69, 'uncertain': False,
                     'unknowns': [], 'evidence': {'site': proof, 'items': proof, 'window': proof}}
                p = self.payload(403, clarification, side=side, site=site, bag=items)
                p['llmResp'] = self.reply('treasure', h)
                response = self.run_round(p)
                self.assertFalse(any(c.get('action') == 'summonTreasure' for c in response['roleCommandMap'].values()))
                response = self.run_round(self.payload(start, clarification, side=side, site=site, bag=items))
                self.assertEqual(response['roleCommandMap']['10011'], {
                    'action':'summonTreasure', 'targetPos':[dict(x=site[0],y=site[1])], 'item':items})
                p = self.payload(start+1, clarification, side=side, site=site)
                p['lastSummonTreasureResult'] = 1
                response = self.run_round(p)
                self.assertTrue(self.state.team_agent.world.taken)
                self.assertFalse(any(c.get('action') == 'summonTreasure' for c in response['roleCommandMap'].values()))


if __name__ == '__main__':
    unittest.main()
