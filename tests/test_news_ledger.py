"""Independent expectations for the bounded public-news event ledger.

These tests do not derive expectations from the implementation: every amount,
date and correction below is a hand-written public example, and the pure
price/date helpers are exercised directly.  R01/R06/R07; task book 4.8/5.1.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))
from agent import brain, news_ledger, planner  # noqa: E402
from agent.world_agent import WorldAgent  # noqa: E402
from test_metal_economy import board  # noqa: E402


class PriceEvidenceTests(unittest.TestCase):
    def test_only_bound_amounts_are_extracted(self):
        self.assertEqual(news_ledger.parse_price_mentions('铁价上涨'), [])
        self.assertEqual(news_ledger.parse_price_mentions('第6天价格上涨'), [])
        self.assertEqual(news_ledger.parse_price_mentions('停工期间收购价上涨，之后恢复原价。'), [])
        for text, amount, basis in (
                ('铁收购价上涨到6金币', 6, 'absolute'),
                ('价格上涨到6金币', 6, 'absolute'),
                ('每份6金币', 6, 'absolute'),
                ('铁价上涨2金币', 2, 'delta'),
                ('涨了2金币', 2, 'delta'),
                ('铁价上涨20%', 20, 'percent'),
                ('涨幅为20%', 20, 'percent'),
                ('iron price rises to 6 gold', 6, 'absolute'),
                ('iron price rises by 2 gold', 2, 'delta'),
                ('iron price up 20%', 20, 'percent')):
            with self.subTest(text=text):
                self.assertTrue(news_ledger.verify_price([text], amount, basis))
                self.assertIn((float(amount), basis), news_ledger.parse_price_mentions(text))

    def test_a_date_number_cannot_support_an_amount(self):
        self.assertFalse(news_ledger.verify_price(['第6天价格上涨'], 6, 'absolute'))
        self.assertFalse(news_ledger.verify_price(['第6天铁价上涨'], 6, 'delta'))

    def test_falling_prices_are_bound_with_the_same_rules(self):
        for text, amount, basis in (
                ('铁价下降到6金币', 6, 'absolute'),
                ('铁价跌到6金币', 6, 'absolute'),
                ('铁价下降2金币', 2, 'delta'),
                ('铁价下跌2金币', 2, 'delta'),
                ('铁价下降20%', 20, 'percent'),
                ('iron price falls to 6 gold', 6, 'absolute'),
                ('iron price falls by 2 gold', 2, 'delta'),
                ('iron price drops 2 gold', 2, 'delta'),
                ('iron price down 20%', 20, 'percent')):
            with self.subTest(text=text):
                self.assertTrue(news_ledger.verify_price([text], amount, basis))
        self.assertFalse(news_ledger.verify_price(['铁价下降到6金币'], 6, 'delta'))
        self.assertFalse(news_ledger.verify_price(['铁价下降2金币'], 2, 'percent'))

    def test_negation_and_percentage_points_do_not_support_a_positive_price(self):
        self.assertFalse(news_ledger.verify_price(['铁价并未上涨到6金币'], 6, 'absolute'))
        self.assertFalse(news_ledger.verify_price(['铁价没有下降到6金币'], 6, 'absolute'))
        self.assertFalse(news_ledger.verify_price(['iron price did not rise to 6 gold'], 6, 'absolute'))
        self.assertFalse(news_ledger.verify_price(['iron price never fell to 6 gold'], 6, 'absolute'))
        self.assertFalse(news_ledger.verify_price(['铁价上涨20个百分点'], 20, 'percent'))
        self.assertFalse(news_ledger.verify_price(['iron price up 20 percentage points'], 20, 'percent'))
        # A negation in an earlier sentence must not leak into a later claim.
        self.assertTrue(news_ledger.verify_price(['此前没有停矿。铁价上涨到6金币'], 6, 'absolute'))

    def test_amounts_match_exactly_without_relative_tolerance(self):
        self.assertFalse(news_ledger.verify_price(['铁价上涨到1000000金币'], 1000001, 'absolute'))
        self.assertFalse(news_ledger.verify_price(['铁价上涨到1000001金币'], 1000000, 'absolute'))
        self.assertTrue(news_ledger.verify_price(['铁价上涨到6.10金币'], 6.1, 'absolute'))
        self.assertTrue(news_ledger.verify_price(['铁价上涨到6金币'], 6.0, 'absolute'))

    def test_wrong_amount_basis_or_unit_is_rejected(self):
        self.assertFalse(news_ledger.verify_price(['铁收购价上涨到6金币'], 6, 'delta'))
        self.assertFalse(news_ledger.verify_price(['铁价上涨2金币'], 2, 'percent'))
        self.assertFalse(news_ledger.verify_price(['铁价上涨2金币'], 6, 'delta'))
        self.assertFalse(news_ledger.verify_price(['铁价上涨'], 6, 'unknown'))
        self.assertFalse(news_ledger.verify_price(['铁价上涨'], None, 'absolute'))

    def test_unknown_amount_is_the_only_unknown_basis(self):
        self.assertTrue(news_ledger.verify_price(['铁价上涨'], None, 'unknown'))
        self.assertFalse(news_ledger.verify_price(['铁价上涨'], None, 'delta'))

    def test_amounts_reject_booleans_non_finite_and_negative(self):
        self.assertEqual(news_ledger.normalize_amount(None), (None, True))
        self.assertEqual(news_ledger.normalize_amount(6), (6, True))
        self.assertEqual(news_ledger.normalize_amount(6.0), (6, True))
        for bad in (True, False, '6', float('nan'), float('inf'), float('-inf'), -1, [], {}):
            with self.subTest(bad=bad):
                self.assertFalse(news_ledger.normalize_amount(bad)[1])

    def test_large_integers_stay_exact_without_overflow(self):
        for amount in (9007199254740993, 10 ** 500):
            with self.subTest(amount=str(amount)[:24]):
                normalized, ok = news_ledger.normalize_amount(amount)
                self.assertTrue(ok)
                self.assertEqual(normalized, amount)
                self.assertIsInstance(normalized, int)


class DateConsistencyTests(unittest.TestCase):
    def test_resume_must_be_after_the_end(self):
        self.assertTrue(news_ledger.dates_consistent(5, 6, 7))
        self.assertFalse(news_ledger.dates_consistent(5, 6, 6))
        self.assertFalse(news_ledger.dates_consistent(5, 6, 4))
        self.assertTrue(news_ledger.dates_consistent(5, 6, None))
        self.assertTrue(news_ledger.dates_consistent(None, None, None))

    def test_each_endpoint_may_be_unknown_but_known_ones_stay_consistent(self):
        # Parent review #6: a lone resumeDay is a known endpoint to validate,
        # not a reason to reject the whole interpretation.
        self.assertTrue(news_ledger.dates_consistent(5, None, 7))
        self.assertTrue(news_ledger.dates_consistent(None, None, 7))
        self.assertTrue(news_ledger.dates_consistent(None, 6, 7))
        self.assertFalse(news_ledger.dates_consistent(5, None, 5))
        self.assertFalse(news_ledger.dates_consistent(None, 6, 6))
        self.assertFalse(news_ledger.dates_consistent(6, 5, None))
        self.assertFalse(news_ledger.dates_consistent(1, 11, None))

    def test_boolean_or_out_of_range_endpoints_are_rejected(self):
        self.assertFalse(news_ledger.dates_consistent(True, 5, None))
        self.assertFalse(news_ledger.dates_consistent(None, None, True))
        self.assertFalse(news_ledger.dates_consistent(0, None, None))
        self.assertFalse(news_ledger.dates_consistent(None, None, 11))


class IdentityTests(unittest.TestCase):
    def identity(self, **overrides):
        fields = dict(resource='iron', availability='unavailable', start_day=2, end_day=3,
                      resume_day=4, price_direction='up', price_amount=None,
                      price_basis='unknown', source_ids=['a' * 64])
        fields.update(overrides)
        return news_ledger.news_id(**fields)

    def test_quote_punctuation_and_evidence_order_do_not_split_a_fact(self):
        self.assertEqual(self.identity(), self.identity())
        self.assertEqual(self.identity(source_ids=['a' * 64]), self.identity(source_ids=['a' * 64]))
        self.assertEqual(self.identity(source_ids=['a' * 64, 'b' * 64]),
                         self.identity(source_ids=['b' * 64, 'a' * 64]))

    def test_semantics_and_publication_change_the_identity(self):
        self.assertNotEqual(self.identity(), self.identity(availability='available'))
        self.assertNotEqual(self.identity(), self.identity(end_day=4))
        self.assertNotEqual(self.identity(), self.identity(source_ids=['b' * 64]))
        self.assertNotEqual(self.identity(), self.identity(price_amount=2, price_basis='delta'))

    def test_correction_wording_is_explicit(self):
        self.assertTrue(news_ledger.has_correction_marker('更正：今天已恢复'))
        self.assertTrue(news_ledger.has_correction_marker('此前消息撤回。'))
        self.assertFalse(news_ledger.has_correction_marker('铁矿今日恢复开采。'))


class LedgerHarness(unittest.TestCase):
    def agent_with(self, text, round_no=391):
        agent = WorldAgent()
        identity = self.add_source(agent, text, round_no)
        return agent, identity

    def add_source(self, agent, text, round_no):
        identity = agent.memories['news'].observe(text, round_no)
        self.assertIsNotNone(identity)
        agent.sources['news'].append({'id': identity, 'firstRound': round_no,
                                      'text': text[:3000], 'truncated': len(text) > 3000})
        agent.memories['news'].expose(identity, 0, min(len(text), 3000))
        return identity

    def news_event(self, source_id, quote, **overrides):
        event = {'resource': 'iron', 'availability': 'unavailable', 'startDay': None,
                 'endDay': None, 'priceDirection': 'unknown',
                 'evidence': [{'sourceId': source_id, 'quote': quote}]}
        event.update(overrides)
        return event

    def submit(self, agent, events, resources=('iron', 'copper')):
        link = {'resources': list(resources), 'width': 41, 'height': 32}
        merged = agent._merge_news(agent._validate_news_events(events, link))
        agent.news_events = merged
        return merged


class ResumeDayLedgerTests(LedgerHarness):
    TEXT = '今天第4天，明天停工两天，第7天恢复。'

    def test_single_event_resume_day_is_accepted_and_day_seven_is_not_banned(self):
        agent, sid = self.agent_with(self.TEXT)
        ledger = self.submit(agent, [self.news_event(
            sid, self.TEXT, startDay=5, endDay=6, resumeDay=7, priceDirection='up')])
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]['resumeDay'], 7)
        self.assertEqual(agent.policy_view(521)['unavailable'], ['iron'])  # day 5
        self.assertEqual(agent.policy_view(651)['unavailable'], ['iron'])  # day 6
        self.assertEqual(agent.policy_view(781)['unavailable'], [])        # day 7

    def test_resume_day_equal_to_end_is_rejected(self):
        agent, sid = self.agent_with(self.TEXT)
        with self.assertRaises(ValueError):
            self.submit(agent, [self.news_event(
                sid, self.TEXT, startDay=5, endDay=6, resumeDay=6, priceDirection='up')])
        self.assertEqual(agent.news_events, [])

    def test_lone_resume_day_round_trips_and_still_needs_date_evidence(self):
        agent, sid = self.agent_with(self.TEXT)  # "今天第4天" is the date basis
        ledger = self.submit(agent, [self.news_event(sid, self.TEXT, resumeDay=7)])
        self.assertEqual(ledger[0]['resumeDay'], 7)
        self.assertIsNone(ledger[0]['startDay'])
        self.assertIsNone(ledger[0]['endDay'])
        self.assertEqual(agent.policy_view(651)['unavailable'], [], 'no full outage range, no ban')
        restored = WorldAgent.load(json.loads(json.dumps(agent.dump())))
        self.assertFalse(restored.degraded)
        self.assertEqual(restored.news_events[0]['resumeDay'], 7)

    def test_known_endpoint_without_public_date_evidence_is_rejected(self):
        agent, sid = self.agent_with('铁矿第7天恢复。', round_no=391)
        with self.assertRaises(ValueError):
            self.submit(agent, [self.news_event(sid, '铁矿第7天恢复。', resumeDay=7)])
        with self.assertRaises(ValueError):
            self.submit(agent, [self.news_event(sid, '铁矿第7天恢复。', startDay=7, endDay=7)])

    def test_boolean_or_out_of_range_event_dates_are_rejected(self):
        agent, sid = self.agent_with(self.TEXT)
        for overrides in ({'resumeDay': True}, {'startDay': True, 'endDay': 6},
                          {'resumeDay': 11}, {'startDay': 0, 'endDay': 3}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    self.submit(agent, [self.news_event(sid, self.TEXT, **overrides)])


class PriceAmountLedgerTests(LedgerHarness):
    def ledger_for(self, quote, amount, basis):
        agent, sid = self.agent_with(quote)
        event = self.news_event(sid, quote, availability='available', priceDirection='up',
                                priceAmount=amount, priceBasis=basis)
        return self.submit(agent, [event])

    def test_bound_amounts_are_stored_with_their_basis(self):
        cases = (('铁收购价上涨到6金币。', 6, 'absolute'),
                 ('铁价上涨2金币。', 2, 'delta'),
                 ('铁价上涨20%。', 20, 'percent'))
        for quote, amount, basis in cases:
            with self.subTest(quote=quote):
                ledger = self.ledger_for(quote, amount, basis)
                self.assertEqual(ledger[0]['priceAmount'], amount)
                self.assertEqual(ledger[0]['priceBasis'], basis)

    def test_unknown_magnitude_stays_null(self):
        ledger = self.ledger_for('铁价上涨。', None, 'unknown')
        self.assertIsNone(ledger[0]['priceAmount'])
        self.assertEqual(ledger[0]['priceBasis'], 'unknown')

    def test_unbound_or_mismatched_amounts_are_rejected(self):
        for quote, amount, basis in (('第6天价格上涨。', 6, 'absolute'),
                                     ('铁价上涨。', 6, 'unknown'),
                                     ('铁收购价上涨到6金币。', 6, 'delta'),
                                     ('铁价上涨2金币。', 6, 'delta'),
                                     ('铁价上涨2金币。', 2, 'percent')):
            with self.subTest(quote=quote, amount=amount, basis=basis):
                with self.assertRaises(ValueError):
                    self.ledger_for(quote, amount, basis)

    def test_non_finite_amounts_are_rejected(self):
        for amount in (float('nan'), float('inf'), float('-inf'), True, -1):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    self.ledger_for('铁价上涨2金币。', amount, 'delta')

    def test_forged_quote_is_rejected(self):
        agent, sid = self.agent_with('铁价上涨2金币。')
        with self.assertRaises(ValueError):
            self.submit(agent, [self.news_event(
                sid, 'a sentence never published', availability='available',
                priceDirection='up', priceAmount=2, priceBasis='delta')])


class ProvenanceLedgerTests(LedgerHarness):
    TEXT = '游戏第1天，明天铁矿停工两天。'

    def test_every_record_is_program_marked_inferred(self):
        agent, sid = self.agent_with(self.TEXT, round_no=1)
        record = self.submit(agent, [self.news_event(sid, self.TEXT, startDay=2, endDay=3)])[0]
        self.assertEqual(record['kind'], 'inferred')
        self.assertEqual(record['status'], 'active')
        self.assertIsNone(record['resolution'])
        self.assertEqual(record['sourceRound'], 1)

    def test_model_cannot_declare_observed_or_supply_internal_fields(self):
        agent, sid = self.agent_with(self.TEXT, round_no=1)
        for extra in ({'kind': 'observed'}, {'status': 'corrected'}, {'id': 'n' + '0' * 16},
                      {'inferred': True}, {'sourceRound': 1}, {'observed': True}, {'price': 999}):
            with self.subTest(extra=extra):
                event = self.news_event(sid, self.TEXT, startDay=2, endDay=3)
                event.update(extra)
                with self.assertRaises(ValueError):
                    self.submit(agent, [event])
        self.assertEqual(agent.news_events, [])

    def test_quote_punctuation_variant_is_one_fact_with_both_quotes(self):
        agent, sid = self.agent_with(self.TEXT, round_no=1)
        record = self.submit(agent, [self.news_event(sid, self.TEXT, startDay=2, endDay=3)])[0]
        variant = self.TEXT[:-1]
        ledger = self.submit(agent, [self.news_event(sid, variant, startDay=2, endDay=3)])
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]['id'], record['id'])
        self.assertEqual(len(ledger[0]['evidence']), 2)


class ConflictAndCorrectionTests(LedgerHarness):
    FIRST = '游戏第1天，明天铁矿停工两天。'

    def test_explicit_correction_keeps_old_and_new_records(self):
        agent, sid = self.agent_with(self.FIRST, round_no=1)
        record = self.submit(agent, [self.news_event(sid, self.FIRST, startDay=2, endDay=3)])[0]
        self.assertEqual(agent.policy_view(131)['unavailable'], ['iron'])
        corrected = '更正：今天第三天已恢复铁矿开采。'
        cid = self.add_source(agent, corrected, 263)
        ledger = self.submit(agent, [self.news_event(
            cid, corrected, availability='available', startDay=3, endDay=10)])
        self.assertEqual(len(ledger), 2)
        old = next(item for item in ledger if item['id'] == record['id'])
        new = next(item for item in ledger if item['id'] != record['id'])
        self.assertEqual(old['status'], 'corrected')
        self.assertEqual(old['evidence'], record['evidence'], 'the old evidence is retained')
        self.assertEqual(new['status'], 'active')
        self.assertEqual(new['resolution']['kind'], 'corrected')
        self.assertEqual(new['resolution']['targetId'], record['id'])
        # Parent review #3: the correction only applies from its own day onward,
        # so the un-corrected day 2 outage must survive.
        self.assertEqual(agent.policy_view(131)['unavailable'], ['iron'],
                         'day 2 outage is not erased by a day 3 recovery')
        self.assertEqual(agent.policy_view(263)['unavailable'], [], 'day 3 recovery takes effect')
        restored = WorldAgent.load(json.loads(json.dumps(agent.dump())))
        self.assertFalse(restored.degraded)
        self.assertEqual(restored.policy_view(131)['unavailable'], ['iron'],
                         'time-scoped correction survives JSON')

    def test_uncorrected_opposite_messages_keep_both_and_conflict(self):
        agent, sid = self.agent_with(self.FIRST, round_no=1)
        self.submit(agent, [self.news_event(sid, self.FIRST, startDay=2, endDay=3)])
        second = '另有消息称铁矿明天照常开采。'
        cid = self.add_source(agent, second, 2)
        ledger = self.submit(agent, [self.news_event(
            cid, second, availability='available', startDay=2, endDay=3)])
        self.assertEqual(len(ledger), 2)
        view = agent.policy_view(131)
        self.assertEqual(view['unavailable'], [])
        self.assertEqual([row['resource'] for row in view['conflicts']], ['iron'])
        # A later reply that omits the conflict cannot erase it.
        ledger = self.submit(agent, [])
        self.assertEqual(len(ledger), 2)
        self.assertEqual(agent.policy_view(131)['unavailable'], [])

    def test_explicit_resolution_points_at_a_known_record(self):
        agent, sid = self.agent_with(self.FIRST, round_no=1)
        record = self.submit(agent, [self.news_event(sid, self.FIRST, startDay=2, endDay=3)])[0]
        # Parent review #2: resolution evidence must carry correction/retraction
        # wording; an arbitrary quote cannot retire a public fact.
        corrected = '游戏第2天白昼，更正：此前铁矿停矿消息撤回。'
        cid = self.add_source(agent, corrected, 263)
        resolution = {'kind': 'cancelled', 'targetId': record['id'],
                      'evidence': [{'sourceId': cid, 'quote': corrected}]}
        ledger = self.submit(agent, [self.news_event(
            cid, corrected, availability='available', startDay=2, endDay=3, resolution=resolution)])
        self.assertEqual(len(ledger), 2)
        old = next(item for item in ledger if item['id'] == record['id'])
        new = next(item for item in ledger if item['id'] != record['id'])
        self.assertEqual(old['status'], 'cancelled')
        self.assertEqual(new['status'], 'active')
        self.assertEqual(new['resolution'], resolution)
        with self.assertRaises(ValueError):
            self.submit(agent, [self.news_event(
                cid, corrected, availability='available', startDay=2, endDay=3,
                resolution={'kind': 'corrected', 'targetId': 'n' + 'f' * 16,
                            'evidence': [{'sourceId': cid, 'quote': corrected}]})])

    def test_explicit_resolution_without_correction_wording_is_rejected(self):
        agent, sid = self.agent_with(self.FIRST, round_no=1)
        record = self.submit(agent, [self.news_event(sid, self.FIRST, startDay=2, endDay=3)])[0]
        with self.assertRaises(ValueError):
            self.submit(agent, [self.news_event(
                sid, self.FIRST, availability='available', startDay=2, endDay=3,
                resolution={'kind': 'cancelled', 'targetId': record['id'],
                            'evidence': [{'sourceId': sid, 'quote': self.FIRST}]})])

    def test_cross_resource_or_non_overlapping_resolution_is_rejected(self):
        agent, sid = self.agent_with(self.FIRST, round_no=1)
        record = self.submit(agent, [self.news_event(sid, self.FIRST, startDay=2, endDay=3)])[0]
        copper = '游戏第2天白昼，更正：铜矿照常开采。'
        cid = self.add_source(agent, copper, 2)
        with self.assertRaises(ValueError):
            self.submit(agent, [self.news_event(
                cid, copper, resource='copper', availability='available', startDay=2, endDay=3,
                resolution={'kind': 'corrected', 'targetId': record['id'],
                            'evidence': [{'sourceId': cid, 'quote': copper}]})])
        far = '游戏第8天白昼，更正：铁矿第8天恢复。'
        fid = self.add_source(agent, far, 263)
        with self.assertRaises(ValueError):
            self.submit(agent, [self.news_event(
                fid, far, availability='available', startDay=8, endDay=10,
                resolution={'kind': 'corrected', 'targetId': record['id'],
                            'evidence': [{'sourceId': fid, 'quote': far}]})])
        self.assertEqual(len(agent.news_events), 1, 'the old fact is untouched by rejected resolutions')

    def test_bounded_correction_chain_updates_its_own_fact(self):
        # Parent graph-followup #1/#2 supersede the earlier "resolved chains are
        # frozen" rule: a later correction may target an earlier correction, and
        # it then applies to that correction's own effective days.
        text = '游戏第1天，铁矿第2天到第5天停工。'
        agent, sid = self.agent_with(text, round_no=1)
        original = self.submit(agent, [self.news_event(sid, text, startDay=2, endDay=5)])[0]
        first = '更正：铁矿第3天到第5天恢复开采。'
        fid = self.add_source(agent, first, 2)
        ledger = self.submit(agent, [self.news_event(
            fid, first, availability='available', startDay=3, endDay=5,
            resolution={'kind': 'corrected', 'targetId': original['id'],
                        'evidence': [{'sourceId': fid, 'quote': first}]})])
        correction = next(record for record in ledger if record['id'] != original['id'])
        second = '更正：铁矿仅第4天继续停工。'
        lid = self.add_source(agent, second, 3)
        ledger = self.submit(agent, [self.news_event(
            lid, second, availability='unavailable', startDay=4, endDay=4,
            resolution={'kind': 'corrected', 'targetId': correction['id'],
                        'evidence': [{'sourceId': lid, 'quote': second}]})])
        self.assertEqual(len(ledger), 3, 'the whole bounded chain is preserved')
        self.assertEqual(agent.policy_view(131)['unavailable'], ['iron'])   # day 2
        self.assertEqual(agent.policy_view(261)['unavailable'], [])         # day 3
        self.assertEqual(agent.policy_view(391)['unavailable'], ['iron'])   # day 4
        self.assertEqual(agent.policy_view(521)['unavailable'], [])         # day 5
        restored = WorldAgent.load(json.loads(json.dumps(agent.dump())))
        self.assertFalse(restored.degraded)
        self.assertEqual(restored.policy_view(391)['unavailable'], ['iron'])

    def test_different_price_bases_are_not_compared_as_a_conflict(self):
        text_a = '游戏第1天，铁价上涨到6金币。'
        text_b = '游戏第1天，铁价上涨6金币。'
        agent, sid = self.agent_with(text_a, round_no=1)
        aid = self.add_source(agent, text_b, 1)
        self.submit(agent, [self.news_event(sid, text_a, availability='available', startDay=2, endDay=2,
                                            priceDirection='up', priceAmount=6, priceBasis='absolute'),
                            self.news_event(aid, text_b, availability='available', startDay=2, endDay=2,
                                            priceDirection='up', priceAmount=6, priceBasis='delta')])
        view = agent.policy_view(131)
        self.assertEqual(view['conflicts'], [], 'absolute 6 and delta 6 are different concepts')
        self.assertEqual(view['unavailable'], [])

    def test_opposite_directions_at_the_same_time_conflict(self):
        text = '游戏第1天，明天铁价有变。'
        agent, sid = self.agent_with(text, round_no=1)
        other = '游戏第1天，另一报道称铁价走向相反。'
        oid = self.add_source(agent, other, 1)
        self.submit(agent, [self.news_event(sid, text, availability='available', startDay=2, endDay=2,
                                            priceDirection='up'),
                            self.news_event(oid, other, availability='available', startDay=2, endDay=2,
                                            priceDirection='down')])
        self.assertEqual([row['resource'] for row in agent.policy_view(131)['conflicts']], ['iron'])

    def test_ambiguous_correction_is_never_guessed(self):
        agent, sid = self.agent_with(self.FIRST, round_no=1)
        self.submit(agent, [self.news_event(sid, self.FIRST, startDay=2, endDay=3)])
        self.submit(agent, [self.news_event(sid, self.FIRST, startDay=3, endDay=4)])
        corrected = '更正：今天第三天已恢复铁矿开采。'
        cid = self.add_source(agent, corrected, 263)
        ledger = self.submit(agent, [self.news_event(
            cid, corrected, availability='available', startDay=3, endDay=10)])
        self.assertEqual(len(ledger), 3, 'two matching facts are ambiguous, so nothing is corrected')
        self.assertTrue(all(item['status'] == 'active' for item in ledger))
        self.assertIsNone(ledger[-1]['resolution'])

    def test_pending_publication_keeps_the_valid_ledger(self):
        agent, sid = self.agent_with(self.FIRST, round_no=1)
        self.submit(agent, [self.news_event(sid, self.FIRST, startDay=2, endDay=3)])
        # A newer publication arrives before any new interpretation is accepted.
        self.add_source(agent, '新的公开消息仍在分析。', 132)
        self.assertEqual(agent.policy_view(131)['unavailable'], ['iron'])

    def test_expired_and_undated_events_never_become_a_permanent_ban(self):
        agent, sid = self.agent_with(self.FIRST, round_no=1)
        self.submit(agent, [self.news_event(sid, self.FIRST, startDay=2, endDay=3)])
        self.assertEqual(agent.policy_view(391)['unavailable'], [])
        self.submit(agent, [self.news_event(sid, self.FIRST, availability='unavailable',
                                            startDay=None, endDay=None)])
        self.assertEqual(agent.policy_view(131)['unavailable'], ['iron'])
        self.assertEqual(agent.policy_view(391)['unavailable'], [])


class CapacityAndPersistenceTests(LedgerHarness):
    FIRST = '游戏第1天，明天铁矿停工两天。'

    def build(self):
        agent, sid = self.agent_with(self.FIRST, round_no=1)
        self.submit(agent, [self.news_event(sid, self.FIRST, startDay=2, endDay=3)])
        corrected = '更正：今天第三天已恢复铁矿开采。'
        cid = self.add_source(agent, corrected, 263)
        self.submit(agent, [self.news_event(cid, corrected, availability='available',
                                            startDay=3, endDay=10)])
        return agent

    def test_capacity_marks_a_gap_instead_of_silently_dropping_active_facts(self):
        text = '游戏第1天，公开消息。'
        agent, sid = self.agent_with(text, round_no=1)
        events = []
        for index in range(25):
            events.append(self.news_event(
                sid, text, resource='iron' if index < 20 else 'copper',
                availability='unavailable' if (index // 10) % 2 == 0 else 'available',
                startDay=1 + index % 10, endDay=1 + index % 10))
        ledger = None
        for start in range(0, len(events), 8):
            ledger = self.submit(agent, events[start:start + 8])
        self.assertLessEqual(len(ledger), 24)
        # Parent review #4 + graph-followup #4: gaps are bounded, persistable
        # ranges with bounded lost-id recovery metadata the consumer honours.
        self.assertGreaterEqual(len(agent.news_gaps), 1)
        self.assertTrue(all({'resource', 'startDay', 'endDay', 'count', 'lostIds', 'overflow'} == set(gap)
                            for gap in agent.news_gaps))
        self.assertTrue(all(isinstance(gap['lostIds'], list) and isinstance(gap['overflow'], bool)
                            for gap in agent.news_gaps))

    def test_eviction_gap_survives_json_and_suppresses_a_one_sided_ban(self):
        text = '游戏第1天，甲报称第1—10天铁矿停工，乙报称第1—10天铁矿照常。'
        agent, sid = self.agent_with(text, round_no=1)
        available = self.news_event(sid, text, availability='available', startDay=1, endDay=10)
        unavailable = self.news_event(sid, text, startDay=1, endDay=10)
        self.submit(agent, [available, unavailable])
        self.assertEqual(agent.policy_view(1)['unavailable'], [])
        filler = '游戏第1天，铜矿各个已公布时期照常开采。'
        fid = self.add_source(agent, filler, 1)
        intervals = [(start, end) for start in range(1, 11) for end in range(start, 11)][:23]
        for start in range(0, len(intervals), 8):
            self.submit(agent, [self.news_event(fid, filler, resource='copper', availability='available',
                                                startDay=left, endDay=right)
                                for left, right in intervals[start:start + 8]])
        self.assertTrue(agent.news_gaps, 'an eviction of a conflict leaves a gap')
        self.assertNotIn('iron', agent.policy_view(1)['unavailable'])
        restored = WorldAgent.load(json.loads(json.dumps(agent.dump())))
        self.assertFalse(restored.degraded)
        self.assertNotIn('iron', restored.policy_view(1)['unavailable'], 'the gap persists through JSON')

    def test_dump_load_round_trip_and_tamper_rejection(self):
        agent = self.build()
        raw = json.loads(json.dumps(agent.dump()))
        restored = WorldAgent.load(json.loads(json.dumps(raw)))
        self.assertFalse(restored.degraded)
        self.assertEqual(restored.news_events, agent.news_events)
        self.assertEqual(restored.news_gaps, agent.news_gaps)
        # Day 2 is the un-corrected part of the outage, so it is still banned.
        self.assertEqual(restored.policy_view(131)['unavailable'], ['iron'])
        self.assertEqual(restored.policy_view(263)['unavailable'], [])
        mutations = (
            lambda value: value['news_events'][0].update(id='n' + '0' * 16),
            lambda value: value['news_events'][0].update(kind='observed'),
            lambda value: value['news_events'][0].update(status='active'),
            lambda value: value['news_events'][0]['evidence'][0].update(quote='forged quote never published'),
            lambda value: value['news_events'].append(deepcopy(value['news_events'][0])),
            lambda value: value['news_events'][1]['resolution'].update(targetId='n' + 'f' * 16),
            lambda value: value['news_events'][0]['evidence'].append(
                {'sourceId': 'a' * 64, 'quote': 'unrelated'}),
            lambda value: value['news_events'][1].update(resource='copper'),
            lambda value: value['news_gaps'].append({'resource': 'iron', 'startDay': 0,
                                                     'endDay': 10, 'count': 1}),
        )
        for mutate in mutations:
            tampered = json.loads(json.dumps(raw))
            mutate(tampered)
            self.assertTrue(WorldAgent.load(tampered).degraded)

    def test_v2_archive_migrates_without_refreshing_the_ordinary_quota(self):
        agent = self.build()
        raw = json.loads(json.dumps(agent.dump()))
        raw['schema'] = 'competition-world-agent/2'
        raw.pop('news_gaps')
        for event in raw['news_events']:
            for key in ('id', 'kind', 'status', 'resolution', 'sourceRound',
                        'resumeDay', 'priceAmount', 'priceBasis'):
                event.pop(key, None)
        migrated = WorldAgent.load(json.loads(json.dumps(raw)))
        self.assertFalse(migrated.degraded)
        self.assertEqual(len(migrated.news_events), 2)
        self.assertTrue(all(record['kind'] == 'inferred' for record in migrated.news_events))
        self.assertEqual(migrated.news_gaps, [])
        # Migration must not invent a witness the old format never had.
        self.assertTrue(all(record['witness'] is None for record in migrated.news_events))

    def test_unknown_schema_and_tampered_source_still_degrade(self):
        agent = self.build()
        raw = json.loads(json.dumps(agent.dump()))
        raw['schema'] = 'competition-world-agent/1'
        self.assertTrue(WorldAgent.load(raw).degraded)
        tampered = json.loads(json.dumps(agent.dump()))
        tampered['sources']['news'][0]['text'] = 'tampered'
        self.assertTrue(WorldAgent.load(tampered).degraded)

    def _as_v2(self, agent, keep_sources=True):
        raw = json.loads(json.dumps(agent.dump()))
        raw['schema'] = 'competition-world-agent/2'
        raw.pop('news_gaps')
        if not keep_sources:
            raw['sources']['news'] = []
        for event in raw['news_events']:
            for key in ('id', 'kind', 'status', 'resolution', 'sourceRound', 'witness',
                        'resumeDay', 'priceAmount', 'priceBasis'):
                event.pop(key, None)
        return raw

    def test_migrated_record_without_a_verifiable_source_is_downgraded(self):
        agent, sid = self.agent_with(self.FIRST, round_no=1)
        self.submit(agent, [self.news_event(sid, self.FIRST, startDay=2, endDay=3)])
        migrated = WorldAgent.load(self._as_v2(agent, keep_sources=False))
        self.assertFalse(migrated.degraded)
        self.assertEqual(len(migrated.news_events), 1)
        self.assertIsNone(migrated.news_events[0]['witness'])
        self.assertEqual(migrated.policy_view(131)['unavailable'], [],
                         'a migrated fact without a verifiable source never drives a ban')

    def test_migrated_record_with_a_replaced_quote_is_not_trusted(self):
        agent, sid = self.agent_with(self.FIRST, round_no=1)
        self.submit(agent, [self.news_event(sid, self.FIRST, startDay=2, endDay=3)])
        raw = self._as_v2(agent, keep_sources=True)
        raw['news_events'][0]['evidence'][0]['quote'] = '从未发布过的伪造引文。'
        migrated = WorldAgent.load(raw)
        self.assertFalse(migrated.degraded, 'a v2 archive had no witness to prove tampering')
        self.assertEqual(migrated.policy_view(131)['unavailable'], [],
                         'a replaced quote without a witness is not a trusted citation')

    def test_prompt_context_keeps_omitted_history_and_gaps_visible(self):
        text = '游戏第1天，公开消息。'
        agent, sid = self.agent_with(text, round_no=1)
        self.submit(agent, [self.news_event(sid, text, availability='available', startDay=1, endDay=10),
                            self.news_event(sid, text, availability='unavailable', startDay=1, endDay=10)])
        filler = '游戏第1天，铜矿照常开采。'
        fid = self.add_source(agent, filler, 1)
        intervals = [(i, i) for i in range(1, 11)] + [(1, 2), (2, 3)]
        events = [self.news_event(fid, filler, resource='copper', availability='available',
                                  startDay=left, endDay=right) for left, right in intervals]
        self.submit(agent, events[:8])
        self.submit(agent, events[8:])
        context = agent._ledger_context()
        self.assertGreater(context['omitted'], 0, 'the tail is truncated')
        self.assertTrue(context['conflicts'], 'a conflict beyond the sent tail is still reported')
        prompt = agent._prompt('news', 'token', ['iron', 'copper'], {'width': 41, 'height': 32})
        self.assertIn('账本状态', prompt)
        self.assertIn('omitted', prompt)


class EndToEndLedgerTests(unittest.TestCase):
    def setUp(self):
        self.state = planner.PlannerState()
        env = patch.dict(os.environ, {brain.WORLD_AGENT_ENV: 'on', brain.TASK_AGENT_ENV: 'off'})
        env.start()
        self.addCleanup(env.stop)

    def run_round(self, payload):
        self.state.note_round(payload['roundNo'])
        response = brain.plan_for_state(payload, self.state, judge_tasks=False)
        self.state.note_submission(response.prompt, response.execute, payload['roundNo'], False)
        self.state = planner.PlannerState.load(json.loads(json.dumps(self.state.dump())))
        self.assertFalse(self.state.team_agent.degraded)
        return response.build()

    def news_payload(self, round_no, text):
        payload = board(ores=(('iron', (15, 24)), ('copper', (15, 25))), round_no=round_no)
        payload.pop('_demo')
        payload['worldNews']['officialNews'] = text
        payload['vendorShopList'][1]['price'] = 9
        return payload

    def test_accepted_amount_never_replaces_the_live_vendor_price(self):
        text = '今天第1天，铁收购价上涨到6金币。'
        self.run_round(self.news_payload(13, text))
        world = self.state.team_agent.world
        link = world.links['news']
        event = {'resource': 'iron', 'availability': 'available', 'startDay': 1, 'endDay': 1,
                 'priceDirection': 'up', 'priceAmount': 6, 'priceBasis': 'absolute',
                 'evidence': [{'sourceId': world.sources['news'][0]['id'], 'quote': text}]}
        payload = self.news_payload(14, text)
        payload['llmResp'] = json.dumps({'request_id': link['token'], 'events': [event]}, ensure_ascii=False)
        self.run_round(payload)
        world = self.state.team_agent.world
        self.assertEqual(len(world.news_events), 1)
        self.assertEqual(world.news_events[0]['kind'], 'inferred')
        self.assertEqual(world.news_events[0]['priceAmount'], 6)
        self.assertEqual(world.news_events[0]['priceBasis'], 'absolute')
        self.assertEqual(brain._sellable_metals(self.news_payload(15, text))['iron'], 9)

    def test_four_day_example_bans_five_and_six_but_not_seven(self):
        text = '今天第4天，明天停工两天，第7天恢复。'
        self.run_round(self.news_payload(391, text))
        world = self.state.team_agent.world
        link = world.links['news']
        event = {'resource': 'iron', 'availability': 'unavailable', 'startDay': 5, 'endDay': 6,
                 'resumeDay': 7, 'priceDirection': 'up', 'priceAmount': None, 'priceBasis': 'unknown',
                 'evidence': [{'sourceId': world.sources['news'][0]['id'], 'quote': text}]}
        payload = self.news_payload(392, text)
        payload['llmResp'] = json.dumps({'request_id': link['token'], 'events': [event]}, ensure_ascii=False)
        self.run_round(payload)
        world = self.state.team_agent.world  # restored through PlannerState JSON
        self.assertEqual(len(world.news_events), 1)
        self.assertEqual(world.news_events[0]['kind'], 'inferred')
        self.assertEqual(world.news_events[0]['resumeDay'], 7)
        self.assertEqual(world.policy_view(521)['unavailable'], ['iron'])  # day 5
        self.assertEqual(world.policy_view(651)['unavailable'], ['iron'])  # day 6
        self.assertEqual(world.policy_view(781)['unavailable'], [])        # day 7


if __name__ == '__main__':
    unittest.main()
