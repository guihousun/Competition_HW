"""Independent clearance intervals and real planner actions, no future wave oracle."""
from copy import deepcopy
import json
import unittest

from test_single_operator_rockets import fixture
from test_coordination import unit
from agent import brain, cleared_night, planner, strategy_config, team_trip, turnactions
from agent.protocol import Turn, Pos, distance


def board(round_no=480):
    p = fixture()
    p.update(roundNo=round_no, robot={'roles': []}, weaponShopList=[],
             vendorShopList=[{'name': 'iron', 'price': 3}, {'name': 'copper', 'price': 2}],
             phaseTask='', llmResp='', lastCmdResult='', errors=[], lastRoundRoleActionResults={})
    p['teamOur']['goldNum'] = 0
    p['teamOur']['playerTasks'] = []
    p['mapInfo']['zones'] = [dict(pos=dict(x=12, y=21), neutralType='iron'),
                             dict(pos=dict(x=14, y=23), neutralType='vendor')]
    p['teamOur']['roles'][1]['backpack'] = ['stone'] * 10
    p['teamOur']['roles'][2].update(pos=dict(x=14, y=22), backpack=['copper'] * 10)
    return p


def observe(p, memory):
    return cleared_night.observe(Turn.load(p), p, memory)


def confirmed(p):
    state = planner.PlannerState()
    for n in range(p['roundNo'] - 3, p['roundNo']):
        prior = deepcopy(p)
        prior['roundNo'] = n
        state.cleared_night_state, _ = observe(prior, state.cleared_night_state)
    return state


class ClearanceTests(unittest.TestCase):
    def test_three_complete_intervals_after_first_clear_observation(self):
        p, memory = board(477), {}
        for n, expected in [(477, 0), (478, 1), (479, 2), (480, 3)]:
            p['roundNo'] = n
            memory, report = observe(p, memory)
            self.assertEqual(report['safe_rounds'], expected)
            self.assertEqual(report['phase'] == 'productive', n == 480)

    def test_kill_observation_is_baseline_not_first_safe_interval(self):
        p = board(476)
        p['robot']['roles'] = [dict(id=90, health=40, pos=dict(x=40, y=0))]
        memory, _ = observe(p, {})
        p.update(roundNo=477, robot={'roles': []})
        memory, report = observe(p, memory)
        self.assertEqual(report['safe_rounds'], 0)
        for n in (478, 479, 480):
            p['roundNo'] = n
            memory, report = observe(p, memory)
        self.assertEqual(report['phase'], 'productive')

    def test_duplicate_and_preview_do_not_advance_persistent_count(self):
        p = board()
        state = confirmed(p)
        before = state.dump()
        for _ in range(3):
            brain.plan_for_state(deepcopy(p), state, commit=False, judge_tasks=False)
            self.assertEqual(state.dump(), before)
        brain.plan_for_state(deepcopy(p), state, judge_tasks=False)
        self.assertEqual(state.cleared_night_state['safe_rounds'], 3)
        once = deepcopy(state.cleared_night_state)
        brain.plan_for_state(deepcopy(p), state, judge_tasks=False)
        self.assertEqual(state.cleared_night_state, once)

    def test_json_restore_preserves_only_proven_window(self):
        p = board()
        state = planner.PlannerState.load(json.loads(json.dumps(confirmed(p).dump())))
        _, report = observe(p, state.cleared_night_state)
        self.assertEqual(report['phase'], 'productive')
        malformed = state.dump()
        malformed['clearedNightState']['safe_rounds'] = 10000
        self.assertEqual(planner.PlannerState.load(malformed).cleared_night_state, {})

    def test_alive_our_or_unknown_target_even_without_damage_blocks(self):
        for target in (None, 'challenger', 'unrecognised'):
            p = board()
            p['robot']['roles'] = [dict(id=90, health=40, pos=dict(x=40, y=0), targetTeam=target)]
            _, report = observe(p, confirmed(board()).cleared_night_state)
            self.assertEqual((report['phase'], report['reason']), ('defend', 'visible_threat'))

    def test_far_opponent_robot_allows_work_but_approach_revokes_it(self):
        p = board()
        p['robot']['roles'] = [dict(id=90, health=40, pos=dict(x=40, y=0), targetTeam='defender')]
        _, report = observe(p, confirmed(p).cleared_night_state)
        self.assertEqual(report['phase'], 'productive')
        self.assertEqual(report['relevant_robots'], 0)
        p['robot']['roles'][0]['pos'] = dict(x=15, y=21)
        _, report = observe(p, confirmed(board()).cleared_night_state)
        self.assertEqual(report['reason'], 'visible_threat')

    def test_unusable_health_or_position_is_not_clear(self):
        p = board()
        turn = Turn.load(p)
        for broken in ({'id': 90, 'pos': dict(x=40, y=0)},
                       {'id': 90, 'health': 0}, {'id': 90, 'health': 0, 'pos': dict(x=-1, y=0)}):
            partial = deepcopy(p)
            partial['robot']['roles'] = [broken]
            _, report = cleared_night.observe(turn, partial, confirmed(p).cleared_night_state)
            self.assertEqual(report['reason'], 'incomplete_public_observation')

    def test_hp_loss_death_and_disappearance_include_buildings(self):
        for index, mode in [(0, 'loss'), (1, 'loss'), (3, 'death'), (4, 'remove')]:
            p = board()
            memory = confirmed(p).cleared_night_state
            if mode == 'remove':
                p['teamOur']['roles'].pop(index)
            else:
                p['teamOur']['roles'][index]['health'] = 0 if mode == 'death' else p['teamOur']['roles'][index]['health'] - 1
            _, report = observe(p, memory)
            self.assertEqual(report['safe_rounds'], 0)

    def test_missing_fields_skipped_round_new_night_side_and_reset(self):
        for mode in ('robot', 'ours', 'enemy', 'skip', 'night', 'side', 'rewind'):
            p = board()
            memory = confirmed(p).cleared_night_state
            if mode == 'robot': p.pop('robot')
            elif mode == 'ours': p['teamOur'].pop('roles')
            elif mode == 'enemy': p.pop('teamEnemy')
            elif mode == 'skip': p['roundNo'] += 2
            elif mode == 'night': p['roundNo'] += 130
            elif mode == 'side': p['teamOur']['type'] = 'defender'
            else: p['roundNo'] = 478
            _, report = observe(p, memory)
            self.assertEqual(report['safe_rounds'], 0, mode)

    def test_action_legality_and_private_hit_logs_are_not_damage_receipts(self):
        p = board()
        memory = confirmed(p).cleared_night_state
        p['lastRoundRoleActionResults'] = {'2': False}
        p['_demo'] = {'robotAttacks': [{'victim': 2, 'damage': 99}]}
        _, report = observe(p, memory)
        self.assertEqual(report['phase'], 'productive')
        self.assertIn('healing_can_mask', report['damage_limit'])


