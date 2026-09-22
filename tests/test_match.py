"""Two-team match tests.

What is checked here is the *shape* of 1v1 play that the published rules state:
both sides are asked for commands for the same round, neither sees the other's
hidden units, and each side's private state (gold, backpack, score, unit list)
stays its own. Victory conditions are local: a destroyed base or the round limit.

Nothing here claims an official result — settlement order and the anomaly limit
are not reproduced.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))

from agent import vision  # noqa: E402
from agent.match import TwoTeamMatch, base_health, score_of  # noqa: E402
from agent.scenarios import scenario, configure_spawns  # noqa: E402


def two_team_world(seed=1, pressure=1):
    """A world with a real team on both sides, facing each other."""
    world = scenario(seed, "challenger", pressure)
    other = scenario(seed, "defender", pressure)
    world["teamEnemy"] = other["teamOur"]
    world["_demo"]["twoTeam"] = True
    return world


class SetupTests(unittest.TestCase):
    def test_world_has_two_real_teams(self):
        world = two_team_world()
        self.assertEqual(world["teamOur"]["type"], "challenger")
        self.assertEqual(world["teamEnemy"]["type"], "defender")
        self.assertTrue(any(r["roleType"] == "station"
                            for r in world["teamEnemy"]["roles"]))
        self.assertTrue(any(r["roleType"] == "worker"
                            for r in world["teamEnemy"]["roles"]))

    def test_each_side_gets_its_own_planner_memory(self):
        match = TwoTeamMatch(two_team_world())
        self.assertEqual(len(match.states), 2)
        self.assertIsNot(match.states["challenger"], match.states["defender"])

    def test_requests_are_vision_filtered_for_each_side(self):
        match = TwoTeamMatch(two_team_world())
        for side in match.sides:
            payload = match.request_for(side)
            self.assertEqual(payload["teamOur"]["type"], side)
            self.assertNotEqual(payload["teamEnemy"]["type"], side)
        # Put an enemy worker far from the challenger and check it is hidden.
        world = match.world
        enemy_worker = next(r for r in world["teamEnemy"]["roles"]
                            if r["roleType"] == "worker")
        enemy_worker["pos"] = {"x": 40, "y": 31}
        seen = match.request_for("challenger")
        ids = [unit["id"] for unit in seen["teamEnemy"]["roles"]]
        self.assertNotIn(enemy_worker["id"], ids, "视野外的敌方角色不应出现")
        # The opponent's private fields never cross over either.
        self.assertNotIn("backpack", seen["teamEnemy"])


class SpawnIsolationTests(unittest.TestCase):
    """Issue19: each side owns its fixed spawn pool; the two must not share one."""

    def _match_at_night(self):
        world = two_team_world()
        # Round 70 is the last day round: step() generates the night wave for the
        # round that follows, which is exactly where the judge would show it.
        world['roundNo'] = 70
        return TwoTeamMatch(world, max_rounds=200)

    def test_each_side_gets_its_own_fixed_pool_columns(self):
        match = self._match_at_night()
        blue = match.private['challenger']['spawn_layout']
        red = match.private['defender']['spawn_layout']
        self.assertEqual(18, blue['center']['x'])
        self.assertEqual(22, red['center']['x'])
        self.assertIsNot(blue, red)
        self.assertNotEqual(blue['slots'], red['slots'])
        # Mutating one side's pool must not move the other's.
        blue['slots'] = [{'x': 1, 'y': 1}]
        self.assertNotEqual([{'x': 1, 'y': 1}],
                            match.private['defender']['spawn_layout']['slots'])

    def test_custom_blue_pool_is_preserved_and_red_gets_its_own(self):
        world = scenario(1, 'challenger')
        custom = [{'x': 25, 'y': 10}, {'x': 25, 'y': 11}]
        configure_spawns(world, custom)
        world['teamEnemy'] = scenario(1, 'defender')['teamOur']
        match = TwoTeamMatch(world)
        blue = match.private['challenger']['spawn_layout']
        self.assertTrue(blue['custom'])
        self.assertEqual('challenger', blue['ownerTeam'])
        self.assertEqual(custom, blue['slots'])
        red = match.private['defender']['spawn_layout']
        self.assertEqual(22, red['center']['x'])
        self.assertNotEqual(custom, red['slots'])

    def test_custom_red_pool_from_the_mirror_is_preserved(self):
        world = scenario(1, 'challenger')
        red = scenario(1, 'defender')
        custom = [{'x': 15, 'y': 5}, {'x': 15, 'y': 6}]
        configure_spawns(red, custom)
        world['teamEnemy'] = red['teamOur']
        world['_mirror_demo'] = red['_demo']
        match = TwoTeamMatch(world)
        red_layout = match.private['defender']['spawn_layout']
        self.assertTrue(red_layout['custom'])
        self.assertEqual('defender', red_layout['ownerTeam'])
        self.assertEqual(custom, red_layout['slots'])
        self.assertEqual(18, match.private['challenger']['spawn_layout']['center']['x'])

    def test_legacy_layout_without_owner_is_adopted_for_its_side_only(self):
        world = two_team_world()
        world['_demo']['spawn_layout'].pop('ownerTeam', None)
        match = TwoTeamMatch(world)
        blue = match.private['challenger']['spawn_layout']
        self.assertEqual('challenger', blue['ownerTeam'])
        # The red side only had a copy of the blue bookkeeping, so it must rebuild
        # rather than inherit (or be "adopted" into) the blue pool.
        self.assertEqual(22, match.private['defender']['spawn_layout']['center']['x'])
        self.assertEqual('defender', match.private['defender']['spawn_layout']['ownerTeam'])

    def test_both_sides_spawn_their_own_full_observed_wave_at_night(self):
        match = self._match_at_night()
        entry = match.round()
        events = entry['events']
        self.assertTrue(any(line.startswith('[challenger]') and '35' in line and '实测' in line
                            for line in events), events)
        self.assertTrue(any(line.startswith('[defender]') and '35' in line and '实测' in line
                            for line in events), events)
        for side, first_x in (('challenger', 18), ('defender', 22)):
            layout = match.private[side]['spawn_layout']
            self.assertEqual(first_x, layout['center']['x'])
            self.assertEqual(35, match.private[side]['wave']['total_count'])


class RoundTests(unittest.TestCase):
    def test_waves_actually_spawn_in_a_two_team_match(self):
        """The local lifecycle must run: robots, mines and pressure are part of it.

        Regression: the settle projection dropped the simulator's own bookkeeping,
        so the wave generator saw an "imported snapshot" and spawned nothing —
        both teams then played 1300 rounds against an empty board and the score
        degenerated to the survival-only floor.
        """
        match = TwoTeamMatch(two_team_world())
        seen_robots = 0
        for _ in range(80):
            match.round()
            seen_robots = max(seen_robots,
                              len((match.world.get("robot") or {}).get("roles") or ()))
        self.assertGreater(seen_robots, 0, "夜战波次必须在本地双队对局中生成")
        # And the private bookkeeping survives, since the next round needs it.
        self.assertIn("_demo", match.world)
        self.assertIn("seed", match.world["_demo"])
        self.assertIn("_mirror_demo", match.world,
                      "另一侧的模拟簿记必须分开存放")

    def test_each_side_reads_its_own_published_fields(self):
        """A team must read the task text the judge sent *it*.

        Regression: the two sides shared one set of published fields, so whichever
        side settled last overwrote the other's phaseTask/playerTasks — one team
        then read the other's task text, or none at all, and its task loop stalled.
        """
        match = TwoTeamMatch(two_team_world())
        seen: dict[str, list[str]] = {side: [] for side in match.sides}
        for _ in range(40):
            for side in match.sides:
                payload = match.request_for(side)
                tasks = payload["teamOur"].get("playerTasks") or []
                own_ids = {role["id"] for role in payload["teamOur"]["roles"]}
                self.assertTrue(own_ids, f"{side} 必须看到自己的单位")
                for task in tasks:
                    seen[side].append(str(task.get("taskType")))
            match.round()
        # Both sides must have seen their own point list at some stage; the exact
        # contents differ per side because each has its own points.
        for side, kinds in seen.items():
            self.assertTrue(kinds, f"{side} 应当看到自己的任务点列表")

    def test_published_judge_fields_survive_the_round(self):
        """phaseTask / lastCmdResult must reach the next round's request.

        Regression: the loop used to write back only the team object, so these
        top-level fields were dropped every round and the task pipeline — which
        needs phaseTask, and feeds lastCmdResult back to its solver — waited
        forever for an answer it could never produce.
        """
        match = TwoTeamMatch(two_team_world())
        for _ in range(30):
            entry = match.round()
        round_no = int(match.world["roundNo"])
        tasks = match.world["teamOur"].get("playerTasks") or []
        self.assertTrue(tasks, f"第 {round_no} 回合仍应发布任务点")
        self.assertIn("isValid", tasks[0])
        # The round's own action results are published back as well.
        self.assertIn("lastRoundRoleActionResults", match.world)
        # And each side's request sees the fields for its own team.
        for side in match.sides:
            payload = match.request_for(side)
            self.assertTrue(payload["teamOur"].get("playerTasks"),
                            f"{side} 的请求应带上自己的任务点")

    def test_both_sides_are_asked_before_either_is_settled(self):
        """No first-mover advantage: both command sets come from one snapshot."""
        asked: list[str] = []

        def recorder(side):
            def policy(payload):
                asked.append(side)
                return {"roleCommandMap": {}}
            return policy

        match = TwoTeamMatch(two_team_world(),
                             policies={side: recorder(side) for side in ("challenger", "defender")})
        match.round()
        self.assertEqual(asked, ["challenger", "defender"])
        entry = match.log[0]
        self.assertIn("challenger", entry["commands"])
        self.assertIn("defender", entry["commands"])

    def test_a_round_advances_once_and_keeps_both_teams(self):
        match = TwoTeamMatch(two_team_world())
        before = int(match.world["roundNo"])
        match.round()
        self.assertEqual(int(match.world["roundNo"]), before + 1,
                         "一回合只能前进一次，不能因为两侧各结算一次而跳两回合")
        # Each side keeps its own unit list (a build may add to it, so the check is
        # that both keep their core three and neither absorbs the other's units).
        for side in match.sides:
            roles = match.world[match._slot(side)]["roles"]
            kinds = [role["roleType"] for role in roles]
            self.assertIn("station", kinds)
            self.assertIn("worker", kinds)
            self.assertIn("pioneer", kinds)
            ids = {role["id"] for role in roles}
            self.assertEqual(len(ids), len(roles), "同一队内不应出现重复单位")

    def test_each_side_keeps_its_own_score_and_gold(self):
        match = TwoTeamMatch(two_team_world())
        match.world["teamOur"]["goldNum"] = 75
        match.world["teamEnemy"]["goldNum"] = 40
        match.world["teamOur"]["totalScore"] = 11
        match.world["teamEnemy"]["totalScore"] = 22
        for _ in range(5):
            match.round()
        self.assertEqual(score_of(match.world, "challenger"),
                         match.world["teamOur"]["totalScore"])
        self.assertEqual(score_of(match.world, "defender"),
                         match.world["teamEnemy"]["totalScore"])
        self.assertNotEqual(score_of(match.world, "challenger"),
                            score_of(match.world, "defender"))
        self.assertGreaterEqual(match.world["teamEnemy"]["goldNum"], 0)

    def test_both_teams_act_in_the_same_round(self):
        """Both sides must produce activity, not just the first one."""
        match = TwoTeamMatch(two_team_world())
        actions = {"challenger": 0, "defender": 0}
        for _ in range(12):
            entry = match.round()
            for side in match.sides:
                actions[side] += len(entry["executed"][side])
        for side, count in actions.items():
            self.assertGreater(count, 0, f"{side} 应在同一局中行动")


class EndConditionTests(unittest.TestCase):
    def test_broken_base_ends_the_match(self):
        world = two_team_world()
        match = TwoTeamMatch(world)
        for role in world["teamEnemy"]["roles"]:
            if role["roleType"] == "station":
                role["health"] = 0
        self.assertEqual(base_health(world, "defender"), 0)
        match.round()
        result = match.result()
        self.assertTrue(result["finished"])
        self.assertEqual(result["winner"], "challenger")
        self.assertIn("基地被摧毁", result["reason"])

    def test_round_limit_ends_the_match(self):
        match = TwoTeamMatch(two_team_world(), max_rounds=3)
        match.run()
        result = match.result()
        self.assertTrue(result["finished"])
        self.assertIn("回合上限", result["reason"])
        self.assertEqual(result["rounds"], 3)

    def test_result_is_labelled_local(self):
        match = TwoTeamMatch(two_team_world(), max_rounds=2)
        result = match.run()
        self.assertTrue(result["local"], "本地对局结果必须标明不是官方成绩")
        self.assertIn("scores", result)
        self.assertIn("baseHp", result)

    def test_frame_reports_both_sides_and_their_vision(self):
        match = TwoTeamMatch(two_team_world(), max_rounds=2)
        match.run()
        frame = match.frame()
        self.assertEqual(set(frame["scores"]), {"challenger", "defender"})
        self.assertEqual(set(frame["vision"]), {"challenger", "defender"})
        for side, report in frame["vision"].items():
            self.assertEqual(report["visionDistance"], 4)
            self.assertGreaterEqual(report["enemyTotal"], report["enemyVisible"])


if __name__ == "__main__":
    unittest.main()
