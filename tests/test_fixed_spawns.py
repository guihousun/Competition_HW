"""Issue #12/#19: fixed local spawn slots and auditable wave profiles.

These tests hard-code the expected counts from the Issue #19 attachment
(``default.xlsx`` Sheet1 B2:H5 = 35/45/58/69/75/86/91 over days 1..7) instead of
re-deriving them from the implementation, so the table is checked independently.
"""
from copy import deepcopy
import json
import unittest

from test_robots import board
from agent.scenarios import scenario, configure_spawns, prepare_round, observation
from agent.scenarios import spawn_row_band, SPAWN_MAX_PER_COLUMN
from agent import wave_data
from agent.debug import series_payload
from agent.recordings import RecordingJobs

OBSERVED_TOTALS = [35, 45, 58, 69, 75, 86, 91]
# Days 8–10 have no attachment rows: the local continuation adds 5 small per
# night to day 7 (96/101/106), a documented assumption, not a measured value.
EXTRAPOLATED_TOTALS = [96, 101, 106]
OBSERVED_TYPES = {
    1: {'smallRobot': 30, 'middleRobot': 5, 'largeRobot': 0, 'bossRobot': 0},
    2: {'smallRobot': 35, 'middleRobot': 10, 'largeRobot': 0, 'bossRobot': 0},
    3: {'smallRobot': 40, 'middleRobot': 15, 'largeRobot': 3, 'bossRobot': 0},
    4: {'smallRobot': 45, 'middleRobot': 20, 'largeRobot': 4, 'bossRobot': 0},
    5: {'smallRobot': 50, 'middleRobot': 20, 'largeRobot': 4, 'bossRobot': 1},
    6: {'smallRobot': 55, 'middleRobot': 25, 'largeRobot': 5, 'bossRobot': 1},
    7: {'smallRobot': 57, 'middleRobot': 27, 'largeRobot': 5, 'bossRobot': 2},
}
EXTRAPOLATED_TYPES = {
    day: {'smallRobot': 62, 'middleRobot': 27, 'largeRobot': 5, 'bossRobot': 2}
    for day in (8, 9, 10)
}
EXTRAPOLATED_TYPES[9] = {'smallRobot': 67, 'middleRobot': 27, 'largeRobot': 5, 'bossRobot': 2}
EXTRAPOLATED_TYPES[10] = {'smallRobot': 72, 'middleRobot': 27, 'largeRobot': 5, 'bossRobot': 2}
TIER_RANK = {kind: rank for rank, kind in enumerate(wave_data.TIER_ORDER)}


def kinds_of(born):
    return [record['kind'] for record in born]


def counts_of(kinds):
    return {kind: kinds.count(kind) for kind in wave_data.TIER_ORDER}


