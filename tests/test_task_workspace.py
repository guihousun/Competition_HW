"""Independent filesystem fixtures for the generated read-only probe."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))
from agent import task_workspace as workspace
from agent.task_context import COMMAND_LIMIT
from agent.local_task_sandbox import execute


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def probe(self, ref):
        proc = subprocess.run([sys.executable, '-c', workspace.PROBE_SOURCE,
            json.dumps({'ref': ref, 'roots': [str(self.root)]})],
            cwd=self.root, capture_output=True, text=True, encoding='utf-8', timeout=6)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_nested_task_and_sibling_document_in_one_operation(self):
        path = self.write('random/work/task_other.md', 'Use current API docs; JSON answer.')
        self.write('random/work/API_DOCS.md', 'Changed endpoint /v9/catalog and fields.')
        result = self.probe('task_other.md')
        self.assertEqual(result['status'], 'found')
        self.assertEqual([x['path'] for x in result['files']], [str(path), str(path.parent/'API_DOCS.md')])
        self.assertEqual(result['files'][1]['text'], 'Changed endpoint /v9/catalog and fields.')

    def test_duplicate_names_do_not_read_an_arbitrary_task(self):
        self.write('a/task.md', 'a')
        self.write('b/task.md', 'b')
        result = self.probe('task.md')
        self.assertEqual(result['status'], 'ambiguous')
        self.assertEqual(result['files'], [])
        self.assertEqual(len(result['candidates']), 2)

    def test_literal_spaces_quotes_unicode_and_shell_metacharacters(self):
        name = "task '中文; echo injected.md"
        self.write(name, 'literal data')
        result = self.probe(name)
        self.assertEqual(result['files'][0]['text'], 'literal data')
        self.assertFalse((self.root/'injected').exists())
        self.assertIsNotNone(workspace.command_for(name))

    def test_missing_is_explicit_and_large_source_is_marked(self):
        self.assertEqual(self.probe('absent.md')['status'], 'not_found')
        self.write('large.md', 'a' * 5000)
        file = self.probe('large.md')['files'][0]
        self.assertTrue(file['truncated'])
        self.assertEqual(len(file['text']), 4500)

    def test_explicit_absolute_file_beats_same_name_elsewhere(self):
        path = self.write('a/task.md', 'selected')
        self.write('b/task.md', 'other')
        self.assertEqual(self.probe(str(path))['files'][0]['text'], 'selected')

    def test_symbolic_file_is_not_read(self):
        target = self.write('private.bin', 'do not follow')
        try:
            (self.root/'task.md').symlink_to(target)
        except OSError:
            self.skipTest('OS does not allow creating symlinks')
        self.assertEqual(self.probe('task.md')['status'], 'not_found')

    def test_reference_is_data_and_multiple_references_fall_back(self):
        self.assertEqual(workspace.file_reference('请阅读task_1_alpha.md，获取任务信息'), 'task_1_alpha.md')
        self.assertEqual(workspace.file_reference('Read "任务 文件.md"'), '任务 文件.md')
        self.assertIsNone(workspace.file_reference('read a.md and b.md'))
        self.assertIsNone(workspace.file_reference('read "../a.md"'))
        command = workspace.bootstrap('请阅读task_1_alpha.md，获取任务信息')
        self.assertLessEqual(len(command), COMMAND_LIMIT)

    def test_unnamed_task_uses_bounded_automatic_probe(self):
        self.write('job/task_current.md', '读取同目录 API_DOCS.md')
        self.write('job/API_DOCS.md', 'python3 /svc/query.py --city 城市')
        command = workspace.bootstrap('请根据当前任务说明完成 API 查询并提交 JSON')
        self.assertIsNotNone(command)
        result = json.loads(execute(command, {
            'cwd': '/', 'files': {
                '/workspace/job/task_current.md': '读取同目录 API_DOCS.md',
                '/workspace/job/API_DOCS.md': 'python3 /svc/query.py --city 城市'}}, active=True).split('\n', 1)[1])
        self.assertEqual(result['status'], 'found')
        self.assertEqual(result['files'][1]['path'], '/workspace/job/API_DOCS.md')

    def test_virtual_fixture_parity_and_no_arbitrary_execution(self):
        command = workspace.bootstrap('请阅读task_new.md，获取任务信息')
        fixture = {'cwd': '/', 'files': {
            '/tmp/alternate/deep/task_new.md': 'read API_DOCS.md',
            '/tmp/alternate/deep/API_DOCS.md': 'Use the current service'}}
        result = json.loads(execute(command, fixture, active=True).split('\n', 1)[1])
        self.assertEqual(result['status'], 'found')
        self.assertEqual(len(result['files']), 2)
        self.assertIn('[exitCode:2]', execute(command+'; touch INJECTED', fixture, active=True))
        self.assertIn('[JUDGER_ERROR]', execute(command, fixture, active=False))


if __name__ == '__main__':
    unittest.main()
