"""R01/R02/R03/R06: hand-worked quiet-night policy cases."""
from copy import deepcopy
import unittest
from test_baseline import ROOT
from test_coordination import unit
from agent import brain, nightwork
from agent.protocol import Turn


def board():
    return dict(roundNo=90, mapInfo=dict(width=41, height=32, zones=[
        dict(pos=dict(x=9, y=5), neutralType='stone')]),
        teamOur=dict(type='challenger', goldNum=75, roles=[
            unit(1, 'worker', 4, 5), unit(2, 'worker', 7, 5),
            unit(40, 'rocket', 5, 5, 1000), unit(41, 'rocket', 7, 6, 1000),
            unit(13, 'station', 5, 7, 1500)]),
        teamEnemy=dict(roles=[]), robot=dict(roles=[]), weaponShopList=[], vendorShopList=[])


def plan(payload):
    turn = Turn.load(payload)
    return nightwork.plan(turn, payload, brain._tower_pairs(turn))


class QuietNightTests(unittest.TestCase):
    def test_one_worker_leaves_other_keeps_post(self):
        result = plan(board())
        self.assertEqual(result, {2: dict(action='move', targetPos=[dict(x=8, y=4)])})

    def test_collects_at_mine_instead_of_returning_to_tower(self):
        payload = board()
        payload['teamOur']['roles'][1]['pos'] = dict(x=8, y=5)
        self.assertEqual(plan(payload), {2: dict(action='collect', targetPos=[dict(x=9, y=5)])})
        commands = {}
        brain._night(Turn.load(payload), commands, payload)
        self.assertEqual(commands[2]['action'], 'collect')
        self.assertNotIn(1, commands)

    def test_live_robot_anywhere_immediately_cancels_work_and_returns(self):
        payload = board()
        payload['teamOur']['roles'][1]['pos'] = dict(x=10, y=5)
        payload['robot']['roles'] = [dict(id=91, pos=dict(x=40, y=0), health=40)]
        self.assertEqual(plan(payload), {})
        commands = {}
        brain._night(Turn.load(payload), commands, payload)
        self.assertEqual(commands[2]['action'], 'move')
        target = commands[2]['targetPos'][0]
        self.assertLess(target['x'], 10)
        self.assertFalse(any(c['action'] in ('collect', 'buy', 'sell', 'use', 'build')
                             for c in commands.values()))

    def test_missing_robot_observation_is_not_assumed_clear(self):
        for value in [None, {}, dict(roles=None)]:
            payload = board()
            payload['robot'] = value
            self.assertEqual(plan(payload), {})

    def test_day_and_first_night_round_do_not_start_night_jobs(self):
        for round_no in [1, 70, 71, 131, 201]:
            payload = board()
            payload['roundNo'] = round_no
            self.assertEqual(plan(payload), {})

    def test_dead_robot_does_not_block_clearance(self):
        payload = board()
        payload['robot']['roles'] = [dict(id=91, pos=dict(x=20, y=20), health=0)]
        self.assertIn(2, plan(payload))

    def test_guard_missing_or_away_prevents_departure(self):
        for mode in ['dead', 'away']:
            payload = board()
            if mode == 'dead':
                payload['teamOur']['roles'][0]['health'] = 0
            else:
                payload['teamOur']['roles'][0]['pos'] = dict(x=30, y=20)
            self.assertEqual(plan(payload), {})

    def test_visible_enemy_crew_nearby_prevents_work(self):
        payload = board()
        payload['teamEnemy']['roles'] = [unit(99, 'worker', 12, 5)]
        self.assertEqual(plan(payload), {})

    def test_far_mine_full_backpack_and_sufficient_stone_do_not_send_worker(self):
        for mode in ['far', 'full', 'enough']:
            payload = board()
            if mode == 'far':
                payload['mapInfo']['zones'][0]['pos'] = dict(x=20, y=5)
            else:
                payload['teamOur']['roles'][1]['backpack'] = ['stone'] * (100 if mode == 'full' else 10)
            self.assertEqual(plan(payload), {})

    def test_holding_medicine_heals_without_a_purchase(self):
        payload = board()
        payload['teamOur']['roles'][0].update(health=100, backpack=['Medicine'])
        self.assertEqual(plan(payload)[1], dict(action='use', name='Medicine'))

    def test_repair_threshold_uses_actual_hp_of_each_wall_level(self):
        for level, threshold, full in [(1, 700, 1000), (2, 1050, 1500), (3, 1400, 2000)]:
            for hp in [threshold, threshold + 1, full]:
                payload = board()
                payload['teamOur']['roles'][0]['backpack'] = ['WallFixer']
                wall = unit(99, 'wall', 3, 5, hp)
                wall['level'] = level
                payload['teamOur']['roles'].append(wall)
                result = plan(payload)
                with self.subTest(level=level, hp=hp):
                    if hp <= threshold:
                        self.assertEqual(result[1]['name'], 'WallFixer')
                    else:
                        self.assertNotIn(1, result)

    def test_two_workers_do_not_upgrade_same_building_twice(self):
        payload = board()
        payload['teamOur']['roles'] = [unit(1, 'worker', 4, 5), unit(2, 'worker', 6, 5),
                                      unit(40, 'rocket', 5, 5, 1000)]
        for r in payload['teamOur']['roles'][:2]:
            r['backpack'] = ['WeaponUpgradeVoucher1']
        result = plan(payload)
        self.assertEqual(result, {1: dict(action='use', name='WeaponUpgradeVoucher1',
                                         targetPos=[dict(x=5, y=5)])})

    def test_shop_respects_budget_and_does_not_buy_owned_supplies(self):
        payload = board()
        payload['teamOur']['goldNum'] = 10
        payload['teamOur']['roles'][0]['health'] = 100
        payload['mapInfo']['zones'].append(dict(pos=dict(x=3, y=5), neutralType='weaponShop'))
        payload['weaponShopList'] = [dict(name='Medicine', price=10)]
        self.assertEqual(plan(payload)[1], dict(action='buy', name='Medicine', num=1))
        payload['teamOur']['goldNum'] = 9
        self.assertNotIn(1, plan(payload))
        payload['teamOur']['goldNum'] = 10
        payload['teamOur']['roles'][1]['backpack'] = ['Medicine']
        self.assertNotIn(1, plan(payload))

    def test_sells_surplus_but_keeps_stone_for_morning(self):
        payload = board()
        payload['teamOur']['roles'][0]['backpack'] = ['stone'] * 12
        payload['vendorShopList'] = [dict(name='stone', price=2)]
        payload['mapInfo']['zones'].append(dict(pos=dict(x=3, y=5), neutralType='vendor'))
        self.assertEqual(plan(payload)[1], dict(action='sell', name='stone', num=2))

    def test_no_hidden_fields_or_mutation_or_night_builds(self):
        payload = board()
        original = deepcopy(payload)
        result = plan(payload)
        self.assertEqual(payload, original)
        payload['_demo'] = dict(seed=1234, futureWave='secret')
        self.assertEqual(plan(payload), result)
        self.assertFalse(any(c['action'] == 'build' for c in result.values()))


if __name__ == '__main__':
    unittest.main()
