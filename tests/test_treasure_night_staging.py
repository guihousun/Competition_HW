"""Independent defensive and permission boundaries for public trip preparation."""
from copy import deepcopy
import os
import unittest
from unittest.mock import patch
from test_coordination import unit
from agent import brain, planner
from agent.protocol import Turn, Pos, distance


def board():
    pioneer = unit(3, 'pioneer', 5, 5)
    pioneer['backpack'] = ['AcientTablet']
    return {'roundNo': 100, 'mapInfo': {'width': 41, 'height': 32,
        'zones': [{'pos': {'x': 9, 'y': 7}, 'neutralType': 'stone'}]},
        'teamOur': {'type': 'challenger', 'goldNum': 75, 'roles': [
            unit(1, 'worker', 5, 7), unit(2, 'worker', 7, 7), pioneer,
            unit(40, 'rocket', 5, 8, 1000), unit(41, 'rocket', 7, 8, 1000),
            unit(42, 'rocket', 5, 4, 1000), unit(13, 'station', 3, 9, 1500)]},
        'teamEnemy': {'roles': []}, 'robot': {'roles': []}, 'weaponShopList': [], 'vendorShopList': []}


NOTES = {'preparable': True, 'taken': False, 'site': {'x': 15, 'y': 5},
         'items': ['AcientTablet'], 'opensAt': None, 'closesAt': None}


