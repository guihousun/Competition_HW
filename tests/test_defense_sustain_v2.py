"""Independent public-state expectations for a single interior repair lease."""
from copy import deepcopy
from pathlib import Path
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))
from agent import defense_sustain, simulator
from agent.home_defense import inside
from agent.protocol import (Turn, Pos, distance, WALL_FIXER,
                            WALL_UPGRADE_VOUCHER_1, WALL_UPGRADE_VOUCHER_2)


def unit(uid, kind, x, y, *, bag=(), hp=1000, level=1, cooldown=0):
    return {'id': uid, 'roleType': kind, 'pos': {'x': x, 'y': y},
            'health': hp, 'level': level, 'cooldown': cooldown,
            'backpack': list(bag), 'backPackCapability': 100}


def board(*, uid=731, side='challenger', bag=(WALL_FIXER,), mirror=False):
    roles = [unit(87, 'station', 10, 10, hp=1500),
             unit(412, 'rocket', 9, 9),
             unit(uid, 'worker', 9, 10, bag=bag, hp=220),
             unit(9081, 'worker', 9, 8, hp=220),
             unit(611, 'wall', 13, 10, hp=500)]
    # Hand-drawn corridor: exactly three moves out, use, three moves back.
    free = {(9, 10), (10, 11), (11, 11), (12, 11), (9, 8)}
    free.update((r['pos']['x'], r['pos']['y']) for r in roles)
    zones = [{'neutralType': 'obstacle', 'pos': {'x': x, 'y': y}}
             for x in range(41) for y in range(32) if (x, y) not in free]
    if mirror:
        for role in roles:
            role['pos']['x'] = 40 - role['pos']['x'] - (1 if role['roleType'] == 'station' else 0)
        for zone in zones:
            zone['pos']['x'] = 40 - zone['pos']['x']
    return {'roundNo': 80, 'mapInfo': {'width': 41, 'height': 32, 'zones': zones},
            'teamOur': {'type': side, 'goldNum': 0, 'totalScore': 0, 'roles': roles},
            'teamEnemy': {'roles': []}, 'robot': {'roles': []},
            'worldNews': {}, 'weaponShopList': [], 'vendorShopList': [], 'phaseTask': ''}


def role(state, uid):
    return next(r for r in state['teamOur']['roles'] if r['id'] == uid)


class DefenseSustainV2Tests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {defense_sustain.ENV: 'on'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_disabled_does_not_rewrite_legacy_memory(self):
        memory={'opaque_old_field':7}; before=deepcopy(memory)
        defense_sustain.plan(Turn.load(board()),memory,[],{},enabled_override=False)
        self.assertEqual(memory,before)

    def test_explicit_task_or_safety_reservation_removes_covering_role(self):
        state=board()
        result=self.plan(state,excluded_roles={9081})
        self.assertEqual(result['commands'],{})
        self.assertIn('fire_coverage',result['report']['rejections'])

    def test_rejected_new_dispatch_never_acquires_lease(self):
        state=board();memory={};result=self.plan(state,memory)
        self.assertIn('lease',memory)
        defense_sustain.finalize(memory,result,{})
        self.assertNotIn('lease',memory)
        self.assertEqual(result['owned_roles'],[])

    def test_rejected_existing_use_keeps_owner_without_use_marker(self):
        state=board();memory={};result=self.plan(state,memory)
        defense_sustain.finalize(memory,result,result['commands'])
        role(state,731)['pos']={'x':12,'y':11};state['roundNo']+=1
        result=self.plan(state,memory)
        self.assertEqual(result['commands'][731]['action'],'use')
        defense_sustain.finalize(memory,result,{})
        self.assertEqual(result['owned_roles'],[731])
        self.assertEqual(memory['lease']['phase'],'return')
        self.assertNotIn('use_round',memory['lease'])

