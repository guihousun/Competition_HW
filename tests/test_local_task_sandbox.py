"""Virtual environment checks; no model, real shell, network or hidden answers."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))
from agent.local_task_sandbox import execute
from agent.local_llm import after_step, before_step
from agent.sandbox import parse_command_result
from agent.scenarios import observation


def fixture():
    return {'cwd': '/lab', 'files': {'guide.txt': 'Run python3 /api/weather.py --city Beijing'},
            'programs': {'/api/weather.py': {
                'filters': ['city'], 'required_filters': ['city'],
                'records': [{'city': 'Beijing', 'temperature': 23}, {'city': 'Shanghai', 'temperature': 29}],
                'output_fields': ['city', 'temperature']},
                '/api/totals.py': {'operation': 'sum', 'filters': ['zone'], 'value_field': 'amount',
                                  'records': [{'zone': 'A', 'amount': '0.1'}, {'zone': 'A', 'amount': '0.2'},
                                              {'zone': 'B', 'amount': '100'}]}}}


class VirtualSandboxTests(unittest.TestCase):
    def run_cmd(self, cmd, env=None):
        return parse_command_result(execute(cmd, fixture() if env is None else env, active=True))

    def test_discovery_read_query_and_changed_parameters(self):
        self.assertEqual(self.run_cmd('pwd').output, '/lab')
        self.assertEqual(self.run_cmd('ls -la /').output, 'api\nlab')
        self.assertEqual(self.run_cmd('ls').output, 'guide.txt')
        self.assertIn('/api/weather.py', self.run_cmd('cat guide.txt').output)
        for city, expected in [('Beijing', 23), ('Shanghai', 29)]:
            result = self.run_cmd('python3 /api/weather.py --city ' + city)
            self.assertTrue(result.ok)
            self.assertEqual(json.loads(result.output), [{'city': city, 'temperature': expected}])

    def test_exact_decimal_sum_does_not_include_other_records(self):
        self.assertEqual(json.loads(self.run_cmd('python3 /api/totals.py --zone A').output), {'total': '0.3'})

    def test_changed_path_is_discovered_from_new_document(self):
        env = fixture()
        env['programs']['/v2/query.py'] = env['programs'].pop('/api/weather.py')
        env['files']['guide.txt'] = 'Run python3 /v2/query.py --city Shanghai'
        self.assertFalse(self.run_cmd('python3 /api/weather.py --city Shanghai', env).ok)
        self.assertTrue(self.run_cmd('python3 /v2/query.py --city Shanghai', env).ok)

    def test_directory_read_is_not_reported_as_a_missing_file(self):
        result = self.run_cmd('cat /api')
        self.assertFalse(result.ok)
        self.assertIn('Is a directory', result.output)
        self.assertNotIn('No such', result.output)
        self.assertIn('weather.py', self.run_cmd('ls /api').output)
        result = self.run_cmd('cat /api/weather.py')
        self.assertFalse(result.ok)
        self.assertIn('documented interface', result.output)

    def test_empty_declared_directory_and_cwd_exist(self):
        env = {'cwd': '/workspace', 'files': {'/brief/README': 'instructions'}, 'directories': ['/empty']}
        for path in ('/workspace', '/empty'):
            result = self.run_cmd('ls ' + path, env)
            self.assertTrue(result.ok)
            self.assertEqual(result.output, '')
        self.assertFalse(self.run_cmd('ls /missing', env).ok)

    def test_no_host_commands_file_reads_or_partial_shell_execution(self):
        with patch('subprocess.run', side_effect=AssertionError('host execution')), \
             patch('builtins.open', side_effect=AssertionError('host file read')):
            for cmd in ('cat /etc/passwd', 'ls; whoami', 'pwd && ls', 'python3 -c "print(42)"',
                        'curl https://example.com', 'cat $(whoami)', 'cat ../secret', 'pwd > out'):
                with self.subTest(cmd=cmd):
                    self.assertFalse(self.run_cmd(cmd).ok)
            self.assertTrue(self.run_cmd('cat guide.txt').ok)

    def test_bad_queries_have_nonzero_exit_and_can_be_corrected(self):
        for cmd in ('python3 /api/weather.py', 'python3 /api/weather.py --city Nope',
                    'python3 /api/weather.py --unknown Beijing',
                    'python3 /api/weather.py --city Beijing --city Shanghai'):
            self.assertFalse(self.run_cmd(cmd).ok)
        self.assertTrue(self.run_cmd('python3 /api/weather.py --city Beijing').ok)

    def test_faults_and_multibyte_truncation(self):
        env = fixture()
        env['files']['large'] = '金' * 30000
        env['faults'] = {'pwd': 'TIMEOUT', 'ls': 'JUDGER_ERROR', 'cat guide.txt': 'TRUNCATED'}
        self.assertEqual(self.run_cmd('pwd', env).status, 'timeout')
        self.assertEqual(self.run_cmd('ls', env).status, 'judger_error')
        self.assertTrue(self.run_cmd('cat guide.txt', env).truncated)
        result = self.run_cmd('cat large', env)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.output.encode()), 65536)
        self.assertNotIn('\ufffd', result.output)

    def test_task_permission_and_input_bounds(self):
        self.assertEqual(parse_command_result(execute('pwd', fixture(), active=False)).status, 'judger_error')
        self.assertFalse(self.run_cmd('x' * 9000).ok)
        self.assertFalse(self.run_cmd('cat "').ok)

    def test_adapter_delivers_official_shaped_result_and_keeps_private_fixture_private(self):
        for side in ('challenger', 'defender'):
            state = {'teamOur': {'type': side, 'roles': []}, 'phaseTask': 'Read guide',
                     '_demo': {'task_world': {'points': {side + 'TaskPoint1': {
                         'active': {'sandbox_fixture': fixture()}}}}}}
            result = {'state': state, 'judgeRequest': {'executeCmd': 'cat guide.txt'}}
            updated = after_step(result)
            visible_next, pending = before_step(updated['state'])
            self.assertFalse(pending)
            self.assertTrue(parse_command_result(visible_next['lastCmdResult']).ok)
            public = observation(visible_next)
            self.assertNotIn('_demo', public)
            self.assertNotIn('temperature', json.dumps(public))
            self.assertIn('lastCmdResult', public)


if __name__ == '__main__':
    unittest.main()
