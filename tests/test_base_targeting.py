"""R02/R05: role-owned base footprints must be attackable from every side."""
from copy import deepcopy
import unittest
from test_robots import board, health_of
from agent.simulator import step


class BaseTargetingTests(unittest.TestCase):
    def test_base_is_hit_from_all_four_footprint_edges(self):
        for origin in [(9, 5), (12, 5), (10, 6), (11, 3)]:
            for zones in ('none', 'neutral-base-alias'):
                with self.subTest(origin=origin, zones=zones):
                    state = board(robot_pos=origin)
                    if zones == 'none': state['mapInfo']['zones'] = []
                    result = step(state, {})
                    self.assertEqual([1495], health_of(result['state'], 'station'))
                    attack = result['frame']['robotAttacks'][0]
                    self.assertEqual(10013, attack['building'])
                    self.assertEqual('station', attack['buildingKind'])

    def test_role_only_wall_still_protects_base(self):
        state = board(robot_pos=(8, 5), wall_pos=(9, 5))
        state['mapInfo']['zones'] = []
        result = step(state, {})
        self.assertEqual([995], health_of(result['state'], 'wall'))
        self.assertEqual([1500], health_of(result['state'], 'station'))

    def test_destroyed_wall_allows_later_base_damage(self):
        state = board(robot_pos=(8, 5), wall_pos=(9, 5))
        state['mapInfo']['zones'] = []
        state['teamOur']['roles'][1]['health'] = 5
        for _ in range(3): state = step(state, {})['state']
        self.assertEqual([0], health_of(state, 'wall'))
        self.assertEqual([1495], health_of(state, 'station'))

    def test_lethal_base_hit_is_recorded_and_input_is_not_mutated(self):
        state = board(robot_pos=(12, 4))
        state['teamOur']['roles'][0]['health'] = 5
        before = deepcopy(state)
        result = step(state, {})
        self.assertEqual(before, state)
        self.assertEqual([0], health_of(result['state'], 'station'))
        self.assertTrue(result['done'])  # existing single-team local terminal, not official 1v1


if __name__ == '__main__': unittest.main()
