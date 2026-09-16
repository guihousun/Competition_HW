"""Hand-worked simultaneous movement: 任务书 §4.2, §4.5.4 第5条.

Pair swaps collide; chains and noncontested >=3 cycles vacate simultaneously.
Hard obstacles and stationary roles never become free merely by listing a move.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))

from agent.protocol import Pos  # noqa: E402
from agent.simulator import resolve_moves  # noqa: E402


def P(x, y):
    return Pos(x, y)


def as_pairs(resolved):
    """Compare (uid, x, y) triples: Pos is mutable, so it is neither orderable nor hashable."""
    return sorted((uid, pos.x, pos.y) for uid, pos in resolved)


class MoveResolutionTests(unittest.TestCase):
    def test_free_moves_all_apply(self):
        moves = {"a": P(1, 0), "b": P(3, 0)}
        origins = {"a": P(0, 0), "b": P(4, 0)}
        resolved, rejected = resolve_moves(moves, origins, set())
        self.assertEqual(as_pairs(resolved), [("a", 1, 0), ("b", 3, 0)])
        self.assertEqual(rejected, [])

    def test_swap_stops_both_roles(self):
        moves = {"a": P(1, 0), "b": P(0, 0)}
        origins = {"a": P(0, 0), "b": P(1, 0)}
        resolved, rejected = resolve_moves(moves, origins, set())
        self.assertEqual(resolved, [])
        self.assertEqual(len(rejected), 2)
        self.assertTrue(all("互换" in why for _, _, why in rejected))

    def test_chain_into_a_vacated_cell(self):
        moves = {"a": P(1, 0), "b": P(2, 0), "c": P(3, 0)}
        origins = {"a": P(0, 0), "b": P(1, 0), "c": P(2, 0)}
        resolved, rejected = resolve_moves(moves, origins, set())
        self.assertEqual(len(resolved), 3, "整条链都应移动")
        self.assertEqual(rejected, [])
        self.assertEqual(as_pairs(resolved), [("a", 1, 0), ("b", 2, 0), ("c", 3, 0)])

    def test_two_roles_aimed_at_one_cell_nobody_moves(self):
        moves = {"a": P(1, 0), "b": P(1, 0)}
        origins = {"a": P(0, 0), "b": P(2, 0)}
        resolved, rejected = resolve_moves(moves, origins, set())
        self.assertEqual(resolved, [])
        self.assertEqual(len(rejected), 2)
        self.assertTrue(all("争抢" in why for _uid, _t, why in rejected))

    def test_triangle_cycle_moves_simultaneously(self):
        moves = {"a": P(1, 0), "b": P(1, 1), "c": P(0, 0)}
        origins = {"a": P(0, 0), "b": P(1, 0), "c": P(1, 1)}
        resolved, rejected = resolve_moves(moves, origins, set())
        self.assertEqual(as_pairs(resolved), [("a", 1, 0), ("b", 1, 1), ("c", 0, 0)])
        self.assertEqual(rejected, [])

    def test_blocked_terrain_is_refused_with_a_reason(self):
        wall = P(1, 0)
        moves = {"a": wall}
        origins = {"a": P(0, 0)}
        blocked = {wall}
        resolved, rejected = resolve_moves(moves, origins, blocked)
        self.assertEqual(resolved, [])
        self.assertEqual(len(rejected), 1)
        self.assertIn("障碍", rejected[0][2])

    def test_stationary_occupant_blocks_the_step(self):
        # 'b' does not move, so its cell is never freed for 'a'.
        moves = {"a": P(1, 0)}
        origins = {"a": P(0, 0), "b": P(1, 0)}
        resolved, rejected = resolve_moves(moves, origins, set())
        self.assertEqual(resolved, [])
        self.assertIn("占用", rejected[0][2])

    def test_resolution_is_deterministic(self):
        moves = {"b": P(2, 0), "a": P(1, 0), "c": P(3, 0)}
        origins = {"a": P(0, 0), "b": P(1, 0), "c": P(2, 0)}
        first = resolve_moves(moves, origins, set())
        second = resolve_moves(dict(reversed(list(moves.items()))), origins,
                               set())
        self.assertEqual(as_pairs(first[0]), as_pairs(second[0]), "同一批意图必须得到同一结果")
        self.assertEqual(len(first[1]), len(second[1]))


class SimulatorMoveIntegrationTests(unittest.TestCase):
    """The same cases through a real settle pass."""

    def test_swap_through_step_stops_both_workers(self):
        from agent.scenarios import scenario
        from agent.simulator import step
        state = scenario(5, "challenger", 1)
        workers = [r for r in state["teamOur"]["roles"] if r["roleType"] == "worker"]
        # Park the two workers side by side on clear ground, then swap them.
        workers[0]["pos"] = {"x": 20, "y": 20}
        workers[1]["pos"] = {"x": 21, "y": 20}
        commands = {
            str(workers[0]["id"]): {"action": "move", "targetPos": [{"x": 21, "y": 20}]},
            str(workers[1]["id"]): {"action": "move", "targetPos": [{"x": 20, "y": 20}]},
        }
        result = step(state, commands)
        positions = {r["id"]: r["pos"] for r in result["state"]["teamOur"]["roles"]}
        self.assertEqual(positions[workers[0]["id"]], {"x": 20, "y": 20})
        self.assertEqual(positions[workers[1]["id"]], {"x": 21, "y": 20})
        successes = result["state"]["lastRoundRoleActionResults"]
        self.assertFalse(successes[str(workers[0]["id"])])
        self.assertFalse(successes[str(workers[1]["id"])])



if __name__ == "__main__":
    unittest.main()