class ObservedWaveTableTests(unittest.TestCase):
    def test_first_seven_days_match_the_attachment_totals_and_types(self):
        for side in ('challenger', 'defender'):
            for day in range(1, 8):
                with self.subTest(side=side, day=day):
                    state = scenario(7, side)
                    state['roundNo'] = (day - 1) * 130 + 71
                    born = prepare_round(state, [])
                    self.assertEqual(OBSERVED_TOTALS[day - 1], len(born))
                    self.assertEqual(OBSERVED_TYPES[day], counts_of(kinds_of(born)))

    def test_days_eight_to_ten_are_marked_unobserved_and_extrapolate_five_small(self):
        for side in ('challenger', 'defender'):
            for day in (8, 9, 10):
                with self.subTest(side=side, day=day):
                    state = scenario(7, side)
                    events = []
                    state['roundNo'] = (day - 1) * 130 + 71
                    born = prepare_round(state, events)
                    self.assertEqual(EXTRAPOLATED_TOTALS[day - 8], len(born))
                    self.assertEqual(EXTRAPOLATED_TYPES[day], counts_of(kinds_of(born)))
                    wave = state['_demo']['wave']
                    self.assertFalse(wave['observed'])
                    self.assertEqual(7, wave['source_day'])
                    self.assertTrue(wave['extrapolation'])
                    self.assertEqual(5, wave['local_future_small_per_day'])
                    self.assertEqual(5 * (day - 7), wave['small_added'])
                    # No raw value is invented for an unobserved day; the day-7
                    # starting point is kept separately.
                    self.assertEqual({kind: None for kind in wave_data.TIER_ORDER},
                                     wave['raw_counts'])
                    self.assertEqual(wave_data.OBSERVED_TYPES[7], wave['seed_counts_day7'])
                    self.assertTrue(any('未观测' in line and '多 5 只小型' in line for line in events),
                                    events)
                    self.assertFalse(any('实测' in line for line in events if '未观测' not in line),
                                     'an unobserved night must not be described as observed')

    def test_blank_attachment_types_are_zeroed_only_with_an_explicit_note(self):
        state = scenario(7)
        events = []
        state['roundNo'] = 71
        prepare_round(state, events)
        self.assertIn('largeRobot', state['_demo']['wave']['blank_types_zeroed'])
        self.assertTrue(any('空白' in line and '按 0 解释' in line for line in events), events)

    def test_pressure_never_multiplies_the_observed_profile(self):
        for pressure in (1, 2, 3):
            with self.subTest(pressure=pressure):
                state = scenario(7, 'challenger', pressure)
                state['roundNo'] = 71
                self.assertEqual(35, len(prepare_round(state, [])))

    def test_unknown_profile_is_rejected(self):
        for bad in ('official', '', 'pressure'):
            with self.subTest(profile=bad), self.assertRaises(ValueError):
                scenario(1, 'challenger', profile=bad)
            with self.subTest(profile=bad), self.assertRaises(ValueError):
                series_payload(1, 'challenger', 1, 5, profile=bad)
            with self.subTest(profile=bad), self.assertRaises(ValueError):
                RecordingJobs().start(profile=bad)

    def test_pressure_profile_keeps_the_old_growth_experiment(self):
        counts = []
        for night in range(1, 11):
            state = scenario(7, 'challenger', 3, profile='local-pressure')
            state['roundNo'] = (night - 1) * 130 + 71
            counts.append(len(prepare_round(state, [])))
        self.assertEqual([4, 7, 10, 13, 16, 19, 22, 25, 28, 31], counts)


