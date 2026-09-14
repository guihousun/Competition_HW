"""Conditional methods across actual task generations, never cached answers.

The comparison uses a fixed scripted model, so it measures available saved
discovery operations rather than claiming a real-model average speedup.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Demo/CoreGeek/src"))
from agent import brain, local_task_cases, local_task_sandbox, planner
from test_tasks import observation


class Session:
    def __init__(self):
        self.state = planner.PlannerState()
        self.round_no = 1
        self.case = deepcopy(local_task_cases.CASES[0])
        self.trace = []

    def step(self, *, model="", tool="", text=None):
        payload = observation(round_no=self.round_no, role_pos=(6, 5), timeout_rounds=30,
                              phase_task=self.case['description'] if text is None else text,
                              llm_resp=model, cmd_result=tool)
        with patch.object(planner, 'state_for', return_value=self.state):
            response = brain.respond(payload)
        self.state = planner.PlannerState.load(json.loads(json.dumps(self.state.dump())))
        self.trace.append(response)
        self.round_no += 1
        return response

    def plan(self, kind, value):
        pending = self.state.team_agent.task.pending
        reply = {'request_id': pending['token'], 'plan': {
            'kind': kind, 'command' if kind == 'run' else 'answer': value,
            'reason': '根据当前文档和查询结果', 'evidence_ids': pending['evidence_ids']}}
        return self.step(model=json.dumps(reply, ensure_ascii=False))

    def tool(self, command):
        response = self.plan('run', command)
        assert response.get('executeCmd') == command
        result = local_task_sandbox.execute(command, self.case['sandbox_fixture'], active=True)
        return result, self.step(tool=result)

    def warm(self):
        self.step()
        self.tool('cat /brief/README.txt')
        self.tool('python3 /svc/weather.py --city 北京')
        self.plan('answer', '{"city":"北京","temperature":23}')
        self.step(text='')
        self.round_no += 31

    def shanghai(self):
        self.case = deepcopy(local_task_cases.CASES[0])
        self.case['description'] = self.case['description'].replace('北京', '上海')
        self.case['answer'] = '{"city":"上海","temperature":29}'


class MethodTransferTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {brain.TASK_AGENT_ENV: 'on'})
        env.start()
        self.addCleanup(env.stop)

    def test_new_city_rechecks_document_and_queries_new_data(self):
        s = Session()
        s.warm()
        self.assertEqual(len(s.state.team_agent.skills.entries), 1)
        old_generation = s.state.team_agent.task.generation
        s.shanghai()
        first = s.step()
        self.assertIn('/brief/README.txt', first['prompt'])
        self.assertIn('requires_current_document_verification', first['prompt'])
        self.assertNotIn('<current-value>', first['prompt'], 'query template stays hidden before fresh doc verification')
        self.assertEqual(s.state.team_agent.evidence, [])
        self.assertNotEqual(s.state.team_agent.task.generation, old_generation)
        _, after_doc = s.tool('cat /brief/README.txt')
        self.assertIn('document_revalidated_query_method', after_doc['prompt'])
        result, _ = s.tool('python3 /svc/weather.py --city 上海')
        self.assertEqual(json.loads(result.split('\n', 1)[1]), [{'city': '上海', 'temperature': 29}])
        response = s.plan('answer', '{"city":"上海","temperature":29}')
        self.assertEqual(json.loads(response['roleCommandMap']['10011']['taskAnswer']), {'city': '上海', 'temperature': 29})
        self.assertEqual(s.state.team_agent.task.commands, 2)
        saved = json.dumps(s.state.team_agent.skills.dump(), ensure_ascii=False)
        self.assertNotIn('北京', saved)
        self.assertNotIn('上海', saved)

    def test_same_document_path_changed_interface_does_not_reuse_old_format(self):
        s = Session()
        s.warm()
        s.shanghai()
        fixture = s.case['sandbox_fixture']
        fixture['files']['/brief/README.txt'] = '接口已变为 python3 /svc/weather.py --place 城市，返回当前记录。'
        fixture['programs']['/svc/weather.py'] = {
            'filters': ['place'], 'required_filters': ['place'],
            'records': [{'place': '上海', 'city': '上海', 'temperature': 17}],
            'output_fields': ['city', 'temperature']}
        s.step()
        _, after_doc = s.tool('cat /brief/README.txt')
        self.assertNotIn('document_revalidated_query_method', after_doc['prompt'])
        self.assertNotIn('"--city", "<current-value>"', after_doc['prompt'])
        result, _ = s.tool('python3 /svc/weather.py --place 上海')
        self.assertEqual(json.loads(result.split('\n', 1)[1]), [{'city': '上海', 'temperature': 17}])
        response = s.plan('answer', '{"city":"上海","temperature":17}')
        self.assertEqual(json.loads(response['roleCommandMap']['10011']['taskAnswer'])['temperature'], 17)
        self.assertEqual(len(s.state.team_agent.skills.entries), 2, 'two document versions remain distinct methods')

    def test_method_hint_saves_one_discovery_operation_under_fixed_script(self):
        results = []
        for warm in (False, True):
            s = Session()
            if warm:
                s.warm()
            s.shanghai()
            first = s.step()
            if 'requires_current_document_verification' not in first['prompt']:
                listing, _ = s.tool('ls /brief')
                self.assertIn('README.txt', listing)
            s.tool('cat /brief/README.txt')
            result, _ = s.tool('python3 /svc/weather.py --city 上海')
            value = json.loads(result.split('\n', 1)[1])[0]
            response = s.plan('answer', json.dumps(value, ensure_ascii=False))
            self.assertEqual(json.loads(response['roleCommandMap']['10011']['taskAnswer']), {'city': '上海', 'temperature': 29})
            agent = s.state.team_agent.task
            results.append((agent.prompts, agent.commands))
        self.assertEqual(results, [(4, 3), (3, 2)])


if __name__ == '__main__':
    unittest.main()
