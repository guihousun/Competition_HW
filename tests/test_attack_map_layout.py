"""Independent Issue46 coordinate expectations; local profile, not official PASS."""
import hashlib
import json
import threading
import unittest
from copy import deepcopy
from unittest.mock import patch
from test_baseline import ROOT
from agent import debug, map_layout
from agent.scenarios import scenario, observation
from agent.protocol import Pos, Turn, distance, station_footprint
from agent.taskworld import point_cells
from agent.recordings import RecordingJobs
from agent.server import Handler


class AttackMapLayoutTests(unittest.TestCase):
    def test_known_grid_coordinates_are_fixed_across_seeds_and_sides(self):
        expected = {'vendor': (20, 15), 'weaponShop': (25, 11),
                    'challengerTaskPoint1': (14, 17), 'challengerTaskPoint2': (16, 14),
                    'defenderTaskPoint1': (23, 17), 'defenderTaskPoint2': (26, 14)}
        for seed in (1, 7, 19):
            for side in ('challenger', 'defender'):
                with self.subTest(seed=seed, side=side):
                    state = scenario(seed, side, map_layout=map_layout.OBSERVED)
                    known = {z['neutralType']: (z['pos']['x'], 31-z['pos']['y'])
                             for z in state['mapInfo']['zones'] if z['neutralType'] in expected}
                    self.assertEqual(expected, known)
                    bases = {side: state['teamOur']['roles'][0]['pos'],
                             'defender' if side == 'challenger' else 'challenger':
                                 state['teamEnemy']['roles'][0]['pos']}
                    self.assertEqual({'challenger': {'x': 9, 'y': 22},
                                      'defender': {'x': 30, 'y': 10}}, bases)
                    grid_cells = {(p.x, 31-p.y) for p in station_footprint(Pos.load(bases['challenger']))}
                    self.assertEqual({(9, 9), (10, 9), (9, 10), (10, 10)}, grid_cells)
                    self.assertEqual((Pos(16, 17), Pos(17, 17)), point_cells('challengerTaskPoint2', Pos(16, 17)))

    def test_roles_mines_and_full_task_footprints_do_not_overlap(self):
        self.assertEqual(scenario(7, 'challenger', map_layout=map_layout.OBSERVED)['mapInfo'],
                         scenario(7, 'defender', map_layout=map_layout.OBSERVED)['mapInfo'])
        for side in ('challenger', 'defender'):
            state = scenario(7, side, map_layout=map_layout.OBSERVED)
            turn = Turn.load(state)
            occupied = set(turn.occupied_cells())
            self.assertEqual(11, len(occupied))
            for zone in state['mapInfo']['zones']:
                cells = set(point_cells(zone['neutralType'], Pos.load(zone['pos'])))
                self.assertFalse(cells & occupied, zone)
                occupied |= cells
                if zone['neutralType'] in ('stone', 'iron', 'copper'):
                    for base in (u for u in turn.ours + turn.enemies if u.kind == 'station'):
                        self.assertGreater(min(distance(Pos.load(zone['pos']), p)
                                               for p in station_footprint(base.pos)), 2)
        roles = scenario(map_layout=map_layout.OBSERVED)['teamOur']['roles']
        self.assertEqual([{'x':9,'y':22}, {'x':8,'y':21}, {'x':8,'y':23}, {'x':8,'y':22}],
                         [r['pos'] for r in roles])

    def test_unresolved_types_and_missing_roles_are_evidence_only(self):
        state = scenario(2, 'defender', map_layout=map_layout.OBSERVED)
        meta = state['_demo']['map_layout']
        self.assertFalse(meta['official_certified'])
        self.assertEqual(14, len(meta['unresolved_cells']))
        first = meta['unresolved_cells'][0]
        self.assertEqual({'mapType':23, 'grid':{'x':2,'y':7}, 'pos':{'x':2,'y':24},
                          'status':'unresolved_not_applied'}, first)
        self.assertTrue(meta['local_role_fallbacks'])
        self.assertFalse(any(z['neutralType'] in (23,24,25,'water','wall')
                             for z in state['mapInfo']['zones']))
        self.assertNotIn('_demo', observation(state))
        self.assertNotIn('map_layout', observation(state))

    def test_seeded_historical_layout_oracle_is_preserved(self):
        # Hashes captured from pre-change HEAD mapInfo/teams, not this generator.
        expected = {'challenger':'de56b0869140e06ad4bb84c26ee728702d50b72e15eae93a2f55d659fdd7a1ca',
                    'defender':'b16716fcd04b93a76957950326b2c7b7fb86aabd653f41e7a460f1b5f90564da'}
        for side, digest in expected.items():
            state = scenario(7, side)
            self.assertEqual(state, scenario(7, side, map_layout=map_layout.SEEDED))
            data = {k:state[k] for k in ('mapInfo','teamOur','teamEnemy')}
            self.assertEqual(digest, hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest())

    def test_debug_defaults_explicit_selection_and_no_import_migration(self):
        payload = debug.scenario_payload(7, 'challenger', 1)
        self.assertEqual(map_layout.OBSERVED, payload['metadata']['mapLayout']['id'])
        old = debug.scenario_payload(7, 'challenger', 1, map_layout=map_layout.SEEDED)['state']
        old['teamOur']['roles'][0]['pos'] = {'x': 6, 'y': 25}
        self.assertEqual(old['mapInfo'], debug._viewer_state(json.loads(json.dumps(old)))['mapInfo'])
        self.assertEqual({'x':6,'y':25}, debug._viewer_state(old)['teamOur']['roles'][0]['pos'])
        stats = debug.stats_payload()
        self.assertEqual(map_layout.OBSERVED, stats['mapLayoutDefault'])
        self.assertEqual(set(map_layout.LAYOUTS), set(stats['mapLayouts']))
        demo = debug.llm_scenario_payload(7, backend='scripted')
        self.assertEqual(map_layout.OBSERVED, demo['metadata']['mapLayout']['id'])
        self.assertIn('llm_fixture_override', demo['metadata']['mapLayout'])

    def test_series_cache_keys_include_layout_and_record_its_identity(self):
        debug._SERIES_CACHE.clear()
        a = debug.series_payload(3, 'challenger', 1, 1)
        b = debug.series_payload(3, 'challenger', 1, 1, map_layout=map_layout.SEEDED)
        self.assertIs(a, debug.series_payload(3, 'challenger', 1, 1))
        self.assertIsNot(a, b)
        self.assertNotEqual(a['initial']['teamOur'], b['initial']['teamOur'])
        self.assertEqual(map_layout.OBSERVED, a['metadata']['mapLayout']['id'])
        self.assertEqual(map_layout.SEEDED, b['map_layout'])

    def test_recording_jobs_forward_and_preserve_layout(self):
        done = threading.Event()
        received = {}
        def producer(*args, **kwargs):
            received.update(kwargs)
            done.set()
            return {'frames': [], 'states': [{}]}
        jobs = RecordingJobs(producer)
        first = jobs.start(map_layout=map_layout.SEEDED)
        self.assertTrue(done.wait(3))
        self.assertEqual(map_layout.SEEDED, received['map_layout'])
        self.assertEqual(map_layout.SEEDED, first['map_layout'])

    def test_invalid_layouts_rejected_before_recording_creation(self):
        for call in (lambda: scenario(map_layout='other'),
                     lambda: debug.scenario_payload(1, 'challenger', 1, map_layout='other'),
                     lambda: debug.series_payload(1, 'challenger', 1, 1, map_layout='other'),
                     lambda: debug.llm_scenario_payload(backend='scripted', map_layout='other'),
                     lambda: RecordingJobs().start(map_layout='other')):
            with self.assertRaisesRegex(ValueError, 'map layout'):
                call()

    def test_http_routes_forward_selected_layout(self):
        handler = object.__new__(Handler)
        handler.client_address = ('127.0.0.1', 1)
        handler._json = lambda *args: None
        for path, target in (('/debug/scenario', 'scenario_payload'), ('/debug/series', 'series_payload')):
            handler.path = path + '?map_layout=seeded-local-v1'
            with patch.object(debug, target, return_value={}) as call:
                handler._debug_get(path)
                self.assertEqual(map_layout.SEEDED, call.call_args.kwargs['map_layout'])
        with patch.object(debug, 'series_payload', return_value={}) as call:
            handler._debug_post('/debug/series', {'map_layout': map_layout.SEEDED})
            self.assertEqual(map_layout.SEEDED, call.call_args.kwargs['map_layout'])
        with patch.object(debug, 'llm_scenario_payload', return_value={}) as call:
            handler._debug_post('/debug/llm/scenario', {'map_layout': map_layout.SEEDED})
            self.assertEqual(map_layout.SEEDED, call.call_args.args[-1])


if __name__ == '__main__':
    unittest.main()
