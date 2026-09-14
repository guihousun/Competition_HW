"""Configured tower counts must survive site ordering and simultaneous builds."""
from collections import Counter
import unittest
from unittest.mock import patch
from test_baseline import ROOT
from agent import brain
from agent.scenarios import scenario
from agent.simulator import step


class LoadoutTests(unittest.TestCase):
    def test_mixed_loadouts_construct_exact_roster_on_both_sides(self):
        for loadout in [('gatling','railgun','rocket'), ('rocket','rocket','railgun')]:
            for side in ['challenger','defender']:
                with self.subTest(loadout=loadout, side=side), patch.object(brain,'TOWER_LOADOUT',loadout):
                    state = scenario(1,side,1)
                    built = Counter()
                    for _ in range(35):
                        state = step(state)['state']
                        built = Counter(u['roleType'] for u in state['teamOur']['roles']
                                        if u['health'] > 0 and u['roleType'] in ('gatling','railgun','rocket'))
                        if sum(built.values()) == 3: break
                    self.assertEqual(Counter(loadout),built)


if __name__ == '__main__': unittest.main()
