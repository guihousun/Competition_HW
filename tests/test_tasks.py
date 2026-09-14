"""Judge-channel and task-pipeline tests.

Expectations are taken from the official text, not from the implementation:

  * ``lastCmdResult`` format and its four documented shapes (接口文档 §1.1);
  * 3 LLM calls per game day, waived while a task runs (接口文档 §1.7);
  * ``executeCmd`` only during a task, ≤15 s, failures not counted as team
    exceptions (接口文档 §2.1);
  * task endings: completed / timed out / left the ring / pioneer died
    (任务书 §五), then a 30-round refresh;
  * only a pioneer inside the ring of an **own** task point may accept, and task
    point 2 spans two cells (任务书 §4.6.2).
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))

from agent import sandbox, tasks  # noqa: E402
from agent.planner import PlannerState  # noqa: E402
from agent.protocol import Pos, Turn  # noqa: E402


def observation(*, round_no=1, team="challenger", role_pos=(5, 5),
                task_points=None, phase_task="", llm_resp="", cmd_result="",
                ready=True, timeout_rounds=20):
    """A minimal official-shaped observation with one pioneer."""
    points = task_points if task_points is not None else [
        {"taskType": "自进化类1", "taskPosition": {"x": 6, "y": 5},
         "coldDownRounds": 0, "scoreReward": 50, "goldReward": 30,
         "isValid": ready, "timeoutRounds": timeout_rounds},
    ]
    zones = [{"pos": {"x": 6, "y": 5}, "neutralType": f"{team}TaskPoint1"}]
    return {
        "roundNo": round_no,
        "mapInfo": {"width": 41, "height": 32, "zones": zones},
        "teamOur": {
            "type": team, "teamId": "t1", "goldNum": 75, "totalScore": 0,
            "playerTasks": points,
            "roles": [
                {"id": 10011, "roleType": "pioneer",
                 "pos": {"x": role_pos[0], "y": role_pos[1]},
                 "health": 200, "level": 0, "backPackCapability": 40, "backpack": []},
                {"id": 10010, "roleType": "worker", "pos": {"x": 3, "y": 3},
                 "health": 220, "level": 0, "backPackCapability": 100, "backpack": []},
                {"id": 10013, "roleType": "station", "pos": {"x": 10, "y": 10},
                 "health": 1500, "level": 1, "backPackCapability": 0, "backpack": []},
            ],
        },
        "teamEnemy": {"roles": []},
        "robot": {"roles": []},
        "phaseTask": phase_task,
        "llmResp": llm_resp,
        "lastCmdResult": cmd_result,
        "worldNews": {"officialNews": "", "folkLegends": ""},
        "vendorShopList": [], "weaponShopList": [], "errors": [],
        "lastRoundRoleActionResults": {},
    }


def judge_with(result):
    judge = sandbox.JudgeState()
    judge.last_result = result
    return judge


class SandboxParsingTests(unittest.TestCase):
    def test_documented_shapes(self):
        ok = sandbox.parse_command_result("[exitCode:0]\nhello world")
        self.assertEqual((ok.status, ok.exit_code, ok.output, ok.ok), ("exit", 0, "hello world", True))

        bad = sandbox.parse_command_result("[exitCode:2]\nboom")
        self.assertEqual(bad.exit_code, 2)
        self.assertFalse(bad.ok)
        self.assertFalse(bad.is_failure, "a non-zero exit is a result, not a sandbox failure")

        timeout = sandbox.parse_command_result("[TIMEOUT]\npartial output")
        self.assertEqual(timeout.status, "timeout")
        self.assertTrue(timeout.is_failure)
        self.assertIn("15", timeout.summary())

        broken = sandbox.parse_command_result("[JUDGER_ERROR]\nreason")
        self.assertEqual(broken.status, "judger_error")
        self.assertTrue(broken.is_failure)

        truncated = sandbox.parse_command_result("[exitCode:0]\nbig\n[TRUNCATED]")
        self.assertTrue(truncated.truncated)
        self.assertEqual(truncated.output, "big")

        self.assertEqual(sandbox.parse_command_result("").status, "empty")
        self.assertEqual(sandbox.parse_command_result(None).status, "empty")
        # An unrecognised shape must not look like success.
        self.assertEqual(sandbox.parse_command_result("whatever").status, "judger_error")

    def test_daily_llm_quota_and_in_task_exemption(self):
        judge = sandbox.JudgeState()
        self.assertEqual(sandbox.LLM_DAILY_QUOTA, 3)
        for _ in range(3):
            self.assertTrue(judge.llm_available(in_task=False))
            judge.consume_llm(in_task=False)
        self.assertFalse(judge.llm_available(in_task=False), "4th call must be refused locally")
        self.assertTrue(judge.llm_available(in_task=True), "in-task calls are not limited")
        judge.consume_llm(in_task=True)
        self.assertEqual(judge.llm_used_today, 3, "in-task calls are not counted")

    def test_quota_resets_each_game_day(self):
        judge = sandbox.JudgeState()
        judge.consume_llm(in_task=False)
        judge.consume_llm(in_task=False)
        judge.note_round(1)
        self.assertEqual(judge.llm_used_today, 2)
        judge.note_round(130)
        self.assertEqual(judge.llm_used_today, 2, "still day 1")
        judge.note_round(131)
        self.assertEqual(judge.llm_used_today, 0, "day 2 resets the allowance")

    def test_response_builder_omits_empty_channels(self):
        builder = sandbox.ResponseBuilder()
        builder.commands = {"10010": {"action": "move", "targetPos": [{"x": 1, "y": 1}]}}
        self.assertEqual(list(builder.build()), ["roleCommandMap"])
        builder.prompt = "hello"
        builder.execute = "ls"
        built = builder.build()
        self.assertEqual(built["prompt"], "hello")
        self.assertEqual(built["executeCmd"], "ls")
        self.assertEqual(len(built["roleCommandMap"]), 1)

    def test_shell_helper_rejects_nul_and_flattens_newlines(self):
        self.assertEqual(sandbox.shell("ls -la"), "ls -la")
        self.assertEqual(sandbox.shell("ls\n-la"), "ls -la")
        with self.assertRaises(ValueError):
            sandbox.shell("ls\x00-la")


class PlannerStateTests(unittest.TestCase):
    def test_pending_request_lifecycle(self):
        state = PlannerState()
        state.note_round(1)
        state.note_submission("q", "ls", 5, in_task=False)
        self.assertEqual(state.judge.llm_used_today, 1)
        self.assertIsNotNone(state.judge.pending_prompt)
        self.assertIsNotNone(state.judge.pending_cmd)
        # The judge answers one round later.
        summary = state.note_results({"llmResp": "answer", "lastCmdResult": "[exitCode:0]\nok"}, 6)
        self.assertEqual(summary["llm"], "answered")
        self.assertEqual(summary["cmd"], "exit:0")
        self.assertIsNone(state.judge.pending_prompt)
        self.assertEqual(state.judge.cmd_runs, 1)

    def test_missing_command_result_is_a_sandbox_failure_not_an_answer(self):
        state = PlannerState()
        state.note_round(1)
        state.note_submission(None, "ls", 5, in_task=True)
        self.assertEqual(state.note_results({}, 6), {}, "too early to give up")
        summary = state.note_results({}, 7)
        self.assertEqual(summary["cmd"], "missing")
        self.assertEqual(state.judge.cmd_failures, 1)
        self.assertFalse(state.judge.last_result.ok)

    def test_prompt_without_llm_response_is_recorded_as_no_answer(self):
        state = PlannerState()
        state.note_round(1)
        state.note_submission("q", None, 1, in_task=False)
        summary = state.note_results({}, 3)
        self.assertEqual(summary["llm"], "no-answer")


class SolverRegistryTests(unittest.TestCase):
    def test_default_solvers_are_ordered_and_named(self):
        registry = tasks.default_registry()
        self.assertEqual(registry.names(), ("keyword-fill", "task-agent", "llm-ask", "probe-command"))

    def test_duplicate_registration_is_refused(self):
        registry = tasks.default_registry()
        with self.assertRaises(tasks.TaskError):
            registry.register("keyword-fill", tasks.solver_keyword_fill)
        registry.register("keyword-fill", tasks.solver_keyword_fill, replace=True)
        self.assertEqual(registry.names().count("keyword-fill"), 1)

    def test_registry_returns_none_when_nobody_can_solve(self):
        registry = tasks.SolverRegistry()
        cycle = tasks.TaskCycle(point={"x": 1, "y": 1}, accepted_round=1)
        context = tasks.SolverContext(cycle=cycle, round_no=1, phase_task="",
                                      llm_resp="", cmd_output="", cmd_status="empty",
                                      notes={}, backpack=(), is_day=True)
        self.assertIsNone(registry.solve(context))


class KeywordSolverTests(unittest.TestCase):
    def context(self, description, notes=None, llm_resp="", cmd_status="empty", cmd_output=""):
        cycle = tasks.TaskCycle(point={"x": 1, "y": 1}, accepted_round=1,
                                description=description, timeout_rounds=20)
        return tasks.SolverContext(cycle=cycle, round_no=2, phase_task=description,
                                   llm_resp=llm_resp, cmd_output=cmd_output,
                                   cmd_status=cmd_status, notes=notes or {},
                                   backpack=(), is_day=True)

    def test_values_stated_in_the_description_are_submitted(self):
        plan = tasks.solver_keyword_fill(self.context("端口：8080 协议：tcp"))
        self.assertEqual(plan.kind, "submit")
        self.assertIn("端口=8080", plan.answer)
        self.assertIn("协议=tcp", plan.answer)

    def test_a_question_without_a_value_is_not_guessed(self):
        self.assertIsNone(tasks.solver_keyword_fill(self.context("请查询北京天气?")))

    def test_empty_description_is_not_answered(self):
        self.assertIsNone(tasks.solver_keyword_fill(self.context("")))


class LlmSolverTests(unittest.TestCase):
    def context(self, **kwargs):
        cycle = tasks.TaskCycle(point={"x": 1, "y": 1}, accepted_round=1,
                                description=kwargs.pop("description", "题目正文"),
                                timeout_rounds=20)
        base = dict(round_no=2, phase_task=cycle.description, llm_resp="",
                    cmd_output="", cmd_status="empty", notes={}, backpack=(), is_day=True)
        base.update(kwargs)
        return tasks.SolverContext(cycle=cycle, **base)

    def test_first_call_asks_the_model(self):
        notes = {}
        plan = tasks.solver_llm_ask(self.context(notes=notes))
        self.assertEqual(plan.kind, "prompt")
        self.assertIn("题目正文", plan.prompt)
        self.assertTrue(notes["llm_asked"])

    def test_response_is_submitted_and_field_lines_preferred(self):
        cycle = tasks.TaskCycle(point={"x": 1, "y": 1}, accepted_round=1,
                                description="d", timeout_rounds=20)
        notes = {"llm_asked": True}
        context = tasks.SolverContext(cycle=cycle, round_no=3, phase_task="d",
                                      llm_resp="说明文字\n温度=21; 湿度=40\n多余的解释",
                                      cmd_output="", cmd_status="empty", notes=notes,
                                      backpack=(), is_day=True)
        plan = tasks.solver_llm_ask(context)
        self.assertEqual(plan.kind, "submit")
        self.assertEqual(plan.answer, "温度=21; 湿度=40")

    def test_empty_response_is_not_submitted(self):
        notes = {"llm_asked": True}
        self.assertIsNone(tasks.solver_llm_ask(self.context(notes=notes, llm_resp="  ")))
        self.assertTrue(notes["llm_asked"])

    def test_no_second_prompt_without_a_response(self):
        notes = {"llm_asked": True}
        self.assertIsNone(tasks.solver_llm_ask(self.context(notes=notes)))


class ProbeSolverTests(unittest.TestCase):
    def context(self, notes, cmd_status="empty", cmd_output=""):
        cycle = tasks.TaskCycle(point={"x": 1, "y": 1}, accepted_round=1,
                                description="自进化任务", timeout_rounds=20)
        return tasks.SolverContext(cycle=cycle, round_no=2, phase_task="自进化任务",
                                   llm_resp="", cmd_output=cmd_output,
                                   cmd_status=cmd_status, notes=notes,
                                   backpack=(), is_day=True)

    def test_probes_once_and_never_guesses(self):
        notes = {}
        plan = tasks.solver_probe_command(self.context(notes))
        self.assertEqual(plan.kind, "cmd")
        self.assertTrue(plan.command)
        self.assertTrue(notes["probed"])
        self.assertIsNone(tasks.solver_probe_command(self.context(notes)))

    def test_sandbox_trouble_stops_probing(self):
        notes = {}
        self.assertIsNone(tasks.solver_probe_command(self.context(notes, cmd_status="timeout")))
        self.assertTrue(notes["probed"])

    def test_no_probe_while_one_is_pending(self):
        notes = {}
        self.assertIsNone(tasks.solver_probe_command(self.context(notes, cmd_status="pending")))
        self.assertFalse(notes.get("probed"))


class PipelineFrameTests(unittest.TestCase):
    """The official frame: who may accept, when a task ends, what refreshes."""

    def setUp(self):
        self.pipeline = tasks.TaskPipeline(registry=tasks.SolverRegistry())
        self.judge = judge_with(sandbox.CommandResult("empty"))
        self.notes = tasks.new_task_notes()

    def step(self, payload, notes=None, judge=None, pioneer=None):
        turn = Turn.load(payload)
        role = pioneer or turn.pioneer()
        return self.pipeline.step(state=payload, turn=turn, pioneer=role,
                                  judge=judge or self.judge,
                                  notes=notes if notes is not None else self.notes,
                                  is_day=True, pending=None)

    def test_pioneer_accepts_when_standing_on_its_own_point(self):
        payload = observation(role_pos=(6, 5))
        command, plan = self.step(payload)
        self.assertEqual(command, {"action": "acceptTask"})
        self.assertEqual(plan.kind, "accept")

    def test_pioneer_walks_to_the_point_when_far_away(self):
        payload = observation(role_pos=(20, 20))
        command, plan = self.step(payload)
        self.assertEqual(command["action"], "move")
        self.assertEqual(plan.kind, "move")

    def test_second_task_point_spans_two_cells(self):
        zone = {"pos": {"x": 30, "y": 30}, "neutralType": "challengerTaskPoint2"}
        cells = tasks.TaskPipeline.point_cells(zone)
        self.assertEqual(cells, (Pos(30, 30), Pos(31, 30)))

    def test_enemy_task_point_is_not_a_target(self):
        payload = observation(team="challenger")
        payload["mapInfo"]["zones"] = [{"pos": {"x": 6, "y": 5},
                                        "neutralType": "defenderTaskPoint1"}]
        self.assertIsNone(self.step(payload)[0], "no own point means no task work")

    def test_dead_pioneer_ends_the_task_and_starts_refresh(self):
        cycle = tasks.TaskCycle(point={"x": 6, "y": 5}, accepted_round=1,
                                description="d", timeout_rounds=20)
        self.notes["cycle"] = cycle
        payload = observation(round_no=5)
        payload["teamOur"]["roles"][0]["health"] = 0
        command, plan = self.step(payload)
        self.assertIsNone(command)
        self.assertEqual(cycle.end_reason, "开拓者死亡")
        self.assertEqual(self.notes["cooldown_until"], 5 + tasks.REFRESH_ROUNDS)
        self.assertIsNone(self.notes["cycle"])

    def test_timeout_ends_the_task(self):
        cycle = tasks.TaskCycle(point={"x": 6, "y": 5}, accepted_round=1,
                                description="d", timeout_rounds=10)
        self.notes["cycle"] = cycle
        payload = observation(round_no=11, role_pos=(6, 5))
        # The point is still ready, but our own deadline has passed.
        command, plan = self.step(payload)
        self.assertIsNone(command)
        self.assertEqual(cycle.end_reason, "任务超时")

    def test_leaving_the_ring_ends_the_task(self):
        cycle = tasks.TaskCycle(point={"x": 6, "y": 5}, accepted_round=1,
                                description="d", timeout_rounds=40)
        self.notes["cycle"] = cycle
        payload = observation(round_no=4, role_pos=(20, 20))
        command, plan = self.step(payload)
        self.assertEqual(cycle.end_reason, "离开任务点范围")
        self.assertIsNone(command, "the task is over; the pioneer is free")

    def test_refresh_delay_is_per_point_not_global(self):
        """The 30-round refresh belongs to the point that ended a task (任务书 §五).

        Regression: acceptance was gated on a *team-wide* cooldown held in memory,
        which blocked a perfectly usable second task point for 30 rounds after every
        task — and only on the path that kept memory, so the stateless judge path
        behaved differently. What gates acceptance is the judge's own `isValid`
        plus the point's `coldDownRounds`.
        """
        payload = observation(round_no=50, role_pos=(6, 5), ready=False)
        payload["teamOur"]["playerTasks"][0]["coldDownRounds"] = 12
        self.assertIsNone(self.step(payload)[0],
                          '冷却中的任务点不可接取')
        payload = observation(round_no=100, role_pos=(6, 5))
        self.assertEqual(self.step(payload)[0], {"action": "acceptTask"})
        # Memory alone must not block it: the other point is simply available.
        self.notes["cooldown_until"] = 999
        payload = observation(round_no=100, role_pos=(6, 5))
        self.assertEqual(self.step(payload)[0], {"action": "acceptTask"},
                         '全局冷却记忆不应阻止另一个可接取的任务点')

    def test_not_ready_point_is_skipped(self):
        payload = observation(ready=False, role_pos=(6, 5))
        self.assertIsNone(self.step(payload)[0])

    def test_submission_uses_the_solver_and_records_every_answer(self):
        payload = observation(round_no=3, role_pos=(6, 5),
                              phase_task="端口：8080", timeout_rounds=20)
        registry = tasks.default_registry()
        pipeline = tasks.TaskPipeline(registry=registry)
        cycle = tasks.TaskCycle(point={"x": 6, "y": 5}, accepted_round=1,
                                description="端口：8080", timeout_rounds=20)
        self.notes["cycle"] = cycle
        turn = Turn.load(payload)
        command, plan = pipeline.step(state=payload, turn=turn, pioneer=turn.pioneer(),
                                      judge=self.judge, notes=self.notes, is_day=True,
                                      pending=None)
        self.assertEqual(command["action"], "submitAnswer")
        self.assertIn("端口=8080", command["taskAnswer"])
        self.assertEqual(cycle.phase, "submitted")
        self.assertEqual(len(cycle.submissions), 1)

    def test_llm_path_is_taken_when_no_rule_solver_matches(self):
        payload = observation(round_no=3, role_pos=(6, 5),
                              phase_task="请查询北京天气?", timeout_rounds=20)
        registry = tasks.default_registry()
        pipeline = tasks.TaskPipeline(registry=registry)
        cycle = tasks.TaskCycle(point={"x": 6, "y": 5}, accepted_round=1,
                                description="请查询北京天气?", timeout_rounds=20)
        self.notes["cycle"] = cycle
        turn = Turn.load(payload)
        command, plan = pipeline.step(state=payload, turn=turn, pioneer=turn.pioneer(),
                                      judge=self.judge, notes=self.notes, is_day=True,
                                      pending=None)
        self.assertIsNone(command, "nothing to command: the prompt travels separately")
        self.assertEqual(plan.kind, "prompt")
        self.assertIn("只输出答案本身", plan.prompt)


class CycleScoringTests(unittest.TestCase):
    def test_best_so_far_is_kept_not_the_last_answer(self):
        cycle = tasks.TaskCycle(point={"x": 1, "y": 1}, accepted_round=1,
                                description="d", timeout_rounds=10)
        cycle.record(3, "a=1", "keyword-fill")
        cycle.record(5, "a=2; b=3", "llm-ask")
        # 任务书 §五: a timeout is scored on the best pass rate submitted so far,
        # so both answers must survive in the record.
        self.assertEqual([s.answer for s in cycle.submissions], ["a=1", "a=2; b=3"])
        self.assertEqual(cycle.last_answer, "a=2; b=3")

    def test_empty_answer_is_refused(self):
        cycle = tasks.TaskCycle(point={"x": 1, "y": 1}, accepted_round=1)
        with self.assertRaises(tasks.TaskError):
            cycle.record(2, "   ", "test")

    def test_deadline_maths(self):
        cycle = tasks.TaskCycle(point={"x": 1, "y": 1}, accepted_round=10,
                                timeout_rounds=25)
        self.assertEqual(cycle.deadline, 35)
        self.assertEqual(cycle.rounds_left(30), 5)
        self.assertEqual(cycle.rounds_left(35), 0)


class StatelessPathTests(unittest.TestCase):
    """The official POST is stateless: the same decision must come out either way.

    Regression: planner memory used to be keyed partly by the map's zone list,
    which changes whenever a mine depletes — silently resetting cooldowns and
    quota. These tests pin the two properties that made that invisible.
    """

    def test_match_key_survives_a_changing_map(self):
        from agent import planner
        before = observation()
        after = observation()
        # A mine depleted and respawned somewhere else: same match, different zones.
        after["mapInfo"]["zones"] = list(after["mapInfo"]["zones"]) + [
            {"pos": {"x": 30, "y": 30}, "neutralType": "stone"}]
        self.assertEqual(planner._match_key(before), planner._match_key(after),
                         "同一次对局的 key 不应随矿区变化而改变")
        self.assertNotEqual(planner._match_key(before),
                            planner._match_key(observation(team="defender")))

    def test_memory_survives_between_rounds(self):
        from agent import planner
        planner.reset()
        first = observation(round_no=1)
        state = planner.state_for(first)
        state.note_round(1)
        state.note_submission("q", None, 1, in_task=False)
        self.assertEqual(state.judge.llm_used_today, 1)
        # The next round is a fresh payload object, as an HTTP request would be.
        second = observation(round_no=2)
        self.assertIs(planner.state_for(second), state,
                      "跨回合必须复用同一条 planner 记忆")
        self.assertEqual(planner.state_for(second).judge.llm_used_today, 1)
        planner.reset()

    def test_task_walk_needs_no_memory(self):
        """Walking to a point must be decidable from published fields alone."""
        from agent.brain import _task_walk
        from agent.protocol import Turn
        payload = observation(round_no=20, role_pos=(1, 1))
        turn = Turn.load(payload)
        has_work, command = _task_walk(turn, turn.pioneer(), payload)
        self.assertTrue(has_work)
        self.assertIsNotNone(command, "应走向可接取的任务点")
        self.assertEqual(command["action"], "move")

    def test_standing_on_the_point_holds_instead_of_leaving(self):
        """Standing on a point must not hand the turn back to defence."""
        from agent.brain import _task_walk
        from agent.protocol import Turn
        payload = observation(round_no=20, role_pos=(5, 5))
        turn = Turn.load(payload)
        pion = turn.pioneer()
        goal = Pos(6, 5)
        self.assertLessEqual(max(abs(pion.pos.x - goal.x), abs(pion.pos.y - goal.y)), 1)
        has_work, command = _task_walk(turn, pion, payload)
        self.assertTrue(has_work, "站在任务点上也算有任务工作")
        self.assertIsNone(command, "站在点上应保持不动，而不是被防御调度拉走")

    def test_cooling_point_is_walked_to_and_waited_at(self):
        from agent.brain import _task_walk
        from agent.protocol import Turn
        payload = observation(round_no=20, role_pos=(1, 1), ready=False)
        payload["teamOur"]["playerTasks"][0]["coldDownRounds"] = 12
        turn = Turn.load(payload)
        has_work, command = _task_walk(turn, turn.pioneer(), payload)
        self.assertTrue(has_work, "冷却中的任务点应作为等待目标")
        self.assertIsNotNone(command)


    def test_new_match_resets_stale_task_memory(self):
        """A new match must not inherit the previous match's cooldown.

        Regression: the match key cannot tell two matches apart when they share a
        team id and map size (which is what a benchmark loop does), so the last
        task's 30-round cooldown leaked into the next run and silently blocked
        every acceptance there — the whole task subsystem produced nothing.
        """
        from agent import planner
        planner.reset()
        first = planner.state_for(observation(round_no=1200))
        first.note_round(1200)
        first.tasks["cooldown_until"] = 1230
        first.tasks["cycle"] = tasks.TaskCycle(point={"x": 6, "y": 5}, accepted_round=1200)
        first.judge.consume_llm(in_task=False)
        # The next run starts at round 1: the round counter went backwards.
        second = planner.state_for(observation(round_no=1))
        self.assertIs(second, first, "同一 key 仍复用同一条记忆")
        second.note_round(1)
        self.assertEqual(second.tasks.get("cooldown_until", 0), 0,
                         "新一轮不应继承上一轮的冷却")
        self.assertIsNone(second.tasks.get("cycle"), "新一轮不应继承上一轮的任务")
        self.assertEqual(second.judge.llm_used_today, 0, "新一轮的 LLM 额度应重置")
        # Inside the same match the counter moves forward and nothing is reset.
        second.tasks["cooldown_until"] = 75
        second.note_round(2)
        self.assertEqual(second.tasks["cooldown_until"], 75, "同一局内不应重置")
        planner.reset()


class PlannerPersistenceTests(unittest.TestCase):
    """The local debug page posts its state back, so the planner must survive JSON.

    Regression: the planner's live object was replaced by a summary (and then by a
    plain type-name string), so every round of the viewer started from a fresh
    planner. Task cycles, cooldowns and the LLM quota never persisted there while
    they did on the judge path — the viewer was exercising a different strategy.
    """

    def test_memory_round_trips_through_a_dump(self):
        from agent import planner
        from agent.tasks import TaskCycle
        planner.reset()
        state = planner.PlannerState()
        state.note_round(50)
        cycle = TaskCycle(point={"x": 6, "y": 5}, accepted_round=48,
                          description="任务描述", timeout_rounds=25)
        cycle.phase = "submitted"
        cycle.last_answer = "服务=D9"
        cycle.best_rate = 0.667
        state.tasks["cycle"] = cycle
        state.tasks["cooldown_until"] = 78
        state.judge.consume_llm(in_task=False)

        rebuilt = planner.PlannerState.load(state.dump())
        self.assertEqual(rebuilt.last_round, 50)
        self.assertEqual(rebuilt.tasks["cooldown_until"], 78)
        self.assertEqual(rebuilt.tasks["cycle"].phase, "submitted")
        self.assertEqual(rebuilt.tasks["cycle"].point, {"x": 6, "y": 5})
        self.assertEqual(rebuilt.tasks["cycle"].last_answer, "服务=D9")
        self.assertAlmostEqual(rebuilt.tasks["cycle"].best_rate, 0.667, places=3)
        self.assertEqual(rebuilt.judge.llm_used_today, state.judge.llm_used_today)
        planner.reset()

    def test_a_dump_is_json_serialisable(self):
        import json
        from agent import planner
        state = planner.PlannerState()
        state.note_round(3)
        json.dumps(state.dump(), ensure_ascii=False)
        planner.reset()

    def test_load_tolerates_partial_and_broken_input(self):
        from agent import planner
        self.assertEqual(planner.PlannerState.load(None).last_round, 0)
        self.assertIsNone(planner.PlannerState.load({}).tasks["cycle"])
        self.assertIsNone(planner.PlannerState.load("not a dump").tasks["cycle"])
        self.assertIsNone(planner.PlannerState.load({"tasks": {"cycle": 5}}).tasks["cycle"])
        planner.reset()


class DebugTransportTests(unittest.TestCase):
    """A state sent to the debug page must come back as the same strategy."""

    def test_viewer_state_keeps_the_planner_as_data(self):
        from agent import debug, planner
        from agent.scenarios import scenario
        from agent.simulator import step
        planner.reset()
        state = scenario(3, "challenger", 1)
        settled = step(state, {})["state"]
        wire = debug._viewer_state(settled)
        dump = (wire.get("_demo") or {}).get("planner")
        self.assertIsInstance(dump, dict, 'planner 必须以可回传的数据形式出现在调试响应里')
        self.assertIn("tasks", dump)
        self.assertIn("judge", dump)
        # And it rebuilds into a usable planner. `last_round` is the round the
        # planner *processed*, which is the one before the settled state's round.
        rebuilt = planner.PlannerState.load(dump)
        self.assertEqual(rebuilt.last_round, settled["roundNo"] - 1)
        self.assertIsInstance(rebuilt.tasks, dict)
        planner.reset()

    def test_debug_step_keeps_the_task_cycle_across_calls(self):
        """Two consecutive debug steps must share one planner memory."""
        from agent import debug, planner
        planner.reset()
        state = debug.scenario_payload(3, "challenger", 1)["state"]
        seen_cycle = False
        for _ in range(60):
            out = debug.step_payload(state)
            state = out["state"]
            dump = (state.get("_demo") or {}).get("planner") or {}
            tasks = dump.get("tasks") or {}
            if tasks.get("cooldown_until") or tasks.get("cycle"):
                seen_cycle = True
                break
        self.assertTrue(seen_cycle,
                        '调试流程必须保留任务冷却/任务周期，否则它跑的不是判题路径的策略')
        planner.reset()


class RunPathParityTests(unittest.TestCase):
    """The ways we drive the strategy must not diverge in behaviour.

    * judge POST — the competition entry, which carries no memory between requests
    * viewer     — ``/debug/step``, which serialises the planner over the wire
    * in-process — ``scenario`` + ``plan_for_state`` with one persistent planner

    Round 19 found the viewer silently running a *memoryless* strategy because the
    planner could not survive the round trip. These walk two paths side by side and
    compare what actually happened, so a transport change cannot quietly reintroduce
    a second behaviour.

    Each path gets its **own** planner, which is what production does — an earlier
    version of this test reset the global planner between judge rounds, so it was
    comparing a memoryless run against a remembering one and failing for the wrong
    reason.
    """

    ROUNDS = 60

    def _fingerprint(self, state, events):
        return (state["roundNo"], state["teamOur"]["totalScore"],
                state["teamOur"]["goldNum"], str(events))

    def test_viewer_transport_matches_a_direct_run(self):
        """Feeding the viewer's own serialised memory back must change nothing.

        The direct path is handed exactly the memory the viewer carries, so any
        behavioural difference is a real transport defect rather than two paths
        simply remembering different things.
        """
        from agent import debug, planner
        from agent.brain import plan_for_state
        from agent.scenarios import observation, scenario
        from agent.simulator import step
        for seed in (3, 7):
            viewer_state = debug.scenario_payload(seed, "challenger", 1)["state"]
            direct_state = scenario(seed, "challenger", 1)
            for index in range(self.ROUNDS):
                carried = (viewer_state.get("_demo") or {}).get("planner")
                viewer_out = debug.step_payload(viewer_state)
                viewer_state = viewer_out["state"]
                payload = observation(direct_state)
                commands = plan_for_state(payload, planner.PlannerState.load(carried),
                                          judge_tasks=False).commands
                direct_out = step(direct_state, commands)
                direct_state = direct_out["state"]
                self.assertEqual(
                    self._fingerprint(viewer_state, viewer_out["events"]),
                    self._fingerprint(direct_state, direct_out["events"]),
                    f'seed {seed} 第 {index + 1} 回合：调试传输路径与直接驱动必须一致')
                if direct_out["done"]:
                    break

    def test_state_less_requests_still_run_the_task_loop(self):
        """A fresh planner per request must still accept, submit and settle tasks.

        This is the property the stateless fallbacks exist for, and it is what the
        official judge path relies on: a POST carries no memory, so anything the
        task loop needs has to come from the published request.

        It is deliberately **not** an assertion that a memoryless run matches a
        remembering one command for command, or even outcome for outcome: memory
        legitimately changes movement planning (a planner that knows a cycle is in
        flight suppresses a redundant accept, and the pioneer then walks a different
        route), so the two diverge without either being wrong. What must hold is
        that the memoryless path completes tasks at all.
        """
        from agent import planner
        from agent.brain import plan_for_state
        from agent.scenarios import observation, scenario
        from agent.simulator import step
        seed = 3
        state = scenario(seed, "challenger", 1)
        accepts = submits = 0
        for index in range(self.ROUNDS * 3):
            payload = observation(state)
            commands = plan_for_state(payload, planner.PlannerState(),
                                      judge_tasks=False).commands
            for value in commands.values():
                if value.get("action") == "acceptTask":
                    accepts += 1
                elif value.get("action") == "submitAnswer":
                    submits += 1
            outcome = step(state, commands)
            state = outcome["state"]
            if outcome["done"]:
                break
        self.assertGreater(accepts, 0, '无状态请求也必须能领取任务')
        self.assertGreater(submits, 0, '无状态请求也必须能提交答案')
        self.assertGreater(state["teamOur"]["totalScore"], 0,
                           '无状态请求也必须拿到任务积分')


if __name__ == "__main__":
    unittest.main()
