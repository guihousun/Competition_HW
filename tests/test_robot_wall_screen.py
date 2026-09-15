"""User S04: a robot's ranged role attack hits the intervening wall first."""
from copy import deepcopy
import unittest
from test_robots import board, health_of
from agent.simulator import step


def with_worker(state, pos=(8, 5), side='challenger'):
    state['teamOur']['type'] = side
    for robot in state['robot']['roles']:
        robot['targetTeam'] = side
    state['teamOur']['roles'].append({'id': 10010, 'roleType': 'worker', 'health': 220,
                                    'level': 1, 'backpack': [], 'backPackCapability': 100,
                                    'pos': {'x': pos[0], 'y': pos[1]}})
    return state


def advance(state):
    return step(state, external_response={'roleCommandMap': {}})


class RobotWallScreenTests(unittest.TestCase):
    def test_wall_protects_worker_for_both_sides_and_all_robot_types(self):
        for side in ('challenger', 'defender'):
            for kind, damage in [('smallRobot', 5), ('middleRobot', 10), ('largeRobot', 20), ('bossRobot', 40)]:
                state = with_worker(board(wall_pos=(7, 5), robot_kind=kind), side=side)
                before = deepcopy(state)
                result = advance(state)
                self.assertEqual(health_of(result['state'], 'worker'), [220])
                self.assertEqual(health_of(result['state'], 'wall'), [1000 - damage])
                self.assertEqual(result['frame']['robotAttacks'][0]['building'], 40000)
                self.assertEqual(state, before)

    def test_clear_line_still_hits_worker_in_range(self):
        result = advance(with_worker(board()))
        self.assertEqual(health_of(result['state'], 'worker'), [215])

    def test_wall_two_cells_away_still_screens_range_three_attack(self):
        result = advance(with_worker(board(wall_pos=(8, 5)), pos=(9, 5)))
        self.assertEqual(health_of(result['state'], 'worker'), [220])
        self.assertEqual(health_of(result['state'], 'wall'), [995])

    def test_first_of_two_walls_is_hit_not_the_wall_behind_it(self):
        state = with_worker(board(wall_pos=(7, 5)), pos=(9, 5))
        state['teamOur']['roles'].append({'id': 40001, 'roleType': 'wall', 'health': 1000,
                                         'level': 1, 'pos': {'x': 8, 'y': 5}})
        result = advance(state)
        self.assertEqual(health_of(result['state'], 'wall'), [995, 1000])

    def test_wall_destruction_only_exposes_worker_next_round(self):
        state = with_worker(board(wall_pos=(7, 5)))
        next(r for r in state['teamOur']['roles'] if r['roleType'] == 'wall')['health'] = 5
        second = deepcopy(state['robot']['roles'][0])
        second.update(id=307101, pos={'x': 6, 'y': 6})
        state['robot']['roles'].append(second)
        first = advance(state)['state']
        self.assertEqual(health_of(first, 'worker'), [220])
        self.assertEqual(health_of(first, 'wall'), [0])
        second = advance(first)['state']
        self.assertEqual(health_of(second, 'worker'), [210])

    def test_diagonal_wall_blocks_but_wall_off_the_segment_does_not(self):
        blocked = advance(with_worker(board(wall_pos=(7, 6)), pos=(8, 7)))['state']
        self.assertEqual(health_of(blocked, 'worker'), [220])
        clear = advance(with_worker(board(wall_pos=(7, 7)), pos=(8, 5)))['state']
        self.assertEqual(health_of(clear, 'worker'), [215])
