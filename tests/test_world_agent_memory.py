"""Public long-text reasoning through the actual planner/router/JSON boundary.

Replies are scripted inputs, not claimed model-quality results. Expectations
come from explicit hand-written clues and the shared ordinary call limit.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))
from agent import brain, planner
from agent.world_agent import WorldAgent
from test_tasks import observation


class WorldAgentMemoryTests(unittest.TestCase):
    def setUp(self):
        self.state = planner.PlannerState()
        setting = patch.dict(os.environ, {brain.WORLD_AGENT_ENV: 'on', brain.TASK_AGENT_ENV: 'off'})
        setting.start()
        self.addCleanup(setting.stop)

    @property
    def world(self):
        return self.state.team_agent.world

    def turn(self, n, text, reply=None, *, side='challenger', owner='treasure'):
        payload = observation(round_no=n, role_pos=(4, 4), task_points=[])
        payload['teamOur']['type'] = side
        payload['teamEnemy']['type'] = 'defender' if side == 'challenger' else 'challenger'
        payload['mapInfo']['zones'] = []
        payload['worldNews'] = {'officialNews': text if owner == 'news' else '',
                                'folkLegends': text if owner == 'treasure' else ''}
        payload['vendorShopList'] = [{'name': 'iron', 'price': 3}]
        payload['teamOur']['roles'][0]['backpack'] = []
        if reply is not None:
            link = self.world.links[owner]
            payload['llmResp'] = json.dumps({'request_id': link['token'], **reply}, ensure_ascii=False)
        self.state.note_round(n)
        response = brain.plan_for_state(payload, self.state, judge_tasks=False)
        self.state.note_submission(response.prompt, response.execute, n, False)
        self.state = planner.PlannerState.load(json.loads(json.dumps(self.state.dump())))
        self.assertFalse(self.state.team_agent.degraded)
        self.assertFalse(self.world.degraded)
        self.assertFalse(response.execute, 'news and rumours cannot use the task sandbox')
        return response.build()

    def proof(self, text, owner='treasure'):
        return [{'sourceId': self.world.sources[owner][0]['id'], 'quote': text}]

    def hypothesis(self, site=None, items=None, window=None, **extra):
        return {'site': site, 'items': items, 'opensAt': window[0] if window else None,
                'closesAt': window[1] if window else None, 'uncertain': True,
                'evidence': {'site': [], 'items': [], 'window': []}, **extra}

    def test_three_chunks_keep_negation_and_tail_correction_on_both_sides(self):
        head = '祭坛位于横坐标3、纵坐标4，最初告示要求IronWhistle。'
        middle = '补充：不能献祭AncientScroll。'
        tail = '更正：IronWhistle要求取消，只需两份AcientTablet。游戏第2天白昼前30回合开启。'
        text = head + '市井闲谈。' * 440 + middle + '无关见闻。' * 400 + tail
        self.assertGreater(len(text), 4000)
        for side in ('challenger', 'defender'):
            with self.subTest(side=side):
                self.state = planner.PlannerState()
                first = self.turn(1, text, side=side)
                self.assertNotIn(middle, first['prompt'])
                self.assertNotIn(tail, first['prompt'])
                sid = self.world.sources['treasure'][0]['id']
                h = self.hypothesis(site={'x': 3, 'y': 4}, items=['IronWhistle'], unknowns=['window'])
                h['evidence'].update(site=self.proof(head), items=self.proof(head))
                second = self.turn(2, text, {'inspect': {'source_id': sid, 'offset': 1900}, 'draft': h}, side=side)
                self.assertIn(middle, second['prompt'])
                self.assertFalse(self.world.policy_view(2)['treasure'].get('preparable'))
                draft = deepcopy(self.world.drafts['treasure'])
                draft['candidates'].append({'field': 'items', 'value': ['AncientScroll'],
                    'polarity': 'exclude', 'evidence': self.proof(middle)})
                third = self.turn(3, text, {'inspect': {'source_id': sid, 'offset': 3800}, 'draft': draft}, side=side)
                self.assertIn(tail, third['prompt'])
                self.assertEqual(self.state.judge.llm_used_today, 3)
                self.assertTrue(self.world.memories['treasure'].reviewed(sid))
                final = deepcopy(self.world.drafts['treasure'])
                final.update(items=['AcientTablet', 'AcientTablet'], opensAt=131, closesAt=160,
                             uncertain=False, unknowns=[])
                final['evidence'].update(items=self.proof(tail), window=self.proof(tail))
                for candidate in final['candidates']:
                    if candidate['field'] == 'items' and candidate['value'] == ['IronWhistle']:
                        candidate['resolution'] = {'kind': 'corrected', 'evidence': self.proof(tail)}
                self.turn(4, text, {'hypothesis': final}, side=side)
                notes = self.world.policy_view(131)['treasure']
                self.assertTrue(notes['known'])
                self.assertEqual(notes['items'], ['AcientTablet', 'AcientTablet'])
                ledger = self.world.hypothesis['candidates']
                self.assertTrue(any(c.get('polarity') == 'exclude' for c in ledger))
                self.assertTrue(any(c.get('resolution', {}).get('kind') == 'corrected' for c in ledger))

    def test_quota_wait_does_not_mark_queued_chunk_read(self):
        text = '只有末尾才有结论。' + '背景资料。' * 1400 + '最终没有公开开启时间。'
        self.turn(1, text)
        sid = self.world.sources['treasure'][0]['id']
        self.turn(2, text, {'inspect': {'source_id': sid, 'offset': 1900}})
        self.turn(3, text, {'inspect': {'source_id': sid, 'offset': 3800}})
        fourth = self.turn(4, text, {'inspect': {'source_id': sid, 'offset': 5700}})
        self.assertNotIn('prompt', fourth)
        self.assertEqual(self.state.judge.llm_used_today, 3)
        self.assertFalse(self.world.memories['treasure'].quote_visible(sid, '最终没有公开开启时间。'))
        self.assertFalse(self.world.memories['treasure'].reviewed(sid))
        resumed = self.turn(131, text)
        self.assertIn('最终没有公开开启时间。', resumed['prompt'])
        self.assertEqual(self.state.judge.llm_used_today, 1)
        self.assertTrue(self.world.memories['treasure'].reviewed(sid))

    def test_late_relative_news_stays_unknown_and_does_not_ban_iron(self):
        text = '明天铁矿停工两天，价格上涨。'
        self.turn(145, text, owner='news')
        event = {'resource': 'iron', 'availability': 'unavailable', 'startDay': 3, 'endDay': 4,
                 'priceDirection': 'up', 'evidence': self.proof(text, 'news')}
        self.turn(146, text, {'events': [event]}, owner='news')
        self.assertEqual(self.world.news_events, [])
        event.update(startDay=None, endDay=None)
        self.turn(147, text, {'events': [event]}, owner='news')
        self.assertEqual(self.world.status['news'], 'interpreted')
        self.assertEqual(self.world.policy_view(261)['unavailable'], [])
        self.assertIsNone(self.world.memories['news'].index()[0]['anchor_day'])

    def test_invalid_inspect_types_do_not_poison_persistent_state(self):
        text = '待核对的公开消息。'
        self.turn(1, text)
        sid = self.world.sources['treasure'][0]['id']
        self.turn(2, text, {'inspect': {'source_id': sid, 'offset': []}})
        self.assertIsNone(self.world.focus['treasure'])
        self.assertEqual(self.world.failures['treasure'], 1)

    def test_topic_value_with_inspect_is_a_checked_draft_not_an_action_plan(self):
        text = '祭坛在(3,4)，尚未公布时间。' + '背景资料。' * 600
        self.turn(1, text)
        sid = self.world.sources['treasure'][0]['id']
        h = self.hypothesis(site={'x': 3, 'y': 4}, unknowns=['items', 'window', 'unread'])
        h['evidence']['site'] = self.proof('祭坛在(3,4)')
        self.turn(2, text, {'inspect': {'source_id': sid, 'offset': 2000}, 'hypothesis': h})
        self.assertEqual(self.world.failures['treasure'], 0)
        self.assertEqual(self.world.drafts['treasure']['site'], {'x': 3, 'y': 4})
        self.assertIsNone(self.world.hypothesis)
        self.assertFalse(self.world.policy_view(2)['treasure'].get('preparable'))
        # Two competing draft values are ambiguous and must not be selected
        # arbitrarily, even when their individual schemas are valid.
        competing = deepcopy(h)
        competing['site'] = {'x': 7, 'y': 8}
        self.turn(3, text, {'inspect': {'source_id': sid, 'offset': 2000}, 'hypothesis': h, 'draft': competing})
        self.assertEqual(self.world.failures['treasure'], 1)

    def test_identical_nested_draft_and_oversized_read_are_canonicalized(self):
        text = '祭坛在(3,4)。' + '背景资料。' * 1000
        self.turn(1, text)
        sid = self.world.sources['treasure'][0]['id']
        h = self.hypothesis(site={'x': 3, 'y': 4}, unknowns=['items', 'window', 'unread'])
        h['evidence']['site'] = self.proof('祭坛在(3,4)')
        self.turn(2, text, {'inspect': {'source_id': sid, 'offset': 2000, 'length': 8000, 'draft': h},
                            'hypothesis': h})
        self.assertEqual(self.world.failures['treasure'], 0)
        self.assertEqual(len(self.world.focus['treasure']['text']), 2000)
        self.assertFalse(self.world.memories['treasure'].reviewed(sid))
        self.assertIsNone(self.world.hypothesis)

    def test_recorded_model_correction_is_not_blocked_by_quote_punctuation(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/world_memory_real6.json').read_text(encoding='utf-8-sig'))
        self.turn(1, fixture['text'])
        # The first three actual answers read the source and reach the correct
        # final result. The old implementation rejected that final answer and
        # needed a fourth (also rejected) call the next day.
        for n, raw in enumerate(fixture['replies'][:3], 2):
            data = json.loads(raw)
            self.assertEqual(data.pop('request_id'), self.world.links['treasure']['token'])
            self.turn(n, fixture['text'], data)
        notes = self.world.policy_view(261)['treasure']
        self.assertTrue(notes['known'])
        self.assertEqual(notes['items'], ['AcientTablet', 'AcientTablet'])
        self.assertEqual((notes['opensAt'], notes['closesAt']), (261, 290))
        self.assertEqual(self.world.failures['treasure'], 0)
        self.assertEqual(self.state.judge.llm_used_today, 3)
        old = [c for c in self.world.hypothesis['candidates'] if c['field'] == 'items' and c['value'] == ['IronWhistle']]
        self.assertEqual(len(old), 1)
        self.assertEqual(old[0]['resolution']['kind'], 'cancelled')

    def test_news_alias_does_not_allow_cross_topic_drafts(self):
        text = '游戏第1天，明天铁矿停工。' + '背景资料。' * 600
        self.turn(1, text, owner='news')
        sid = self.world.sources['news'][0]['id']
        event = {'resource': 'iron', 'availability': 'unavailable', 'startDay': 2, 'endDay': 2,
                 'priceDirection': 'unknown', 'evidence': self.proof('游戏第1天，明天铁矿停工。', 'news')}
        self.turn(2, text, {'inspect': {'source_id': sid, 'offset': 2000}, 'events': [event]}, owner='news')
        self.assertEqual(self.world.failures['news'], 0)
        self.assertEqual(self.world.drafts['news'], [event])
        self.assertFalse(self.world.news_events)
        self.turn(3, text, {'inspect': {'source_id': sid, 'offset': 2000}, 'hypothesis': {}}, owner='news')
        self.assertEqual(self.world.failures['news'], 1)

    def test_excluded_ingredient_cannot_hide_inside_a_larger_offering(self):
        text = '祭坛(3,4)，游戏第2天白昼，需要AcientTablet；禁止献祭AncientScroll。'
        self.turn(1, text)
        h = self.hypothesis(site={'x': 3, 'y': 4}, items=['AcientTablet', 'AncientScroll'],
                            window=(131, 160), uncertain=False, unknowns=[])
        h['evidence'] = {field: self.proof(text) for field in ('site', 'items', 'window')}
        h['candidates'] = [{'field': 'items', 'value': ['AncientScroll'], 'polarity': 'exclude',
                            'evidence': self.proof('禁止献祭AncientScroll。')}]
        self.turn(2, text, {'hypothesis': h})
        self.assertIsNone(self.world.hypothesis)
        self.assertEqual(self.world.failures['treasure'], 1)

    def test_new_day_preserves_partial_constraints_and_no_premature_purchase(self):
        text = '祭坛位置(3,4)，两份AcientTablet或两份IronWhistle，尚有冲突。'
        self.turn(1, text)
        h = self.hypothesis(site={'x': 3, 'y': 4}, items=['AcientTablet'] * 2,
                            unknowns=['window', 'conflict'])
        h['evidence'].update(site=self.proof(text), items=self.proof(text))
        h['candidates'] = [{'field': 'items', 'value': items, 'evidence': self.proof(text)}
                           for items in (['AcientTablet'] * 2, ['IronWhistle'] * 2)]
        self.turn(2, text, {'hypothesis': h})
        self.assertFalse(self.world.policy_view(2)['treasure']['preparable'])
        # The next day's prompt must still contain both competing requirements.
        response = self.turn(131, '补充消息仍在调查中。')
        self.assertIn('AcientTablet', response['prompt'])
        self.assertIn('IronWhistle', response['prompt'])
        h['unknowns'] = ['window']
        h.pop('candidates')
        self.turn(132, '补充消息仍在调查中。', {'hypothesis': h})
        self.assertEqual(self.world.status['treasure'], 'waiting_model')
        self.assertEqual(self.world.failures['treasure'], 1, 'omitting a conflicting candidate is not a correction')
        self.assertFalse(self.world.policy_view(132)['treasure'].get('preparable'))

    def test_old_schema_degrades_without_restoring_free_daily_calls(self):
        self.turn(1, '尚无完整宝藏线索。')
        raw = self.state.dump()
        # Locate the actual serialized coordinator, rather than resetting the
        # JudgeState or manufacturing a fresh game on version incompatibility.
        raw['teamAgent']['world']['schema'] = 'competition-world-agent/1'
        restored = planner.PlannerState.load(raw)
        self.assertTrue(restored.team_agent.degraded)
        self.assertEqual(restored.judge.llm_used_today, 1)

    def test_prompt_unread_pointer_accounts_for_its_own_text_without_exposure(self):
        text = '公开原文。' * 900
        self.turn(1, text)
        sid = self.world.sources['treasure'][0]['id']
        self.world.memories['treasure'].visible = {}
        view = self.world._source_view('treasure')[0]
        self.assertEqual(view['memory']['next_unread'], 2000)
        self.assertFalse(view['memory']['reviewed_after_this_prompt'])
        self.assertFalse(self.world.memories['treasure'].visible, 'building a prompt is not sending it')
        self.world.focus['treasure'] = self.world.memories['treasure'].inspect(sid, offset=1900)
        self.assertEqual(self.world._source_view('treasure')[0]['memory']['next_unread'], 3900)
        self.assertFalse(self.world.memories['treasure'].visible)

    def test_forged_saved_link_cannot_claim_unsent_tail_was_read(self):
        text = '公开前言。' * 900 + '未发送的尾部。'
        response = self.turn(1, text)
        world = self.world
        sid = world.sources['treasure'][0]['id']
        world.memories['treasure'].visible = {}
        link = world.links['treasure']
        link['acknowledged'] = False
        link['views'] = [{'id': sid, 'start': 0, 'end': len(text)}]
        pending = SimpleNamespace(request_id=link['request_id'], sent_round=1, payload=response['prompt'])
        world.acknowledge({'roundNo': 1}, SimpleNamespace(pending={'prompt': pending}),
                          SimpleNamespace(prompt=response['prompt'], commands={}))
        self.assertEqual(world.memories['treasure'].visible[sid], [[0, 2000]])
        self.assertFalse(world.memories['treasure'].reviewed(sid))


if __name__ == '__main__':
    unittest.main()
