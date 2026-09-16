"""Deterministically schedule the post-write race; no timing-based oracle."""
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))
from agent.ordered_diagnostics import OrderedDiagnostics
from agent.task_journal import TaskJournal


class OrderedDiagnosticsTests(unittest.TestCase):
    def test_later_http_write_cannot_overtake_earlier_summary(self):
        sink = OrderedDiagnostics()
        order = []
        first = sink.reserve(lambda: order.append(1))
        second = sink.reserve(lambda: order.append(2))
        sink.complete(second)
        self.assertEqual(order, [])  # consumer cannot pass the first gate
        sink.complete(first)
        self.assertTrue(sink.drain())
        self.assertEqual(order, [1, 2])

    def test_slow_logger_does_not_block_response_completion_or_fill_without_bound(self):
        sink = OrderedDiagnostics(capacity=1)
        entered, release = threading.Event(), threading.Event()
        def block():
            entered.set(); release.wait(2)
        first = sink.reserve(block); sink.complete(first)
        self.assertTrue(entered.wait(1))
        try:
            second = sink.reserve(lambda: None)
            self.assertIsNotNone(second)
            sink.complete(second)
            self.assertIsNone(sink.reserve(lambda: None))
            self.assertEqual(sink.dropped, 1)
        finally:
            release.set()
        self.assertTrue(sink.drain())

    def test_callback_failure_does_not_lose_later_summaries(self):
        sink = OrderedDiagnostics(); seen = []
        first = sink.reserve(lambda: 1 / 0)
        second = sink.reserve(lambda: seen.append(2))
        sink.complete(first); sink.complete(second)
        self.assertTrue(sink.drain())
        self.assertEqual(seen, [2])

    def test_task_check_association_survives_delayed_earlier_post_write_work(self):
        sink = OrderedDiagnostics(); journal = TaskJournal(); rows = []
        def event(n, output='', command=''):
            return lambda: rows.extend(journal.observe(
                {'roundNo': n, 'phaseTask': 'current task', 'teamOur': {'type': 'challenger', 'roles': []},
                 'lastCmdResult': output}, {'roleCommandMap': {}, 'executeCmd': command}, stream='test'))
        a = sink.reserve(event(1, command='cd /tmp/work && ./check'))
        b = sink.reserve(event(2, output='[exitCode:0]\n[ OK ] 全部通过 (6/6)\nTOKEN: fixture'))
        sink.complete(b); sink.complete(a)
        self.assertTrue(sink.drain())
        self.assertTrue(any(r['kind'] == 'task_check_result' for r in rows))


if __name__ == '__main__':
    unittest.main()
