"""User-supplied robot route deviation boundary, kept separate from combat range."""
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))

from agent.scenarios import scenario
from agent.simulator import ROBOT_DEVIATION_RADIUS, _plan_robot_actions


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
        self.assertEqual(at_three[0], {}, 'distance 3 can engage the nearby role')
        self.assertEqual(at_three[1][0]['victim'], 10011)
        self.assertEqual(at_four[1], [], 'distance 4 must not create a chase attack')
        self.assertTrue(at_four[0].get('900'), 'distance 4 keeps a base-directed move')

    def test_other_team_robot_never_deviates_into_our_roles(self):
        moves, attacks = _plan_robot_actions(self.state((13, 21), target='defender'))
        self.assertEqual(attacks, [])
        self.assertEqual(moves, {}, 'other-team robots are not redirected by our policy')


if __name__ == '__main__':
    unittest.main()
