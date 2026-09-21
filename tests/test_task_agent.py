"""Task Agent control-loop tests with scripted model replies, not model accuracy."""
from copy import deepcopy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))
from agent.task_agent import TaskAgent, MAX_PROMPTS
from agent.local_task_sandbox import execute
from agent.local_task_cases import CASES


class TaskAgentTests(unittest.TestCase):
    def agent(self):
        agent = TaskAgent()
        agent.begin('generation-1', 'task-source')
        return agent

    def request(self, agent, round_no):
        action = agent.decide('原题和已核验的工具结果', ['task-source'], active=True, round_no=round_no)
        self.assertEqual(action['kind'], 'prompt')
        self.assertTrue(agent.acknowledge(action['token'], round_no=round_no))
        return action

    def reply(self, action, kind, **extra):
        return json.dumps({'request_id': action['token'], 'plan': {
            'kind': kind, 'evidence_ids': ['task-source'], **extra}}, ensure_ascii=False)

    def test_full_model_tool_loop_and_exact_answer(self):
        agent = self.agent()
        fixture = CASES[0]['sandbox_fixture']
        round_no = 1
        for command in ('ls /brief', 'cat /brief/README.txt', 'python3 /svc/weather.py --city 北京'):
            ask = self.request(agent, round_no)
            self.assertTrue(agent.receive(ask['token'], 'prompt', self.reply(ask, 'run', command=command),
                                          round_no=round_no + 1, verified=True))
            run = agent.decide('same context', ['task-source'], active=True, round_no=round_no + 1)
            self.assertEqual(run['kind'], 'cmd')
            self.assertEqual(run['payload'], command)
            self.assertTrue(agent.acknowledge(run['token'], round_no=round_no + 1))
            agent = TaskAgent.load(json.loads(json.dumps(agent.dump())))
            output = execute(run['payload'], fixture, active=True)
            self.assertTrue(agent.receive(run['token'], 'cmd', output, round_no=round_no + 2, verified=True))
            round_no += 2
        ask = self.request(agent, round_no)
        answer = '{\n "city": "北京", "temperature": 23\n}\n'
        self.assertTrue(agent.receive(ask['token'], 'prompt', self.reply(ask, 'answer', answer=answer),
                                      round_no=round_no + 1, verified=True))
        submit = agent.decide('context', ['task-source'], active=True, round_no=round_no + 1)
        self.assertEqual(submit['payload'], answer)
        self.assertTrue(agent.acknowledge(submit['token'], round_no=round_no + 1))
        self.assertEqual(agent.stage, 'awaiting_judgement')
        self.assertIsNone(agent.decide('context', ['task-source'], active=True, round_no=round_no + 2))
        self.assertEqual((agent.prompts, agent.commands, agent.answers), (4, 3, 1))

    def test_wrong_answer_feedback_allows_a_new_plan_not_fake_success(self):
        agent = self.agent()
        ask = self.request(agent, 1)
        agent.receive(ask['token'], 'prompt', self.reply(ask, 'answer', answer='wrong'), round_no=2, verified=True)
        submit = agent.decide('context', ['task-source'], active=True, round_no=2)
        agent.acknowledge(submit['token'], round_no=2)
        agent.feedback([{'errorCode': 2, 'description': 'not fully correct'}], round_no=3)
        self.assertEqual(agent.stage, 'ready')
        retry = agent.decide('原题，输出格式和工具证据', ['task-source'], active=True, round_no=3)
        self.assertIn('not fully correct', retry['payload'])
        self.assertNotEqual(retry['token'], ask['token'])

    def test_offer_preview_and_duplicate_ack_do_not_consume_counts(self):
        agent = self.agent()
        a = agent.decide('context', ['task-source'], active=True, round_no=1)
        self.assertEqual(a, agent.decide('context', ['task-source'], active=True, round_no=1))
        self.assertEqual(agent.prompts, 0)
        self.assertTrue(agent.acknowledge(a['token'], round_no=1))
        self.assertFalse(agent.acknowledge(a['token'], round_no=1))
        self.assertEqual(agent.prompts, 1)

    def test_old_generation_same_round_and_unverified_receipts_do_not_act(self):
        agent = self.agent()
        ask = self.request(agent, 1)
        reply = self.reply(ask, 'run', command='cat /brief/README.txt')
        self.assertFalse(agent.receive(ask['token'], 'prompt', reply, round_no=1, verified=True))
        self.assertFalse(agent.receive(ask['token'], 'prompt', reply, round_no=2, verified=False))
        agent.begin('generation-2', 'task-source')
        self.assertFalse(agent.receive(ask['token'], 'prompt', reply, round_no=2, verified=True))
        new = agent.decide('context', ['task-source'], active=True, round_no=2)
        self.assertNotEqual(new['token'], ask['token'])
        self.assertEqual(agent.commands, 0)

    def test_invalid_json_and_forged_evidence_or_official_fields_never_execute(self):
        cases = [lambda a: 'not json',
                 lambda a: self.reply(a, 'run', command='ls', roleCommandMap={'7': {'action': 'attack'}}),
                 lambda a: self.reply(a, 'run', command='ls', evidence_ids=['invented-source']),
                 lambda a: self.reply(a, 'run', command='ls', answer='also answer'),
                 lambda a: self.reply(a, 'run', command='ls').replace(a['token'], 'wrong'),
                 lambda a: '{"request_id":"x","request_id":"' + a['token'] + '","plan":{}}']
        for make in cases:
            agent = self.agent(); ask = self.request(agent, 1)
            self.assertFalse(agent.receive(ask['token'], 'prompt', make(ask), round_no=2, verified=True))
            self.assertIsNone(agent.proposal)
            self.assertEqual(agent.commands, 0)
            self.assertEqual(agent.stage, 'ready')

    def test_rejected_plan_diagnostic_is_specific_bounded_and_reaches_journal(self):
        from agent.team_agent import TeamAgent
        from agent.task_journal import TaskJournal
        from test_task_journal import request
        agent=self.agent(); ask=self.request(agent,1)
        agent.receive(ask['token'],'prompt',self.reply(ask,'run',command='ls',
                      evidence_ids=['secret-not-in-logs']),round_no=2,verified=True)
        self.assertEqual(agent.history[-1]['text'],'unverified evidence reference')
        team=TeamAgent('team-42'); team.task=agent
        summary=team.summary()
        self.assertEqual(summary['lastPlanRejection'],{'round':2,'reason':'unverified evidence reference'})
        rows=TaskJournal().observe(request(2,phaseTask='题目'),{},decision={'agent':summary})
        states=[json.loads(r['content']['text']) for r in rows if r['kind']=='agent_state']
        self.assertEqual(states[0]['lastPlanRejection'],summary['lastPlanRejection'])
        self.assertNotIn('secret-not-in-logs',str(states))

    def test_repair_calls_are_bounded_and_task_end_drops_work(self):
        agent = self.agent()
        for i in range(MAX_PROMPTS):
            ask = self.request(agent, 2*i + 1)
            agent.receive(ask['token'], 'prompt', 'malformed', round_no=2*i+2, verified=True)
        self.assertIsNone(agent.decide('context', ['task-source'], active=True, round_no=100))
        self.assertEqual(agent.stage, 'stopped')
        agent = self.agent(); self.request(agent, 1)
        self.assertIsNone(agent.decide('context', ['task-source'], active=False, round_no=2))
        self.assertIsNone(agent.pending)
        self.assertEqual(agent.stage, 'ended')

    def test_restore_rejects_corrupted_or_unbounded_state(self):
        agent = self.agent(); self.request(agent, 1)
        mutations = [('schema', 'newer'), ('prompts', -1), ('commands', True),
                     ('history', ['not an event']), ('seen', [True]),
                     ('command_attempts', {'x'*24: '2'}), ('stage', 'ready')]
        for key, value in mutations:
            raw = agent.dump(); raw[key] = value
            restored = TaskAgent.load(raw)
            self.assertEqual(restored.stop_reason, 'state_restore_rejected', key)
            self.assertFalse(restored.begin('another', 'source'))
        raw = agent.dump(); raw['pending']['sent_round'] = '1'
        self.assertEqual(TaskAgent.load(raw).stop_reason, 'state_restore_rejected')
        raw = agent.dump(); raw['pending']['payload'] = 'x'*25000
        self.assertEqual(TaskAgent.load(raw).stop_reason, 'state_restore_rejected')

    def test_expired_router_operation_can_replan_without_fabricating_a_reply(self):
        agent = self.agent(); ask = self.request(agent, 1)
        self.assertFalse(agent.failed('other-request', 'expired', round_no=5))
        self.assertTrue(agent.failed(ask['token'], 'router_expired', round_no=5))
        retry = agent.decide('context', ['task-source'], active=True, round_no=5)
        self.assertEqual(retry['kind'], 'prompt')
        self.assertIn('router_expired', retry['payload'])
        self.assertEqual(agent.prompts, 1, 'retry proposal is not yet another emitted call')


if __name__ == '__main__':
    unittest.main()
