"""Independent regressions from Issue 27 patterns, not official answer fixtures."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))
from agent.task_agent import TaskAgent
from agent import task_feedback, task_tools

ERROR = [{'errorCode': 2, 'description': '键值比对不通过: $/oldest_era: 值不符'}]
FAIL = '[exitCode:126]\n/bin/bash: ./check: /bin/sh^M: bad interpreter: No such file or directory\n'


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.agent = TaskAgent()
        self.agent.begin('task-a', 'source-a')

    def answer(self, value, round_no):
        ask = self.agent.decide('原题与数据', ['source-a'], active=True, round_no=round_no)
        self.assertIsNotNone(ask)
        self.agent.acknowledge(ask['token'], round_no=round_no)
        reply = json.dumps({'request_id': ask['token'], 'plan': {
            'kind': 'answer', 'answer': value, 'evidence_ids': ['source-a']}})
        return self.agent.receive(ask['token'], 'prompt', reply, round_no=round_no + 1, verified=True)

    def fail_answer(self, value, round_no):
        self.assertTrue(self.answer(value, round_no))
        self.agent.acknowledge(self.agent.proposal['token'], round_no=round_no + 1)
        self.agent.feedback(ERROR, round_no=round_no + 2)

    def test_a_b_a_rejected_after_restore_and_json_reordering(self):
        a = '{"city":"测试城市","oldest_era":"A"}'
        b = '{"city":"测试城市","oldest_era":"B"}'
        self.fail_answer(a, 1)
        self.fail_answer(b, 3)
        self.agent = TaskAgent.load(self.agent.dump())
        self.assertFalse(self.answer(' {"oldest_era": "A", "city": "测试城市"} ', 5))
        self.assertIsNone(self.agent.proposal)
        self.assertEqual(self.agent.answers, 2)
        prompt = self.agent.decide('ctx', ['source-a'], active=True, round_no=7)['payload']
        self.assertIn('oldest_era', prompt)
        self.assertIn('"A"', prompt)
        self.assertIn('"B"', prompt)
        self.assertNotIn('official_success', self.agent.dump())

    def test_rejection_outlives_short_history_but_not_new_task(self):
        self.fail_answer('wrong', 1)
        for i in range(20):
            self.agent._event('memory_result', 'unrelated', 4 + i)
        self.assertTrue(self.agent.answer_rejected('wrong'))
        self.agent.begin('task-b', 'source-a')
        self.assertTrue(self.answer('wrong', 30))

    def test_unknown_or_nonadjacent_feedback_does_not_blacklist(self):
        for errors, at in ((ERROR, 7), ([{'errorCode': 4}], 3), ([], 3)):
            self.setUp()
            self.answer('value', 1)
            self.agent.acknowledge(self.agent.proposal['token'], round_no=2)
            self.agent.feedback(errors, round_no=at)
            self.assertFalse(self.agent.answer_rejected('value'))

    def test_new_answer_and_tool_investigation_remain_available(self):
        self.fail_answer('A', 1)
        self.assertTrue(self.answer('C', 3))
        self.assertEqual(self.agent.proposal['payload'], 'C')

    def test_legacy_migration_and_invalid_memory_rejected(self):
        old = self.agent.dump()
        old['schema'] = 'competition-task-agent/2'
        old.pop('last_submission'); old.pop('rejected_answers')
        restored = TaskAgent.load(old)
        self.assertEqual(restored.stage, 'ready')
        self.assertEqual(restored.rejected_answers, [])
        for bad in ([{'sha256': 'fake'}], 'bad', [None] * 9):
            raw = self.agent.dump(); raw['rejected_answers'] = bad
            self.assertEqual(TaskAgent.load(raw).stop_reason, 'state_restore_rejected')

    def test_rejection_memory_has_its_own_prompt_budget(self):
        for i in range(8):
            self.agent.rejected_answers.append(task_feedback.rejection(
                json.dumps({'longfield': str(i) + 'x' * 99}),
                [{'errorCode': 2, 'description': '$/longfield: ' + 'y' * 340}], i + 1))
        ask = self.agent.decide('x' * 7500, ['source-a'], active=True, round_no=20)
        self.assertIsNotNone(ask)
        self.assertLessEqual(len(ask['payload']), 12000)
        self.assertEqual(len(self.agent.rejected_answers), 8)

    def test_http_page_diagnostics_preserve_recovery_and_unknown(self):
        command = task_tools.command('http', {'url': 'http://localhost:8899/search'})
        def quality(rows, total=12, **extra):
            obj = {'tool': 'task-http/1', 'status': 200, 'truncated': False,
                   'attempts': [{'status': 401}, {'status': 400}, {'status': 200}],
                   'data': {'code': 200, 'data': {'records': rows,
                            'pagination': {'total_count': total, 'offset': 0}}}, **extra}
            return task_feedback.http_quality(command, '[exitCode:0]\n' + json.dumps(obj))
        partial = quality([{}] * 10)
        self.assertEqual(partial['state'], 'partial_page')
        self.assertEqual(partial['http_status'], 200)
        self.assertEqual((partial['returned'], partial['total']), (10, 12))
        self.assertEqual(quality([{}] * 15, 15)['state'], 'count_consistent')
        self.assertEqual(quality([], 0)['state'], 'count_consistent')
        self.assertEqual(quality([], status=401)['state'], 'request_failed')
        self.assertEqual(quality([], data={'code': 401})['state'], 'application_error')
        self.assertEqual(quality([], truncated=True)['state'], 'truncated')
        self.assertEqual(quality([], data=[])['state'], 'unknown')
        self.assertEqual(task_feedback.http_quality(command, '[exitCode:0]\n{"partial":\n[TRUNCATED]')['state'], 'truncated')
        self.assertIsNone(task_feedback.http_quality('echo fake', '{}'))

    def test_compound_crlf_recovery_only_proposes_check(self):
        cmd = 'cd /tmp/ws_1/ && mkdir -p logs/alpha && chmod 755 logs/alpha && echo -e "# config\\nport 8080" > config.conf && ./check'
        recovered = task_tools.failed_check_recovery(cmd, FAIL)
        self.assertEqual(recovered, task_tools.command('check', {'path': '/tmp/ws_1/check'}))
        self.assertNotIn('mkdir', recovered)
        for invalid in ('cd relative && mkdir x && ./check',
                        'cd /tmp/ws && cd other && ./check',
                        'cd /tmp/ws && sh setup && ./check',
                        'cd /tmp/ws && echo $(pwd) && ./check',
                        'cd /tmp/ws && mkdir x; ./check',
                        'cd /tmp/ws && mkdir x && ./check extra'):
            self.assertIsNone(task_tools.failed_check_recovery(invalid, FAIL), invalid)
        self.assertIsNone(task_tools.failed_check_recovery(cmd, '[exitCode:0]\nTOKEN: x'))

    def test_router_verified_failure_auto_recovery_is_bounded(self):
        ask = self.agent.decide('ctx', ['source-a'], active=True, round_no=1)
        self.agent.acknowledge(ask['token'], round_no=1)
        value = 'cd /tmp/ws && mkdir -p logs && ./check'
        reply = json.dumps({'request_id': ask['token'], 'plan': {
            'kind': 'run', 'command': value, 'evidence_ids': ['source-a']}})
        self.agent.receive(ask['token'], 'prompt', reply, round_no=2, verified=True)
        run = self.agent.proposal
        self.agent.acknowledge(run['token'], round_no=2)
        self.assertFalse(self.agent.receive(run['token'], 'cmd', FAIL, round_no=3, verified=False))
        self.assertIsNone(self.agent.proposal)
        self.assertTrue(self.agent.receive(run['token'], 'cmd', FAIL, round_no=3, verified=True))
        self.assertTrue(self.agent.proposal['payload'].startswith('# task-check/1'))
        self.assertEqual(self.agent.commands, 1, 'proposal does not count as sent')

    @unittest.skipUnless(os.name == 'posix', 'actual POSIX recovery')
    def test_real_check_only_recovery_preserves_prefix_side_effects_and_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            check = Path(tmp) / 'check'
            original = b'#!/bin/sh\r\nprintf "TOKEN: fixture\\n"\r\n'
            check.write_bytes(original); check.chmod(0o755)
            import shlex
            cmd = 'cd ' + shlex.quote(tmp) + ' && printf x >> count && ./check'
            first = subprocess.run(['bash', '-c', cmd], capture_output=True, text=True)
            # Bash 5.2 reports this missing CRLF interpreter as 127, unlike the
            # recorded judge's 126/bad-interpreter text. Feed the documented
            # judge receipt to selection, then execute the REAL recovered tool.
            self.assertIn(first.returncode, (126, 127))
            self.assertNotEqual(first.stderr, '')
            recovered = task_tools.failed_check_recovery(cmd, FAIL)
            self.assertIsNotNone(recovered)
            result = subprocess.run(['sh', '-c', recovered], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['stdout'], 'TOKEN: fixture\n')
            self.assertEqual((Path(tmp) / 'count').read_text(), 'x')
            self.assertEqual(check.read_bytes(), original)


class PipelineFeedbackTests(unittest.TestCase):
    def test_both_sides_reject_repeated_answer_through_real_router_and_state(self):
        import test_task_pipeline as support
        from unittest.mock import patch
        from agent import local_task_sandbox, planner
        for side in ('challenger', 'defender'):
            case = support.PipelineTests()
            case.setUp()
            self.addCleanup(case.doCleanups)
            first = case.step(side=side)
            case.step(cmd=local_task_sandbox.execute(first['executeCmd'], case.fixture, active=True), side=side)
            first_answer = case.step(llm=case.answer('answer', '{"oldest_era":"A"}'), side=side)
            self.assertTrue(any(x['action'] == 'submitAnswer' for x in first_answer['roleCommandMap'].values()))
            original = support.observation
            def with_error(**kwargs):
                return {**original(**kwargs), 'errors': ERROR}
            with patch.object(support, 'observation', side_effect=with_error):
                case.step(side=side)
            second = case.step(llm=case.answer('answer', '{"oldest_era":"B"}'), side=side)
            self.assertTrue(any(x['action'] == 'submitAnswer' for x in second['roleCommandMap'].values()))
            with patch.object(support, 'observation', side_effect=with_error):
                case.step(side=side)
            third = case.step(llm=case.answer('answer', ' {"oldest_era": "A"} '), side=side)
            self.assertFalse(any(x['action'] == 'submitAnswer' for x in third['roleCommandMap'].values()))
            self.assertIn('repeated_answer_rejected', third['prompt'])
            self.assertEqual(case.state.team_agent.task.answers, 2)


if __name__ == '__main__':
    unittest.main()
