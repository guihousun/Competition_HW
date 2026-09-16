"""Geometry caching must preserve direction order and reusable traversal."""
from dataclasses import FrozenInstanceError
import unittest

from test_baseline import ROOT
from agent import construction_trip, team_trip
from agent.protocol import Pos


class GeometryCacheTests(unittest.TestCase):
    def test_repeated_traversal_has_original_order_and_frozen_values(self):
        expected = (Pos(4, 6), Pos(4, 7), Pos(4, 8), Pos(5, 6),
                    Pos(5, 8), Pos(6, 6), Pos(6, 7), Pos(6, 8))
        for adjacent in (team_trip.neighbours, construction_trip._neighbours):
            with self.subTest(function=adjacent.__module__):
                adjacent.cache_clear()
                result = adjacent(Pos(5, 7))
                self.assertEqual(tuple(result), expected)
                self.assertEqual(tuple(result), expected)  # not an exhausted generator
                self.assertIs(adjacent(Pos(5, 7)), result)
                with self.assertRaises(FrozenInstanceError):
                    result[0].x = 100

    def test_cache_bound_and_eviction_do_not_change_geometry(self):
        for adjacent in (team_trip.neighbours, construction_trip._neighbours):
            with self.subTest(function=adjacent.__module__):
                adjacent.cache_clear()
                first = adjacent(Pos(0, 0))
                for n in range(1, 4200):
                    adjacent(Pos(n, -n))
                self.assertEqual(adjacent.cache_info().maxsize, 4096)
                self.assertLessEqual(adjacent.cache_info().currsize, 4096)
                self.assertEqual(adjacent(Pos(0, 0)), first)
                adjacent.cache_clear()


if __name__ == '__main__':
    unittest.main()
