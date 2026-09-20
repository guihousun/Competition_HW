import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('battle_identity', Path(__file__).resolve().parents[1]/'tools/audit_battle_identity.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
SOURCE = '2296d9a16aa9a96df41225a785f0b313e12463f2'
DIGEST = '28fbbd4945c12e06f5bbbb45a63a95a5d78cdfd3fa7981de18be3fb6527371c1'


class IdentityTests(unittest.TestCase):
    def line(self, source=SOURCE, state='verified', digest=DIGEST):
        return '2026-09-20 10:00:00 | identity '+json.dumps({
            'code_commit': source, 'commit_source': 'manifest',
            'manifest': {'commit': source, 'state': state, 'source_digest': digest}})+'\n'

    def scan(self, text, encoding='utf-8'):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)/'teamA.log'
            p.write_text(text, encoding=encoding)
            return audit.scan(p, (SOURCE, DIGEST))

    def test_delivery_sha_absent_is_not_old(self):
        line = self.line()
        self.assertNotIn('9c6ed09', line)
        self.assertNotIn('baac9f4', line)
        self.assertEqual(self.scan(line)['classification'], 'EXPECTED_VERIFIED_SOURCE')

    def test_missing_is_unknown(self):
        self.assertEqual(self.scan('normal log\n')['classification'], 'UNKNOWN_NO_IDENTITY')

    def test_narrative_substring_is_not_identity(self):
        self.assertEqual(self.scan('note: expected '+SOURCE)['classification'], 'UNKNOWN_NO_IDENTITY')

    def test_other_source_requires_explicit_identity(self):
        self.assertEqual(self.scan(self.line('a'*40))['classification'], 'OTHER_REPORTED_SOURCE')

    def test_mixed_is_not_confirmed(self):
        self.assertEqual(self.scan(self.line()+self.line('a'*40))['classification'], 'MIXED_IDENTITIES')

    def test_unverified_or_digest_mismatch(self):
        for line in (self.line(state='mismatch'), self.line(digest='b'*64)):
            self.assertEqual(self.scan(line)['classification'], 'EXPECTED_SOURCE_UNVERIFIED')

    def test_truncated_identity_is_incomplete(self):
        self.assertEqual(self.scan(self.line()+'identity {broken\n')['classification'], 'UNKNOWN_INCOMPLETE')

    def test_utf16_log(self):
        self.assertEqual(self.scan(self.line(), 'utf-16')['classification'], 'EXPECTED_VERIFIED_SOURCE')

    def test_short_delivery_id_rejected(self):
        with self.assertRaises(ValueError):
            audit.expected_identity({'commit': '9c6ed09', 'source_digest': DIGEST})

    def test_read_failure_is_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            result = audit.scan(Path(td)/'absent.log', (SOURCE, DIGEST))
        self.assertEqual(result['classification'], 'UNKNOWN_INCOMPLETE')
        self.assertEqual(result['read_error'], 'FileNotFoundError')


if __name__ == '__main__':
    unittest.main()
