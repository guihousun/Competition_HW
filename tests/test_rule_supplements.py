"""User-provided rule supplements; independent geometry and growth checks."""
from copy import deepcopy
import unittest

from test_baseline import fixture
from agent.scenarios import scenario, prepare_round
from agent.simulator import step
from agent.debug import RULE_ROWS


class RuleSupplementTests(unittest.TestCase):
    def test_friendly_wall_does_not_block_gatling_railgun_or_rocket(self):
        for kind, expected_damage in [('gatling', 10), ('railgun', 10), ('rocket', 20)]:
            with self.subTest(kind=kind):
                state = fixture()
                state['roundNo'] = 85
                def unit(uid, kind, x, y, health):
                    return dict(id=uid, roleType=kind, pos={'x': x, 'y': y},
                                health=health, level=1, backpack=[], backPackCapability=100)
                state['teamOur']['roles'] = [unit(1, 'station', 5, 20, 1500),
                    unit(2, kind, 10, 10, 1000), unit(3, 'worker', 10, 11, 220),
                    unit(4, 'wall', 11, 10, 1000)]
                state['teamEnemy']['roles'] = []
                state['mapInfo']['zones'] = []
                state['robot']['roles'] = [unit(99, 'smallRobot', 12, 10, 40)]
                response = step(state, {'2': {'action': 'attack', 'controllerId': '3',
                                              'targetPos': [{'x': 12, 'y': 10}]}})
                self.assertTrue(response['state']['lastRoundRoleActionResults']['2'])
                target = next(r for r in response['state']['robot']['roles'] if r['id'] == 99)
                self.assertEqual(40 - expected_damage, target['health'])

    def test_basic_wave_size_increases_over_ten_nights_on_clear_boards(self):
        for side in ('challenger', 'defender'):
            for pressure in (1, 2, 3):
                with self.subTest(side=side, pressure=pressure):
                    initial = scenario(7, side, pressure)
                    counts = []
                    for night in range(1, 11):
                        state = deepcopy(initial)
                        state['roundNo'] = (night - 1) * 130 + 71
                        counts.append(len(prepare_round(state, [])))
                    self.assertGreater(counts[0], 0)
                    self.assertTrue(all(a < b for a, b in zip(counts, counts[1:])), counts)

    def test_random_spawn_is_labelled_as_conflicting_with_fixed_spawn_rule(self):
        row = next(row for row in RULE_ROWS if row['id'] == 'R05/S03')
        self.assertEqual('approx', row['status'])
        self.assertIn('冲突', row['note'])


if __name__ == '__main__':
    unittest.main()
