"""Independent travel/action/budget expectations for treasure admission (R02/R06/R07)."""
import unittest
from unittest.mock import patch
from test_metal_economy import board
from agent import brain
from agent.protocol import Turn


def situation(gold=30, closes=50, held=(), items=('AcientTablet', 'IronWhistle')):
    state = board(gold=gold, round_no=20, walls='missing', towers=0)
    pioneer = next(r for r in state['teamOur']['roles'] if r['roleType'] == 'pioneer')
    pioneer.update(pos={'x': 20, 'y': 20}, backpack=list(held))
    state['mapInfo']['zones'].append({'pos': {'x': 21, 'y': 20}, 'neutralType': 'weaponShop'})
    state['weaponShopList'] += [{'name': 'AcientTablet', 'price': 15}, {'name': 'IronWhistle', 'price': 15}]
    notes = {'known': True, 'open': True, 'taken': False, 'site': {'x': 23, 'y': 20},
             'items': list(items), 'opensAt': 1, 'closesAt': closes}
    return state, notes


class TreasureSchedulingTests(unittest.TestCase):
    def route(self, state, notes, commands=None):
        turn = Turn.load(state)
        return brain._treasure_route(turn, state, turn.pioneer(), notes, commands)

    def test_full_cart_not_one_affordable_item_controls_admission(self):
        state, notes = situation(gold=29)
        self.assertIsNone(self.route(state, notes))
        state['teamOur']['goldNum'] = 30
        self.assertIsNotNone(self.route(state, notes))
        state['teamOur']['goldNum'] = 110
        self.assertIsNone(self.route(state, notes, {10010: {'action': 'buy', 'name': 'WeaponUpgradeVoucher1'}}))

    def test_duplicate_material_counts_only_missing_copies(self):
        state, notes = situation(gold=15, held=['AcientTablet'], items=['AcientTablet', 'AcientTablet'])
        route = self.route(state, notes)
        self.assertEqual(['AcientTablet'], route['missing'])
        self.assertEqual(23, route['summon_round'])  # buy20, move21+22, summon23

    def test_buy_move_and_summon_are_distinct_rounds(self):
        state, notes = situation(closes=23)
        self.assertIsNone(self.route(state, notes))  # buy20+21, move22+23, summon24
        notes['closesAt'] = 24
        self.assertEqual(24, self.route(state, notes)['summon_round'])
        notes['closesAt'] = 19
        turn = Turn.load(state)
        commands = {}
        with patch.object(brain, '_treasure_notes', return_value=notes):
            self.assertFalse(brain._treasure_errand(turn, turn.pioneer(), commands, state, None))
        self.assertEqual({}, commands, 'no purchases after expiry, even while already beside the shop')

    def test_sealed_altar_is_not_reachable_by_straight_line(self):
        state, notes = situation(held=['AcientTablet', 'IronWhistle'])
        state['mapInfo']['zones'] += [{'pos': {'x': x, 'y': y}, 'neutralType': 'terrain'}
            for x in range(22, 25) for y in range(19, 22) if (x, y) != (23, 20)]
        self.assertIsNone(self.route(state, notes))

    def test_existing_task_cannot_be_displaced_by_any_treasure_entry(self):
        state, notes = situation(held=['AcientTablet', 'IronWhistle'])
        state['phaseTask'] = '已接受且尚未完成的任务'
        turn = Turn.load(state)
        commands = {}
        with patch.object(brain, '_treasure_notes', return_value=notes):
            self.assertFalse(brain._treasure_claims_pioneer(turn, state, turn.pioneer()))
            self.assertIsNone(brain._treasure_step(turn, state, turn.pioneer()))
            self.assertFalse(brain._treasure_errand(turn, turn.pioneer(), commands, state, None))
        self.assertEqual({}, commands)

    def test_future_window_with_materials_does_not_idle_the_task_pipeline(self):
        state, notes = situation(held=['AcientTablet', 'IronWhistle'], closes=160)
        notes.update(open=False, opensAt=131)
        turn = Turn.load(state)
        with patch.object(brain, '_treasure_notes', return_value=notes):
            self.assertFalse(brain._treasure_claims_pioneer(turn, state, turn.pioneer()))
            self.assertFalse(brain._treasure_errand(turn, turn.pioneer(), {}, state, None))

    def test_known_night_window_can_still_summon_when_defence_allows_it(self):
        # R07 specifies the inferred window, not a blanket daytime-only rule.
        state, notes = situation(held=['AcientTablet', 'IronWhistle'], closes=128)
        state['roundNo'] = 88
        next(r for r in state['teamOur']['roles'] if r['roleType'] == 'pioneer')['pos'] = {'x': 22, 'y': 20}
        notes['opensAt'] = 88
        turn = Turn.load(state)
        self.assertFalse(turn.is_day)
        with patch.object(brain, '_treasure_notes', return_value=notes):
            self.assertEqual('summonTreasure', brain._treasure_step(turn, state, turn.pioneer())['action'])

    def test_capacity_and_unknown_price_do_not_start_a_partial_trip(self):
        state, notes = situation()
        pioneer = next(r for r in state['teamOur']['roles'] if r['roleType'] == 'pioneer')
        pioneer['backPackCapability'] = 1
        self.assertIsNone(self.route(state, notes))
        pioneer['backPackCapability'] = 40
        state['weaponShopList'] = [r for r in state['weaponShopList'] if r['name'] != 'IronWhistle']
        self.assertIsNone(self.route(state, notes))


if __name__ == '__main__':
    unittest.main()