class TreasureNightStagingTests(unittest.TestCase):
    def test_whole_planner_preserves_staging_against_later_task_hold(self):
        with patch.dict(os.environ, {brain.WORLD_AGENT_ENV: 'off', brain.TASK_AGENT_ENV: 'off'}), \
             patch.object(brain, '_treasure_notes', return_value=NOTES), \
             patch.object(brain, '_task_step', side_effect=AssertionError('must not start a competing task')), \
             patch.object(brain, '_task_walk', return_value=(True, None)):
            response = brain.plan_for_state(board(), planner.PlannerState(), judge_tasks=False)
        self.assertEqual(response.commands['3']['action'], 'move')
        self.assertEqual(brain.decision_report()['treasure_preparation']['phase'], 'moving')

    def run_night(self, payload, notes=None):
        turn, commands = Turn.load(payload), {}
        with patch.object(brain, '_treasure_notes', return_value=NOTES if notes is None else notes):
            brain._night(turn, commands, payload)
        return commands

    def proposal(self, payload, notes=None):
        turn = Turn.load(payload)
        with patch.object(brain, '_treasure_notes', return_value=NOTES if notes is None else notes):
            return brain._treasure_night_staging(turn, payload, brain._tower_pairs(turn))

    def test_two_guards_stay_ready_and_pioneer_holds_at_four_cell_boundary(self):
        payload = board()
        before = deepcopy(payload)
        first = self.run_night(payload)
        self.assertEqual(payload, before, 'planning cannot mutate the observation')
        self.assertEqual(first[3]['action'], 'move')
        self.assertNotIn(1, first)
        self.assertNotIn(2, first, 'ordinary worker scouting must wait during pioneer preparation')
        for _ in range(8):
            commands = self.run_night(payload)
            self.assertNotIn(1, commands)
            self.assertNotIn(2, commands)
            if 3 in commands:
                self.assertEqual(commands[3]['action'], 'move')
                target = commands[3]['targetPos'][0]
                self.assertLessEqual(distance(Pos.load(target), Pos(5, 4)), 4)
                payload['teamOur']['roles'][2]['pos'] = target
            payload['roundNo'] += 1
        self.assertEqual(payload['teamOur']['roles'][2]['pos']['x'], 9)
        self.assertNotIn(3, self.run_night(payload), 'holding is internal, not a fake move or a forced return')

    def test_absent_guard_returns_before_pioneer_leaves(self):
        payload = board()
        payload['teamOur']['roles'][1]['pos'] = {'x': 10, 'y': 7}
        commands = self.run_night(payload)
        self.assertNotIn(3, commands)
        self.assertEqual(commands[2]['action'], 'move')
        self.assertLess(commands[2]['targetPos'][0]['x'], 10)
        self.assertFalse(self.proposal(payload)['hold'])

    def test_new_threat_aborts_hold_and_restores_defence(self):
        payload = board()
        payload['teamOur']['roles'][2]['pos'] = {'x': 9, 'y': 5}
        payload['robot']['roles'] = [{'id': 91, 'pos': {'x': 40, 'y': 0}, 'health': 40}]
        self.assertIsNone(self.proposal(payload))
        command = self.run_night(payload)[3]
        self.assertEqual(command['action'], 'move')
        self.assertLess(command['targetPos'][0]['x'], 9)

    def test_missing_observation_task_materials_and_uncertainty_block_staging(self):
        for mode in ('unknown', 'first_night', 'day', 'task', 'material', 'health', 'guard', 'conflict'):
            payload = board()
            notes = deepcopy(NOTES)
            if mode == 'unknown':
                payload.pop('robot')
            elif mode == 'first_night':
                payload['roundNo'] = 71
            elif mode == 'day':
                payload['roundNo'] = 10
            elif mode == 'task':
                payload['phaseTask'] = '仍在执行的任务'
            elif mode == 'material':
                payload['teamOur']['roles'][2]['backpack'] = []
            elif mode == 'health':
                payload['teamOur']['roles'][-1]['health'] = 999
            elif mode == 'guard':
                payload['teamOur']['roles'][0]['health'] = 0
                payload['teamOur']['roles'][1]['health'] = 0
            elif mode == 'conflict':
                notes['preparable'] = False
            self.assertIsNone(self.proposal(payload, notes), mode)

    def test_confirmed_worker_loss_allows_one_ready_guard_on_a_cleared_field(self):
        payload = board()
        payload['teamOur']['roles'][0]['health'] = 0
        result = self.proposal(payload)
        self.assertTrue(result['confirmed_reduced_crew'])
        self.assertEqual(result['guard_count'], 1)
        self.assertTrue(result['hold'])
        commands = self.run_night(payload)
        self.assertEqual(commands[3]['action'], 'move')
        self.assertNotIn(2, commands, 'surviving guard stays at its gun')
        self.assertLessEqual(distance(Pos.load(commands[3]['targetPos'][0]), Pos(5, 4)), 4)

    def test_missing_worker_or_unknown_health_is_not_a_confirmed_loss(self):
        for mode in ('missing', 'unknown', 'boolean'):
            payload = board()
            if mode == 'missing':
                payload['teamOur']['roles'].pop(0)
            elif mode == 'unknown':
                payload['teamOur']['roles'][0].pop('health')
                with self.assertRaises(KeyError):
                    self.proposal(payload)  # protocol rejects missing required health
                continue
            else:
                payload['teamOur']['roles'][0]['health'] = False
            self.assertIsNone(self.proposal(payload), mode)

    def test_reduced_crew_waits_for_guard_and_aborts_on_any_new_robot(self):
        payload = board()
        payload['teamOur']['roles'][0]['health'] = 0
        payload['teamOur']['roles'][1]['pos'] = {'x': 10, 'y': 7}
        self.assertFalse(self.proposal(payload)['hold'])
        commands = self.run_night(payload)
        self.assertNotIn(3, commands)
        self.assertEqual(commands[2]['action'], 'move')
        payload['teamOur']['roles'][1]['pos'] = {'x': 7, 'y': 7}
        payload['teamOur']['roles'][2]['pos'] = {'x': 9, 'y': 5}
        payload['robot']['roles'] = [{'id': 91, 'pos': {'x': 40, 'y': 0},
                                     'health': 40, 'targetTeam': 'defender'}]
        self.assertIsNone(self.proposal(payload))
        self.assertLess(self.run_night(payload)[3]['targetPos'][0]['x'], 9)

    def test_geometric_radius_does_not_bypass_a_blocked_return_route(self):
        payload = board()
        payload['teamOur']['roles'][2]['pos'] = {'x': 9, 'y': 5}
        # Full-height barrier lies between the pioneer and the gun. A small
        # coordinate distance alone cannot certify a safe return.
        payload['mapInfo']['zones'] += [{'pos': {'x': 7, 'y': y}, 'neutralType': 'water'} for y in range(32)]
        self.assertIsNone(self.proposal(payload))

    def test_unreachable_guard_opens_one_own_wall_then_uses_the_new_route(self):
        payload = board()
        payload['teamOur']['roles'][0]['pos'] = {'x': 3, 'y': 6}
        wall_cells = [(4, 7), (4, 8), (4, 9), (5, 7), (5, 9), (6, 7), (6, 8), (6, 9)]
        payload['teamOur']['roles'] += [unit(100 + i, 'wall', x, y, 1000) for i, (x, y) in enumerate(wall_cells)]
        commands = self.run_night(payload)
        self.assertEqual(commands[1], {'action': 'remove', 'targetPos': [{'x': 4, 'y': 7}]})
        self.assertNotIn(3, commands, 'pioneer waits while the access is being opened')
        payload['teamOur']['roles'] = [r for r in payload['teamOur']['roles'] if r['id'] != 100]
        commands = self.run_night(payload)
        self.assertEqual(commands[1], {'action': 'move', 'targetPos': [{'x': 4, 'y': 7}]})
        self.assertFalse(any(c['action'] == 'remove' for c in commands.values()))
        payload['robot']['roles'] = [{'id': 91, 'pos': {'x': 40, 'y': 0}, 'health': 40}]
        self.assertFalse(any(c['action'] == 'remove' for c in self.run_night(payload).values()))

    def test_projected_route_vacates_only_its_own_previous_position(self):
        payload = board()
        role = payload['teamOur']['roles'][2]
        role['pos'] = {'x': 2, 'y': 1}
        payload['mapInfo']['zones'] = [{'pos': {'x': x, 'y': y}, 'neutralType': 'water'}
            for x in range(41) for y in range(32) if y != 1]
        turn = Turn.load(payload)
        self.assertEqual(brain._RouteCost(turn, turn.pioneer())(Pos(3, 1), Pos(1, 1)), 2)
        payload['teamOur']['roles'][0]['pos'] = {'x': 1, 'y': 1}
        turn = Turn.load(payload)
        self.assertGreaterEqual(brain._RouteCost(turn, turn.pioneer())(Pos(3, 1), Pos(1, 1)), 10 ** 6)


if __name__ == '__main__':
    unittest.main()
