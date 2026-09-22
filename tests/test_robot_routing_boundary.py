"""User-supplied robot route deviation boundary, kept separate from combat range."""
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))

from agent.scenarios import scenario
from agent.simulator import ROBOT_ATTACK_RANGE, ROBOT_DEVIATION_RADIUS, _plan_robot_actions, _stable_base_goal
from agent.protocol import Pos, Turn


class RobotRoutingBoundaryTests(unittest.TestCase):
    def state(self, pos, target='challenger'):
        state = scenario(1, 'challenger')
        state['roundNo'] = 71
        state['robot']['roles'] = [{
            'id': 900, 'roleType': 'smallRobot', 'pos': {'x': pos[0], 'y': pos[1]},
            'health': 40, 'targetTeam': target, 'abnormalState': '',
        }]
        return state

    def test_three_cells_can_deviate_but_four_cells_keeps_base_route(self):
        self.assertEqual(ROBOT_DEVIATION_RADIUS, 3)
        at_three = _plan_robot_actions(self.state((12, 22)))
        at_four = _plan_robot_actions(self.state((13, 21)))
        # Pioneer is (9,25): Chebyshev distances are 3 and 4. At three the
        # local model may target the nearby role; at four it stays base-directed.
        self.assertEqual(ROBOT_ATTACK_RANGE, 1)
        self.assertEqual(at_three[1], [], 'distance 3 can redirect, but melee cannot attack yet')
        self.assertTrue(at_three[0].get('900'), 'distance 3 should move toward the nearby role')
        self.assertEqual(at_four[1], [], 'distance 4 must not create a chase attack')
        self.assertTrue(at_four[0].get('900'), 'distance 4 keeps a base-directed move')

    def test_other_team_robot_never_deviates_into_our_roles(self):
        moves, attacks = _plan_robot_actions(self.state((13, 21), target='defender'))
        self.assertEqual(attacks, [])
        self.assertEqual(moves, {}, 'other-team robots are not redirected by our policy')

    def test_base_entry_goal_stays_stable_and_does_not_reverse_vertical_direction(self):
        state = self.state((20, 26))
        turn = Turn.load(state)
        self.assertEqual(_stable_base_goal(turn, Pos(20, 26)), Pos(8, 26))
        state['teamOur']['roles'] = [r for r in state['teamOur']['roles']
                                     if r['roleType'] == 'station']
        positions = []
        for _ in range(6):
            positions.append(tuple(state['robot']['roles'][0]['pos'].values()))
            result = __import__('agent.simulator', fromlist=['step']).step(state)
            state = result['state']
        self.assertEqual([y for _x, y in positions], [26] * 6)


if __name__ == '__main__':
    unittest.main()
