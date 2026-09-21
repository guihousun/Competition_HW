"""Independent action/budget expectations for public daylight construction.

Historical hand-drawn routes deliberately build the rear edge. Run them under
explicit legacy strategy to preserve their original geometry/budget oracle;
new enabled rear-row policy is covered by test_rear_wall_build_policy.
"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from legacy_strategy import LegacyStrategyCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))
from agent import construction_trip, simulator
from agent.home_defense import inside
from agent.protocol import Turn, Pos, distance


def unit(uid, kind, x, y, *, bag=(), hp=None, capacity=100):
    return {'id': uid, 'roleType': kind, 'pos': {'x': x, 'y': y},
            'health': hp if hp is not None else (1500 if kind == 'station' else 220),
            'level': 1, 'backpack': list(bag), 'backPackCapability': capacity}


def board(*, worker_pos=(9, 10), bag=(), side='defender', worker_id=731):
    return {'roundNo': 1, 'mapInfo': {'width': 41, 'height': 32, 'zones': [
                {'neutralType': 'stone', 'pos': {'x': 5, 'y': 10}}]},
            'teamOur': {'type': side, 'goldNum': 0, 'totalScore': 0, 'roles': [
                unit(87, 'station', 10, 10), unit(412, 'rocket', 9, 9),
                unit(worker_id, 'worker', *worker_pos, bag=bag),
                unit(9081, 'worker', 15, 15)]},
            'teamEnemy': {'roles': []}, 'robot': {'roles': []},
            'phaseTask': '', 'worldNews': {}, 'weaponShopList': [], 'vendorShopList': []}


def confine(state, free):
    """A hand-drawn corridor: all non-corridor cells are neutral obstacles."""
    free = set(free)
    state['mapInfo']['zones'] = [{'neutralType': 'obstacle', 'pos': {'x': x, 'y': y}}
                               for x in range(41) for y in range(32) if (x, y) not in free]


class ConstructionTripTests(LegacyStrategyCase):
    def test_worker_on_future_wall_cell_must_move_aside_then_build(self):
        state=board(worker_pos=(8,10),bag=['stone'])
        confine(state,{(7,10),(8,10),(9,10)})
        command,report=self.plan(state,(Pos(8,10),))
        self.assertEqual(command,{'action':'move','targetPos':[{'x':9,'y':10}]})
        self.assertEqual(report['phase'],'to_wall')
        self.assertEqual(report['planned_walls'],[{'x':8,'y':10}])
        state=simulator.step(state,external_response={'roleCommandMap':{'731':command}})['state']
        command,report=self.plan(state,(Pos(8,10),))
        self.assertEqual(command['action'],'build')
        self.assertEqual(command['targetPos'],[{'x':8,'y':10}])

    def plan(self, state, walls=(Pos(8, 10), Pos(8, 11)), worker_id=731, **kw):
        turn = Turn.load(state)
        role = next(r for r in turn.ours if r.unit_id == worker_id)
        return construction_trip.plan(turn, role, walls, **kw)

    def test_real_collect_build_return_loop_both_sides_dynamic_ids(self):
        for side, uid in (('challenger', 731), ('defender', 592047)):
            state = board(side=side, worker_id=uid)
            targets = (Pos(8, 10), Pos(8, 11))
            actions = []
            for _ in range(40):
                before = deepcopy(state)
                result = self.plan(state, targets, worker_id=uid)
                self.assertEqual(state, before, 'planning must not mutate the observation')
                if result is None:
                    break
                command, report = result
                actions.append(command['action'])
                self.assertGreaterEqual(report['slack'], 2)
                state = simulator.step(state, external_response={
                    'roleCommandMap': {str(uid): command}})['state']
                self.assertTrue(state['lastRoundRoleActionResults'][str(uid)])
            self.assertEqual(actions.count('collect'), 2)
            self.assertEqual(actions.count('build'), 2)
            self.assertIn('move', actions)
            walls = [u for u in state['teamOur']['roles'] if u['roleType'] == 'wall']
            self.assertEqual({Pos.load(w['pos']) for w in walls}, set(targets))
            self.assertTrue(all(w['health'] == 1000 and w['level'] == 1 for w in walls))
            turn = Turn.load(state)
            worker = next(w for w in turn.workers() if w.unit_id == uid)
            self.assertEqual(worker.backpack.count('stone'), 0)
            self.assertTrue(inside(turn, worker.pos))
            self.assertTrue(any(distance(worker.pos, g.pos) == 1 for g in turn.weapons()))
            self.assertEqual(state['teamOur']['goldNum'], 0)

    def test_mine_adjacent_partial_load_finishes_batch_before_building(self):
        state = board(worker_pos=(6, 10), bag=['stone'])
        command, report = self.plan(state)
        self.assertEqual(command, {'action': 'collect', 'targetPos': [{'x': 5, 'y': 10}]})
        self.assertEqual(report['collect_remaining'], 1)
        state['teamOur']['roles'][2]['backpack'].append('stone')
        command, report = self.plan(state)
        self.assertNotEqual(command['action'], 'collect')
        self.assertEqual(report['collect_remaining'], 0)

    def test_carried_stone_away_from_mine_builds_before_more_collection(self):
        state = board(bag=['stone'])
        command, report = self.plan(state)
        self.assertEqual(command['action'], 'build')
        self.assertEqual(command['targetPos'], [{'x': 8, 'y': 10}])
        self.assertEqual(len(report['planned_walls']), 1)
        self.assertNotIn('mine', report)

    def test_capacity_and_batch_limit_count_total_stone_target(self):
        state = board(worker_pos=(6, 10), bag=['Medicine'] * 99)
        command, report = self.plan(state)
        self.assertEqual(command['action'], 'collect')
        self.assertEqual(report['collect_remaining'], 1)
        self.assertEqual(len(report['planned_walls']), 1)
        state = board(worker_pos=(6, 10))
        command, report = self.plan(state, batch_limit=1)
        self.assertEqual(report['collect_remaining'], 1)
        state['teamOur']['roles'][2]['backpack'] = ['Medicine'] * 100
        command, report = self.plan(state)
        self.assertEqual(command['action'], 'move')
        self.assertEqual(report['phase'], 'return')
        state['teamOur']['roles'][2]['backpack'] = ['Medicine'] * 99 + ['stone']
        command, report = self.plan(state)
        self.assertIn(report['phase'], ('to_wall', 'build'))

    def test_late_day_and_night_only_return_never_collect_or_build(self):
        for round_no in (54, 55, 56, 70, 71, 130):
            state = board(worker_pos=(6, 10))
            state['roundNo'] = round_no
            with self.subTest(round=round_no):
                command, report = self.plan(state)
                self.assertEqual(command['action'], 'move')
                self.assertEqual(report['phase'], 'return')
        state = board()
        state['roundNo'] = 55
        self.assertIsNone(self.plan(state), 'already at a gun post: hold instead of starting an errand')

    def test_exact_budget_counts_build_return_and_two_spare_rounds(self):
        state = board(worker_pos=(7, 10), bag=['stone'])
        state['teamOur']['roles'][1]['pos'] = {'x': 9, 'y': 9}
        confine(state, {(7, 10), (8, 10), (9, 10)})
        # Must cross the gap, stand on its inner side, then build behind itself:
        # two moves + one build + zero return = 3 actions, plus 2 spare rounds.
        state['roundNo'] = 51  # day index 50; 50 + 3 + 2 == 55
        command, report = self.plan(state, (Pos(8, 10),))
        self.assertEqual(report['remaining_actions'], 3)
        self.assertEqual(report['slack'], 2)
        self.assertEqual(command, {'action': 'move', 'targetPos': [{'x': 8, 'y': 10}]})
        state['roundNo'] = 52
        command, report = self.plan(state, (Pos(8, 10),))
        self.assertEqual(report['phase'], 'return')
        self.assertNotEqual(command['action'], 'build')

    def test_new_wall_is_an_obstacle_before_accepting_build_stand(self):
        state = board(worker_pos=(7, 10), bag=['stone'])
        confine(state, {(7, 10), (8, 10), (9, 10)})
        # Building immediately at (7,10) would strand the worker outside.
        command, report = self.plan(state, (Pos(8, 10),))
        self.assertEqual(command['action'], 'move')
        self.assertEqual(command['targetPos'], [{'x': 8, 'y': 10}])
        self.assertEqual(report['post'], {'x': 9, 'y': 10})
        state['teamOur']['roles'][2]['pos'] = {'x': 9, 'y': 10}
        command, report = self.plan(state, (Pos(8, 10),))
        self.assertEqual(command['action'], 'build')
        self.assertEqual(report['remaining_actions'], 1)

    def test_robot_and_visible_enemy_abort_but_global_enemy_base_does_not(self):
        for enemy in ('robot', 'worker', 'rocket'):
            state = board(worker_pos=(6, 10))
            if enemy == 'robot':
                state['robot']['roles'] = [{'id': 34, 'health': 40, 'pos': {'x': 30, 'y': 20}}]
            else:
                state['teamEnemy']['roles'] = [unit(999, enemy, 30, 20)]
            with self.subTest(threat=enemy):
                command, report = self.plan(state)
                self.assertEqual(report['reason'], 'visible_threat')
                self.assertEqual(command['action'], 'move')
        state = board(worker_pos=(6, 10))
        state['teamEnemy']['roles'] = [unit(999, 'station', 30, 20)]
        self.assertEqual(self.plan(state)[0]['action'], 'collect')

    def test_mine_unavailable_or_disappeared_replans_from_current_inventory(self):
        state = board(worker_pos=(6, 10))
        command, report = self.plan(state, mine_available=False)
        self.assertEqual(report['phase'], 'return')
        state['teamOur']['roles'][2]['backpack'] = ['stone']
        command, report = self.plan(state, mine_available=False)
        self.assertIn(report['phase'], ('to_wall', 'build'))
        self.assertNotEqual(command['action'], 'collect')
        state['teamOur']['roles'][2]['backpack'] = []
        state['mapInfo']['zones'] = []
        command, report = self.plan(state)
        self.assertEqual(report['phase'], 'return')

    def test_claimed_cells_and_unreachable_mine_never_produce_stale_moves(self):
        state = board(worker_pos=(6, 10))
        claimed = {Pos(7, 9), Pos(7, 10), Pos(7, 11)}
        command, report = self.plan(state, claimed=claimed)
        self.assertNotIn(Pos.load(command['targetPos'][0]), claimed)
        self.assertEqual(distance(Pos.load(command['targetPos'][0]), Pos(6, 10)), 1)
        # All mine-adjacent cells are reserved: no collection or outbound trip.
        claimed = {Pos(5 + dx, 10 + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
        state['teamOur']['roles'][2]['pos'] = {'x': 9, 'y': 10}
        self.assertIsNone(self.plan(state, claimed=claimed))

    def test_unreachable_post_refuses_trip_and_occupied_wall_is_not_built(self):
        state = board(worker_pos=(6, 10), bag=['stone'])
        confine(state, {(6, 10), (7, 10), (8, 10)})
        self.assertIsNone(self.plan(state, (Pos(8, 10),)))
        state = board(bag=['stone'])
        state['teamOur']['roles'].append(unit(27, 'worker', 8, 10))
        self.assertIsNone(self.plan(state, (Pos(8, 10),)))

    def test_diagonal_return_between_two_adjacent_obstacles_is_legal(self):
        state = board(worker_pos=(6, 6))
        state['teamOur']['roles'][0]['pos'] = {'x': 8, 'y': 8}
        state['teamOur']['roles'][1]['pos'] = {'x': 7, 'y': 8}
        confine(state, {(6, 6), (7, 7)})
        command, report = self.plan(state, ())
        self.assertEqual(command['targetPos'], [{'x': 7, 'y': 7}])
        self.assertEqual(report['remaining_actions'], 1)

    def test_sheltered_worker_never_returns_via_an_outside_detour(self):
        for round_no, robots in ((71, []), (1, [
                {'id': 95, 'health': 40, 'pos': {'x': 30, 'y': 20}}])):
            state = board(worker_pos=(12, 11))
            state['roundNo'] = round_no
            state['robot']['roles'] = robots
            state['teamOur']['roles'][1]['pos'] = {'x': 11, 'y': 8}
            # Both start and gun post are inside, but the sole connecting path
            # leaves the shelter through x=13. Holding is the safe outcome.
            confine(state, {(12, 11), (13, 10), (13, 9), (12, 9)})
            with self.subTest(round=round_no):
                self.assertIsNone(self.plan(state, ()))

    def test_both_team_task_point2_second_cells_are_not_return_posts(self):
        for kind in ('challengerTaskPoint2', 'defenderTaskPoint2'):
            state = board(worker_pos=(6, 9))
            # (9,10) would otherwise be the sole interior gun post. The task
            # point anchor is (8,10), so its second cell occupies that post.
            confine(state, {(6, 9), (7, 9), (8, 9), (8, 10), (9, 10)})
            state['mapInfo']['zones'].append({'neutralType': kind,
                                              'pos': {'x': 8, 'y': 10}})
            with self.subTest(kind=kind):
                self.assertIsNone(self.plan(state, ()))

    def test_role_must_be_a_living_observed_worker(self):
        state = board()
        state['teamOur']['roles'][2]['roleType'] = 'pioneer'
        self.assertIsNone(self.plan(state))
        state['teamOur']['roles'][2]['roleType'] = 'worker'
        state['teamOur']['roles'][2]['health'] = 0
        self.assertIsNone(self.plan(state))


if __name__ == '__main__':
    unittest.main()
