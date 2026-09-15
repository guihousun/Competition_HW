"""Independent expectations for bounded corrections, gaps and the shared view.

Every case comes from the parent graph-followup/interval spec, not from the
implementation: dates, corrections and conflict dimensions are hand-written.
"""
from copy import deepcopy
import json
import unittest

from test_news_ledger import LedgerHarness
from agent import news_ledger
from agent.world_agent import WorldAgent


def _rebind(record):
    record['witness'] = news_ledger.witness({'evidence': record['evidence'],
                                             'resolution': record['resolution']})


class MultiCorrectionTests(LedgerHarness):
    OUTAGE = '游戏第1天，铁矿第2天到第5天停工。'

    def test_two_uncorrected_days_survive_two_bounded_corrections(self):
        agent, sid = self.agent_with(self.OUTAGE, round_no=1)
        original = self.submit(agent, [self.news_event(sid, self.OUTAGE, startDay=2, endDay=5)])[0]
        first = '更正：铁矿第3天恢复开采。'
        cid = self.add_source(agent, first, 2)
        self.submit(agent, [self.news_event(
            cid, first, availability='available', startDay=3, endDay=3,
            resolution={'kind': 'corrected', 'targetId': original['id'],
                        'evidence': [{'sourceId': cid, 'quote': first}]})])
        for day, expected in ((131, ['iron']), (261, []), (391, ['iron']), (521, ['iron'])):
            self.assertEqual(agent.policy_view(day)['unavailable'], expected, day)
        second = '更正：铁矿第5天恢复开采。'
        lid = self.add_source(agent, second, 3)
        self.submit(agent, [self.news_event(
            lid, second, availability='available', startDay=5, endDay=5,
            resolution={'kind': 'corrected', 'targetId': original['id'],
                        'evidence': [{'sourceId': lid, 'quote': second}]})])
        for day, expected in ((131, ['iron']), (261, []), (391, ['iron']), (521, [])):
            self.assertEqual(agent.policy_view(day)['unavailable'], expected, day)
        self.assertEqual(len(agent.news_events), 3, 'both corrections are retained')
        restored = WorldAgent.load(json.loads(json.dumps(agent.dump())))
        self.assertFalse(restored.degraded)
        self.assertEqual(restored.policy_view(131)['unavailable'], ['iron'])
        self.assertEqual(restored.policy_view(391)['unavailable'], ['iron'])
        self.assertEqual(restored.policy_view(521)['unavailable'], [])

    def test_later_correction_applies_to_its_own_fact(self):
        agent, sid = self.agent_with(self.OUTAGE, round_no=1)
        original = self.submit(agent, [self.news_event(sid, self.OUTAGE, startDay=2, endDay=5)])[0]
        first = '更正：铁矿第3天到第5天恢复开采。'
        cid = self.add_source(agent, first, 2)
        ledger = self.submit(agent, [self.news_event(
            cid, first, availability='available', startDay=3, endDay=5,
            resolution={'kind': 'corrected', 'targetId': original['id'],
                        'evidence': [{'sourceId': cid, 'quote': first}]})])
        correction = next(record for record in ledger if record['id'] != original['id'])
        second = '更正：铁矿仅第4天继续停工。'
        lid = self.add_source(agent, second, 3)
        self.submit(agent, [self.news_event(
            lid, second, availability='unavailable', startDay=4, endDay=4,
            resolution={'kind': 'corrected', 'targetId': correction['id'],
                        'evidence': [{'sourceId': lid, 'quote': second}]})])
        self.assertEqual(agent.policy_view(131)['unavailable'], ['iron'])  # day 2
        self.assertEqual(agent.policy_view(261)['unavailable'], [])        # day 3
        self.assertEqual(agent.policy_view(391)['unavailable'], ['iron'])  # day 4
        self.assertEqual(agent.policy_view(521)['unavailable'], [])        # day 5
        self.assertEqual(len(agent.news_events), 3)

    def test_later_unknown_correction_does_not_revive_a_retracted_day(self):
        # Parent counterexample: B explicitly retracts A on D3-D5, then C edits
        # only B on D4 (unknown).  A must not silently come back on D4, and the
        # retracted original must not form a pseudo-conflict with C.
        agent, sid = self.agent_with(self.OUTAGE, round_no=1)
        original = self.submit(agent, [self.news_event(sid, self.OUTAGE, startDay=2, endDay=5)])[0]
        first = '更正：铁矿第3天到第5天恢复开采。'
        fid = self.add_source(agent, first, 2)
        ledger = self.submit(agent, [self.news_event(
            fid, first, availability='available', startDay=3, endDay=5,
            resolution={'kind': 'corrected', 'targetId': original['id'],
                        'evidence': [{'sourceId': fid, 'quote': first}]})])
        correction = next(record for record in ledger if record['id'] != original['id'])
        second = '更正：铁矿第4天情况未知，以本条为准。'
        lid = self.add_source(agent, second, 3)
        ledger = self.submit(agent, [self.news_event(
            lid, second, availability='unknown', startDay=4, endDay=4,
            resolution={'kind': 'corrected', 'targetId': correction['id'],
                        'evidence': [{'sourceId': lid, 'quote': second}]})])
        latest = next(record for record in ledger
                      if record['id'] not in (original['id'], correction['id']))
        facts = {fact['id']: fact for fact in agent.news_view()['facts']}
        self.assertEqual(facts[original['id']]['effectiveDays'], [2],
                         'D4 must not silently revive the explicitly retracted original')
        self.assertEqual(facts[original['id']]['possibleDays'], [2],
                         'a retracted day is not even a possible day for the original')
        self.assertEqual(facts[correction['id']]['effectiveDays'], [3, 5])
        self.assertEqual(facts[latest['id']]['effectiveDays'], [4])
        self.assertEqual(agent.policy_view(131)['unavailable'], ['iron'])  # D2 outage stays
        self.assertEqual(agent.policy_view(261)['unavailable'], [])        # D3 recovered
        self.assertEqual(agent.policy_view(391)['unavailable'], [])        # D4 unknown, no revival
        self.assertEqual(agent.policy_view(521)['unavailable'], [])        # D5 recovered
        self.assertEqual(agent.news_view()['conflicts'], [],
                         'no pseudo-conflict between the retracted original and the later correction')
        restored = WorldAgent.load(json.loads(json.dumps(agent.dump())))
        self.assertFalse(restored.degraded)
        self.assertEqual(restored.policy_view(131)['unavailable'], ['iron'])
        self.assertEqual(restored.policy_view(391)['unavailable'], [])
        restored_facts = {fact['id']: fact for fact in restored.news_view()['facts']}
        self.assertEqual(restored_facts[original['id']]['effectiveDays'], [2])

    def test_later_price_only_correction_does_not_revive_a_retracted_day(self):
        agent, sid = self.agent_with(self.OUTAGE, round_no=1)
        original = self.submit(agent, [self.news_event(sid, self.OUTAGE, startDay=2, endDay=5)])[0]
        first = '更正：铁矿第3天到第5天恢复开采，铁价上涨到6金币。'
        fid = self.add_source(agent, first, 2)
        ledger = self.submit(agent, [self.news_event(
            fid, first, availability='available', startDay=3, endDay=5,
            priceDirection='up', priceAmount=6, priceBasis='absolute',
            resolution={'kind': 'corrected', 'targetId': original['id'],
                        'evidence': [{'sourceId': fid, 'quote': first}]})])
        correction = next(record for record in ledger if record['id'] != original['id'])
        second = '更正：铁矿第4天铁价上涨到7金币，开采情况不变。'
        lid = self.add_source(agent, second, 3)
        ledger = self.submit(agent, [self.news_event(
            lid, second, availability='available', startDay=4, endDay=4,
            priceDirection='up', priceAmount=7, priceBasis='absolute',
            resolution={'kind': 'corrected', 'targetId': correction['id'],
                        'evidence': [{'sourceId': lid, 'quote': second}]})])
        latest = next(record for record in ledger
                      if record['id'] not in (original['id'], correction['id']))
        self.assertEqual(agent.policy_view(131)['unavailable'], ['iron'])
        self.assertEqual(agent.policy_view(391)['unavailable'], [])  # D4 still available
        self.assertFalse(any(set(entry['ids']) == {original['id'], latest['id']}
                             for entry in agent.news_view()['conflicts']))

    def test_auto_association_builds_a_chain_without_a_cycle(self):
        agent, sid = self.agent_with(self.OUTAGE, round_no=1)
        self.submit(agent, [self.news_event(sid, self.OUTAGE, startDay=2, endDay=5)])
        first = '更正：铁矿第3天到第5天恢复开采。'
        cid = self.add_source(agent, first, 2)
        self.submit(agent, [self.news_event(cid, first, availability='available',
                                            startDay=3, endDay=5)])
        second = '更正：铁矿仅第4天继续停工。'
        lid = self.add_source(agent, second, 3)
        ledger = self.submit(agent, [self.news_event(lid, second, availability='unavailable',
                                                     startDay=4, endDay=4)])
        edges = [record for record in ledger if record['resolution']]
        self.assertEqual(len(edges), 2, 'both corrections bound a target')
        self.assertEqual(agent.policy_view(131)['unavailable'], ['iron'])
        self.assertEqual(agent.policy_view(261)['unavailable'], [])
        self.assertEqual(agent.policy_view(391)['unavailable'], ['iron'])
        self.assertEqual(agent.policy_view(521)['unavailable'], [])

    def test_repeated_correction_is_idempotent_even_with_quote_variants(self):
        agent, sid = self.agent_with(self.OUTAGE, round_no=1)
        original = self.submit(agent, [self.news_event(sid, self.OUTAGE, startDay=2, endDay=5)])[0]
        first = '更正：铁矿第3天恢复开采。'
        cid = self.add_source(agent, first, 2)
        resolver = {'kind': 'corrected', 'targetId': original['id'],
                    'evidence': [{'sourceId': cid, 'quote': first}]}
        ledger = self.submit(agent, [self.news_event(
            cid, first, availability='available', startDay=3, endDay=3, resolution=resolver)])
        variant = first[:-1]  # same wording without the trailing punctuation
        ledger = self.submit(agent, [self.news_event(
            cid, variant, availability='available', startDay=3, endDay=3,
            resolution={'kind': 'corrected', 'targetId': original['id'],
                        'evidence': [{'sourceId': cid, 'quote': variant}]})])
        self.assertEqual(len(ledger), 2, 'a repeated correction adds no record')
        self.assertEqual(len([record for record in ledger if record['resolution']]), 1)
        self.assertEqual(len(agent.policy_view(261)['unavailable']), 0)

    def test_a_bound_correction_cannot_swap_its_target(self):
        agent, sid = self.agent_with(self.OUTAGE, round_no=1)
        first_target = self.submit(agent, [self.news_event(sid, self.OUTAGE, startDay=2, endDay=5)])[0]
        other_text = '游戏第1天，铁矿第3天到第4天停工。'
        oid = self.add_source(agent, other_text, 2)
        second_target = self.submit(agent, [self.news_event(oid, other_text,
                                                            startDay=3, endDay=4)])[-1]
        correction = '更正：铁矿第3天恢复开采。'
        cid = self.add_source(agent, correction, 3)
        self.submit(agent, [self.news_event(
            cid, correction, availability='available', startDay=3, endDay=3,
            resolution={'kind': 'corrected', 'targetId': first_target['id'],
                        'evidence': [{'sourceId': cid, 'quote': correction}]})])
        with self.assertRaises(ValueError):
            self.submit(agent, [self.news_event(
                cid, correction, availability='available', startDay=3, endDay=3,
                resolution={'kind': 'corrected', 'targetId': second_target['id'],
                            'evidence': [{'sourceId': cid, 'quote': correction}]})])

    def test_json_rejects_cycles_and_self_reference(self):
        agent, sid = self.agent_with(self.OUTAGE, round_no=1)
        original = self.submit(agent, [self.news_event(sid, self.OUTAGE, startDay=2, endDay=5)])[0]
        correction = '更正：铁矿第3天恢复开采。'
        cid = self.add_source(agent, correction, 2)
        self.submit(agent, [self.news_event(
            cid, correction, availability='available', startDay=3, endDay=3,
            resolution={'kind': 'corrected', 'targetId': original['id'],
                        'evidence': [{'sourceId': cid, 'quote': correction}]})])
        base = json.loads(json.dumps(agent.dump()))
        records = {record['id']: record for record in base['news_events']}
        resolver = next(record for record in base['news_events'] if record['resolution'])
        # Close a real two-node cycle A -> B -> A with consistent statuses/witnesses.
        original_record = records[original['id']]
        original_record['resolution'] = {'kind': 'corrected', 'targetId': resolver['id'],
                                         'evidence': deepcopy(resolver['resolution']['evidence'])}
        original_record['status'] = 'corrected'
        resolver['status'] = 'corrected'
        _rebind(original_record)
        _rebind(resolver)
        self.assertTrue(WorldAgent.load(base).degraded, 'a correction cycle is rejected')

        self_loop = json.loads(json.dumps(agent.dump()))
        target = next(record for record in self_loop['news_events'] if record['resolution'])
        target['resolution']['targetId'] = target['id']
        _rebind(target)
        self.assertTrue(WorldAgent.load(self_loop).degraded, 'self reference is rejected')


