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

    def test_four_cells_can_deviate_but_five_cells_keeps_base_route(self):
        self.assertEqual(ROBOT_DEVIATION_RADIUS, 4)
        at_four = _plan_robot_actions(self.state((13, 21)))
        at_five = _plan_robot_actions(self.state((14, 22)))
        # Pioneer is (9,25): Chebyshev distances are 4 and 5. At four the
        # local model may target the nearby role; at five it advances toward base.
        self.assertEqual(at_four[1], [], 'distance 4 changes route, not attack range')
        self.assertEqual(at_four[0]['900'].dump(), {'x': 12, 'y': 22},
                         'distance 4 may deviate toward the nearby role')
        self.assertEqual(at_five[1], [], 'distance 5 must not create a chase attack')
        self.assertNotEqual(at_five[0].get('900'), at_four[0]['900'],
                            'distance 5 keeps the base-directed route')

    def test_other_team_robot_never_deviates_into_our_roles(self):
        moves, attacks = _plan_robot_actions(self.state((13, 21), target='defender'))
        self.assertEqual(attacks, [])
        self.assertEqual(moves, {}, 'other-team robots are not redirected by our policy')


if __name__ == '__main__':
    unittest.main()
