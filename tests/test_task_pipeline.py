"""Issue 21/22 regressions through the real planner/router; scripted model only."""
import json
import os
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))
from agent import brain, planner, local_task_sandbox
from agent.task_agent import TaskAgent
from agent.task_context import COMMAND_LIMIT
from agent.llm_router import LLMRouter
from test_tasks import observation


class PipelineTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {brain.TASK_AGENT_ENV: 'on', brain.WORLD_AGENT_ENV: 'off'})
        env.start()
        self.addCleanup(env.stop)
        self.state = planner.PlannerState()
        self.round = 1
        self.task = '请阅读task_varied.md，获取任务信息'
        self.fixture = {'cwd': '/', 'files': {
            '/tmp/unseen/work/task_varied.md': '阅读同目录API_DOCS.md，查询丙地；仅返回JSON字段city和value。',
            '/tmp/unseen/work/API_DOCS.md': 'python3 /api/lookup.py --place 城市，返回单条记录数组。' + '详细说明。'*180 + 'END_OF_REAL_DOC'},
            'programs': {'/api/lookup.py': {'filters':['place'], 'required_filters':['place'],
                'records':[{'place':'丙地','city':'丙地','value':17}], 'output_fields':['city','value']}}}

    def step(self, llm='', cmd='', timeout=15, side='challenger', description=None):
        payload = observation(round_no=self.round, team=side, role_pos=(6,5),
            phase_task=self.task if description is None else description,
            llm_resp=llm, cmd_result=cmd, timeout_rounds=timeout)
        with patch.object(planner, 'state_for', return_value=self.state):
            response = brain.respond(payload)
        self.state = planner.PlannerState.load(json.loads(json.dumps(self.state.dump())))
        self.assertFalse(self.state.team_agent.degraded)
        self.round += 1
        return response

    def answer(self, kind, value, **extra):
        pending = self.state.team_agent.task.pending
        return json.dumps({'request_id':pending['token'], 'plan':{
            'kind':kind, 'command' if kind=='run' else 'answer':value,
            'evidence_ids':pending['evidence_ids']}, **extra}, ensure_ascii=False)

    def test_both_sides_first_command_reads_and_submits_before_deadline(self):
        for side in ('challenger','defender'):
            with self.subTest(side=side):
                self.state, self.round = planner.PlannerState(), 1
                first = self.step(side=side)
                self.assertIn('executeCmd', first)
                self.assertNotIn('prompt', first)
                result = local_task_sandbox.execute(first['executeCmd'], self.fixture, active=True)
                prompt = self.step(cmd=result, side=side)
                self.assertIn('END_OF_REAL_DOC', prompt['prompt'])
                self.assertIn('rounds_remaining', prompt['prompt'])
                query = self.step(llm=self.answer('run', 'python3 /api/lookup.py --place 丙地'), side=side)
                self.assertIn('executeCmd', query)
                output = local_task_sandbox.execute(query['executeCmd'], self.fixture, active=True)
                self.step(cmd=output, side=side)
                submitted = self.step(llm=self.answer('answer', '{"city":"丙地","value":17}',
                    summary='已读取资料，下一步提交'), side=side)
                actions = list(submitted['roleCommandMap'].values())
                self.assertIn({'action':'submitAnswer','taskAnswer':'{"city":"丙地","value":17}'}, actions)
                self.assertNotIn('prompt', submitted)
                self.assertEqual(self.state.team_agent.task.prompts, 2)
                self.assertEqual(self.state.team_agent.task.commands, 2)
                self.assertEqual(self.state.team_agent.task.answers, 1)
                self.assertEqual(self.state.judge.llm_used_today, 0)
                self.assertLess(self.round, 15)

    def test_oversized_command_can_replan_instead_of_stopping_at_router(self):
        first = self.step()
        self.step(cmd=local_task_sandbox.execute(first['executeCmd'], self.fixture, active=True))
        retried = self.step(llm=self.answer('run', 'x'*(COMMAND_LIMIT+1)))
        self.assertNotIn('executeCmd', retried)
        self.assertIn('prompt', retried)
        self.assertIn('payload_over_limit', retried['prompt'])
        self.assertEqual(self.state.team_agent.task.commands, 1)
        good = self.step(llm=self.answer('run','cat /tmp/unseen/work/API_DOCS.md'))
        self.assertIn('executeCmd', good)

    def test_missing_probe_result_is_evidence_not_automatic_give_up(self):
        first = self.step()
        result = local_task_sandbox.execute(first['executeCmd'], {'cwd':'/','files':{}}, active=True)
        prompt = self.step(cmd=result)
        self.assertIn('not_found', prompt['prompt'])
        self.assertIn('一次文件缺失不等于任务无解', prompt['prompt'])
        self.assertEqual(self.state.team_agent.task.stage, 'waiting_model')

    def test_deadline_stops_tool_and_unknown_timeout_is_not_invented(self):
        self.step()
        self.round = 16
        expired = self.step()
        self.assertNotIn('executeCmd', expired)
        self.assertNotIn('prompt', expired)
        self.state, self.round = planner.PlannerState(), 1
        first = self.step(timeout=0)
        output = local_task_sandbox.execute(first['executeCmd'], self.fixture, active=True)
        unknown = self.step(cmd=output, timeout=0)
        self.assertIn('"rounds_remaining": null', unknown['prompt'])

    def test_summary_is_optional_and_invalid_summary_cannot_block_answer(self):
        for summary in (None, {'bad':'type'}, 'x'*10000):
            self.state, self.round = planner.PlannerState(), 1
            first = self.step()
            self.step(cmd=local_task_sandbox.execute(first['executeCmd'],self.fixture,active=True))
            response = self.step(llm=self.answer('answer','{"city":"丙地","value":17}',summary=summary))
            self.assertTrue(any(c['action']=='submitAnswer' for c in response['roleCommandMap'].values()))

    def test_last_window_keeps_answer_but_does_not_start_a_late_command(self):
        for kind, value in (('run', 'cat /tmp/unseen/work/API_DOCS.md'),
                            ('answer', '{"city":"丙地","value":17}')):
            self.state, self.round = planner.PlannerState(), 1
            first = self.step(timeout=3)
            self.step(cmd=local_task_sandbox.execute(first['executeCmd'], self.fixture, active=True), timeout=3)
            final = self.step(llm=self.answer(kind, value), timeout=3)
            self.assertNotIn('executeCmd', final)
            self.assertNotIn('prompt', final)
            self.assertEqual(any(c['action']=='submitAnswer' for c in final['roleCommandMap'].values()),kind=='answer')

    def test_pending_and_router_share_command_boundary(self):
        for length in (COMMAND_LIMIT, COMMAND_LIMIT+1):
            a=TaskAgent(); a.begin('g','s')
            p=a.decide('context',['s'],active=True,round_no=1); a.acknowledge(p['token'],round_no=1)
            reply=json.dumps({'request_id':p['token'],'plan':{'kind':'run','command':'x'*length,'evidence_ids':['s']}})
            accepted=a.receive(p['token'],'prompt',reply,round_no=2,verified=True)
            offered=LLMRouter().offer('task','g','s',kind='cmd',payload='x'*length,in_task=True)
            self.assertEqual(accepted, offered is not None)
            if accepted:
                restored=TaskAgent.load(json.loads(json.dumps(a.dump())))
                self.assertEqual(restored.proposal['payload'],'x'*length)
                invalid=deepcopy(a.dump()); invalid['proposal']['payload']+='x'
                self.assertEqual(TaskAgent.load(invalid).stop_reason,'state_restore_rejected')


if __name__=='__main__':
    unittest.main()