    def test_consumption_with_false_receipt_is_unknown_not_success(self):
        state=board();memory={};role(state,731)['pos']={'x':12,'y':11}
        self.plan(state,memory);state['roundNo']+=1
        role(state,731)['backpack']=[];role(state,611)['health']=400
        state['lastRoundRoleActionResults']={'731':False}
        result=self.plan(state,memory)
        self.assertEqual(result['report']['reason'],'use_unknown_consumed')
        self.assertEqual(result['report']['phase'],'return')

    def test_consumption_and_success_receipt_confirm_despite_net_damage(self):
        state=board();memory={};role(state,731)['pos']={'x':12,'y':11}
        self.plan(state,memory);state['roundNo']+=1
        role(state,731)['backpack']=[];role(state,611)['health']=400
        state['lastRoundRoleActionResults']={'731':True}
        result=self.plan(state,memory)
        self.assertEqual(result['report']['reason'],'use_confirmed')
        self.assertEqual(result['report']['phase'],'return')

    def plan(self, state, memory=None, *, uid=731, ready=(412,), commands=None, **kw):
        turn = Turn.load(state)
        units = {r.unit_id: r for r in turn.ours}
        pairs = [(units[uid], units[412]), (units[9081], units[412])]
        return defense_sustain.plan(turn, {} if memory is None else memory, pairs,
                                    commands or {}, payload=state, ready_weapons=[units[g] for g in ready], **kw)

