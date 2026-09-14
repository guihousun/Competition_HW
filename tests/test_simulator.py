import copy
import unittest
from unittest.mock import patch

from test_baseline import fixture
from agent.simulator import step

# The official sample has no local lifecycle. Generated scenarios carry a private
# `_demo` block; tests that need waves/mines/revives add it explicitly so the
# strategy's day plan (build/sell) stays out of the way of the rule under test.
DEMO = {"seed": 1, "pressure": 1, "mines": {}, "dead": {}, "erases": {},
        "kills": 0, "finished": False, "waves": 0, "elapsed": 0}


def quiet(payload):
    """Fixture whose only commands are the ones a test submits."""
    payload["_demo"] = copy.deepcopy(DEMO)
    return payload


class SimulatorTests(unittest.TestCase):
    def test_progress_and_input_unchanged(self):
        p = fixture()
        original = copy.deepcopy(p)
        r = step(p)
        self.assertEqual(p, original)
        self.assertEqual(r['state']['roundNo'], 86)
        worker = next(u for u in r['state']['teamOur']['roles'] if u['id'] == 10010)
        self.assertEqual(worker['pos'], {'x': 6, 'y': 22})
        for _ in range(40):
            r = step(r['state'])
            if r['done']:
                break
        self.assertGreater(r['state']['roundNo'], 86)
        self.assertTrue(all(u['health'] >= 0 for u in r['state']['teamOur']['roles']))

    def simulate(self, p, commands):
        with patch('agent.simulator.decide', return_value=commands):
            return step(p, commands)

    def test_collision(self):
        p = quiet(fixture())
        p['roundNo'] = 1
        workers = [u for u in p['teamOur']['roles'] if u['roleType'] == 'worker']
        workers[0]['pos'] = {'x': 6, 'y': 10}
        workers[1]['pos'] = {'x': 8, 'y': 10}
        cmd = {str(u['id']): {'action': 'move', 'targetPos': [{'x': 7, 'y': 10}]} for u in workers}
        r = self.simulate(p, cmd)
        self.assertEqual(r['state']['lastRoundRoleActionResults'], {'10010': False, '10012': False})

    def test_build_and_collect(self):
        p = quiet(fixture())
        p['roundNo'] = 1
        worker = next(u for u in p['teamOur']['roles'] if u['id'] == 10010)
        worker['pos'] = {'x': 8, 'y': 21}
        r = self.simulate(p, {'10010': {'action': 'build', 'name': 'wall', 'targetPos': [{'x': 8, 'y': 22}]}})
        w = next(u for u in r['state']['teamOur']['roles'] if u['id'] == 10010)
        self.assertNotIn('stone', w['backpack'])
        self.assertEqual(len(r['state']['teamOur']['roles']), len(p['teamOur']['roles']) + 1)
        worker['pos'] = {'x': 5, 'y': 23}
        r = self.simulate(p, {'10010': {'action': 'collect', 'targetPos': [{'x': 4, 'y': 24}]}})
        w = next(u for u in r['state']['teamOur']['roles'] if u['id'] == 10010)
        self.assertEqual(w['backpack'].count('stone'), 2)

    def test_attack_cooldown_and_terminal(self):
        p = quiet(fixture())
        worker = next(u for u in p['teamOur']['roles'] if u['id'] == 10010)
        worker['pos'] = {'x': 8, 'y': 25}
        p['robot']['roles'] = [{'id': 1, 'pos': {'x': 7, 'y': 25}, 'health': 40}]
        cmd = {'10040': {'action': 'attack', 'controllerId': '10010', 'targetPos': [{'x': 7, 'y': 25}]}}
        r = self.simulate(p, cmd)
        self.assertEqual(r['state']['robot']['roles'][0]['health'], 20)
        tower = next(u for u in r['state']['teamOur']['roles'] if u['id'] == 10040)
        self.assertEqual(tower['cooldown'], 3)
        r2 = self.simulate(r['state'], cmd)
        self.assertFalse(r2['state']['lastRoundRoleActionResults']['10040'])
        p['roundNo'] = 1300
        self.assertTrue(step(p)['done'])


if __name__ == '__main__':
    unittest.main()
