"""Hand-worked policy expectations for R02/R03/R06; no simulator oracle."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_baseline import ROOT
from agent import brain
from agent.protocol import Turn, Pos, move_command
from agent.coordination import available_gold, reconcile


def unit(uid, kind, x, y, health=220):
    return dict(id=uid, roleType=kind, pos=dict(x=x, y=y), health=health,
                level=1, backPackCapability=100, backpack=[])


def observation():
    return dict(roundNo=1, mapInfo=dict(width=41, height=32, zones=[
        dict(pos=dict(x=10, y=10), neutralType='weaponShop')]),
        teamOur=dict(goldNum=14, roles=[unit(1, 'worker', 9, 10),
            unit(2, 'worker', 10, 9), unit(3, 'pioneer', 20, 20, 0),
            unit(4, 'wall', 30, 20, 500)]), teamEnemy=dict(roles=[]),
        weaponShopList=[dict(name='Medicine', price=10), dict(name='WallFixer', price=10)])


def final_plan(payload, proposed, late_move=None):
    def day(turn, commands, state, planner_state):
        commands.update(deepcopy(proposed))
    with patch.object(brain, '_day', day), patch.object(brain, '_night', day), \
            patch.object(brain, '_task_step', return_value=None), \
            patch.object(brain, '_treasure_step', return_value=None), \
            patch.object(brain, '_task_walk', return_value=(late_move is not None, late_move)):
        return brain.plan_for_state(payload, SimpleNamespace(tasks={}),
                                    judge_tasks=False).commands


class CoordinationRegressionTests(unittest.TestCase):
    def test_two_workers_cannot_spend_fourteen_as_twenty(self):
        payload = observation()
        turn = Turn.load(payload)
        commands = {}
        for worker in turn.workers():
            brain._try_trade(turn, worker, commands, payload)
        self.assertEqual(commands, {1: dict(action='buy', name='Medicine', num=1)})

    def test_late_task_walk_cannot_reintroduce_contested_destination(self):
        payload = observation()
        payload['teamOur']['roles'] = [unit(1, 'worker', 4, 5), unit(2, 'pioneer', 6, 5)]
        result = final_plan(payload, {1: move_command(Pos(5, 5)),
                                     2: move_command(Pos(7, 5))}, move_command(Pos(5, 5)))
        self.assertEqual(result, {'1': move_command(Pos(5, 5))})

    def test_both_purchases_are_kept_when_budget_is_exactly_twenty(self):
        payload = observation()
        payload['teamOur']['goldNum'] = 20
        turn, commands = Turn.load(payload), {}
        for worker in turn.workers():
            brain._try_trade(turn, worker, commands, payload)
        self.assertEqual([cmd['name'] for cmd in commands.values()], ['Medicine', 'WallFixer'])

    def test_prior_treasure_purchase_reserves_money_before_tower_construction(self):
        payload = observation()
        payload['teamOur']['goldNum'] = 30
        payload['teamOur']['roles'][0]['pos'] = dict(x=4, y=5)
        turn = Turn.load(payload)
        commands = {3: dict(action='buy', name='Medicine')}
        brain._worker_day(turn, turn.workers()[0], (Pos(5, 5),), [Pos(5, 5)], [],
                          set(), commands, payload, set())
        self.assertEqual(commands, {3: dict(action='buy', name='Medicine')})

    def test_queued_tower_reserves_money_before_buying(self):
        payload = observation()
        payload['teamOur']['goldNum'] = 30
        turn = Turn.load(payload)
        commands = {2: dict(action='build', name='rocket', targetPos=[dict(x=11, y=9)])}
        self.assertFalse(brain._try_trade(turn, turn.workers()[0], commands, payload))
        self.assertEqual(len(commands), 1)

    def test_bulk_prices_and_replaced_intents_release_reservations(self):
        payload = observation()
        payload['teamOur']['goldNum'] = 50
        payload['weaponShopList'][0]['price'] = 7
        turn = Turn.load(payload)
        commands = {1: dict(action='buy', name='Medicine', num=3),
                    2: dict(action='build', name='railgun')}
        self.assertEqual(available_gold(turn, payload, commands), 4)
        self.assertEqual(available_gold(turn, payload, commands, replacing=1), 25)
        commands[1] = move_command(Pos(8, 10))
        self.assertEqual(available_gold(turn, payload, commands), 25)
        del commands[2]
        self.assertEqual(available_gold(turn, payload, commands), 50)

    def test_sales_cannot_fund_same_round_spending(self):
        payload = observation()
        payload['teamOur']['goldNum'] = 9
        proposed = {1: dict(action='sell', name='iron', num=100),
                    2: dict(action='buy', name='Medicine')}
        self.assertEqual(final_plan(payload, proposed), {'1': proposed[1]})

    def test_final_budget_gate_covers_late_or_future_spending_branches(self):
        payload = observation()
        proposed = {1: dict(action='buy', name='Medicine'),
                    2: dict(action='buy', name='WallFixer')}
        self.assertEqual(final_plan(payload, proposed), {'1': proposed[1]})

    def test_unknown_or_invalid_purchase_not_assumed_free(self):
        payload = observation()
        for command in [dict(action='buy', name='unpublished'),
                        dict(action='buy', name='Medicine', num=-1),
                        dict(action='buy', name='Medicine', num=True)]:
            with self.subTest(command=command):
                self.assertEqual(final_plan(payload, {1: command}), {})

    def test_zero_price_is_observed_and_walls_spend_no_gold(self):
        payload = observation()
        payload['teamOur']['goldNum'] = 0
        payload['weaponShopList'][0]['price'] = 0
        proposed = {1: dict(action='buy', name='Medicine'),
                    2: dict(action='build', name='wall', targetPos=[dict(x=11, y=9)])}
        self.assertEqual(final_plan(payload, proposed), {str(k): v for k, v in proposed.items()})


class MovementReservationTests(unittest.TestCase):
    def setUp(self):
        self.payload = observation()
        self.payload['teamOur']['roles'] = [unit(1, 'worker', 4, 5), unit(2, 'pioneer', 6, 5)]

    def resolve(self, proposed):
        return reconcile(Turn.load(self.payload), self.payload, proposed)

    def test_rotation_is_deterministic_independent_of_insertion_order(self):
        proposed = {1: move_command(Pos(5, 5)), 2: move_command(Pos(5, 5))}
        for round_no, winner in [(1, 1), (2, 2), (3, 1)]:
            self.payload['roundNo'] = round_no
            self.assertEqual(self.resolve(proposed), {winner: proposed[winner]})
            self.assertEqual(self.resolve(dict(reversed(list(proposed.items())))),
                             {winner: proposed[winner]})

    def test_independent_moves_and_diagonal_moves_are_preserved(self):
        proposed = {1: move_command(Pos(5, 6)), 2: move_command(Pos(7, 6))}
        self.assertEqual(self.resolve(proposed), proposed)

    def test_swap_and_occupied_chain_are_conservatively_held(self):
        self.payload['teamOur']['roles'][1]['pos'] = dict(x=5, y=5)
        swap = {1: move_command(Pos(5, 5)), 2: move_command(Pos(4, 5))}
        self.assertEqual(self.resolve(swap), {})
        chain = {1: move_command(Pos(5, 5)), 2: move_command(Pos(6, 5))}
        self.assertEqual(self.resolve(chain), {2: chain[2]})

    def test_task_hold_is_not_displaced(self):
        self.payload['teamOur']['roles'][1]['pos'] = dict(x=5, y=5)
        proposed = {1: move_command(Pos(5, 5)), 2: dict(action='acceptTask')}
        self.assertEqual(self.resolve(proposed), {2: proposed[2]})

    def test_move_cannot_enter_same_round_build_site(self):
        proposed = {1: move_command(Pos(5, 5)),
                    2: dict(action='build', name='wall', targetPos=[dict(x=5, y=5)])}
        self.assertEqual(self.resolve(proposed), {2: proposed[2]})

    def test_rejected_unaffordable_build_does_not_reserve_cell(self):
        proposed = {1: move_command(Pos(5, 5)),
                    2: dict(action='build', name='rocket', targetPos=[dict(x=5, y=5)])}
        self.assertEqual(self.resolve(proposed), {1: proposed[1]})

    def test_terrain_robot_enemy_and_base_footprint_stay_blocked(self):
        for kind in ['terrain', 'robot', 'enemy', 'base']:
            payload = deepcopy(self.payload)
            if kind == 'terrain':
                payload['mapInfo']['zones'].append(dict(pos=dict(x=5, y=5), neutralType='iron'))
            elif kind == 'robot':
                payload['robot'] = dict(roles=[dict(id=90, pos=dict(x=5, y=5), health=40)])
            elif kind == 'enemy':
                payload['teamEnemy']['roles'] = [unit(90, 'worker', 5, 5)]
            else:
                payload['teamEnemy']['roles'] = [unit(90, 'station', 5, 6)]
            with self.subTest(kind=kind):
                self.assertEqual(reconcile(Turn.load(payload), payload, {1: move_command(Pos(5, 5))}), {})

    def test_planning_has_no_hidden_reservation_state(self):
        proposed = {1: move_command(Pos(5, 5)), 2: move_command(Pos(5, 5))}
        original = deepcopy((self.payload, proposed))
        first = self.resolve(proposed)
        self.assertEqual(self.resolve(proposed), first)
        self.assertEqual((self.payload, proposed), original)
        self.payload['_demo'] = dict(seed=987654321, future='unavailable')
        self.assertEqual(self.resolve(proposed), first)

    def test_boxed_in_mover_gets_an_opening_from_idle_worker(self):
        self.payload['teamOur']['roles'] = [unit(1, 'worker', 4, 5), unit(2, 'pioneer', 5, 5)]
        self.payload['mapInfo']['zones'] = [dict(pos=dict(x=x, y=y), neutralType='wall')
            for x in (4, 5, 6) for y in (4, 5, 6) if (x, y) not in [(4, 5), (5, 5)]]
        proposed = {2: move_command(Pos(5, 4))}
        self.assertEqual(self.resolve(proposed), {1: move_command(Pos(3, 4))})
        self.payload['teamOur']['roles'][0]['pos'] = dict(x=3, y=4)
        self.payload['roundNo'] = 2
        self.assertEqual(self.resolve({2: move_command(Pos(4, 5))}),
                         {2: move_command(Pos(4, 5))})

    def test_yield_never_interrupts_an_active_worker_or_controller(self):
        self.payload['teamOur']['roles'] = [unit(1, 'worker', 4, 5), unit(2, 'pioneer', 5, 5)]
        self.payload['mapInfo']['zones'] = [dict(pos=dict(x=x, y=y), neutralType='wall')
            for x in (4, 5, 6) for y in (4, 5, 6) if (x, y) not in [(4, 5), (5, 5)]]
        move = {2: move_command(Pos(5, 4))}
        for active in [{1: dict(action='collect', targetPos=[dict(x=3, y=5)])},
                       {10: dict(action='attack', controllerId='1', targetPos=[dict(x=3, y=5)])}]:
            with self.subTest(active=active):
                self.assertEqual(self.resolve({**move, **active}), active)

    def test_yield_opens_exit_instead_of_only_moving_blockage_deeper(self):
        self.payload['teamOur']['roles'] = [unit(1, 'worker', 5, 6),
            unit(2, 'worker', 4, 5), unit(3, 'pioneer', 5, 5)]
        free = {(5, 5), (5, 6), (5, 7), (4, 5), (3, 4), (3, 5), (2, 5), (2, 4), (1, 5)}
        self.payload['mapInfo']['zones'] = [dict(pos=dict(x=x, y=y), neutralType='wall')
            for x in range(41) for y in range(32) if (x, y) not in free]
        self.assertEqual(self.resolve({3: move_command(Pos(5, 4))}),
                         {2: move_command(Pos(3, 4))})


if __name__ == '__main__':
    unittest.main()