class PossibleConflictTests(LedgerHarness):
    def test_unknown_end_bound_is_a_possible_not_settled_conflict(self):
        text = '游戏第1天，铁矿从第2天开始停工，结束日未知。'
        agent, sid = self.agent_with(text, round_no=1)
        self.submit(agent, [self.news_event(sid, text, startDay=2, endDay=None)])
        other = '另一报道称铁矿第3天照常开采。'
        oid = self.add_source(agent, other, 3)
        self.submit(agent, [self.news_event(oid, other, availability='available',
                                            startDay=3, endDay=3)])
        self.assertEqual(agent.policy_view(261)['unavailable'], [], 'no ban from a partial fact')
        context = agent._ledger_context()
        self.assertTrue(context['conflicts'], 'a possible conflict is not dropped')
        entry = context['conflicts'][0]
        self.assertTrue(entry['possible'])
        self.assertEqual(entry['dimensions'], ['availability'])
        restored = WorldAgent.load(json.loads(json.dumps(agent.dump())))
        self.assertFalse(restored.degraded)
        self.assertTrue(restored._ledger_context()['conflicts'])

    def test_price_conflict_keeps_the_agreed_outage_ban(self):
        first = '游戏第1天，铁矿第2天停工，铁价上涨到6金币。'
        agent, sid = self.agent_with(first, round_no=1)
        other = '游戏第1天，铁矿第2天停工，另一报道称铁价上涨到7金币。'
        oid = self.add_source(agent, other, 1)
        asset = {'resource': 'iron', 'availability': 'unavailable', 'startDay': 2, 'endDay': 2,
                 'priceDirection': 'up'}
        self.submit(agent, [
            {**asset, 'priceAmount': 6, 'priceBasis': 'absolute',
             'evidence': [{'sourceId': sid, 'quote': first}]},
            {**asset, 'priceAmount': 7, 'priceBasis': 'absolute',
             'evidence': [{'sourceId': oid, 'quote': other}]}])
        view = agent.policy_view(131)  # day 2
        self.assertEqual(view['unavailable'], ['iron'], 'an agreed outage is still a ban')
        price = [entry for entry in view['conflicts'] if 'price' in entry['dimensions']]
        self.assertTrue(price)
        self.assertEqual(price[0]['prices'], ['absolute:6', 'absolute:7'])

    def test_possible_availability_conflict_suppresses_a_one_sided_ban(self):
        # Parent counterexample: A definitely stops iron D2-D3 while B says it
        # is available from D2 with an unknown end.  B has no definite days yet,
        # so the consumer must not show the possible contradiction and still ban.
        text = '游戏第1天，铁矿第2天到第3天停工。'
        agent, sid = self.agent_with(text, round_no=1)
        self.submit(agent, [self.news_event(sid, text, startDay=2, endDay=3)])
        other = '另一报道称铁矿从第2天起照常开采，结束日未知。'
        oid = self.add_source(agent, other, 2)
        self.submit(agent, [self.news_event(oid, other, availability='available',
                                            startDay=2, endDay=None)])
        self.assertEqual(agent.policy_view(131)['unavailable'], [],
                         'no one-sided ban beside a possible availability conflict')
        self.assertEqual(agent.policy_view(261)['unavailable'], [])
        self.assertNotIn('iron', agent.policy_view(1)['unavailable'], 'D1 cannot overlap, so unaffected')
        possible = [entry for entry in agent.news_view()['conflicts']
                    if 'availability' in entry['dimensions'] and not entry['definite']]
        self.assertTrue(possible, 'the possible conflict stays visible')
        self.assertEqual(sorted(possible[0]['possibleDays']), [2, 3])
        restored = WorldAgent.load(json.loads(json.dumps(agent.dump())))
        self.assertFalse(restored.degraded)
        self.assertEqual(restored.policy_view(131)['unavailable'], [])
        self.assertEqual(restored.policy_view(261)['unavailable'], [])

    def test_possible_price_only_conflict_keeps_the_outage_ban(self):
        first = '游戏第1天，铁矿第2天停工，铁价上涨到6金币。'
        agent, sid = self.agent_with(first, round_no=1)
        other = '另一报道称铁价上涨到7金币，停工日期尚未公布。'
        oid = self.add_source(agent, other, 2)
        self.submit(agent, [self.news_event(sid, first, startDay=2, endDay=2,
                                            priceDirection='up', priceAmount=6,
                                            priceBasis='absolute'),
                            self.news_event(oid, other, availability='unavailable',
                                            startDay=None, endDay=None, priceDirection='up',
                                            priceAmount=7, priceBasis='absolute')])
        self.assertEqual(agent.policy_view(131)['unavailable'], ['iron'],
                         'a possible price-only conflict never cancels the outage advice')
        price = [entry for entry in agent.news_view()['conflicts']
                 if 'price' in entry['dimensions'] and not entry['definite']]
        self.assertTrue(price, 'the possible price conflict stays visible')

    def test_availability_conflict_blocks_a_one_sided_ban(self):
        text = '游戏第1天，铁矿第2天停工。'
        agent, sid = self.agent_with(text, round_no=1)
        other = '另一报道称铁矿第2天照常开采。'
        oid = self.add_source(agent, other, 1)
        self.submit(agent, [self.news_event(sid, text, startDay=2, endDay=2),
                            self.news_event(oid, other, availability='available',
                                            startDay=2, endDay=2)])
        view = agent.policy_view(131)  # day 2
        self.assertEqual(view['unavailable'], [])
        self.assertEqual([entry['resource'] for entry in view['conflicts']], ['iron'])