class ProductiveNightTests(unittest.TestCase):
    def plan(self, p, state=None):
        return brain.plan_for_state(p, state or confirmed(p), judge_tasks=False).commands

    def test_single_gunner_mines_spare_sells_and_no_night_build(self):
        p = board()
        commands = self.plan(p)
        self.assertEqual(commands['2']['action'], 'collect')
        self.assertEqual(commands['3'], dict(action='sell', name='copper', num=10))
        self.assertFalse(any(c['action'] in ('build', 'attack') for c in commands.values()))
        report = brain.decision_report()
        self.assertEqual(report['cleared_night']['phase'], 'productive')

    def test_far_opponent_robots_do_not_force_productive_roles_to_posts(self):
        p = board()
        p['robot']['roles'] = [dict(id=90, health=40, pos=dict(x=40, y=0), targetTeam='defender')]
        commands = self.plan(p)
        self.assertEqual(commands['2']['action'], 'collect')
        self.assertEqual(commands['3']['action'], 'sell')

    def test_routes_avoid_opponent_robot_hazard_region(self):
        p = board()
        p['teamOur']['roles'][3]['pos'] = dict(x=20, y=5)
        p['mapInfo']['zones'].append(dict(pos=dict(x=30, y=5), neutralType='challengerTaskPoint1'))
        p['teamOur']['playerTasks'] = [dict(taskPosition=dict(x=30, y=5), isValid=True, coldDownRounds=0)]
        p['robot']['roles'] = [dict(id=90, health=40, pos=dict(x=25, y=5), targetTeam='defender')]
        commands = self.plan(p)
        # The tempting eastward step enters range+one (distance4). The whole
        # detour must instead begin away from that closed region.
        self.assertEqual(commands['4']['action'], 'move')
        destination = Pos.load(commands['4']['targetPos'][0])
        self.assertGreater(distance(destination, Pos(25, 5)), 4)

    def test_first_three_observations_do_not_release_single_operator(self):
        p = board(477)
        state = planner.PlannerState()
        for n in (477, 478, 479):
            p['roundNo'] = n
            commands = self.plan(deepcopy(p), state)
            self.assertNotEqual(commands.get('2', {}).get('action'), 'collect')

    def test_fourth_night_pioneer_can_accept_public_task_after_clearance(self):
        p = board()
        p['teamOur']['roles'][3]['pos'] = dict(x=20, y=20)
        p['mapInfo']['zones'].append(dict(pos=dict(x=20, y=19), neutralType='challengerTaskPoint1'))
        p['teamOur']['playerTasks'] = [dict(taskPosition=dict(x=20, y=19), isValid=True, coldDownRounds=0)]
        commands = self.plan(p)
        self.assertEqual(commands['4']['action'], 'acceptTask')
        self.assertFalse(brain.decision_report()['supervisor']['reserve_pioneer'])

    def test_new_threat_revokes_same_round_task_and_mining(self):
        p = board()
        state = confirmed(p)
        self.plan(deepcopy(p), state)
        p['roundNo'] += 1
        p['robot']['roles'] = [dict(id=90, health=40, pos=dict(x=15, y=21), targetTeam='challenger')]
        commands = self.plan(p, state)
        self.assertFalse(any(c['action'] in ('collect', 'buy', 'acceptTask') for c in commands.values()))
        self.assertTrue(brain.decision_report()['supervisor']['reserve_pioneer'])
        self.assertEqual(state.cleared_night_state['safe_rounds'], 0)

    def test_completed_repair_return_lease_does_not_lock_spare_worker(self):
        p = board()
        state = confirmed(p)
        state.maintenance_state = {'last_round': 479, 'lease': dict(owner=3, target=99,
            issued_round=478, phase='return', home=dict(x=8, y=20))}
        commands = self.plan(p, state)
        self.assertEqual(commands['3']['action'], 'sell')
        self.assertEqual(state.maintenance_state, {})

    def test_held_upgrade_works_at_night_without_falsifying_clock(self):
        p = board()
        p['teamOur']['roles'][1]['backpack'] = ['WeaponUpgradeVoucher1']
        commands = self.plan(p)
        self.assertEqual(commands['2']['action'], 'use')
        self.assertEqual(p['roundNo'], 480)
        self.assertFalse(Turn.load(p).is_day)

    def test_shared_money_and_landing_remain_reconciled(self):
        p = board()
        p['teamOur']['roles'][1]['pos'] = dict(x=12, y=22)
        p['teamOur']['goldNum'] = 100
        p['weaponShopList'] = [dict(name='WeaponUpgradeVoucher1', price=100)]
        p['mapInfo']['zones'].append(dict(pos=dict(x=13, y=22), neutralType='weaponShop'))
        commands = self.plan(p)
        self.assertEqual(sum(c['action'] == 'buy' for c in commands.values()), 1)
        moves = [Pos.load(c['targetPos'][0]) for c in commands.values() if c['action'] == 'move']
        self.assertEqual(len(moves), len(set(moves)))

    def test_real_procurement_buy_then_deliver_and_use_across_sunrise(self):
        p = board(519)
        p['teamOur']['goldNum'] = 100
        p['weaponShopList'] = [dict(name='WeaponUpgradeVoucher1', price=100)]
        p['mapInfo']['zones'].append(dict(pos=dict(x=15, y=23), neutralType='weaponShop'))
        state = confirmed(p)
        seen = []
        for _ in range(25):
            commands = self.plan(p, state)
            by_id = {r['id']: r for r in p['teamOur']['roles']}
            for uid, command in commands.items():
                role = by_id[int(uid)]
                if command['action'] == 'move':
                    target = Pos.load(command['targetPos'][0])
                    turn = Turn.load(p)
                    mover = next(r for r in turn.ours if r.unit_id == int(uid))
                    self.assertTrue(turn.land(target))
                    self.assertNotIn(target, turn.blocked(mover))
                    role['pos'] = target.dump()
                elif command['action'] == 'buy':
                    self.assertEqual(command['name'], 'WeaponUpgradeVoucher1')
                    turnactions.buy(p, role, item=command['name'], amount=1)
                    seen.append('buy')
                elif command['action'] == 'use':
                    turnactions.use(p, role, item=command['name'], target=Pos.load(command['targetPos'][0]), effects={})
                    seen.append('use')
            if 'use' in seen:
                break
            p['roundNo'] += 1
            state = planner.PlannerState.load(json.loads(json.dumps(state.dump())))
        self.assertEqual(seen, ['buy', 'use'])
        self.assertGreater(p['roundNo'], 520)
        self.assertEqual(p['teamOur']['goldNum'], 0)
        self.assertEqual(sum(r['roleType'] == 'rocket' and r['level'] == 2 for r in p['teamOur']['roles']), 1)