class FixedPoolTests(unittest.TestCase):
    def test_issue48_observed_first_column_and_provisional_mirror(self):
        from agent.map_layout import OBSERVED
        for side, expected_x in (('challenger', 18), ('defender', 22)):
            state = scenario(7, side, map_layout=OBSERVED)
            layout = state['_demo']['spawn_layout']
            self.assertEqual(expected_x, layout['center']['x'])
            self.assertEqual('pk-696563', layout['source_replay'])
            self.assertIn('provisional', layout['source'])
            state['roundNo'] = 71
            born = prepare_round(state, [])
            self.assertEqual(expected_x, born[0]['pos']['x'])
            self.assertEqual(35, len(born))

    def test_saved_geometry_is_not_rewritten_by_new_default(self):
        state = scenario(7)
        old = [{'x': 27, 'y': 15}, {'x': 27, 'y': 16}]
        configure_spawns(state, old)
        state['_demo']['spawn_layout']['custom'] = False
        state['roundNo'] = 71
        born = prepare_round(state, [])
        self.assertEqual(old, [r['pos'] for r in born])

    def test_ten_nights_reuse_prefixes_of_one_fixed_pool_for_both_sides(self):
        for profile, pressure, counts in [('observed-seven-days', 3, OBSERVED_TOTALS + EXTRAPOLATED_TOTALS),
                                          ('local-pressure', 3, [4, 7, 10, 13, 16, 19, 22, 25, 28, 31])]:
            for side, first_x in [('challenger', 18), ('defender', 22)]:
                with self.subTest(profile=profile, side=side):
                    state = scenario(90317, side, pressure, profile=profile)
                    layout = deepcopy(state['_demo']['spawn_layout'])
                    self.assertEqual(layout['center']['x'], first_x)
                    previous = []
                    for night in range(1, 11):
                        state['robot']['roles'] = []
                        state['roundNo'] = (night - 1) * 130 + 71
                        born = prepare_round(state, [])
                        positions = [r['pos'] for r in born]
                        self.assertEqual(counts[night - 1], len(positions))
                        self.assertEqual(positions[:len(previous)], previous)
                        self.assertTrue(all(p in layout['slots'] for p in positions))
                        self.assertEqual(state['_demo']['spawn_layout'], layout)
                        self.assertTrue(all(r['targetTeam'] == side for r in state['robot']['roles']))
                        previous = positions

    def test_default_pool_is_a_fixed_column_array_with_at_most_nine_per_column(self):
        for side in ('challenger', 'defender'):
            for seed in range(20):
                with self.subTest(side=side, seed=seed):
                    layout = scenario(seed, side)['_demo']['spawn_layout']
                    self.assertFalse(layout['custom'])
                    self.assertEqual(SPAWN_MAX_PER_COLUMN, layout['max_per_column'])
                    # The pool must hold day 10's extrapolated 106 monsters.
                    self.assertGreaterEqual(layout['pool_size'], 106)
                    self.assertEqual(106, layout['min_pool_size'])
                    slots = layout['slots']
                    self.assertEqual(len(slots), len({(p['x'], p['y']) for p in slots}))
                    for slot in slots:
                        self.assertTrue(0 <= slot['x'] < 41 and 0 <= slot['y'] < 32)
                    by_column = {}
                    for slot in slots:
                        by_column.setdefault(slot['x'], []).append(slot['y'])
                    self.assertTrue(all(len(rows) <= SPAWN_MAX_PER_COLUMN
                                        for rows in by_column.values()), by_column)

    def test_edge_base_keeps_a_full_nine_row_column(self):
        self.assertEqual(list(range(0, 9)), sorted(spawn_row_band(0, 32)))
        self.assertEqual(9, len(spawn_row_band(0, 32)))
        self.assertEqual(9, len(spawn_row_band(31, 32)))
        self.assertEqual(set(range(23, 32)), set(spawn_row_band(31, 32)))
        self.assertEqual(8, len(spawn_row_band(5, 8)), 'a map shorter than 9 uses its real height')

    def test_edge_base_pool_still_fits_the_largest_wave(self):
        for station_y in (0, 31):
            for side in ('challenger', 'defender'):
                with self.subTest(station_y=station_y, side=side):
                    state = scenario(7, side)
                    station = state['teamOur']['roles'][0]
                    station['pos']['y'] = station_y
                    # Move the workers out of the spawn band so the pool is not
                    # reduced by unrelated occupancy.
                    for unit in state['teamOur']['roles'][1:]:
                        unit['pos'] = {'x': 20, 'y': 20}
                    layout = configure_spawns(state)
                    self.assertGreaterEqual(layout['pool_size'], 106)
                    columns = {}
                    for slot in layout['slots']:
                        columns.setdefault(slot['x'], []).append(slot['y'])
                    self.assertTrue(all(len(rows) <= 9 for rows in columns.values()), columns)

    def test_higher_tiers_take_the_nearer_slots_even_when_slots_are_occupied(self):
        state = scenario(90317, 'challenger')
        slots = deepcopy(state['_demo']['spawn_layout']['slots'])
        # Occupy two of the nearest slots; the rest must shift up without a lower
        # tier overtaking a higher one.
        state['teamOur']['roles'][1]['pos'] = slots[0]
        state['teamOur']['roles'][2]['pos'] = slots[1]
        state['roundNo'] = 7 * 130 + 71
        born = prepare_round(state, [])
        kinds = kinds_of(born)
        self.assertEqual(96, len(kinds))
        self.assertEqual(kinds, sorted(kinds, key=lambda kind: TIER_RANK[kind]))
        self.assertEqual(slots[2], born[0]['pos'])
        self.assertEqual('bossRobot', born[0]['kind'])

    def test_occupied_slot_is_skipped_without_spreading_outside_fixed_pool(self):
        state = scenario(1, profile='local-pressure')
        slots = deepcopy(state['_demo']['spawn_layout']['slots'])
        state['teamOur']['roles'][1]['pos'] = slots[0]
        state['roundNo'] = 71
        born = prepare_round(state, [])
        self.assertEqual([r['pos'] for r in born], slots[1:3])

    def test_explicit_points_override_provisional_positions_and_shortage_is_visible(self):
        state = scenario(1, profile='local-pressure')
        chosen = [state['_demo']['spawn_layout']['slots'][5]]
        configure_spawns(state, chosen)
        state['roundNo'] = 71
        events = []
        born = prepare_round(state, events)
        self.assertEqual([r['pos'] for r in born], chosen)
        self.assertTrue(state['_demo']['spawn_layout']['custom'])
        self.assertEqual(state['_demo']['spawn_shortfall'], 1)
        self.assertTrue(any('少生成1' in e for e in events))

    def test_explicit_points_keep_caller_order_when_more_than_one_is_given(self):
        state = scenario(1, 'challenger', 3, profile='local-pressure')
        pool = state['_demo']['spawn_layout']['slots']
        chosen = [pool[9], pool[3], pool[6]]
        configure_spawns(state, chosen)
        state['roundNo'] = 71
        born = prepare_round(state, [])
        self.assertEqual([r['pos'] for r in born], chosen)

    def test_summoned_extras_stack_and_keep_tier_order(self):
        state = scenario(1, 'challenger', profile='local-pressure')
        state['_demo']['summon_load'] = {'bossRobot': 2, 'middleRobot': 1}
        state['roundNo'] = 71
        born = prepare_round(state, [])
        kinds = kinds_of(born)
        self.assertEqual(5, len(kinds))  # 2 base + 3 summoned
        self.assertEqual(kinds, sorted(kinds, key=lambda kind: TIER_RANK[kind]))
        self.assertEqual(2, kinds.count('bossRobot'))
        self.assertEqual(2, kinds.count('smallRobot'))

    def test_spawn_configuration_does_not_enter_competition_observation(self):
        state = scenario(3)
        before = observation(state)
        altered = deepcopy(state)
        altered['_demo']['spawn_layout']['slots'] = [{'x': 20, 'y': 20}]
        self.assertEqual(observation(altered), before)
        self.assertNotIn('spawn_layout', json.dumps(before))
        self.assertNotIn('wave_source', json.dumps(before))

    def test_imported_official_snapshot_does_not_spawn_local_robots(self):
        state = board()
        state['roundNo'] = 71
        self.assertEqual(prepare_round(state, []), [])

    def test_invalid_or_duplicate_configured_cells_are_rejected(self):
        state = scenario(1)
        for invalid in ([], [{'x': True, 'y': 1}], [{'x': 41, 'y': 1}],
                        [{'x': 20, 'y': 20}, {'x': 20, 'y': 20}]):
            with self.assertRaises(ValueError):
                configure_spawns(state, invalid)


