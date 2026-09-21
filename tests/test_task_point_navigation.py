"""R02/R07 task-point footprints agree before and after route planning."""
import json
import os
import sys
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'Demo/CoreGeek/src'))
from agent import brain, planner, taskworld
from agent.coordination import reconcile
from agent.grid import next_step
from agent.protocol import Pos, Turn, move_command


def request(zones):
    return {'roundNo': 12, 'mapInfo': {'width': 41, 'height': 32, 'zones': zones},
            'teamOur': {'type': 'challenger', 'goldNum': 75, 'roles': [
                {'id': 1, 'roleType': 'pioneer', 'health': 200, 'pos': {'x': 16, 'y': 18},
                 'backPackCapability': 40, 'backpack': []}]},
            'teamEnemy': {'roles': []}, 'robot': {'roles': []}}


def zone(x, y, kind='challengerTaskPoint2'):
    return {'neutralType': kind, 'pos': {'x': x, 'y': y}}


class TaskPointNavigationTests(unittest.TestCase):
    def test_single_anchor_blocks_both_cells_without_changing_payload_or_zones(self):
        payload = request([zone(16, 17)])
        before = deepcopy(payload)
        turn = Turn.load(payload)
        self.assertEqual(turn.zones, {Pos(16, 17): 'challengerTaskPoint2'})
        for cell in (Pos(16, 17), Pos(17, 17)):
            self.assertFalse(turn.land(cell))
            self.assertIn(cell, turn.blocked(turn.pioneer()))
        self.assertTrue(turn.land(Pos(18, 17)))
        self.assertEqual(payload, before)

    def test_explicit_cells_do_not_grow_a_third_cell_in_either_order(self):
        for kind in ('challengerTaskPoint2', 'defenderTaskPoint2'):
            for xs in ((16, 17), (17, 16)):
                with self.subTest(kind=kind, xs=xs):
                    payload = request([zone(x, 17, kind) for x in xs])
                    turn = Turn.load(payload)
                    self.assertEqual(turn.neutral_cells(), {Pos(16, 17), Pos(17, 17)})
                    self.assertTrue(turn.land(Pos(18, 17)))
                    # The final filter used to expand the second explicit cell again.
                    payload['teamOur']['roles'][0]['pos'] = {'x': 18, 'y': 18}
                    turn = Turn.load(payload)
                    cmd = {1: move_command(Pos(18, 17))}
                    self.assertEqual(reconcile(turn, payload, cmd), cmd)
                    self.assertEqual(taskworld._zone_lookup(payload)[kind], Pos(16, 17))
                    self.assertEqual(taskworld.point_cells(kind, Pos(16, 17)), (Pos(16, 17), Pos(17, 17)))

    def test_unknown_and_single_cell_zones_keep_their_published_footprint(self):
        for kind in ('futureTaskPoint2', 'challengerTaskPoint1', 'vendor', 'weaponShop', 'stone'):
            with self.subTest(kind=kind):
                turn = Turn.load(request([zone(16, 17, kind)]))
                self.assertFalse(turn.land(Pos(16, 17)))
                self.assertTrue(turn.land(Pos(17, 17)))
        turn = Turn.load(request([zone(16, 17, 'land')]))
        self.assertTrue(turn.land(Pos(16, 17)))

    def test_edge_footprint_never_wraps_or_expands_outside_map(self):
        turn = Turn.load(request([zone(40, 31)]))
        self.assertEqual(turn.neutral_cells(), {Pos(40, 31)})
        self.assertFalse(turn.land(Pos(41, 31)))
        self.assertTrue(turn.land(Pos(0, 30)))
        turn = Turn.load(request([zone(0, 0)]))
        self.assertEqual(turn.neutral_cells(), {Pos(0, 0), Pos(1, 0)})

    def test_ambiguous_nonadjacent_entries_keep_conservative_second_cells(self):
        for other in (zone(20, 17), zone(17, 18)):
            with self.subTest(other=other):
                turn = Turn.load(request([zone(16, 17), other]))
                self.assertFalse(turn.land(Pos(17, 17)))
                self.assertFalse(turn.land(Pos(other['pos']['x']+1, other['pos']['y'])))

    def test_turn_is_a_snapshot_and_replacement_rebuilds_occupancy(self):
        payload = request([zone(16, 17)])
        turn = Turn.load(payload)
        payload['mapInfo']['zones'].clear()
        self.assertFalse(turn.land(Pos(17, 17)))
        updated = replace(turn, zones={Pos(20, 17): 'defenderTaskPoint2'})
        self.assertTrue(updated.land(Pos(17, 17)))
        self.assertFalse(updated.land(Pos(21, 17)))

    def test_final_filter_still_rejects_both_task_cells(self):
        payload = request([zone(16, 17)])
        turn = Turn.load(payload)
        for cell in (Pos(16, 17), Pos(17, 17)):
            with self.subTest(cell=cell):
                self.assertEqual(reconcile(turn, payload, {1: move_command(cell)}), {})

    def test_route_goes_around_second_cell_and_survives_final_filter(self):
        payload = request([zone(16, 17)])
        turn = Turn.load(payload)
        step = next_step(turn, turn.pioneer(), Pos(20, 16))
        self.assertIsNotNone(step)
        self.assertNotIn(step, (Pos(16, 17), Pos(17, 17)))
        cmd = {1: move_command(step)}
        self.assertEqual(reconcile(turn, payload, cmd), cmd)

    def test_original_round12_and55_planner_now_emits_executable_pioneer_move(self):
        data = json.loads((ROOT / 'tests/fixtures/navigation/task-point-navigation-r12-r55.json').read_text(encoding='utf8'))
        for case in data['cases']:
            with self.subTest(round=case['round']):
                payload = deepcopy(case['payload'])
                self.assertNotIn('_demo', payload)
                original_map = deepcopy(payload['mapInfo'])
                state = planner.PlannerState.load(deepcopy(case['planner']))
                with patch.dict(os.environ, {brain.TASK_AGENT_ENV: 'off', brain.WORLD_AGENT_ENV: 'off', brain.ROUTER_ENV: 'off'}):
                    response = brain.plan_for_state(payload, state, judge_tasks=False)
                command = response.commands.get('10011')
                self.assertIsNotNone(command)
                self.assertEqual(command['action'], 'move')
                target = command['targetPos'][0]
                # Independent coordinates of the blocked two-cell point.
                self.assertNotIn(target, ({'x': 16, 'y': 17}, {'x': 17, 'y': 17}))
                self.assertEqual(max(abs(target['x']-16), abs(target['y']-18)), 1)
                turn = Turn.load(payload)
                self.assertTrue(turn.land(Pos.load(target)))
                self.assertNotIn(Pos.load(target), turn.blocked(turn.pioneer()))
                commands = {int(uid): cmd for uid, cmd in response.commands.items()}
                self.assertEqual(reconcile(turn, payload, commands)[10011], command)
                self.assertEqual(payload['mapInfo'], original_map)


if __name__ == '__main__':
    unittest.main()
