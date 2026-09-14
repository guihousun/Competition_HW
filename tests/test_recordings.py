"""Local recording transport tests: cancellation, isolation and faithful frames."""
import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'Demo/CoreGeek/src'))
from agent.recordings import RecordingJobs
from agent.debug import series_payload, scenario_payload, step_payload
from agent.server import Handler


def wait_done(jobs, job_id):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        snap = jobs.snapshot(job_id)
        if snap['state'] not in ('running', 'stopping'):
            return snap
        time.sleep(.01)
    raise AssertionError('recording did not finish')


class RecordingTests(unittest.TestCase):
    def test_cancel_keeps_only_completed_frames_and_never_starts_second_job(self):
        entered, release = threading.Event(), threading.Event()
        def producer(*args, progress, cancelled):
            entered.set()
            release.wait(5)
            progress(1, 4)
            self.assertTrue(cancelled())
            return {'frames': [{'round': 1}], 'states': [{}, {}], 'done': False}
        jobs = RecordingJobs(producer)
        first = jobs.start(limit=4)
        try:
            self.assertTrue(entered.wait(3))
            with self.assertRaises(RuntimeError):
                jobs.start(limit=4)
            self.assertEqual('stopping', jobs.cancel(first['id'])['state'])
            with self.assertRaises(RuntimeError):
                jobs.result(first['id'])
        finally:
            release.set()
        final = wait_done(jobs, first['id'])
        self.assertEqual('cancelled', final['state'])
        self.assertEqual(1, final['round'])
        self.assertEqual(.25, final['progress'])
        self.assertFalse(jobs.result(first['id'])['done'])
        elapsed = final['elapsed']
        time.sleep(.03)
        self.assertEqual(elapsed, jobs.snapshot(first['id'])['elapsed'])

    def test_failed_job_reports_error_and_allows_retry(self):
        def fail(*args, **kwargs):
            raise ValueError('fixture failure')
        jobs = RecordingJobs(fail)
        first = jobs.start()
        self.assertEqual('failed', wait_done(jobs, first['id'])['state'])
        with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
            jobs.result(first['id'])
        self.assertNotEqual(first['id'], jobs.start()['id'])

    def test_bounded_retention_and_unknown_ids_cannot_cancel_new_job(self):
        jobs = RecordingJobs(lambda *a, **k: {'frames': [], 'states': [{}], 'done': False})
        ids = []
        for _ in range(4):
            job = jobs.start(); ids.append(job['id']); wait_done(jobs, job['id'])
        self.assertEqual(3, len(jobs.jobs))
        with self.assertRaises(KeyError):
            jobs.cancel(ids[0])
        self.assertEqual('done', jobs.snapshot(ids[-1])['state'])

    def test_invalid_parameters_do_not_create_jobs(self):
        jobs = RecordingJobs()
        for params in ({'side': 'other'}, {'limit': -1}, {'limit': 1301}, {'pressure': 4}, {'seed': 'oops'}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                jobs.start(**params)
        self.assertEqual({}, jobs.jobs)

    def test_background_frames_equal_serialized_live_steps(self):
        # Both sides; independently drive the public JSON step transport.
        for side in ('challenger', 'defender'):
            jobs = RecordingJobs()
            job = jobs.start(7, side, 2, 5)
            self.assertEqual('done', wait_done(jobs, job['id'])['state'])
            recording = jobs.result(job['id'])
            state = scenario_payload(7, side, 2)['state']
            for index in range(5):
                result = step_payload(json.loads(json.dumps(state)))
                state = result['state']
                self.assertEqual(state['teamOur'], recording['states'][index + 1]['teamOur'])
                self.assertEqual(result['executed'], recording['frames'][index]['executed'])
            self.assertEqual(64, len(recording['metadata']['sourceSha256']))
            self.assertEqual(6, len(recording['states']))
            self.assertFalse(recording['done'])  # a recording limit is NOT a game terminal

    def test_immediate_cancel_is_a_valid_zero_frame_recording(self):
        recording = series_payload(1, 'challenger', 1, 10, cancelled=lambda: True)
        self.assertEqual([], recording['frames'])
        self.assertEqual(1, len(recording['states']))
        self.assertFalse(recording['done'])

    def test_http_start_status_result_and_not_found(self):
        jobs = RecordingJobs()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(path, body=None):
            data = None if body is None else json.dumps(body).encode()
            req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}{path}', data=data,
                                         headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.load(response)
        try:
            with patch('agent.recordings.JOBS', jobs):
                status, job = request('/debug/recording/start', {'limit': 2})
                self.assertEqual(202, status)
                wait_done(jobs, job['id'])
                self.assertEqual('done', request('/debug/recording?id=' + job['id'])[1]['state'])
                self.assertEqual(2, len(request('/debug/recording/result?id=' + job['id'])[1]['frames']))
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request('/debug/recording?id=missing')
                self.assertEqual(404, error.exception.code)
        finally:
            server.shutdown(); server.server_close(); thread.join(3)


if __name__ == '__main__':
    unittest.main()