class MigrationAndRecordingTests(unittest.TestCase):
    def test_agent_demo_keeps_selected_local_pressure_and_observed_counts(self):
        from agent.debug import llm_scenario_payload
        for profile, expected in [('local-pressure', 4), ('observed-seven-days', 35)]:
            state = llm_scenario_payload(7, 'defender', 'world', 'scripted',
                                         profile=profile, pressure=3)['state']
            self.assertEqual(state['_demo']['pressure'], 3)
            self.assertEqual(state['_demo']['profile'], profile)
            state['roundNo'] = 71
            self.assertEqual(len(prepare_round(state, [])), expected)

    def test_legacy_snapshot_without_profile_migrates_to_the_old_experiment_with_a_notice(self):
        state = scenario(7, 'challenger', 3)
        state['_demo'].pop('profile', None)
        state['_demo'].pop('spawn_layout', None)
        state['roundNo'] = 71
        events = []
        born = prepare_round(state, events)
        self.assertEqual(4, len(born), 'a legacy snapshot keeps its day*pressure+1 waves')
        self.assertEqual('local-pressure', state['_demo']['profile'])
        self.assertTrue(any('旧快照' in line and '未改标为附件实测' in line for line in events), events)

    def test_new_recording_saves_its_profile(self):
        recording = series_payload(1, 'challenger', 1, 5, profile='local-pressure')
        self.assertEqual('local-pressure', recording['profile'])
        self.assertEqual('local-pressure', recording['initial']['_demo']['profile'])
        default = series_payload(1, 'challenger', 1, 5)
        self.assertEqual('observed-seven-days', default['profile'])


if __name__ == '__main__':
    unittest.main()
