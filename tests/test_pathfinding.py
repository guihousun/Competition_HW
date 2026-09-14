"""Pathfinding tests: the first step must be the start of a shortest route.

`next_step` returns only the first step of a route, and callers repeat it every
round to walk. So the properties that matter are:

* when a route exists the step is the first step of *some shortest route*
  (verified by re-running the search from the candidate, not by hard-coding a
  coordinate — several first steps are often equally optimal);
* repeating the call reaches the goal and never revisits a cell (the live symptom
  was a pioneer that shuffled between two cells);
* an unreachable goal ends the search at its bound instead of hanging.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))

from agent.grid import (MAX_EXPANSIONS, _cost_to_goal,  # noqa: E402
                        _reachable_in_one_hop, next_step)
from agent.protocol import Pos, distance  # noqa: E402


class FakeTurn:
    """The smallest Turn-shaped object `next_step` needs."""

    def __init__(self, width=41, height=32, walls=(), units=()):
        self.width = width
        self.height = height
        self.walls = set(walls)
        self.units = {(x, y) for x, y in units}

    def land(self, pos):
        if not (0 <= pos.x < self.width and 0 <= pos.y < self.height):
            return False
        return pos not in self.walls

    def blocked(self, moving):
        return set(self.walls) | {Pos(x, y) for x, y in self.units
                                  if Pos(x, y) != moving.pos}


class FakeUnit:
    def __init__(self, x, y):
        self.pos = Pos(x, y)


def P(x, y):
    return Pos(x, y)


def shortest_cost(turn, start, goal):
    """Shortest step count from `start` to `goal`, or None when unreachable."""
    cost = _cost_to_goal(turn, start, goal, turn.width * turn.height * 2)
    return None if cost >= 10 ** 9 else cost


class NextStepTests(unittest.TestCase):
    def assert_optimal_first_step(self, turn, start, goal, msg=''):
        step = next_step(turn, FakeUnit(start.x, start.y), goal)
        self.assertIsNotNone(step, f'{msg} 应当能找到路线')
        total = shortest_cost(turn, start, goal)
        self.assertIsNotNone(total, f'{msg} 目标应当可达')
        rest = shortest_cost(turn, step, goal)
        self.assertIsNotNone(rest, f'{msg} 第一步之后应当仍可达')
        self.assertEqual(rest + 1, total,
                         f'{msg} 第一步必须是某条最短路线的一部分：'
                         f'走 {step.dump()} 后还需 {rest} 步，最短共 {total} 步')
        return step

    def test_straight_line_is_optimal(self):
        self.assert_optimal_first_step(FakeTurn(), P(1, 1), P(10, 1), '直线')

    def test_diagonal_is_optimal(self):
        self.assert_optimal_first_step(FakeTurn(), P(1, 1), P(10, 10), '对角')

    def test_clear_progress_is_preferred_when_distances_differ(self):
        """With a tie between candidates the step that closes more distance wins."""
        turn = FakeTurn(width=41, height=32)
        start, goal = P(1, 15), P(12, 15)
        step = self.assert_optimal_first_step(turn, start, goal, '横向')
        self.assertLess(distance(step, goal), distance(start, goal),
                        '明显更近的一步应当被选中')

    def test_repeated_stepping_never_oscillates(self):
        wall = {P(5, y) for y in range(0, 31)}
        turn = FakeTurn(walls=wall)
        goal = P(9, 15)
        unit = FakeUnit(1, 15)
        seen = [(unit.pos.x, unit.pos.y)]
        for _ in range(80):
            step = next_step(turn, unit, goal)
            if step is None:
                break
            unit.pos = step
            seen.append((step.x, step.y))
        self.assertEqual(seen[-1], (goal.x, goal.y), f'应当到达目标，实际 {seen}')
        self.assertEqual(len(seen), len(set(seen)), f'路径不应重复格子: {seen}')

    def test_repeated_stepping_takes_the_shortest_number_of_rounds(self):
        turn = FakeTurn()
        goal = P(38, 30)
        unit = FakeUnit(2, 2)
        steps = 0
        while unit.pos != goal and steps < 200:
            step = next_step(turn, unit, goal)
            self.assertIsNotNone(step, '开阔地图上不应找不到路线')
            unit.pos = step
            steps += 1
        self.assertEqual(unit.pos.dump(), goal.dump())
        self.assertEqual(steps, distance(P(2, 2), goal),
                         '每回合一步，切比雪夫距离就是最少回合数')

    def test_detour_around_a_wall_is_optimal_and_shortens_distance(self):
        wall = {P(5, y) for y in range(0, 31)}
        turn = FakeTurn(walls=wall)
        start, goal = P(1, 15), P(9, 15)
        step = self.assert_optimal_first_step(turn, start, goal, '绕墙')
        self.assertLessEqual(distance(step, goal), distance(start, goal))

    def test_unreachable_goal_returns_none_instead_of_flooding(self):
        walls = ({P(3, y) for y in range(0, 32)} | {P(x, 3) for x in range(0, 41)}
                 | {P(x, 4) for x in range(0, 41)})
        turn = FakeTurn(walls=walls)
        self.assertIsNone(next_step(turn, FakeUnit(2, 2), P(30, 30)))

    def test_goal_on_the_start_is_none(self):
        self.assertIsNone(next_step(FakeTurn(), FakeUnit(4, 4), P(4, 4)))

    def test_another_unit_is_an_obstacle_but_not_itself(self):
        turn = FakeTurn(units=[(2, 1)])
        step = self.assert_optimal_first_step(turn, P(1, 1), P(10, 1), '同伴挡路')
        self.assertNotEqual((step.x, step.y), (2, 1), '不应走进入被占的格子')

    def test_deterministic_for_the_same_board(self):
        turn = FakeTurn(walls={P(5, y) for y in range(0, 31)})
        first = next_step(turn, FakeUnit(1, 15), P(9, 15))
        second = next_step(turn, FakeUnit(1, 15), P(9, 15))
        self.assertEqual(first.dump(), second.dump())

    def test_one_hop_neighbours_exclude_blocked_cells(self):
        turn = FakeTurn(walls={P(2, 1)}, units=[(1, 2)])
        neighbours = _reachable_in_one_hop(turn, P(1, 1), turn.blocked(FakeUnit(1, 1)))
        cells = {(pos.x, pos.y) for pos in neighbours}
        self.assertNotIn((2, 1), cells, '围墙不能走')
        self.assertNotIn((1, 2), cells, '被占的格子不能走')
        self.assertIn((2, 2), cells, '空地对角线可以走')

    def test_default_bound_covers_the_whole_map(self):
        self.assertGreater(MAX_EXPANSIONS, 1000,
                           '默认搜索上限应当能覆盖 41x32 的地图')


if __name__ == "__main__":
    unittest.main()
