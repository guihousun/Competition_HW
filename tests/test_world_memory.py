"""World source retention, actual prompt exposure and date uncertainty."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))
from agent.world_memory import WorldMemory


class WorldMemoryTests(unittest.TestCase):
    def test_middle_negation_is_retained_but_not_claimed_read_from_prefix(self):
        memory = WorldMemory('treasure')
        text = '噪声。' * 1800 + '更正：不是三个，必须献祭两个星砂。' + '其他。' * 1800
        identity = memory.observe(text, 1)
        memory.expose(identity, 0, 3000)
        self.assertFalse(memory.reviewed(identity))
        self.assertFalse(memory.quote_visible(identity, '必须献祭两个星砂'))
        result = memory.inspect(identity, query='更正', length=300)
        self.assertIn('不是三个', result['text'])
        self.assertFalse(memory.quote_visible(identity, '必须献祭两个星砂'), 'inspection is not actual prompt emission')
        memory.expose(identity, result['offset'], result['end'])
        self.assertTrue(memory.quote_visible(identity, '必须献祭两个星砂'))
        self.assertFalse(memory.reviewed(identity), 'unread tail is still unknown')

    def test_review_requires_gap_free_full_coverage(self):
        memory = WorldMemory('news'); identity = memory.observe('0123456789', 1)
        memory.expose(identity, 0, 3); memory.expose(identity, 5, 10)
        self.assertEqual(3, memory.index()[0]['next_unread'])
        self.assertFalse(memory.reviewed(identity))
        memory.expose(identity, 3, 5)
        self.assertTrue(memory.reviewed(identity))
        self.assertEqual([[0, 10]], memory.visible[identity])

    def test_late_first_observation_does_not_invent_publication_day(self):
        memory = WorldMemory('news'); identity = memory.observe('明天停矿两天。', 145)
        self.assertIsNone(memory.origins[identity]['anchor_day'])
        memory.observe('明天停矿两天。', 261)
        self.assertEqual(145, memory.origins[identity]['first_round'])
        self.assertIsNone(memory.origins[identity]['anchor_day'])

    def test_witnessed_daily_change_and_first_game_day_have_explicit_bases(self):
        memory = WorldMemory('news'); first = memory.observe('旧公告', 130)
        second = memory.observe('新公告', 131)
        self.assertEqual(1, memory.origins[first]['anchor_day'])
        self.assertEqual('observed_day_boundary', memory.origins[second]['date_basis'])
        self.assertEqual(2, memory.origins[second]['anchor_day'])
        skipped = WorldMemory('news'); skipped.observe('旧公告', 129)
        second = skipped.observe('新公告', 131)
        self.assertIsNone(skipped.origins[second]['anchor_day'])

    def test_evicted_source_keeps_first_seen_date_when_rebroadcast(self):
        memory = WorldMemory('news'); first = memory.observe('首条公告', 1)
        for n in range(2, 16): memory.observe(f'第{n}条其他公告', n)
        self.assertIsNone(memory.archive.document(first))
        memory.observe('首条公告', 261)
        self.assertEqual(1, memory.origins[first]['first_round'])
        self.assertEqual(1, memory.origins[first]['anchor_day'])
        self.assertFalse(memory.reviewed(first))

    def test_json_bounds_dates_content_and_owner_are_validated(self):
        memory = WorldMemory('news'); identity = memory.observe('事实全文', 1)
        memory.expose(identity, 0, 4)
        restored = WorldMemory.load(json.loads(json.dumps(memory.dump())), owner='news')
        self.assertFalse(restored.degraded)
        self.assertTrue(restored.reviewed(identity))
        for mutate in (lambda raw: raw['visible'][identity].append([99, 100]),
                       lambda raw: raw['origins'][identity].update(anchor_day=2),
                       lambda raw: raw['archive']['records'][0].update(text='伪造原文')):
            raw = deepcopy(memory.dump()); mutate(raw)
            self.assertTrue(WorldMemory.load(raw, owner='news').degraded)
        self.assertTrue(WorldMemory.load(memory.dump(), owner='treasure').degraded)

    def test_rebroadcast_replenishes_evicted_body_without_claiming_new_knowledge(self):
        memory = WorldMemory('news')
        text = 'A' * 120000
        first = memory.observe(text, 1)
        memory.expose(first, 0, 100)
        memory.observe('B' * 120000, 2)
        self.assertIsNone(memory.archive.document(first)['text'])
        memory.observe(text, 261)
        self.assertEqual(text, memory.archive.document(first)['text'])
        self.assertEqual(1, memory.origins[first]['first_round'])
        self.assertFalse(memory.quote_visible(first, 'AAA'))

    def test_oversized_original_never_becomes_fully_reviewed(self):
        memory = WorldMemory('treasure')
        identity = memory.observe('x' * 140000, 1)
        self.assertTrue(memory.expose(identity, 0, 131072))
        self.assertFalse(memory.reviewed(identity))
        self.assertFalse(memory.index()[0]['complete'])

    def test_inspection_is_not_a_file_tool_and_old_frames_do_not_change_dates(self):
        memory = WorldMemory('news'); identity = memory.observe('收到的文本', 132)
        self.assertIn('error', memory.inspect('C:/private/file.txt'))
        self.assertIsNone(memory.observe('未见文本', 1))
        self.assertEqual([identity], list(memory.origins))
        self.assertFalse(memory.expose(identity, True, 2))


if __name__ == '__main__': unittest.main()
