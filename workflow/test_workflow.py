"""Focused regression tests for workflow/state.py.

Run from the repository root:

    python -m unittest discover -s workflow -p test_workflow.py -v

Every test writes its runtime state under tempfile.TemporaryDirectory; the real
.workflow/ directory, the network and the GitHub API are never touched.
Expected values below are derived from the documented contract in
workflow/README.md (owner-only input, marker replies do not open revisions,
partial snapshots must not close work, one owner per lock), not from replaying
the implementation against itself.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import state  # noqa: E402  (resolved from the workflow/ directory above)

REPOSITORY = 'guihousun/Competition_HW'
OWNER = 'guihousun'
OTHER = 'someone-else'
# Automated replies start with this marker; posting one must not open a revision.
MARKER = '<!-- competition-workflow:1:1:implementing -->'


def comment(number, author=OWNER, body='owner comment'):
    return {'id': number, 'author': author, 'body': body,
            'updated_at': '2026-09-09T00:00:00Z'}


def issue(number, author=OWNER, title='Issue title', body='Issue body',
          state_name='open', comments=()):
    return {'number': number, 'author': author, 'title': title, 'body': body,
            'state': state_name, 'comments': list(comments)}


def snapshot(issues, repository=REPOSITORY):
    return {'repository': repository, 'issues': list(issues)}


class StateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='state-test-')
        self.addCleanup(self._tmp.cleanup)
        self.state_path = Path(self._tmp.name) / 'state.json'

    def ingest(self, issues, repository=REPOSITORY):
        return state.ingest(self.state_path, snapshot(issues, repository))

    def read_state(self):
        return json.loads(self.state_path.read_text(encoding='utf-8'))

    def write_state(self, data):
        self.state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                   encoding='utf-8')

    def stored(self, number):
        return self.read_state()['issues'][str(number)]


class IngestTest(StateTestCase):
    def test_conflicting_duplicate_does_not_persist_partial_revision(self):
        self.ingest([issue(1, body='saved')])
        before = self.state_path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'Conflicting duplicate'):
            self.ingest([issue(1, body='new'), issue(1, body='old')])
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_identical_duplicates_enqueue_once(self):
        self.assertEqual(self.ingest([issue(1), issue(1)]), [1])

    def test_first_owner_issue_enqueues_and_initialises_row(self):
        changed = self.ingest([issue(1, comments=[comment(10)])])
        self.assertEqual(changed, [1])
        row = self.stored(1)
        self.assertTrue(row['needs_triage'])
        self.assertEqual(row['stage'], 'new')
        self.assertEqual(row['source']['title'], 'Issue title')
        self.assertEqual(row['source']['state'], 'open')
        self.assertEqual([c['id'] for c in row['source']['comments']], [10])
        self.assertTrue(row['input_digest'])

    def test_identical_snapshot_does_not_enqueue_again(self):
        self.assertEqual(self.ingest([issue(1, comments=[comment(10), comment(11)])]), [1])
        digest = self.stored(1)['input_digest']
        before = self.state_path.read_text(encoding='utf-8')
        # Same content, comments and issues presented in a different order.
        again = self.ingest([issue(1, comments=[comment(11), comment(10)])])
        self.assertEqual(again, [])
        self.assertEqual(self.stored(1)['input_digest'], digest)
        self.assertEqual(self.state_path.read_text(encoding='utf-8'), before)

    def test_new_owner_comment_enqueues_new_revision(self):
        self.assertEqual(self.ingest([issue(1, comments=[comment(10)])]), [1])
        digest = self.stored(1)['input_digest']
        changed = self.ingest([issue(1, comments=[comment(10),
                                                  comment(11, body='INTRANET: PASS')])])
        self.assertEqual(changed, [1])
        row = self.stored(1)
        self.assertNotEqual(row['input_digest'], digest)
        self.assertEqual([c['id'] for c in row['source']['comments']], [10, 11])
        self.assertTrue(row['needs_triage'])

    def test_workflow_marker_comments_do_not_enqueue(self):
        self.assertEqual(self.ingest([issue(1, comments=[comment(10)])]), [1])
        digest = self.stored(1)['input_digest']
        data = self.read_state()
        data['issues']['1']['needs_triage'] = False  # first revision already triaged
        self.write_state(data)
        changed = self.ingest([issue(1, comments=[comment(10),
                                                  comment(11, body=MARKER)])])
        self.assertEqual(changed, [])
        self.assertEqual(self.stored(1)['input_digest'], digest)
        self.assertFalse(self.stored(1)['needs_triage'])
        # A real owner comment next to a marker comment still opens a revision,
        # and the marker itself never enters the source comments.
        changed = self.ingest([issue(1, comments=[comment(10), comment(11, body=MARKER),
                                                  comment(12, body='INTRANET: FAIL')])])
        self.assertEqual(changed, [1])
        self.assertEqual([c['id'] for c in self.stored(1)['source']['comments']], [10, 12])

    def test_non_owner_issues_and_comments_are_ignored(self):
        self.assertEqual(self.ingest([issue(1, author=OTHER)]), [])
        self.assertEqual(self.read_state(), {'issues': {}})
        self.assertEqual(self.ingest([issue(1, comments=[comment(10)])]), [1])
        digest = self.stored(1)['input_digest']
        changed = self.ingest([issue(1, comments=[comment(10),
                                                  comment(20, author=OTHER,
                                                          body='INTRANET: PASS')])])
        self.assertEqual(changed, [])
        self.assertEqual(self.stored(1)['input_digest'], digest)
        self.assertEqual([c['id'] for c in self.stored(1)['source']['comments']], [10])

    def test_wrong_repository_raises_before_writing_state(self):
        with self.assertRaisesRegex(ValueError, 'Wrong repository'):
            self.ingest([issue(1)], repository='other/repo')
        self.assertFalse(self.state_path.exists())
        self.assertEqual(self.ingest([issue(1)]), [1])
        before = self.state_path.read_text(encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Wrong repository'):
            self.ingest([issue(1)], repository='guihousun/Competition')
        self.assertEqual(self.state_path.read_text(encoding='utf-8'), before)

    def test_partial_snapshot_retains_known_issues(self):
        self.assertEqual(self.ingest([issue(1), issue(2)]), [1, 2])
        data = self.read_state()
        data['issues']['2'].update(stage='implementing', needs_triage=False)
        self.write_state(data)
        digest_two = self.stored(2)['input_digest']
        # A truncated read returning only issue 1 must not drop issue 2.
        self.assertEqual(self.ingest([issue(1)]), [])
        self.assertIn('2', self.read_state()['issues'])
        self.assertEqual(self.stored(2)['input_digest'], digest_two)
        self.assertEqual(self.stored(2)['stage'], 'implementing')

    def test_new_revision_preserves_implementing_stage(self):
        self.assertEqual(self.ingest([issue(1, body='first')]), [1])
        data = self.read_state()
        data['issues']['1'].update(stage='implementing', needs_triage=False)
        self.write_state(data)
        digest = self.stored(1)['input_digest']
        self.assertEqual(self.ingest([issue(1, body='second')]), [1])
        row = self.stored(1)
        self.assertEqual(row['stage'], 'implementing')
        self.assertNotEqual(row['input_digest'], digest)
        self.assertTrue(row['needs_triage'])
        self.assertEqual(row['source']['body'], 'second')
        # An unrelated issue in the same snapshot still starts at 'new'.
        self.assertEqual(self.ingest([issue(1, body='second'), issue(2)]), [2])
        self.assertEqual(self.stored(2)['stage'], 'new')

    def test_lock_refuses_concurrent_owner_and_keeps_lock_file(self):
        lock_path = Path(str(self.state_path) + '.lock')
        with state.lock(self.state_path):
            self.assertTrue(lock_path.exists())
            self.assertTrue(lock_path.read_text(encoding='utf-8').isdigit())
            with self.assertRaises(FileExistsError):
                with state.lock(self.state_path):
                    self.fail('second lock acquisition must not succeed')
            # The refused acquisition must not delete the holder's lock file ...
            self.assertTrue(lock_path.exists())
            # ... nor let a concurrent ingest overwrite state.
            with self.assertRaises(FileExistsError):
                self.ingest([issue(1)])
            self.assertFalse(self.state_path.exists())
        self.assertFalse(lock_path.exists())
        # The next owner can take the released lock and ingest normally.
        self.assertEqual(self.ingest([issue(1)]), [1])


if __name__ == '__main__':
    unittest.main()
