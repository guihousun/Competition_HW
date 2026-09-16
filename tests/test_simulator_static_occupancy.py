"""Independent R02 collision expectations, not an engine-generated oracle."""
import hashlib
import json
from pathlib import Path
import unittest

from test_baseline import fixture
from agent.protocol import Pos
from agent.simulator import resolve_moves, step


def unit(uid, kind, x, y, health=220):
    return {'id': uid, 'roleType': kind, 'pos': {'x': x, 'y': y},
            'health': health, 'level': 1, 'cooldown': 0, 'backpack': []}


def scene():
    state = fixture()
    state['roundNo'] = 1
    state['mapInfo']['zones'] = []
    state['teamOur']['roles'] = [unit(301, 'station', 3, 4, 1500),
                                  unit(601, 'worker', 20, 20),
                                  unit(607, 'worker', 22, 20),
                                  unit(777, 'pioneer', 22, 22)]
    state['teamEnemy']['roles'] = []
    state['robot']['roles'] = []
    return state


def move(x, y):
    return {'action': 'move', 'targetPos': [{'x': x, 'y': y}]}


def settle(state, commands):
    return step(state, external_response={'roleCommandMap': commands})['state']


def position(state, uid=601):
    return next(u['pos'] for u in state['teamOur']['roles'] if u['id'] == uid)


class StaticOccupancyTests(unittest.TestCase):
    def assert_blocked(self, state, target=(21, 20)):
        before = position(state)
        after = settle(state, {'601': move(*target)})
        self.assertEqual(position(after), before)
        self.assertIs(after['lastRoundRoleActionResults']['601'], False)

    def test_stationary_role_enemy_building_and_robot(self):
        for side, kind in [('teamOur', 'worker'), ('teamOur', 'pioneer'),
                           ('teamOur', 'wall'), ('teamOur', 'railgun'),
                           ('teamOur', 'rocket'), ('teamOur', 'gatling'),
                           ('teamEnemy', 'worker'), ('teamEnemy', 'wall'),
                           ('teamEnemy', 'railgun'), ('robot', 'small')]:
            with self.subTest(side=side, kind=kind):
                state = scene()
                state[side]['roles'].append(unit(901, kind, 21, 20))
                self.assert_blocked(state)

    def test_every_base_footprint_cell_blocks_for_either_team(self):
        for side in ('teamOur', 'teamEnemy'):
            for target in ((20, 20), (21, 20), (20, 19), (21, 19)):
                with self.subTest(side=side, target=target):
                    state = scene()
                    state['teamOur']['roles'][1]['pos'] = {
                        'x': 19 if target[0] == 20 else 22,
                        'y': 21 if target[1] == 20 else 18}
                    state[side]['roles'].append(unit(902, 'station', 20, 20, 1500))
                    self.assert_blocked(state, target)

    def test_neutral_and_mines_including_task_point_second_cell(self):
        for kind in ('vendor', 'weaponShop', 'stone', 'iron', 'copper',
                     'challengerTaskPoint1', 'defenderTaskPoint1',
                     'challengerTaskPoint2', 'defenderTaskPoint2'):
            for offset in (range(2) if kind.endswith('TaskPoint2') else range(1)):
                with self.subTest(kind=kind, offset=offset):
                    state = scene()
                    state['teamOur']['roles'][1]['pos'] = {'x': 21, 'y': 21}
                    state['mapInfo']['zones'] = [{'pos': {'x': 21-offset, 'y': 20},
                                                 'neutralType': kind}]
                    self.assert_blocked(state)

    def test_dead_unit_does_not_block_or_erase_live_cooccupant(self):
        state = scene()
        state['teamOur']['roles'].append(unit(901, 'wall', 21, 20, 0))
        after = settle(state, {'601': move(21, 20)})
        self.assertEqual(position(after), {'x': 21, 'y': 20})
        state['teamOur']['roles'].append(unit(902, 'railgun', 21, 20, 1000))
        self.assert_blocked(state)

    def test_diagonal_between_obstacles_remains_legal(self):
        state = scene()
        state['teamOur']['roles'] += [unit(901, 'wall', 21, 20), unit(902, 'wall', 20, 21)]
        after = settle(state, {'601': move(21, 21)})
        self.assertEqual(position(after), {'x': 21, 'y': 21})

    def test_new_building_also_blocks_a_move(self):
        state = scene()
        # Base spans x=3..4,y=3..4. (6,4) has wall radius 2; both
        # builders start on empty adjacent cells, not on the future wall.
        state['teamOur']['roles'][1]['pos'] = {'x': 6, 'y': 5}
        state['teamOur']['roles'][2]['pos'] = {'x': 7, 'y': 5}
        state['teamOur']['roles'][2]['backpack'] = ['stone']
        commands = {'601': move(6, 4), '607': {'action': 'build', 'name': 'wall',
                                              'targetPos': [{'x': 6, 'y': 4}]}}
        after = settle(state, commands)
        self.assertTrue(after['lastRoundRoleActionResults']['607'])
        self.assertFalse(after['lastRoundRoleActionResults']['601'])

    def test_native_round742_rejects_live_railgun_cell(self):
        path = Path(__file__).parent/'fixtures/native_survival_r742_public.json'
        record = json.loads(path.read_text(encoding='utf-8'))
        state = record['observation']
        canonical = json.dumps(state, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), record['observation_hash'])
        gun = next(u for u in state['teamOur']['roles'] if u['id'] == 10032)
        self.assertEqual(gun['pos'], {'x': 9, 'y': 24})
        self.assertEqual(gun['health'], 1000)
        self.assertEqual(position(state, 10010), {'x': 9, 'y': 25})
        after = settle(state, record['response']['roleCommandMap'])
        self.assertEqual(position(after, 10010), {'x': 9, 'y': 25})
        self.assertFalse(after['lastRoundRoleActionResults']['10010'])


class SimultaneousOccupancyTests(unittest.TestCase):
    def test_three_adjacent_roles_follow_and_rotate(self):
        for points, targets in [([(20,20),(21,20),(22,20)], [(21,20),(22,20),(23,20)]),
                                ([(20,20),(21,20),(21,21)], [(21,20),(21,21),(20,20)])]:
            with self.subTest(points=points):
                state = scene()
                ids = (601,607,777)
                for role, point in zip(state['teamOur']['roles'][1:], points):
                    role['pos'] = {'x': point[0], 'y': point[1]}
                after = settle(state, {str(uid): move(*target) for uid,target in zip(ids,targets)})
                for uid,target in zip(ids,targets):
                    self.assertEqual(position(after,uid), {'x':target[0],'y':target[1]})
                    self.assertTrue(after['lastRoundRoleActionResults'][str(uid)])

    def test_stopped_departure_blocks_upstream_follower(self):
        origins = {'a':Pos(0,0), 'b':Pos(1,0), 'c':Pos(3,0)}
        moves = {'a':Pos(1,0), 'b':Pos(2,0), 'c':Pos(2,0)}
        resolved,rejected = resolve_moves(moves,origins,set())
        self.assertEqual(resolved, [])
        self.assertEqual({u for u,_,_ in rejected}, set(origins))

    def test_moving_origin_never_erases_overlapping_hard_obstacle(self):
        resolved,_ = resolve_moves({'a':Pos(1,0), 'b':Pos(2,0)},
                                   {'a':Pos(0,0), 'b':Pos(1,0)}, {Pos(1,0)})
        self.assertEqual(resolved, [('b',Pos(2,0))])
