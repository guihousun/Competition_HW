"""Original text recovery and per-task isolation, including the real brain seam."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))
from agent import brain
from agent.evidence_memory import EvidenceMemory, TOTAL_CHARS
from agent.team_agent import TeamAgent
from test_method_transfer import Session


class MemoryTests(unittest.TestCase):
    def test_find_and_range_read_preserve_middle_text_and_whitespace(self):
        text = 'a' * 9000 + '\nAPI入口：  KEEP  SPACES\n' + 'z' * 9000
        memory = EvidenceMemory('one', 'task1')
        memory.put('source', text, pinned=True)
        result = memory.inspect('source', query='API入口', length=300)
        self.assertIn('API入口：  KEEP  SPACES\n', result['text'])
        self.assertEqual(result['match_offset'], 9001)
        self.assertEqual(result['text'], text[result['offset']:result['end']])
        self.assertTrue(result['source_complete'])
        self.assertEqual(memory.inspect('source', offset=9000, length=20)['text'], text[9000:9020])

    def test_upstream_truncation_stays_explicit(self):
        memory = EvidenceMemory('one', 'task1')
        memory.put('tool', 'available fragment', upstream_truncated=True)
        result = memory.inspect('tool', length=200)
        self.assertFalse(result['source_complete'])
        self.assertEqual(result['text'], 'available fragment')
        self.assertEqual(memory.inspect('tool', offset=100)['error'], 'outside_retained_content')

    def test_eviction_keeps_truthful_unavailable_marker(self):
        memory = EvidenceMemory('one', 'task1')
        memory.put('task', 'p' * 100000, pinned=True)
        memory.put('old', 'a' * 64000)
        memory.put('new', 'b' * 64000)
        self.assertEqual(memory.inspect('old')['error'], 'source_not_available')
        self.assertTrue(memory.index()[0]['available'])
        self.assertLessEqual(sum(len(r['text'] or '') for r in memory.records), TOTAL_CHARS)

    def test_json_corruption_and_cross_generation_rejected(self):
        memory = EvidenceMemory('one', 'task1')
        memory.put('s', 'original')
        raw = json.loads(json.dumps(memory.dump()))
        restored = EvidenceMemory.load(raw, owner='one', generation='task1')
        self.assertEqual(restored.inspect('s')['text'], 'original')
        self.assertTrue(EvidenceMemory.load(raw, owner='one', generation='task2').degraded)
        self.assertTrue(EvidenceMemory.load(raw, owner='two', generation='task1').degraded)
        raw['records'][0]['text'] = 'forged'
        self.assertTrue(EvidenceMemory.load(raw, owner='one', generation='task1').degraded)

    def test_unknown_source_is_not_a_host_file_or_network_tool(self):
        memory = EvidenceMemory('one', 'task1')
        with patch('builtins.open', side_effect=AssertionError('must not open files')):
            self.assertEqual(memory.inspect('C:/Users/private.txt')['error'], 'source_not_available')
            self.assertEqual(memory.inspect('https://example.com')['error'], 'source_not_available')
        self.assertEqual(memory.inspect('s', offset=True)['error'], 'invalid_memory_request')
        self.assertEqual(memory.inspect('s', length=99999)['error'], 'invalid_memory_request')


class MemoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {brain.TASK_AGENT_ENV: 'on'})
        env.start()
        self.addCleanup(env.stop)

    def inspect_plan(self, session, source_id, **kwargs):
        pending = session.state.team_agent.task.pending
        data = {'request_id': pending['token'], 'plan': {
            'kind': 'inspect', 'source_id': source_id, 'offset': 0, 'length': 500,
            'query': '', 'reason': '定位长原文中的关键事实',
            'evidence_ids': [source_id], **kwargs}}
        return session.step(model=json.dumps(data, ensure_ascii=False))

    def test_long_question_middle_is_retrieved_without_sandbox_command(self):
        session = Session()
        session.case['description'] = ('阅读全部说明，找出专用口令。\n' + '背景资料。' * 1600
                                       + '\n专用口令是 CERULEAN-29。\n' + '其他资料。' * 1300
                                       + '\n只返回专用口令字符串，不要其他内容。')
        first = session.step()
        self.assertIn('prompt', first)
        self.assertNotIn('CERULEAN-29', first['prompt'], 'middle content should require retrieval')
        source_id = 'task:' + session.state.team_agent.task.generation
        response = self.inspect_plan(session, source_id, query='专用口令是')
        self.assertIn('CERULEAN-29', response['prompt'])
        self.assertNotIn('executeCmd', response)
        self.assertEqual(session.state.team_agent.task.inspections, 1)
        self.assertEqual(session.state.team_agent.task.commands, 0)
        self.assertEqual(session.state.judge.llm_used_today, 0)
        answer = session.plan('answer', 'CERULEAN-29')
        self.assertEqual(answer['roleCommandMap']['10011']['taskAnswer'], 'CERULEAN-29')

    def test_long_tool_document_is_retrievable_after_json_transport(self):
        session = Session()
        session.case['sandbox_fixture']['files']['/brief/README.txt'] = (
            '目录说明。' * 1700 + '\nAPI入口：python3 /svc/weather.py --city 城市。\n' + '附录。' * 3000)
        session.step()
        session.tool('cat /brief/README.txt')
        source_id = session.state.team_agent.evidence[-1]['id']
        response = self.inspect_plan(session, source_id, query='API入口')
        self.assertIn('API入口：python3 /svc/weather.py --city 城市', response['prompt'])
        self.assertEqual(session.state.team_agent.task.commands, 1)
        result, _ = session.tool('python3 /svc/weather.py --city 北京')
        self.assertEqual(json.loads(result.split('\n', 1)[1])[0]['temperature'], 23)
        self.assertEqual(len(session.state.team_agent.skills.entries), 1)

    def test_new_task_drops_old_raw_text_and_focus_tampering_is_rejected(self):
        session = Session()
        session.case['description'] = '请按题意读取资料并找到文字 PRIVATE-OLD。不要直接猜答案。'
        session.step()
        old_id = 'task:' + session.state.team_agent.task.generation
        self.inspect_plan(session, old_id, query='PRIVATE-OLD')
        raw = session.state.team_agent.dump()
        raw['focus']['text'] += ' forged'
        self.assertTrue(TeamAgent.load(raw, raw['owner']).degraded)
        session.step(text='')
        session.round_no += 31
        session.shanghai()
        new = session.step()
        self.assertNotIn('PRIVATE-OLD', new['prompt'])
        self.assertEqual(session.state.team_agent.memory.inspect(old_id)['error'], 'source_not_available')


if __name__ == '__main__':
    unittest.main()
