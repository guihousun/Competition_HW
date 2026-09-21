"""Independent R02/R07 audit must not invent a third task-point cell."""
import sys
import unittest
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmark import own_task_cells, may_accept_from


def payload(team, explicit=False, reverse=False):
    other = 'defender' if team == 'challenger' else 'challenger'
    cells = [
        {'neutralType': f'{team}TaskPoint1', 'pos': {'x': 14, 'y': 14}},
        {'neutralType': f'{team}TaskPoint2', 'pos': {'x': 16, 'y': 17}},
        {'neutralType': f'{other}TaskPoint2', 'pos': {'x': 26, 'y': 17}},
        {'neutralType': 'vendor', 'pos': {'x': 20, 'y': 16}},
        {'neutralType': 'weaponShop', 'pos': {'x': 25, 'y': 20}},
        {'neutralType': 'stone', 'pos': {'x': 7, 'y': 17}},
        {'neutralType': f'{team}UnknownTaskPoint', 'pos': {'x': 30, 'y': 15}},
    ]
    if explicit:
        cells.append({'neutralType': f'{team}TaskPoint2', 'pos': {'x': 17, 'y': 17}})
    if reverse:
        cells.reverse()
    return {'teamOur': {'type': team}, 'mapInfo': {'width': 41, 'height': 32, 'zones': cells}}


class BenchmarkTaskCellsTests(unittest.TestCase):
    def test_anchor_and_explicit_pair_have_identical_exact_cells_for_both_teams(self):
        expected = [{'x': 14, 'y': 14}, {'x': 16, 'y': 17}, {'x': 17, 'y': 17}]
        for team in ('challenger', 'defender'):
            for explicit in (False, True):
                for reverse in (False, True):
                    with self.subTest(team=team, explicit=explicit, reverse=reverse):
                        p = payload(team, explicit, reverse)
                        before = deepcopy(p)
                        self.assertEqual(own_task_cells(p), expected)
                        self.assertEqual(p, before)

    def test_real_second_cell_ring_is_accepted_but_phantom_third_cell_ring_is_not(self):
        for team in ('challenger', 'defender'):
            for explicit in (False, True):
                with self.subTest(team=team, explicit=explicit):
                    p = payload(team, explicit)
                    self.assertTrue(may_accept_from(p, {'x': 18, 'y': 17}))
                    self.assertFalse(may_accept_from(p, {'x': 19, 'y': 17}))
                    self.assertFalse(may_accept_from(p, {'x': 26, 'y': 18}))
                    self.assertFalse(may_accept_from(p, {'x': 20, 'y': 16}))

    def test_duplicate_anchor_does_not_suppress_its_second_cell(self):
        p = payload('challenger')
        p['mapInfo']['zones'].append(deepcopy(p['mapInfo']['zones'][1]))
        self.assertIn({'x': 17, 'y': 17}, own_task_cells(p))
        self.assertNotIn({'x': 18, 'y': 17}, own_task_cells(p))


if __name__ == '__main__':
    unittest.main()