    def test_default_off_and_day_no_lease(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(defense_sustain.enabled())
            self.assertEqual(self.plan(board())['commands'], {})
        state, memory = board(), {}
        self.plan(state, memory)
        state['roundNo'] = 131
        self.assertEqual(self.plan(state, memory)['commands'], {})
        self.assertNotIn('lease', memory)

    def test_actual_seven_action_repair_loop_both_sides_dynamic_ids(self):
        for side, uid, mirror in [('challenger', 731, False), ('defender', 572803, True)]:
            with self.subTest(side=side):
                state, memory = board(uid=uid, side=side, mirror=mirror), {}
                start = deepcopy(role(state, uid)['pos'])
                actions = []
                for _ in range(10):
                    before = deepcopy(state)
                    result = self.plan(state, memory, uid=uid)
                    self.assertEqual(state, before)
                    if not result['commands']:
                        break
                    self.assertEqual(result['owned_roles'], [uid])
                    self.assertEqual(len(result['commands']), 1)
                    command = result['commands'][uid]
                    actions.append(command['action'])
                    if len(actions) == 1:
                        self.assertEqual(result['report']['remaining_actions'], 7)
                    state = simulator.step(state, external_response={
                        'roleCommandMap': {str(uid): command}})['state']
                    self.assertTrue(state['lastRoundRoleActionResults'][str(uid)])
                    current = next(r for r in Turn.load(state).workers() if r.unit_id == uid)
                    self.assertTrue(inside(Turn.load(state), current.pos))
                self.assertEqual(actions, ['move'] * 3 + ['use'] + ['move'] * 3)
                self.assertEqual(role(state, 611)['health'], 1000)
                self.assertEqual(role(state, uid)['backpack'], [])
                self.assertEqual(role(state, uid)['pos'], start)
                self.assertNotIn('lease', memory)

    def test_voucher_matching_level_and_full_heal(self):
        for level, item in [(1, WALL_UPGRADE_VOUCHER_1), (2, WALL_UPGRADE_VOUCHER_2)]:
            state, memory = board(bag=(item,)), {}
            role(state, 611)['level'] = level
            role(state, 731)['pos'] = {'x': 12, 'y': 11}
            result = self.plan(state, memory)
            self.assertEqual(result['commands'][731], {'action': 'use', 'name': item,
                                                       'targetPos': [{'x': 13, 'y': 10}]})
            state = simulator.step(state, external_response={
                'roleCommandMap': {'731': result['commands'][731]}})['state']
            self.assertEqual(role(state, 611)['level'], level + 1)
            self.assertEqual(role(state, 611)['health'], (1500, 2000)[level - 1])
            result = self.plan(state, memory)
            self.assertEqual(result['report']['reason'], 'use_confirmed')
            self.assertEqual(result['commands'][731]['action'], 'move')
        for level, item in [(1, WALL_UPGRADE_VOUCHER_2), (2, WALL_UPGRADE_VOUCHER_1),
                            (3, WALL_UPGRADE_VOUCHER_2)]:
            state = board(bag=(item,))
            role(state, 611)['level'] = level
            self.assertEqual(self.plan(state)['commands'], {})

    def test_exact_seventy_percent_front_entry_not_full_or_rear(self):
        state = board()
        role(state, 611)['health'] = 700
        self.assertIn(731, self.plan(state)['commands'])
        role(state, 611)['health'] = 701
        self.assertEqual(self.plan(state)['commands'], {})
        role(state, 611)['health'] = 1000
        self.assertEqual(self.plan(state)['commands'], {})
        role(state, 611).update(health=500, pos={'x': 8, 'y': 10})
        self.assertEqual(self.plan(state)['commands'], {})

    def test_use_failure_and_duplicate_observation_do_not_confirm(self):
        state, memory = board(), {}
        role(state, 731)['pos'] = {'x': 12, 'y': 11}
        first = self.plan(state, memory)
        saved = deepcopy(memory)
        second = self.plan(deepcopy(state), memory)
        self.assertEqual({k:v for k,v in first.items() if not k.startswith('_')},
                         {k:v for k,v in second.items() if not k.startswith('_')})
        self.assertEqual(memory, saved)
        state['roundNo'] += 1  # unchanged inventory: use failed, not a successful heal
        third = self.plan(state, memory)
        self.assertEqual(third['report']['reason'], 'use_unconfirmed')
        self.assertEqual(third['commands'][731]['action'], 'move')
        self.assertEqual(role(state, 611)['health'], 500)

    def test_skipped_use_observation_is_not_success_and_busy_lease_cancels(self):
        state, memory = board(), {}
        role(state, 731)['pos'] = {'x': 12, 'y': 11}
        self.plan(state, memory)
        state['roundNo'] += 2
        role(state, 731)['backpack'] = []
        role(state, 611)['health'] = 1000
        result = self.plan(state, memory)
        self.assertEqual(result['report']['reason'], 'use_unknown_consumed')
        self.assertEqual(result['report']['phase'], 'return')
        state, memory = board(), {}
        self.plan(state, memory)
        result = self.plan(state, memory, commands={731: {'action': 'reserved'}})
        self.assertEqual(result['report']['phase'], 'cancelled')
        self.assertEqual(result['report']['reason'], 'lease_role_busy')
        self.assertEqual(result['owned_roles'], [])
        self.assertNotIn('lease', memory)

    def test_original_gun_destroyed_returns_to_other_legal_gun(self):
        state, memory = board(), {}
        result = self.plan(state, memory)
        role(state, 731)['pos'] = result['commands'][731]['targetPos'][0]
        state['roundNo'] += 1
        role(state, 412)['health'] = 0
        state['teamOur']['roles'].append(unit(722, 'rocket', 12, 11))
        result = self.plan(state, memory, ready=())
        self.assertEqual(result['report']['phase'], 'return')
        self.assertEqual(result['commands'][731]['targetPos'], [{'x': 11, 'y': 11}])
        role(state, 731)['pos'] = {'x': 11, 'y': 11}
        state['roundNo'] += 1
        result = self.plan(state, memory, ready=())
        self.assertEqual(result['commands'], {})
        self.assertEqual(result['report']['return_weapon_id'], 722)
        self.assertNotIn('lease', memory)

    def test_closed_return_route_rejects_new_adjacent_use(self):
        state = board()
        role(state, 731)['pos'] = {'x': 12, 'y': 11}
        result = self.plan(state, claimed=(Pos(11, 11),))
        self.assertEqual(result['commands'], {})
        self.assertIn('route_or_return_blocked', result['report']['rejections'])

    def test_dead_role_target_and_missing_item_abort(self):
        for change in ('worker_dead', 'wall_dead', 'item_gone'):
            state, memory = board(), {}
            result = self.plan(state, memory)
            role(state, 731)['pos'] = deepcopy(result['commands'][731]['targetPos'][0])
            state['roundNo'] += 1
            if change == 'worker_dead':
                role(state, 731)['health'] = 0
            elif change == 'wall_dead':
                role(state, 611)['health'] = 0
            else:
                role(state, 731)['backpack'] = []
            result = self.plan(state, memory)
            if change == 'worker_dead':
                self.assertEqual(result['commands'], {})
                self.assertNotIn('lease', memory)
            else:
                self.assertEqual(result['report']['phase'], 'return')
                self.assertEqual(result['commands'][731]['targetPos'], [{'x': 9, 'y': 10}])

    def test_blocked_outward_and_blocked_return_keeps_ownership(self):
        state = board()
        result = self.plan(state, claimed=(Pos(10, 11),))
        self.assertEqual(result['commands'], {})
        self.assertIn('route_or_return_blocked', result['report']['rejections'])
        state, memory = board(), {}
        result = self.plan(state, memory)
        role(state, 731)['pos'] = result['commands'][731]['targetPos'][0]
        state['roundNo'] += 1
        role(state, 611)['health'] = 0
        result = self.plan(state, memory, claimed=(Pos(9, 10),))
        self.assertEqual(result['commands'], {})
        self.assertEqual(result['report']['reason'], 'return_blocked')
        self.assertEqual(result['owned_roles'], [731])
        self.assertEqual(memory['lease']['phase'], 'return')

    def test_no_outside_detour_and_task_point_second_cell(self):
        state = board()
        # Open exterior alternatives, but the only interior bridge is blocked.
        state['mapInfo']['zones'] = [{'neutralType': 'obstacle', 'pos': {'x': 10, 'y': 11}},
                                   {'neutralType': 'obstacle', 'pos': {'x': 9, 'y': 9}}]
        # The bottom interior route is also occupied; exterior remains land.
        for x in (10, 11, 12):
            state['mapInfo']['zones'].append({'neutralType': 'obstacle', 'pos': {'x': x, 'y': 8}})
        self.assertEqual(self.plan(state)['commands'], {})
        for kind in ('challengerTaskPoint2', 'defenderTaskPoint2'):
            state = board()
            state['mapInfo']['zones'].append({'neutralType': kind, 'pos': {'x': 9, 'y': 11}})
            self.assertEqual(self.plan(state)['commands'], {})

    def test_ready_coverage_counts_distinct_roles_and_busy_tasks(self):
        state = board()
        self.assertIn(731, self.plan(state)['commands'])
        self.assertEqual(self.plan(state, commands={9081: {'action': 'reserved'}})['commands'], {})
        role(state, 9081)['roleType'] = 'pioneer'
        self.assertEqual(self.plan(state, commands={9081: {'action': 'acceptTask'}})['commands'], {})
        state['teamOur']['roles'].append(unit(913, 'railgun', 10, 8))
        result = self.plan(state, ready=(412, 913))
        self.assertEqual(result['commands'], {})
        self.assertIn('fire_coverage', result['report']['rejections'])

    def test_cooldown_exact_window_and_shared_rocket_group(self):
        # Adjacent use plus zero return is exactly one action. No other gunner.
        state = board()
        role(state, 9081)['health'] = 0
        role(state, 731)['pos'] = {'x': 12, 'y': 11}
        role(state, 412)['pos'] = {'x': 11, 'y': 11}
        role(state, 412)['cooldown'] = 1
        result = self.plan(state, ready=())
        self.assertEqual(result['report']['remaining_actions'], 1)
        self.assertEqual(result['commands'][731]['action'], 'use')
        # Two moves + use + return = more than the remaining one-round window.
        role(state, 731)['pos'] = {'x': 9, 'y': 10}
        role(state, 412)['pos'] = {'x': 9, 'y': 9}
        result = self.plan(state, ready=())
        self.assertEqual(result['commands'], {})
        self.assertIn('cooldown_window', result['report']['rejections'])

    def test_exact_three_round_cooldown_trip_and_shorter_window_rejected(self):
        state = board()
        role(state, 9081)['health'] = 0
        role(state, 731)['pos'] = {'x': 11, 'y': 11}
        role(state, 412)['pos'] = {'x': 10, 'y': 11}
        role(state, 412)['cooldown'] = 3
        result = self.plan(state, ready=())
        # (11,11)->(12,11), use wall(13,10), return(11,11).
        self.assertEqual(result['report']['remaining_actions'], 3)
        self.assertEqual(result['commands'][731]['targetPos'], [{'x': 12, 'y': 11}])
        role(state, 412)['cooldown'] = 2
        result = self.plan(state, ready=())
        self.assertEqual(result['commands'], {})
        self.assertIn('cooldown_window', result['report']['rejections'])
        # A single remaining worker can cover either cooling gun, not both.
        state = board()
        role(state, 412)['cooldown'] = 3
        state['teamOur']['roles'].append(unit(913, 'rocket', 10, 8, cooldown=3))
        result = self.plan(state, ready=())
        self.assertEqual(result['commands'], {})
        self.assertIn('cooldown_window', result['report']['rejections'])

    def test_changed_coverage_and_inside_robot_cancel_to_return(self):
        for cause in ('coverage', 'breach'):
            state, memory = board(), {}
            result = self.plan(state, memory)
            role(state, 731)['pos'] = result['commands'][731]['targetPos'][0]
            state['roundNo'] += 1
            if cause == 'coverage':
                role(state, 9081)['health'] = 0
            else:
                state['robot']['roles'] = [{'id': 222, 'health': 40, 'pos': {'x': 12, 'y': 8}}]
            result = self.plan(state, memory)
            self.assertEqual(result['report']['phase'], 'return')
            self.assertEqual(result['commands'][731]['targetPos'], [{'x': 9, 'y': 10}])
        state = board()
        state['robot']['roles'] = [{'id': 222, 'health': 40, 'pos': {'x': 30, 'y': 20}}]
        self.assertIn(731, self.plan(state)['commands'], 'visible outside combat is permitted')

    def test_new_claim_and_commands_not_mutated(self):
        state, memory = board(), {}
        commands = {9081: {'action': 'reserved'}}
        saved = deepcopy(commands)
        self.plan(state, memory, commands=commands)
        self.assertEqual(commands, saved)
        result = self.plan(state, memory)
        self.assertEqual(result['claimed'], [Pos(10, 11)])
        self.assertEqual(result['commands'][731]['action'], 'move')

    def test_sanitization_public_only_and_rewind(self):
        state, memory = board(), {}
        clean = self.plan(state, memory)
        noisy = deepcopy(state)
        noisy['_demo'] = {'seed': -999, 'next_wave': 'not public', 'answer': 'private'}
        self.assertEqual(clean, self.plan(noisy))
        memory['private'] = {'huge': list(range(100))}
        memory['lease']['path'] = [1, 2, 3]
        sanitized = defense_sustain.sanitize_memory(memory)
        self.assertNotIn('private', sanitized)
        self.assertNotIn('path', sanitized['lease'])
        self.assertEqual(set(sanitized), {'lease', 'last_round'})
        self.assertEqual(defense_sustain.sanitize_memory({'lease': {'role_id': True}}), {})
        self.assertEqual(defense_sustain.sanitize_memory(['not', 'a', 'dict']), {})
        state['roundNo'] = 1
        self.plan(state, memory)
        self.assertNotIn('lease', memory)


if __name__ == '__main__':
    unittest.main()
