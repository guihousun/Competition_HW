"""Issue29 comment5757771651 reference fixtures; scripted replies, no real LLM.

These are labelled paraphrases of supplied reference answers, not raw official
news. Future clues are injected only on their recorded publication day. Explicit
closing times exist only in the separate synthetic action-control test.
"""
import unittest
from copy import deepcopy

import test_world_agent as support
from agent import planner


MATERIALS = ['AcientTablet', 'FlameBreath', 'StarSand']
TIMELINES = (
    ('attack_map_update_long_context_mission', (1, 2, 3, 4), 5),
    ('attack_map', (1, 3, 5, 7), 8),
)


class Issue29UpdatedTimelinesTests(unittest.TestCase):
    setUp = support.WorldAgentTests.setUp
    run_round = support.WorldAgentTests.run_round
    reply = support.WorldAgentTests.reply
    proof = support.WorldAgentTests.proof
    treasure_payload = support.WorldAgentTests.treasure_payload

    def payload(self, n, text, *, bag=()):
        p = self.treasure_payload(n, text)
        p['teamOur']['roles'][0]['pos'] = {'x': 4, 'y': 3}
        p['teamOur']['roles'][0]['backpack'] = list(bag)
        p['weaponShopList'] = [{'name': item, 'price': 15} for item in MATERIALS]
        return p

    def replay(self, case, publication_days, opening_day, *, synthetic_close=False):
        texts = (
            f'用户参考摘述【{case}】：西部石门需要三把钥匙。',
            f'用户参考摘述【{case}】：商店售有古符石板、烈焰之息、星辰之沙。',
            f'用户参考摘述【{case}】：此石门需要古符石板、烈焰之息、星辰之沙各一份。',
            f'用户参考摘述【{case}】：石门位于横坐标3、纵坐标3，第{opening_day}天白昼开始开放；尚未给出关闭时间。',
        )
        if synthetic_close:
            texts = texts[:-1] + (f'合成执行对照【{case}】：石门位于横坐标3、纵坐标3，'
                f'仅第{opening_day}天白天开放，夜晚关闭；古符石板、烈焰之息、星辰之沙各一份。',)
        for index, day in enumerate(publication_days):
            n = (day-1)*130+1
            response = self.run_round(self.payload(n, texts[index], bag=MATERIALS))
            sources = self.state.team_agent.world.sources['treasure']
            self.assertEqual([r['text'] for r in sources], list(texts[:index+1]))
            for future in texts[index+1:]:
                self.assertNotIn(future, response.get('prompt', ''))
            if index < 3:
                self.assertFalse(self.state.team_agent.world.policy_view(n)['treasure']['known'])
                self.assertFalse(any(c['action']=='summonTreasure' for c in response['roleCommandMap'].values()))
        return response, texts, (publication_days[-1]-1)*130+1

    def hypothesis(self, texts, *, window=None):
        return {'site': {'x':3,'y':3}, 'items': list(MATERIALS),
            'opensAt': window[0] if window else None, 'closesAt': window[1] if window else None,
            'uncertain': window is None, 'unknowns': ['window'] if window is None else [],
            'evidence': {'site':self.proof('treasure',texts[-1]),
                'items':self.proof('treasure',texts[2]),
                'window':self.proof('treasure',texts[-1]) if window else []}}

    def test_both_actual_publication_schedules_keep_future_clues_out(self):
        for case, days, opening in TIMELINES:
            with self.subTest(case=case):
                self.state=planner.PlannerState()
                response,texts,last=self.replay(case,days,opening)
                for text in texts:self.assertIn(text,response['prompt'])
                for name in MATERIALS:self.assertIn(name,response['prompt'])
                for label in ('古符石板','烈焰之息','星辰之沙'):
                    self.assertIn(label,response['prompt'])
                self.assertIn('不能假造截止日',response['prompt'])
                self.assertIn('下方外观参考',response['prompt'])
                self.assertIn('AcientTablet',response['prompt'])

    def test_missing_closing_time_remains_unknown_at_opening_and_night(self):
        for case,days,opening in TIMELINES:
            with self.subTest(case=case):
                self.state=planner.PlannerState()
                _,texts,last=self.replay(case,days,opening)
                p=self.payload(last+1,texts[-1],bag=MATERIALS)
                p['llmResp']=self.reply('treasure',self.hypothesis(texts))
                self.run_round(p)
                for n in ((opening-1)*130+1,(opening-1)*130+71):
                    response=self.run_round(self.payload(n,texts[-1],bag=MATERIALS))
                    notes=self.state.team_agent.world.policy_view(n)['treasure']
                    self.assertFalse(notes['known']);self.assertIn('window',notes['unknowns'])
                    self.assertIsNone(notes['closesAt'])
                    self.assertFalse(any(c['action']=='summonTreasure' for c in response['roleCommandMap'].values()))

    def test_scripted_complete_window_follows_day_five_or_eight_not_fixed_day(self):
        for case,days,opening in TIMELINES:
            with self.subTest(case=case):
                self.state=planner.PlannerState()
                _,texts,last=self.replay(case,days,opening,synthetic_close=True)
                start=(opening-1)*130+1
                p=self.payload(last+1,texts[-1],bag=MATERIALS)
                p['llmResp']=self.reply('treasure',self.hypothesis(texts,window=(start,start+69)))
                response=self.run_round(p)
                self.assertFalse(any(c['action']=='summonTreasure' for c in response['roleCommandMap'].values()))
                before=self.run_round(self.payload(start-1,texts[-1],bag=MATERIALS))
                self.assertFalse(any(c['action']=='summonTreasure' for c in before['roleCommandMap'].values()))
                opened=self.run_round(self.payload(start,texts[-1],bag=MATERIALS))
                self.assertEqual(opened['roleCommandMap']['10011'],{'action':'summonTreasure',
                    'targetPos':[{'x':3,'y':3}],'item':MATERIALS})

    def test_separate_game_does_not_inherit_other_maps_day_five_clue(self):
        _,first_texts,last=self.replay(*TIMELINES[0])
        foreign=self.proof('treasure',first_texts[-1])
        self.state=planner.PlannerState()
        _,second_texts,last=self.replay(*TIMELINES[1])
        current_sources=self.state.team_agent.world.sources['treasure']
        self.assertNotIn(first_texts[-1],[r['text'] for r in current_sources])
        guessed=self.hypothesis(second_texts,window=(521,590))
        guessed['evidence']['window']=deepcopy(foreign)
        p=self.payload(last+1,second_texts[-1],bag=MATERIALS)
        p['llmResp']=self.reply('treasure',guessed)
        response=self.run_round(p)
        self.assertFalse(self.state.team_agent.world.policy_view(last+1)['treasure']['known'])
        self.assertFalse(any(c['action']=='summonTreasure' for c in response['roleCommandMap'].values()))


if __name__=='__main__':unittest.main()
