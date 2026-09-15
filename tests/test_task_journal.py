"""Independent wire sequence expectations for Issue #18 task diagnostics."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'Demo/CoreGeek/src'))
sys.path.insert(0, str(ROOT / 'tools'))
from agent import diagnostics, server, task_journal, telemetry
from agent.task_journal import TaskJournal, excerpt, MAX_STREAMS
import trace_tool


def request(round_no, **fields):
    return dict(roundNo=round_no, teamOur={'type': 'defender', 'roles': []}, **fields)


class TaskJournalTests(unittest.TestCase):
    def test_question_model_tool_answer_and_end_are_visible_without_inferred_success(self):
        journal = TaskJournal()
        sequence = [
            (request(1), {'roleCommandMap': {'42': {'action': 'acceptTask'}}}),
            (request(2, phaseTask='计算 7 + 8'), {'roleCommandMap': {}, 'prompt': '读取题目后规划'}),
            (request(3, phaseTask='计算 7 + 8', llmResp='计算并核对'), {'executeCmd': 'python -c "print(7+8)"'}),
            (request(4, phaseTask='计算 7 + 8', lastCmdResult='exitCode:0\n15'), {'prompt': '核对答案格式'}),
            (request(5, phaseTask='计算 7 + 8', llmResp='15'), {'roleCommandMap': {'42': {'action': 'submitAnswer', 'answer': '15'}}}),
            (request(6, phaseTask='', errors=[{'errorCode': 2, 'errorMsg': '部分正确'}]), {'roleCommandMap': {}}),
        ]
        frozen = deepcopy(sequence)
        events = []
        for req, response in sequence:
            events += journal.observe(req, response, event_id=f'run:{req["roundNo"]}', stream='red')
        kinds = [e['kind'] for e in events]
        for kind in ('issued_acceptTask', 'task_text_observed', 'issued_prompt', 'observed_llmResp',
                     'issued_executeCmd', 'observed_lastCmdResult', 'issued_submitAnswer',
                     'task_text_ended', 'observed_errors'):
            self.assertIn(kind, kinds)
        self.assertEqual(sum(e['kind'] == 'task_text_observed' for e in events), 1)
        end = next(e for e in events if e['kind'] == 'task_text_ended')
        self.assertIn('unknown_without_judge_feedback', end['content']['text'])
        self.assertTrue(all(e['team'] == 'defender' for e in events))
        self.assertEqual(sequence, frozen)

    def test_repeated_results_are_suppressed_but_new_identical_requests_are_not(self):
        journal = TaskJournal()
        first = journal.observe(request(1, llmResp='same'), {'prompt': 'again'})
        second = journal.observe(request(2, llmResp='same'), {'prompt': 'again'})
        self.assertEqual([e['kind'] for e in first], ['observed_llmResp', 'issued_prompt'])
        self.assertEqual([e['kind'] for e in second], ['issued_prompt'])
        self.assertEqual(journal.observe(request(2, llmResp='same'), {'prompt': 'again'}), [])
        journal.observe(request(3, llmResp=''), {})
        self.assertEqual(journal.observe(request(4, llmResp='same'), {})[0]['kind'], 'observed_llmResp')

    def test_gap_reset_and_teams_do_not_share_context(self):
        journal = TaskJournal()
        journal.observe(request(2, phaseTask='old'), {}, stream='red')
        blue = journal.observe(request(1, phaseTask='new'), {}, stream='blue')
        self.assertEqual(blue[0]['continuity'], 'first_observation')
        gap = journal.observe(request(5, llmResp='late'), {}, stream='red')
        self.assertTrue(all(e['continuity'] == 'gap' for e in gap))
        reset = journal.observe(request(1, phaseTask='restart'), {}, stream='red')
        self.assertEqual([e['kind'] for e in reset], ['task_text_observed'])
        self.assertEqual(reset[0]['continuity'], 'reset')

    def test_bounded_streams_and_long_content_redacted_before_truncation(self):
        journal = TaskJournal()
        for i in range(20):
            journal.observe(request(1, phaseTask='x'), {}, stream=str(i))
        self.assertLessEqual(len(journal.streams), MAX_STREAMS)
        raw = 'sk-' + 'a' * 30
        result = excerpt(raw + ' ' + 'x' * 2000 + ' IMPORTANT_END')
        self.assertNotIn(raw, result['text'])
        self.assertTrue(result['truncated'])
        self.assertTrue(result['text'].endswith('IMPORTANT_END'))
        self.assertLess(len(result['text']), 1300)
        self.assertEqual(excerpt({'authorization': raw})['text'], '{"authorization": "[REDACTED]"}')

    def test_agent_stop_reason_and_counts_change_without_repeating_idle(self):
        journal = TaskJournal()
        agent = {'generation': 'g1', 'stage': 'stopped', 'stopReason': 'timeout', 'commands': 2}
        first = journal.observe(request(1), {}, decision={'agent': agent})
        self.assertIn('timeout', first[0]['content']['text'])
        self.assertEqual(journal.observe(request(2), {}, decision={'agent': agent}), [])

    def test_console_without_trace_and_off_or_broken_sink_are_safe(self):
        with patch.object(task_journal, '_journal', TaskJournal()), patch.dict('os.environ', {'COMPETITION_HW_CONSOLE': 'compact'}):
            lines = []
            diagnostics.response_summary(request(1, phaseTask='question'), {'prompt': 'prompt'}, event_id=None)
            task_journal.emit(request(2), {'executeCmd': 'echo safe'}, emitter=lines.append)
            self.assertTrue(any('issued_executeCmd' in line for line in lines))
            with patch.dict('os.environ', {'COMPETITION_HW_CONSOLE': 'off'}):
                self.assertEqual(task_journal.emit(request(3), {'prompt': 'hidden'}, emitter=lines.append), [])
            self.assertEqual(task_journal.emit(request(4), {'prompt': 'x'}, emitter=lambda _: (_ for _ in ()).throw(OSError())), [])

    def test_offline_export_keeps_full_task_text_and_trace_quality_without_maps(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            rec = telemetry.Recorder(folder / 'trace', {'code_commit': 'a' * 40})
            try:
                req = request(1, phaseTask='long' * 500, mapInfo={'private_marker': 'NEVER_EXPORT_MAP'})
                rec.submit(rec.begin(), json.dumps(req).encode(), json.dumps({'prompt': 'p'}).encode(), sent=True)
                rec.submit(rec.begin(), json.dumps(request(2, llmResp='answer')).encode(), json.dumps({'executeCmd': 'UNSENT'}).encode(), sent=False)
            finally:
                self.assertTrue(rec.close())
            path = folder / 'tasks.jsonl'
            report = trace_tool.tasks(rec.directory, path)
            data = path.read_text(encoding='utf-8')
            self.assertNotIn('NEVER_EXPORT_MAP', data)
            self.assertNotIn('UNSENT', data)
            self.assertIn('long' * 500, data)
            self.assertEqual(report['turns_read'], 2)
            self.assertTrue(report['task_events'] > 0)
            self.assertIn('unsent_responses', data)
            with self.assertRaises(FileExistsError):
                trace_tool.tasks(rec.directory, path)

    def test_actual_http_bytes_unchanged_when_trace_fails(self):
        from http.server import ThreadingHTTPServer
        answer = {'roleCommandMap': {}, 'executeCmd': 'echo test'}
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        lines = []
        try:
            with patch.object(server, 'respond', return_value=answer), patch.object(server, 'decision_report', return_value=None), \
                 patch.object(telemetry, 'begin', side_effect=OSError()), patch.object(telemetry, 'submit', side_effect=OSError()), \
                 patch.object(diagnostics, '_emit', side_effect=lines.append), patch.object(task_journal, '_journal', TaskJournal()), \
                 patch.dict('os.environ', {'COMPETITION_HW_CONSOLE': 'compact'}):
                data = json.dumps(request(1, phaseTask='test question')).encode()
                req = Request(f'http://127.0.0.1:{httpd.server_port}/', data=data, headers={'Content-Type': 'application/json'})
                with urlopen(req, timeout=5) as res:
                    self.assertEqual(json.loads(res.read()), answer)
                # The hook runs after response send, so synchronize with its end.
                for _ in range(100):
                    if any('issued_executeCmd' in line for line in lines):
                        break
                    threading.Event().wait(.01)
                self.assertTrue(any('task_event ' in line and 'test question' in line for line in lines))
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