class GapRecoveryTests(LedgerHarness):
    def _evict_iron_conflict(self, agent, sid):
        quote = '游戏第1天，公开消息。'
        available = self.news_event(sid, quote, availability='available',
                                    startDay=1, endDay=10)
        unavailable = self.news_event(sid, quote, startDay=1, endDay=10)
        ledger = self.submit(agent, [available, unavailable])
        available_id, unavailable_id = ledger[0]['id'], ledger[1]['id']
        filler = '游戏第1天，铜矿各个已公布时期照常开采。'
        fid = self.add_source(agent, filler, 1)
        intervals = [(start, end) for start in range(1, 11) for end in range(start, 11)][:23]
        for start in range(0, len(intervals), 8):
            self.submit(agent, [self.news_event(fid, filler, resource='copper',
                                                availability='available',
                                                startDay=left, endDay=right)
                                for left, right in intervals[start:start + 8]])
        return available, unavailable, available_id, unavailable_id

    def test_one_sided_replay_keeps_the_gap_both_sides_clear_it(self):
        agent, sid = self.agent_with('游戏第1天，公开消息。', round_no=1)
        available, unavailable, _, _ = self._evict_iron_conflict(agent, sid)
        iron_gaps = [gap for gap in agent.news_gaps if gap['resource'] == 'iron']
        self.assertTrue(iron_gaps, 'the evicted conflict leaves a bounded gap')
        self.assertGreaterEqual(iron_gaps[0]['count'], 2)
        self.assertFalse(iron_gaps[0]['overflow'])
        self.assertNotIn('iron', agent.policy_view(1)['unavailable'])
        # Replaying only one side cannot clear the gap.
        self.submit(agent, [unavailable])
        self.assertTrue([gap for gap in agent.news_gaps if gap['resource'] == 'iron'])
        self.assertNotIn('iron', agent.policy_view(1)['unavailable'])
        restored = WorldAgent.load(json.loads(json.dumps(agent.dump())))
        self.assertTrue([gap for gap in restored.news_gaps if gap['resource'] == 'iron'])
        # Restoring both verified sides clears the gap but keeps the real conflict.
        self.submit(agent, [available])
        self.assertFalse([gap for gap in agent.news_gaps if gap['resource'] == 'iron'])
        view = agent.policy_view(1)
        self.assertNotIn('iron', view['unavailable'])
        self.assertTrue(view['conflicts'])
        restored = WorldAgent.load(json.loads(json.dumps(agent.dump())))
        self.assertFalse([gap for gap in restored.news_gaps if gap['resource'] == 'iron'])
        self.assertTrue(restored.policy_view(1)['conflicts'])

    def test_a_genuine_correction_can_restore_definite_advice(self):
        agent, sid = self.agent_with('游戏第1天，公开消息。', round_no=1)
        available, unavailable, available_id, _ = self._evict_iron_conflict(agent, sid)
        self.submit(agent, [unavailable])
        self.submit(agent, [available])  # both sides verified again -> gap clears
        view = agent.policy_view(1)
        self.assertTrue(view['conflicts'], 'the real conflict is still open')
        self.assertNotIn('iron', view['unavailable'])
        closing = '更正：此前铁矿照常开采的消息撤回，第1天到第10天停工。'
        cid = self.add_source(agent, closing, 5)
        self.submit(agent, [self.news_event(
            cid, closing, availability='unavailable', startDay=1, endDay=10,
            resolution={'kind': 'cancelled', 'targetId': available_id,
                        'evidence': [{'sourceId': cid, 'quote': closing}]})])
        view = agent.policy_view(1)
        self.assertEqual(view['unavailable'], ['iron'], 'a genuine correction restores a ban')
        restored = WorldAgent.load(json.loads(json.dumps(agent.dump())))
        self.assertFalse(restored.degraded)
        self.assertEqual(restored.policy_view(1)['unavailable'], ['iron'])

    def test_gap_metadata_overflow_stays_visible_and_conservative(self):
        agent, sid = self.agent_with('游戏第1天，公开消息。', round_no=1)
        quote = '游戏第1天，公开消息。'
        names = [f'ore{i}' for i in range(9)]
        resources = tuple(names) + ('stone', 'copper')
        pairs = []
        for name in names:
            pairs.append((self.news_event(sid, quote, resource=name, startDay=1, endDay=10),
                          self.news_event(sid, quote, resource=name, availability='available',
                                          startDay=1, endDay=10)))
        flat = [event for pair in pairs for event in pair]
        for start in range(0, len(flat), 8):
            self.submit(agent, flat[start:start + 8], resources=resources)
        intervals = [(start, end) for start in range(1, 11) for end in range(start, 11)][:24]
        fillers = [self.news_event(sid, quote, resource='stone', availability='available',
                                   startDay=left, endDay=right) for left, right in intervals]
        for start in range(0, len(fillers), 8):
            self.submit(agent, fillers[start:start + 8], resources=resources)
        self.assertTrue(agent.news_gap_overflow, 'the overflow marker is explicit')
        self.assertLessEqual(len(agent.news_gaps), 8)
        # A definite outage that would otherwise ban copper is withheld while the
        # overflow is opaque: the loss set is unknown, so no certain advice.
        self.submit(agent, [self.news_event(sid, quote, resource='copper',
                                            startDay=10, endDay=10)], resources=resources)
        self.assertNotIn('copper', agent.policy_view(1171)['unavailable'],
                         'an opaque overflow never manufactures a certain ban')
        context = agent._ledger_context()
        self.assertTrue(context['gapOverflow'])
        restored = WorldAgent.load(json.loads(json.dumps(agent.dump())))
        self.assertFalse(restored.degraded)
        self.assertTrue(restored.news_gap_overflow)


