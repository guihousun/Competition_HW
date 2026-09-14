"""Independent regressions from Codex review: identity, evidence and HTTP isolation."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'Demo/CoreGeek/src'))
from agent import diagnostics as d, server


class DiagnosticReviewTests(unittest.TestCase):
    def observation(self):
        return {'roundNo': 80, 'teamOur': {'roles': [
            {'id': 1, 'roleType': 'worker', 'health': 220, 'pos': {'x': 0, 'y': 0}},
            {'id': 2, 'roleType': 'rocket', 'health': 1000, 'cooldown': 0,
             'pos': {'x': 35, 'y': 25}},
            {'id': 3, 'roleType': 'pioneer', 'health': 200, 'pos': {'x': 35, 'y': 24}},
        ]}}

    def test_no_invented_controller_pairing(self):
        summary = d.build_summary(self.observation(), {'roleCommandMap': {}})
        self.assertEqual(summary['controllers']['issued_attacks'], [])

    def test_pairing_comes_from_issued_action(self):
        response = {'roleCommandMap': {'2': {'action': 'attack', 'controllerId': '3'}}}
        summary = d.build_summary(self.observation(), response)
        self.assertEqual(summary['controllers']['issued_attacks'], [
            {'controller': 3, 'weapon': 2, 'kind': 'rocket', 'cooldown': 0, 'cooldown_state': 'ready'}])

    def test_unknown_controller_health_stays_unknown(self):
        observation = self.observation()
        del observation['teamOur']['roles'][0]['health']
        summary = d.build_summary(observation, {'roleCommandMap': {}})
        self.assertIsNone(summary['controllers']['controllers_live'])

    def test_nonfinite_base_health_keeps_summary_available(self):
        for health in (float('inf'), float('-inf'), float('nan')):
            observation = self.observation()
            observation['teamOur']['roles'].append({'roleType': 'station', 'health': health})
            summary = d.build_summary(observation, {'roleCommandMap': {}})
            self.assertIsNone(summary['base_hp'])
            self.assertEqual(summary['base_health_state'], 'unknown')
            json.dumps(summary, allow_nan=False)

    def test_manifest_cannot_omit_identity_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'CoreGeek'
            root.mkdir()
            (root / 'main3.py').write_text('pass\n')
            import hashlib
            entry = {'path': 'CoreGeek/main3.py', 'sha256': hashlib.sha256(b'pass\n').hexdigest()}
            for field in ('commit', 'source_digest'):
                manifest = {'schema': d.MANIFEST_SCHEMA, 'commit': 'a' * 40,
                            'source_digest': d._recompute_source_digest([entry]), 'files': [entry]}
                del manifest[field]
                (root / d.MANIFEST_NAME).write_text(json.dumps(manifest))
                self.assertNotEqual(d.load_manifest(root)['state'], 'verified')

    def test_unversioned_bundle_inside_git_checkout_has_unknown_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True, capture_output=True)
            (root / 'tracked.txt').write_text('fixture')
            subprocess.run(['git', '-C', str(root), 'add', 'tracked.txt'], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=fixture',
                            '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture'],
                           check=True, capture_output=True)
            bundle = root / 'unpacked/CoreGeek/Demo/CoreGeek'
            bundle.mkdir(parents=True)
            (bundle / 'main3.py').write_text('pass\n')
            self.assertIsNone(d._git_commit(bundle))

    def test_diagnostics_failure_cannot_change_http_body(self):
        expected = {'roleCommandMap': {'1': {'action': 'move', 'targetPos': [{'x': 1, 'y': 0}]}}}
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(server, 'respond', return_value=expected), \
                 patch.object(d, 'response_summary', side_effect=RuntimeError('sink failed')):
                request = Request(f'http://127.0.0.1:{http.server_port}/',
                                  data=json.dumps(self.observation()).encode(),
                                  headers={'Content-Type': 'application/json'})
                with urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.load(response), expected)
        finally:
            http.shutdown()
            http.server_close()
            thread.join(timeout=5)
