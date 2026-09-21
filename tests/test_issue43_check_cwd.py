"""R01/R07 check compatibility: synthetic scripts, no official task/token claims."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))
from agent import task_tools
from agent.task_agent import TaskAgent, MAX_COMMANDS
from agent.task_context import COMMAND_LIMIT

ENV_FAIL = '[exitCode:126]\n./check: line 1: #!/usr/bin/env bash^M: bad interpreter: No such file or directory\n'
BIN_FAIL = '[exitCode:126]\n/bin/bash: ./check: /bin/bash^M: bad interpreter: No such file or directory\n'


class CheckSelectionTests(unittest.TestCase):
    def test_bare_selection_does_not_consult_host_cwd_and_is_bounded(self):
        with patch('os.getcwd', side_effect=AssertionError('not the task sandbox')):
            cmd = task_tools.standalone_check('./check')
        self.assertEqual(cmd, task_tools.command('check', {'path': './check'}))
        self.assertLessEqual(len(cmd), COMMAND_LIMIT)
        for invalid in ('check', './check.sh', './other/check', '../check', './check x',
                        './check | cat', './check > out', './check && echo x',
                        'echo x; ./check', '$(pwd)/check', './check\necho x',
                        'cd relative && ./check'):
            self.assertIsNone(task_tools.standalone_check(invalid), invalid)
        for path in ('check', './check.sh', './other/check', '../check'):
            with self.assertRaises(ValueError):
                task_tools.command('check', {'path': path})

    def test_recovery_requires_126_known_signature_and_narrow_command(self):
        expected = task_tools.command('check', {'path': './check'})
        for result in (ENV_FAIL, BIN_FAIL,
                       '[exitCode:126]\n/usr/bin/env: ‘bash^M’: No such file or directory\n'):
            self.assertEqual(task_tools.failed_check_recovery('./check', result), expected)
            self.assertEqual(task_tools.failed_check_recovery('cd /tmp/task && ./check', result),
                             task_tools.command('check', {'path': '/tmp/task/check'}))
        for result in (ENV_FAIL.replace('126', '0'), ENV_FAIL.replace('126', '127'),
                       '[TIMEOUT]\n' + ENV_FAIL, '[exitCode:126]\npermission denied',
                       ENV_FAIL.replace('bash^M', 'ruby^M')):
            self.assertIsNone(task_tools.failed_check_recovery('./check', result))
        for cmd in ('./check arg', 'echo x && ./check', 'cd /tmp/a && sh setup && ./check',
                    'cd /tmp/a && ./check | cat', 'cd /tmp/a && ./check > out'):
            self.assertIsNone(task_tools.failed_check_recovery(cmd, ENV_FAIL), cmd)
        self.assertIsNone(task_tools.failed_check_recovery(expected, ENV_FAIL),
                          'the generated wrapper must not recursively recover itself')

    def test_real_agent_proposals_only_count_after_emit_and_receipts_match(self):
        agent = TaskAgent()
        agent.begin('task-a', 'source')
        ask = agent.decide('fixture context', ['source'], active=True, round_no=1)
        self.assertTrue(agent.acknowledge(ask['token'], round_no=1))
        reply = json.dumps({'request_id': ask['token'], 'plan': {
            'kind': 'run', 'command': './check', 'evidence_ids': ['source']}})
        self.assertTrue(agent.receive(ask['token'], 'prompt', reply, round_no=2, verified=True))
        run = agent.proposal
        self.assertEqual(run['payload'], task_tools.command('check', {'path': './check'}))
        self.assertEqual(agent.commands, 0)
        self.assertFalse(agent.receive(run['token'], 'cmd', '[exitCode:0]\nfixture', round_no=3, verified=True))
        self.assertTrue(agent.acknowledge(run['token'], round_no=2))
        self.assertEqual(agent.commands, 1)
        self.assertFalse(agent.receive('wrong-token', 'cmd', '[exitCode:0]\nfixture', round_no=3, verified=True))
        self.assertFalse(agent.receive(run['token'], 'cmd', '[exitCode:0]\nfixture', round_no=3, verified=False))
        self.assertTrue(agent.receive(run['token'], 'cmd', '[exitCode:0]\nfixture', round_no=3, verified=True))
        self.assertFalse(agent.receive(run['token'], 'cmd', '[exitCode:0]\nfixture', round_no=4, verified=True))
        self.assertEqual(agent.answers, 0, 'a tool receipt is not a submitted or judged answer')

    def test_legacy_bare_recovery_and_command_budgets_survive_restore(self):
        for exhausted in (False, True):
            agent = TaskAgent(); agent.begin('task-a', 'source')
            # A previously emitted raw bare command from the pre-fix version.
            run = agent._propose('cmd', './check', 'legacy command')
            agent.acknowledge(run['token'], round_no=1)
            if exhausted:
                agent.commands = MAX_COMMANDS
            agent = TaskAgent.load(agent.dump())
            self.assertFalse(agent.receive(run['token'], 'cmd', ENV_FAIL, round_no=2, verified=False))
            self.assertTrue(agent.receive(run['token'], 'cmd', ENV_FAIL, round_no=2, verified=True))
            if exhausted:
                self.assertIsNone(agent.proposal)
            else:
                retry = agent.proposal
                self.assertIsNotNone(retry)
                self.assertEqual(agent.commands, 1)
                agent.acknowledge(retry['token'], round_no=2)
                agent.receive(retry['token'], 'cmd', ENV_FAIL, round_no=3, verified=True)
                self.assertIsNone(agent.proposal, 'a failed compatibility execution is not endlessly retried')


@unittest.skipUnless(os.name == 'posix', 'real POSIX sandbox cwd/process execution')
class CheckSandboxTests(unittest.TestCase):
    def run_check(self, cwd, command=None):
        return subprocess.run(['sh', '-c', command or task_tools.standalone_check('./check')],
                              cwd=cwd, capture_output=True, text=True, encoding='utf-8', timeout=10)

    def test_each_execution_uses_its_current_task_cwd_and_preserves_bytes(self):
        cmd = task_tools.standalone_check('./check')
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('first task', 'second task'):
                folder = Path(tmp) / name; folder.mkdir()
                raw = b'#!/usr/bin/env bash\r\nprintf "%s\\n" "$PWD"\r\ncat marker\r\n'
                (folder / 'check').write_bytes(raw)
                (folder / 'marker').write_text(name)
                result = self.run_check(folder, cmd)
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data['stdout'], str(folder) + '\n' + name)
                self.assertEqual(data['path'], str(folder / 'check'))
                self.assertEqual(data['path_source'], 'sandbox_cwd')
                self.assertTrue(data['crlf_normalized'])
                self.assertEqual((folder / 'check').read_bytes(), raw)

    def test_task_agent_dispatch_real_receipt_and_next_generation_cwd(self):
        agent = TaskAgent()
        previous_token = None
        with tempfile.TemporaryDirectory() as tmp:
            for index, name in enumerate(('task-one', 'task-two')):
                folder = Path(tmp) / name; folder.mkdir()
                (folder / 'check').write_bytes(b'#!/usr/bin/env bash\r\ncat marker\r\n')
                (folder / 'marker').write_text(name)
                agent.begin(name, 'source')
                at = index * 4 + 1
                ask = agent.decide('read current check', ['source'], active=True, round_no=at)
                agent.acknowledge(ask['token'], round_no=at)
                reply = json.dumps({'request_id': ask['token'], 'plan': {
                    'kind': 'run', 'command': './check', 'evidence_ids': ['source']}})
                agent.receive(ask['token'], 'prompt', reply, round_no=at + 1, verified=True)
                run = agent.proposal
                self.assertEqual(agent.commands, 0)
                self.assertTrue(agent.acknowledge(run['token'], round_no=at + 1))
                process = self.run_check(folder, run['payload'])
                receipt = '[exitCode:' + str(process.returncode) + ']\n' + process.stdout + process.stderr
                if previous_token:
                    self.assertFalse(agent.receive(previous_token, 'cmd', receipt, round_no=at + 2, verified=True))
                self.assertTrue(agent.receive(run['token'], 'cmd', receipt, round_no=at + 2, verified=True))
                self.assertEqual(json.loads(process.stdout)['stdout'], name)
                event = next(e for e in reversed(agent.history) if e['kind'] == 'received_cmd')
                self.assertIn(name, event['text'])
                self.assertEqual(agent.commands, 1)
                self.assertEqual(agent.answers, 0)
                previous_token = run['token']

    def test_nonzero_and_large_output_remain_failure_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'check').write_bytes(b'#!/usr/bin/env python3\r\nimport sys\r\nprint("x"*12000)\r\nprint("e"*3000,file=sys.stderr)\r\nsys.exit(7)\r\n')
            result = self.run_check(tmp)
            self.assertEqual(result.returncode, 7)
            data = json.loads(result.stdout)
            self.assertEqual(data['exit_code'], 7)
            self.assertEqual(len(data['stdout']), 10000)
            self.assertEqual(len(data['stderr']), 2000)
            self.assertTrue(data['truncated'])

    def test_missing_directory_unsupported_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp); script = folder / 'check'
            self.assertNotEqual(self.run_check(folder).returncode, 0)
            script.mkdir()
            self.assertNotEqual(self.run_check(folder).returncode, 0)
            script.rmdir()
            script.write_bytes(b'not a script\r\n')
            self.assertNotEqual(self.run_check(folder).returncode, 0)
            script.unlink()
            target = folder / 'other'; target.write_text('#!/bin/sh\ntouch executed\n')
            script.symlink_to(target)
            self.assertNotEqual(self.run_check(folder).returncode, 0)
            self.assertFalse((folder / 'executed').exists())
            real = folder / 'real'; real.mkdir()
            (real / 'check').write_text('#!/bin/sh\ntouch executed\n')
            alias = folder / 'alias'; alias.symlink_to(real, target_is_directory=True)
            command = task_tools.command('check', {'path': str(alias / 'check')})
            self.assertNotEqual(self.run_check(folder, command).returncode, 0)
            self.assertFalse((real / 'executed').exists())

    def test_bare_timeout_is_bounded_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'check').write_bytes(b'#!/bin/sh\r\nsleep 30\r\n')
            start = time.monotonic()
            result = self.run_check(tmp)
            self.assertEqual(result.returncode, 124)
            self.assertEqual(json.loads(result.stdout)['exit_code'], 124)
            self.assertLess(time.monotonic() - start, 9)

    def test_env_recovery_executes_only_check_not_mutating_prefix(self):
        import shlex
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            raw = b'#!/usr/bin/env bash\r\nprintf "fixture result\\n"\r\n'
            (folder / 'check').write_bytes(raw); (folder / 'check').chmod(0o755)
            original = 'cd ' + shlex.quote(tmp) + ' && printf x >> count && ./check'
            failed = subprocess.run(['bash', '-c', original], capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            # The fixture host may return 127; selection uses the documented
            # company's 126 receipt, execution uses the real original script.
            recovered = task_tools.failed_check_recovery(original, ENV_FAIL)
            self.assertIsNotNone(recovered)
            result = self.run_check(folder, recovered)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['stdout'], 'fixture result\n')
            self.assertEqual((folder / 'count').read_text(), 'x')
            self.assertEqual((folder / 'check').read_bytes(), raw)


if __name__ == '__main__':
    unittest.main()
