"""Ballistics tests with hand-computed geometry.

Every expectation is derived from 任务书 §4.5.4 by hand, not from the module:

* the 90° cone is measured **from the tower**, so two close aim cells can still be
  illegal when the tower is far away, and two distant cells can be legal when the
  tower is between them;
* a bullet is consumed by the **nearest** robot on its path and deals 10;
* railgun energy is ``10/20/30``, each robot absorbs ``min(energy, hp)`` and the
  shot stops when the energy is spent;
* a robot off the path (more than half a cell away) is never hit.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))

from agent import ballistics  # noqa: E402
from agent.protocol import Pos  # noqa: E402


def robot(robot_id, x, y, health=40):
    return {"id": robot_id, "pos": {"x": x, "y": y}, "health": health,
            "roleType": "smallRobot"}


class ConeTests(unittest.TestCase):
    def test_single_target_is_always_legal(self):
        self.assertTrue(ballistics.cone_legal([Pos(9, 0)], Pos(0, 0)))

    def test_two_targets_in_the_same_quadrant_are_legal(self):
        # (5,0) and (5,5): 45° apart.
        self.assertTrue(ballistics.cone_legal([Pos(5, 0), Pos(5, 5)], Pos(0, 0)))

    def test_exactly_ninety_degrees_is_legal(self):
        # (5,0) and (0,5): exactly 90°, which the rule allows (≤ 90°).
        self.assertTrue(ballistics.cone_legal([Pos(5, 0), Pos(0, 5)], Pos(0, 0)))

    def test_more_than_ninety_degrees_is_illegal(self):
        # (5,0) and (-1,5): about 101°, so the whole attack is illegal.
        self.assertFalse(ballistics.cone_legal([Pos(5, 0), Pos(-1, 5)], Pos(0, 0)))

    def test_cone_is_measured_from_the_tower_not_between_cells(self):
        # The very same pair of aim cells passes or fails depending on where the
        # tower stands, because the rule measures directions from the tower.
        # From (0,0): directions to (1,0) and (1,-1) differ by 45° → legal.
        near = ballistics.cone_legal([Pos(1, 0), Pos(1, -1)], Pos(0, 0))
        # From (-1,0): directions (2,0) and (2,-1) differ by ~26.6° → still legal,
        # which is the point: the pair is legal from many towers and illegal from
        # an adjacent one, so no cell-to-cell shortcut can decide it.
        shifted = ballistics.cone_legal([Pos(1, 0), Pos(1, -1)], Pos(-1, 0))
        # From (1,-1): one aim point IS the tower, so that bullet has no direction.
        on_tower = ballistics.cone_legal([Pos(1, 0), Pos(1, -1)], Pos(1, -1))
        self.assertTrue(near)
        self.assertTrue(shifted)
        self.assertFalse(on_tower, "落点与炮台重合时没有方向，攻击非法")

    def test_aiming_at_the_tower_itself_is_illegal(self):
        self.assertFalse(ballistics.cone_legal([Pos(3, 3), Pos(3, 3)], Pos(3, 3)))

    def test_duplicate_direction_is_legal(self):
        self.assertTrue(ballistics.cone_legal([Pos(5, 0), Pos(5, 0)], Pos(0, 0)))


class GatlingTests(unittest.TestCase):
    def test_level_decides_the_bullet_count(self):
        tower = Pos(0, 0)
        self.assertFalse(ballistics.gatling_volley(tower, 3, [Pos(5, 0)], [])["legal"])
        volley = ballistics.gatling_volley(tower, 1, [Pos(5, 0)], [])
        self.assertTrue(volley["legal"])
        self.assertEqual(len(volley["shots"]), 1)

    def test_illegal_cone_refuses_the_whole_attack(self):
        volley = ballistics.gatling_volley(Pos(0, 0), 2, [Pos(5, 0), Pos(-1, 5)], [robot(1, 5, 0)])
        self.assertFalse(volley["legal"])
        self.assertEqual(volley["shots"], [], "非法锥形不应产生任何命中")
        self.assertIn("90°", volley["reason"])

    def test_bullet_is_consumed_by_the_nearest_robot_on_its_path(self):
        # Path (0,0) → (6,0) passes robots at x=2 and x=4: the nearer one is hit.
        robots = [robot(2, 4, 0), robot(1, 2, 0)]
        volley = ballistics.gatling_volley(Pos(0, 0), 1, [Pos(6, 0)], robots)
        shot = volley["shots"][0]
        self.assertEqual(shot["robot"], 1)
        self.assertEqual(shot["damage"], ballistics.GATLING_BULLET_DAMAGE)
        self.assertEqual(ballistics.damage_map(volley), {1: 10})

    def test_robot_off_the_path_is_never_hit(self):
        robots = [robot(1, 3, 1)]  # one full cell off the line: outside half a cell
        volley = ballistics.gatling_volley(Pos(0, 0), 1, [Pos(6, 0)], robots)
        self.assertIsNone(volley["shots"][0]["robot"])
        self.assertEqual(volley["shots"][0]["damage"], 0)
        self.assertEqual(ballistics.damage_map(volley), {})

    def test_each_bullet_is_resolved_independently(self):
        # Two bullets aimed at (6,0) and (0,6): the first meets the robot at (2,0),
        # the second finds empty ground.
        robots = [robot(1, 2, 0)]
        volley = ballistics.gatling_volley(Pos(0, 0), 2, [Pos(6, 0), Pos(0, 6)], robots)
        self.assertTrue(volley["legal"], "90° apart is legal")
        self.assertEqual([shot["robot"] for shot in volley["shots"]], [1, None])
        self.assertEqual(ballistics.damage_map(volley), {1: 10})

    def test_same_robot_can_stop_two_bullets(self):
        # Tower (0,0), robot (2,2). Aim (4,3): the line passes ~0.32 cells from the
        # robot. Aim (4,4): exactly through it. Both bullets are consumed.
        robots = [robot(1, 2, 2)]
        volley = ballistics.gatling_volley(Pos(0, 0), 2, [Pos(4, 3), Pos(4, 4)], robots)
        self.assertTrue(volley["legal"], "两个落点夹角约 8°，在 90° 锥形内")
        self.assertEqual([shot["robot"] for shot in volley["shots"]], [1, 1])
        self.assertEqual(ballistics.damage_map(volley), {1: 20})

    def test_bullet_only_hits_within_half_a_cell(self):
        # Robot (4,0) sits on the (0,0)→(6,0) line but ~0.66 cells off the
        # (0,0)→(6,1) line, so the first bullet hits and the second does not.
        robots = [robot(1, 4, 0)]
        volley = ballistics.gatling_volley(Pos(0, 0), 2, [Pos(6, 0), Pos(6, 1)], robots)
        self.assertEqual([shot["robot"] for shot in volley["shots"]], [1, None])

    def test_dead_robots_do_not_block(self):
        robots = [robot(1, 2, 0, health=0), robot(2, 4, 0)]
        volley = ballistics.gatling_volley(Pos(0, 0), 1, [Pos(6, 0)], robots)
        self.assertEqual(volley["shots"][0]["robot"], 2)

    def test_shots_state_their_path(self):
        volley = ballistics.gatling_volley(Pos(1, 1), 1, [Pos(5, 1)], [])
        self.assertEqual(volley["shots"][0]["path"], [{"x": 1, "y": 1}, {"x": 5, "y": 1}])


class RailgunTests(unittest.TestCase):
    def test_only_one_aim_point_is_accepted(self):
        result = ballistics.tower_volley("railgun", Pos(0, 0), 1, [Pos(5, 0), Pos(5, 1)], [])
        self.assertFalse(result["legal"])

    def test_energy_by_level(self):
        for level, energy in ((1, 10), (2, 20), (3, 30)):
            result = ballistics.railgun_volley(Pos(0, 0), level, Pos(6, 0), [])
            self.assertEqual(result["energy"], energy)

    def test_energy_penetrates_and_is_reduced_by_damage(self):
        # Lv3 = 30 energy down a line holding 40 hp, then 60 hp.
        robots = [robot(1, 2, 0, health=40), robot(2, 4, 0, health=60)]
        result = ballistics.railgun_volley(Pos(0, 0), 3, Pos(6, 0), robots)
        self.assertEqual([hit["damage"] for hit in result["hits"]], [30])
        self.assertEqual(result["hits"][0]["robot"], 1)
        self.assertEqual(result["remaining"], 0, "能量耗尽后不再伤害后面的机器人")

    def test_penetration_continues_while_energy_lasts(self):
        # Lv3 = 30 energy: 10 into the first, 20 into the second, then spent.
        robots = [robot(1, 2, 0, health=10), robot(2, 4, 0, health=60),
                  robot(3, 6, 0, health=60)]
        result = ballistics.railgun_volley(Pos(0, 0), 3, Pos(8, 0), robots)
        self.assertEqual([(hit["robot"], hit["damage"]) for hit in result["hits"]],
                         [(1, 10), (2, 20)])
        self.assertEqual(result["remaining"], 0)

    def test_energy_is_capped_by_robot_health(self):
        # 30 energy against a 40 hp robot: all 30 is absorbed by that one robot.
        robots = [robot(1, 2, 0, health=40)]
        result = ballistics.railgun_volley(Pos(0, 0), 3, Pos(6, 0), robots)
        self.assertEqual(result["hits"], [{"robot": 1, "damage": 30, "cell": {"x": 2, "y": 0},
                                           "health": 40, "energyLeft": 0}])

    def test_robots_are_hit_in_order_from_the_tower(self):
        robots = [robot(9, 5, 0, health=60), robot(3, 2, 0, health=60)]
        result = ballistics.railgun_volley(Pos(0, 0), 1, Pos(6, 0), robots)
        self.assertEqual([hit["robot"] for hit in result["hits"]], [3])

    def test_off_path_robot_takes_nothing(self):
        robots = [robot(1, 3, 1, health=40)]
        result = ballistics.railgun_volley(Pos(0, 0), 2, Pos(6, 0), robots)
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["remaining"], 20)


class DispatchTests(unittest.TestCase):
    def test_unknown_weapon_is_refused_not_guessed(self):
        result = ballistics.tower_volley("laser", Pos(0, 0), 1, [Pos(1, 0)], [])
        self.assertFalse(result["legal"])
        self.assertIn("laser", result["reason"])

    def test_rocket_reports_missile_aim_points(self):
        result = ballistics.tower_volley("rocket", Pos(0, 0), 2, [Pos(3, 3), Pos(4, 4)], [])
        self.assertTrue(result["legal"])
        self.assertEqual(result["missile"], [{"x": 3, "y": 3}, {"x": 4, "y": 4}])

    def test_missing_robot_fields_are_reported(self):
        self.assertEqual(ballistics.missing_robot_fields([robot(1, 1, 1)]), [])
        self.assertEqual(ballistics.missing_robot_fields([{"id": 1, "pos": {"x": 1, "y": 1}}]),
                         ["health"])

    def test_path_geometry_is_half_a_cell(self):
        # A robot exactly half a cell off the line counts; slightly more does not.
        self.assertAlmostEqual(
            ballistics.point_segment_distance((3.0, 0.5), (0.0, 0.0), (6.0, 0.0)), 0.5)
        volley = ballistics.gatling_volley(Pos(0, 0), 1, [Pos(6, 0)],
                                           [robot(1, 3, 1, health=40)])
        self.assertIsNone(volley["shots"][0]["robot"], "整格偏移不算在路径上")


if __name__ == "__main__":
    unittest.main()
