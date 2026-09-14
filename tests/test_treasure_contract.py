"""Interface 1.1/2.2 independent treasure result and spending expectations."""
from copy import deepcopy
import unittest
from test_treasure import state_with_pioneer, open_rite, treasure, Pos, scenario, step


class TreasureContractTests(unittest.TestCase):
    def test_exact_item_multiset_extra_missing_wrong_and_duplicate(self):
        for offered in (['AncientScroll'], ['IronWhistle', 'IronWhistle'],
                        ['AncientScroll', 'IronWhistle', 'StarSand']):
            with self.subTest(offered=offered):
                state = state_with_pioneer(offered + ['stone'])
                open_rite(state)
                result = treasure.summon(state, side='challenger', pioneer_id=10011,
                                         site=Pos(5, 6), items=offered, round_no=10)
                self.assertEqual(result['result'], 3)
                self.assertEqual(state['lastSummonTreasureResult'], 3)
                self.assertEqual(state['teamOur']['roles'][0]['backpack'], ['stone'])
                self.assertEqual(state['teamOur']['totalScore'], 0)
                self.assertEqual(state['teamOur']['goldNum'], 75)
                self.assertFalse(treasure.rite_of(state).opened)

    def test_wrong_target_does_not_use_the_pioneer_position_to_award(self):
        for side in ('challenger', 'defender'):
            state = state_with_pioneer(['AncientScroll', 'IronWhistle'])
            state['teamOur']['type'] = side  # no role-ID-prefix assumptions
            open_rite(state, site=(5, 6))
            result = treasure.summon(state, side=side, pioneer_id=10011,
                                     site=Pos(6, 5), items=['AncientScroll', 'IronWhistle'], round_no=10)
            self.assertEqual(result['result'], 2)
            self.assertEqual(state['teamOur']['roles'][0]['backpack'], [])
            self.assertFalse(treasure.rite_of(state).opened)

    def test_illegal_target_and_missing_duplicate_inventory_do_not_spend(self):
        for target, offered in ((Pos(30, 20), ['AncientScroll']),
                                (Pos(5, 6), ['AncientScroll', 'AncientScroll'])):
            state = state_with_pioneer(['AncientScroll'])
            open_rite(state)
            with self.assertRaises(treasure.Refusal):
                treasure.summon(state, side='challenger', pioneer_id=10011,
                                site=target, items=offered, round_no=10)
            self.assertEqual(state['teamOur']['roles'][0]['backpack'], ['AncientScroll'])
            self.assertEqual(state['lastSummonTreasureResult'], 0)

    def test_private_rite_and_future_clues_never_fill_policy_notes(self):
        state = state_with_pioneer()
        open_rite(state)
        state['worldNews']['folkLegends'] = '第一日只有一个模糊地点线索'
        before = treasure.treasure_notes(state, 'challenger', 1)
        state['_demo']['treasure']['site'] = {'x': 31, 'y': 22}
        state['_demo']['treasure']['opened'] = True
        state['_demo']['treasure']['items'] = ['StarSand']
        self.assertEqual(treasure.treasure_notes(state, 'challenger', 1), before)
        self.assertFalse(before['known'])
        self.assertNotIn('site', before)

    def test_settlement_does_not_replace_the_published_clue_with_private_answer(self):
        state = state_with_pioneer(['AncientScroll', 'IronWhistle'])
        open_rite(state)
        state['worldNews']['folkLegends'] = '已发布的局部线索'
        treasure.summon(state, side='challenger', pioneer_id=10011,
                        site=Pos(5, 6), items=['AncientScroll', 'IronWhistle'], round_no=10)
        self.assertEqual(state['worldNews']['folkLegends'], '已发布的局部线索')

    def test_simulator_publishes_result2_spends_items_and_resets_next_round(self):
        for side in ('challenger', 'defender'):
            state = scenario(90601, side, 1)
            pioneer = next(r for r in state['teamOur']['roles'] if r['roleType'] == 'pioneer')
            pos = Pos.load(pioneer['pos'])
            target = Pos(pos.x + 1, pos.y)
            rite = treasure.rite_of(state)
            # Place the hidden fixture elsewhere; the actual requested target is legal.
            rite.site = {'x': 1, 'y': 1}
            rite.opens_at, rite.closes_at = 1, 1300
            treasure.attach(state, rite)
            pioneer['backpack'] = list(rite.items)
            result = step(state, {str(pioneer['id']): {'action': 'summonTreasure',
                            'targetPos': [target.dump()], 'item': list(rite.items)}})
            self.assertEqual(result['state']['lastSummonTreasureResult'], 2)
            self.assertTrue(result['state']['lastRoundRoleActionResults'][str(pioneer['id'])])
            self.assertNotIn('开启宝藏', ' '.join(result['events']))
            self.assertEqual(next(r for r in result['state']['teamOur']['roles']
                                 if r['id'] == pioneer['id'])['backpack'], [])
            state = step(result['state'], {})['state']
            self.assertEqual(state['lastSummonTreasureResult'], 0)


if __name__ == '__main__':
    unittest.main()