class ViewConsistencyTests(LedgerHarness):
    def test_prompt_stays_within_budget_with_a_full_ledger(self):
        prefix = '游戏第1天，' + '公开背景资料。' * 5 + '铁价上涨到6金币。'
        text = prefix + '公开背景资料。' * 900
        agent, sid = self.agent_with(text, round_no=1)
        # A bounded, verbatim prefix (quotes are capped at 500 chars) carrying
        # both the game-day anchor and the price predicate.
        quote = prefix
        events = []
        for index in range(24):
            events.append(self.news_event(
                sid, quote, resource='iron' if index % 2 == 0 else 'copper',
                availability='unavailable' if (index // 10) % 2 == 0 else 'available',
                startDay=1 + index % 10, endDay=1 + index % 10))
        for start in range(0, len(events), 8):
            self.submit(agent, events[start:start + 8])
        prompt = agent._prompt('news', 'token', ['iron', 'copper'],
                               {'width': 41, 'height': 32})
        self.assertLessEqual(len(prompt), 12000)
        self.assertIn('账本状态', prompt)

    def test_page_view_is_the_same_backend_computation(self):
        text = '游戏第1天，铁矿第2天到第3天停工。'
        agent, sid = self.agent_with(text, round_no=1)
        original = self.submit(agent, [self.news_event(sid, text, startDay=2, endDay=3)])[0]
        correction = '更正：游戏第3天起铁矿恢复开采。'
        cid = self.add_source(agent, correction, 263)
        self.submit(agent, [self.news_event(cid, correction, availability='available',
                                            startDay=3, endDay=10)])
        view = agent.news_view()
        self.assertEqual(view['schema'], 'competition-news-view/1')
        facts = {fact['id']: fact for fact in view['facts']}
        self.assertEqual(facts[original['id']]['effectiveDays'], [2])
        self.assertEqual(view['conflicts'], [])
        self.assertEqual(agent.dump()['news_view'], view, 'dump ships the same view')


if __name__ == '__main__':
    unittest.main()
