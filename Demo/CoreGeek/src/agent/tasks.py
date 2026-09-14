"""Task pipeline: accept -> hold -> solve -> submit -> result.

The official task rules (任务书 §五, 接口文档 §1.3.2) are a fixed frame:

* only a pioneer may accept, standing within one cell of **our own** task point;
* a task ends when it is completed, when ``timeoutRounds`` passes, when the
  pioneer leaves the task point's one-cell ring, or when the pioneer dies;
* after the end the point refreshes 30 rounds later and has a finite number of
  tasks per match;
* scoring uses the **best pass rate submitted so far** when a task times out,
  so every answer must be kept, not just the last one.

What is *not* fixed by the documents is how to solve a task. That is therefore a
pluggable registry: a solver declares which task text it can handle and returns a
step plan (submit / ask the LLM / run a sandbox command / wait). The pipeline
owns the state machine, the solver owns the knowledge, so a new task type is a
new solver rather than a change to the loop.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .protocol import PIONEER, Pos, Turn, Unit, distance, station_footprint
from .sandbox import PendingRequest, shell
from . import task_lifecycle

# 任务书 §五: after a task ends the point needs 30 rounds before it is ready.
REFRESH_ROUNDS = 30
# The pioneer must stay inside the task point's one-cell ring (接口文档 §1.3.2).
HOLD_RANGE = 1
# Leave a couple of rounds of margin before a timeout so the answer lands.
SUBMIT_MARGIN = 2


class TaskError(ValueError):
    """Raised when a task plan would violate the official frame."""


# --------------------------------------------------------------------------
# Cycle state
# --------------------------------------------------------------------------

@dataclass
class Submission:
    round_no: int
    answer: str
    source: str


@dataclass
class TaskCycle:
    """One accepted task, from acceptance to its ending."""

    point: dict[str, int]
    accepted_round: int
    task_type: str = ""
    description: str = ""
    timeout_rounds: int = 0
    score_reward: int = 0
    gold_reward: int = 0
    submissions: list[Submission] = field(default_factory=list)
    last_answer: str = ""
    answer_source: str = ""
    phase: str = "accepted"  # accepted | submitted | ended
    end_reason: str = ""
    ended_round: int = 0

    @property
    def deadline(self) -> int | None:
        return self.accepted_round + self.timeout_rounds if self.timeout_rounds > 0 else None

    def rounds_left(self, round_no: int) -> int | None:
        return self.deadline - round_no if self.deadline is not None else None

    def record(self, round_no: int, answer: str, source: str) -> None:
        """Keep every answer: a timeout is scored on the best pass rate so far."""
        answer = str(answer)
        if not answer.strip():
            raise TaskError("答案不能为空")
        self.submissions.append(Submission(round_no, answer, source))
        self.last_answer = answer
        self.answer_source = source
        self.phase = "submitted"

    def end(self, round_no: int, reason: str) -> None:
        self.phase = "ended"
        self.end_reason = reason
        self.ended_round = round_no

    def summary(self) -> dict[str, Any]:
        return {
            "point": self.point,
            "type": self.task_type,
            "accepted": self.accepted_round,
            "deadline": self.deadline,
            "phase": self.phase,
            "endReason": self.end_reason,
            "submissions": [{"round": s.round_no, "source": s.source,
                             "answer": s.answer[:120]} for s in self.submissions],
        }


# --------------------------------------------------------------------------
# Solver registry
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Plan:
    """What a solver wants the pipeline to do this round."""

    kind: str  # submit | prompt | cmd | wait | give_up
    answer: str = ""
    prompt: str = ""
    command: str = ""
    purpose: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SolverContext:
    """Everything a solver may look at: official observations plus its own notes.

    Deliberately excludes simulator-private state; a solver works from the
    published observation, the task description and the judge's own replies.
    """

    cycle: TaskCycle
    round_no: int
    phase_task: str
    llm_resp: str
    cmd_output: str
    cmd_status: str
    notes: dict[str, Any]
    backpack: tuple[str, ...]
    is_day: bool
    cognitive_solver: Any = None


Solver = Callable[[SolverContext], Plan]


class SolverRegistry:
    """Atomic registry of task solvers, tried in priority order.

    Each entry keeps its **own** priority, so ordering is a stored property of the
    registration rather than a snapshot of the last ``register`` call. Ascending
    numeric order wins; equal priorities keep stable insertion order (Python's
    ``sort`` is stable), and ``replace=True`` removes and re-appends the entry, so
    a replaced solver takes a new registration-order position among its peers.
    """

    def __init__(self) -> None:
        # (name, solver, priority) — priority is stored per entry.
        self._solvers: list[tuple[str, Solver, int]] = []
        self._lock = threading.RLock()

    def register(self, name: str, solver: Solver, *, priority: int = 100,
                 replace: bool = False) -> Solver:
        if not callable(solver):
            raise TaskError("solver must be callable")
        if not name:
            raise TaskError("solver needs a name")
        with self._lock:
            if any(existing == name for existing, *_ in self._solvers) and not replace:
                raise TaskError(f"solver {name!r} already registered")
            self._solvers = [(n, s, p) for n, s, p in self._solvers if n != name]
            self._solvers.append((name, solver, priority))
            # Stable sort by the stored priority: equal priorities keep the order
            # in which they were (re)registered.
            self._solvers.sort(key=lambda item: item[2])
        return solver

    def unregister(self, name: str) -> None:
        with self._lock:
            self._solvers = [(n, s, p) for n, s, p in self._solvers if n != name]

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(name for name, *_ in self._solvers)

    def solve(self, context: SolverContext) -> tuple[str, Plan] | None:
        """First solver that returns a plan wins; ``None`` means "no idea yet"."""
        with self._lock:
            solvers = list(self._solvers)
        for name, solver, _priority in solvers:
            plan = solver(context)
            if plan is not None:
                return name, plan
        return None


def _ring(center: Pos) -> tuple[Pos, ...]:
    """The eight cells around a point (Chebyshev distance 1)."""
    return tuple(
        Pos(center.x + dx, center.y + dy)
        for dx in (-1, 0, 1) for dy in (-1, 0, 1)
        if dx or dy
    )


def ring_cells(center: Pos) -> tuple[Pos, ...]:
    """The eight cells around a point (Chebyshev distance 1)."""
    return _ring(center)


def walk_to_ring(turn: Turn, role: Unit, goal: Pos) -> Pos | None:
    """One step toward standing next to `goal`.

    Task points are obstacles (任务书 §4.1), so the search targets the walkable
    cells around them rather than the point itself. Every approach cell is tried,
    nearest first, and a greedy fallback keeps progress on crowded maps where the
    path search budget is exhausted. Shared by the task pipeline and the defence
    planner so both agree on how a point is approached.
    """
    from .grid import ERRAND_EXPANSIONS, next_step  # local import keeps order simple
    if distance(role.pos, goal) <= HOLD_RANGE:
        return None
    stands = [cell for cell in _ring(goal) if turn.land(cell)]
    stands.sort(key=lambda cell: (distance(role.pos, cell), cell.x, cell.y))
    for stand in stands:
        # A task errand is the longest walk in the game, so it gets the larger
        # search budget: running out of expansions here used to return "no route"
        # and leave the pioneer drifting instead of walking to the point.
        step = next_step(turn, role, stand, max_expansions=ERRAND_EXPANSIONS)
        if step is not None:
            return step
    greedy = [
        Pos(role.pos.x + dx, role.pos.y + dy)
        for dx in (-1, 0, 1) for dy in (-1, 0, 1)
        if (dx or dy)
        and turn.land(Pos(role.pos.x + dx, role.pos.y + dy))
        and distance(Pos(role.pos.x + dx, role.pos.y + dy), goal) < distance(role.pos, goal)
    ]
    if greedy:
        greedy.sort(key=lambda cell: (distance(cell, goal), cell.x, cell.y))
        return greedy[0]
    return None


REGISTRY = SolverRegistry()


def register(name: str, solver: Solver, *, priority: int = 100,
             replace: bool = False) -> Solver:
    return REGISTRY.register(name, solver, priority=priority, replace=replace)


# --------------------------------------------------------------------------
# Built-in solvers
# --------------------------------------------------------------------------

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _strip_title(text: str) -> str:
    """Drop a leading bracketed title block (``【接口探测】``) from a description."""
    return re.sub(r"^\s*[【\[][^】\]]{0,40}[】\]]\s*", "", str(text or ""))


# Separators that end a field label or value. Both the ASCII and the full-width
# forms appear in real task text, and an answer must not carry a trailing ';'
# into the judge, so both are listed explicitly rather than "normalised" away.
_FIELD_STOP = set(" \t\n，。；、?？!！;,.")
_INSTRUCTION_MARKERS = ("请", "读取", "回报", "如下", "并", "查询")


def _is_field_name(candidate: str) -> bool:
    """True when a token looks like a field label rather than prose."""
    name = candidate.strip(" \t，。；、")
    if not name or len(name) > 16:
        return False
    if any(marker in name for marker in _INSTRUCTION_MARKERS):
        return False
    return True


def _scan_pairs(text: str) -> list[tuple[str, str]]:
    """Scan for field/value pairs right-to-left so the nearest label wins.

    A regex cannot do this reliably: in ``…并回报：服务：A3`` the engine starts at
    the instruction's colon and ends up with ``服务`` as the *value*. Walking the
    separators backwards means the label closest to a value is always the one
    taken, which is the only reading that makes sense for task text.
    """
    body = _strip_title(text)
    pairs: list[tuple[str, str]] = []
    position = len(body)
    while position > 0:
        index = max(body.rfind(delimiter, 0, position)
                    for delimiter in ("：", ":", "="))
        if index < 0:
            break
        position = index
        start = index
        while start > 0 and body[start - 1] not in _FIELD_STOP and body[start - 1] not in "：:=":
            start -= 1
        name = body[start:index]
        end = index + 1
        while end < len(body) and body[end] not in _FIELD_STOP and body[end] not in "：:=":
            end += 1
        value = body[index + 1:end]
        if value and _is_field_name(name):
            pairs.append((name.strip(" \t，。；、"), value.strip(" \t，。；、,.;")))
        # A name may contain an earlier delimiter (an instruction colon), so keep
        # scanning from inside it rather than jumping past the whole name.
        position = max(start, index - 1)
    pairs.reverse()
    return pairs


def solver_keyword_fill(context: SolverContext) -> Plan | None:
    """Rule solver: answer a task whose fields can be read from the description.

    Handles the published shape of "answer these N fields" tasks by matching
    ``字段名`` against the description text. When the description asks for
    something that must be *looked up* (an API result, a computed value) it
    returns ``None`` so a later solver, or the LLM, can take over.
    """
    text = _strip_title(context.cycle.description or context.phase_task)
    if not text:
        return None
    asks = re.findall(r"([^\s，。；:：]{1,24})\s*[?？]", text)
    pairs = _scan_pairs(text)
    if not pairs:
        return None
    # Values stated in the description itself are safe answers; a field the
    # description only asks about (no value) must not be guessed.
    fields = dict(pairs)
    wanted = [field for field, _ in pairs]
    if asks:
        wanted = [field for field in wanted if field in asks] or wanted
    answer_parts = [f"{field}={fields[field]}" for field in wanted if field in fields]
    if not answer_parts:
        return None
    return Plan(kind="submit", answer="; ".join(answer_parts),
                purpose="规则解析：字段值直接写在任务描述中")


def solver_probe_command(context: SolverContext) -> Plan | None:
    """Rule solver: run one exploratory command when nothing else applies.

    Self-evolving tasks are explicitly about exploring a sandbox and forming a
    reusable procedure (任务书 §5.3). With no official sample available, the best
    honest behaviour is a **read-only probe** that reports what the sandbox
    contains — never a guessed answer.
    """
    notes = context.notes
    if notes.get("probed"):
        return None
    if context.cmd_status == "pending":
        return None
    if context.cmd_status in ("timeout", "judger_error"):
        # Sandbox trouble is not our fault and not worth retrying forever.
        notes["probed"] = True
        return None
    notes["probed"] = True
    return Plan(kind="cmd",
                command=shell("pwd && ls -la && python3 -c \"import sys;print(sys.version)\""),
                purpose="自进化任务：只读探查沙盒环境")


def solver_llm_ask(context: SolverContext) -> Plan | None:
    """LLM-backed solver: ask the judge's model once, then submit its reply.

    The prompt is deliberately explicit about the expected answer shape, and the
    reply is only submitted when it is non-trivial; a refused or empty response
    ends the attempt instead of submitting noise.
    """
    notes = context.notes
    if notes.get("llm_asked") and not context.llm_resp:
        return None
    if context.llm_resp:
        # Pick the answer line *before* normalising whitespace, otherwise the
        # joined text hides the field line and the model's prose leaks in.
        answer = _extract_answer_line(context.llm_resp)
        answer = " ".join(answer.split())
        if len(answer) < 2:
            notes["llm_asked"] = True
            return None
        notes.setdefault("llm_asked", True)
        return Plan(kind="submit", answer=answer, purpose="LLM 回答")
    if notes.get("llm_asked") or not context.cycle.description:
        return None
    notes["llm_asked"] = True
    prompt = (
        "你在协助一个自动化参赛程序回答游戏内的任务。\n"
        "请只输出答案本身，多个字段用 '字段=值' 并用 '; ' 分隔，不要解释、不要多余文字。\n"
        f"任务描述：\n{context.cycle.description}\n"
    )
    return Plan(kind="prompt", prompt=prompt, purpose="LLM 作答")


def _extract_answer_line(text: str) -> str:
    """Prefer a ``字段=值`` line; otherwise the first non-empty line.

    An answer must not carry the model's explanation with it, so the candidate
    line is trimmed after the last field-like token.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if "=" in line:
            return " ".join(line.split())
    return lines[0] if lines else text


def solver_last_resort(**_ignored: Any) -> Plan | None:
    """Never guess. Parking on the point keeps the option to submit later."""
    return None


def default_registry() -> SolverRegistry:
    registry = SolverRegistry()
    registry.register("keyword-fill", solver_keyword_fill, priority=10)
    registry.register("task-agent", lambda context: context.cognitive_solver(context)
                      if context.cognitive_solver is not None else None, priority=40)
    registry.register("llm-ask", solver_llm_ask, priority=50)
    registry.register("probe-command", solver_probe_command, priority=80)
    return registry


DEFAULT_SOLVERS = default_registry


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

class TaskPipeline:
    """Owns the accepted-task state machine and turns it into official commands.

    Stateless with respect to the match: the caller passes the observation and
    receives at most one command for the pioneer plus, optionally, a prompt or a
    sandbox command. Everything the pipeline remembers lives in ``state``, which
    the caller persists under the simulator's private area.
    """

    def __init__(self, registry: SolverRegistry | None = None) -> None:
        # Default to the built-in solver set; the module-level REGISTRY stays an
        # empty extension point so plugins can register without inheriting ours.
        self.registry = registry if registry is not None else default_registry()

    # -- task point helpers ------------------------------------------------
    @staticmethod
    def own_points(state: dict[str, Any], team: str) -> list[dict[str, Any]]:
        """Task points of our own team, taken from the map zones.

        The zones are authoritative (``mapInfo.zones``); ``playerTasks`` is a
        second, sometimes filtered, view of the same points. Task point 2 also
        spans two cells, which the zone entry does not spell out, so the walk and
        hold logic always goes through :meth:`point_cells`.
        """
        points = []
        for zone in (state.get("mapInfo") or {}).get("zones") or ():
            kind = str(zone.get("neutralType", ""))
            if kind.startswith(team) and "TaskPoint" in kind:
                points.append(zone)
        return points

    @classmethod
    def point_cells(cls, zone: dict[str, Any]) -> tuple[Pos, ...]:
        """Task point 2 occupies two cells; both are valid standing spots."""
        pos = Pos.load(zone["pos"])
        if str(zone.get("neutralType", "")).endswith("TaskPoint2"):
            # Officially the second point spans two cells; the observation gives
            # one of them, so both the cell and its right neighbour are checked.
            return (pos, Pos(pos.x + 1, pos.y))
        return (pos,)

    @classmethod
    def published_points(cls, state: dict[str, Any], team: str) -> list[dict[str, Any]]:
        """The ``playerTasks`` view, filled in from zones when absent."""
        published = (state.get("teamOur") or {}).get("playerTasks")
        if isinstance(published, list):
            return published  # An explicit empty offer list is authoritative.
        default = {"scoreReward": 50, "goldReward": 30, "timeoutRounds": 0,
                   "coldDownRounds": 0, "isValid": True}
        return [dict(default, taskPosition=dict(zone["pos"]),
                     taskType=str(zone.get("neutralType")))
                for zone in cls.own_points(state, team)]

    @classmethod
    def on_point(cls, state: dict[str, Any], team: str, pos: Pos) -> dict[str, Any] | None:
        for zone in cls.own_points(state, team):
            if min(distance(pos, cell) for cell in cls.point_cells(zone)) <= HOLD_RANGE:
                return zone
        return None

    # -- main entry --------------------------------------------------------
    def step(self, *, state: dict[str, Any], turn: Turn, pioneer: Unit | None,
             judge: Any, notes: dict[str, Any], is_day: bool,
             pending: PendingRequest | None, cognitive_solver=None) -> tuple[dict[str, Any] | None, Plan | None]:
        """Return ``(command, plan)`` for this round, or ``(None, None)``.

        ``judge`` is a :class:`~agent.sandbox.JudgeState`; ``notes`` is the
        pipeline's own per-match memory. The returned command is always a legal
        official instruction; the plan explains why.

        The pipeline works from the observation alone when it has no memory: the
        judge publishes ``phaseTask`` and ``playerTasks`` every round, so a
        stateless caller (which is what the official POST is) still holds a task
        correctly. Memory only adds solver notes and the exact accept round.
        """
        cycle: TaskCycle | None = notes.get("cycle")
        if pioneer is None or pioneer.health <= 0:
            if cycle is not None and cycle.phase != "ended":
                cycle.end(turn.round_no, "开拓者死亡")
                notes["cycle"] = None
                notes["cooldown_until"] = turn.round_no + REFRESH_ROUNDS
            return None, None
        team = state["teamOur"].get("type", "challenger")
        if cycle is None and self._published_task_open(state, team):
            # Stateless fallback: the judge says a task is running, so rebuild the
            # cycle from published fields. Timeout comes from the observation, so
            # the deadline is still the official one.
            cycle = self._cycle_from_observation(state, turn, team)
            if cycle is not None:
                notes["cycle"] = cycle
        if cycle is None:
            return self._maybe_accept(state, turn, pioneer, team, notes, judge)
        return self._advance(state, turn, pioneer, team, cycle, judge, notes,
                             is_day=is_day, pending=pending, cognitive_solver=cognitive_solver)

    def _published_task_open(self, state: dict[str, Any], team: str) -> bool:
        if str(state.get("phaseTask") or "").strip():
            return True
        # Some judges publish only the point's cooldown while a task is running.
        return False

    def _cycle_from_observation(self, state: dict[str, Any], turn: Turn,
                                team: str) -> TaskCycle | None:
        description = str(state.get("phaseTask") or "").strip()
        if not description:
            return None
        pioneer = turn.pioneer()
        if pioneer is None:
            return None
        zones = self.own_points(state, team)
        candidates = []
        for entry in self.published_points(state, team):
            point = entry.get('taskPosition')
            if not point:
                continue
            separation = min(distance(pioneer.pos, cell)
                             for cell in task_lifecycle.region_cells(point, zones))
            if separation <= HOLD_RANGE:
                candidates.append((separation, point['x'], point['y'], entry))
        if not candidates:
            return None  # No evidence of a current, reachable task region.
        entry = min(candidates, key=lambda item: item[:3])[3]
        return TaskCycle(point=dict(entry['taskPosition']),
                         accepted_round=turn.round_no,
                         task_type=str(entry.get('taskType') or '自进化类'),
                         description=description,
                         timeout_rounds=int(entry.get('timeoutRounds') or 0))

    # -- phases ------------------------------------------------------------
    def _maybe_accept(self, state: dict[str, Any], turn: Turn, pioneer: Unit,
                      team: str, notes: dict[str, Any],
                      judge: Any) -> tuple[dict[str, Any] | None, Plan | None]:
        if notes.get("cycle"):
            return None, None
        # The judge publishes the running task's text in `phaseTask`; while it is
        # non-empty a task is already in flight. Without this guard a strategy that
        # has no cross-round memory re-accepts every round, which keeps restarting
        # the task so it never settles — observed locally as hundreds of "接受任务"
        # events and no completed task at all.
        if str(state.get("phaseTask") or "").strip():
            return None, Plan(kind="hold", purpose="任务进行中，不重复领取")
        # `isValid` is the judge's own answer to "may this point be accepted now?",
        # so it gates acceptance directly, per point. The 30-round refresh is a
        # property of the *point* that ended a task (任务书 §五), not of the team, so
        # there is deliberately no global cooldown gate here: one cooling point must
        # not block a task at the other one. A global gate was measured leaving task
        # income unclaimed for 30 rounds after every single task.
        # Only our own task points exist for us (任务书 §4.6.2): enemy points in
        # the same map must never become a walking target.
        own_points = self.own_points(state, team)
        if not own_points:
            return None, None
        points = self.published_points(state, team)
        # The observation publishes per-point readiness; trust it over our guess.
        # `coldDownRounds` is the point's own refresh countdown (任务书 §五: 30
        # rounds after a task ends), so a cooling point is not acceptable yet even
        # when the judge still marks it valid.
        regions = task_lifecycle.ready_regions(points,own_points,notes,turn.round_no)
        targets = [cell for _anchor,cells in regions for cell in cells]
        if not targets:
            return None, None
        for anchor,cells in regions:
            if min(distance(pioneer.pos,cell) for cell in cells)<=HOLD_RANGE:
                notes['accept_target']=anchor
                return {"action": "acceptTask"}, Plan(kind="accept", purpose="已站在可领取的己方任务点，领取任务")
        goal = min(targets, key=lambda pos: (distance(pioneer.pos, pos), pos.x, pos.y))
        command,plan=self._walk_to(turn, pioneer, goal, notes)
        if plan is not None and plan.kind=='accept':
            notes['accept_target']=next(anchor for anchor,cells in regions if goal in cells)
        return command,plan

    def _advance(self, state: dict[str, Any], turn: Turn, pioneer: Unit, team: str,
                 cycle: TaskCycle, judge: Any, notes: dict[str, Any], *,
                 is_day: bool, pending: PendingRequest | None, cognitive_solver=None
                 ) -> tuple[dict[str, Any] | None, Plan | None]:
        # 0. The judge is authoritative: if it no longer publishes our task, the
        #    cycle is over whatever we believed. Without this we keep submitting
        #    answers for a task that already ended, and every such command is an
        #    execution failure.
        published = str(state.get("phaseTask") or "")
        if not cycle.description and not published:
            results=state.get('lastRoundRoleActionResults') or {}
            rejected=isinstance(results,dict) and (results.get(str(pioneer.unit_id),results.get(pioneer.unit_id)) is False)
            age=turn.round_no-cycle.accepted_round
            if rejected or age>task_lifecycle.TUNING.observation_wait:
                reason='accept_rejected' if rejected else 'task_not_published'
                task_lifecycle.defer(notes,cycle.point,turn.round_no,reason)
                notes['cycle']=None
                notes['solver_notes']={}
                return None,Plan(kind='accept_unconfirmed',purpose='领取未确认，按任务点退避重试')
            notes['acceptance_status']={'phase':'awaiting_observation','point':dict(cycle.point),
                                        'sent_round':cycle.accepted_round}
            return None,Plan(kind='wait',purpose='等待判题器发布任务原文')
        if published and not cycle.description:
            cycle.description=published
            for point in self.published_points(state,team):
                if point.get('taskPosition')==cycle.point:
                    cycle.timeout_rounds=int(point.get('timeoutRounds') or 0)
                    break
            task_lifecycle.confirm(notes,cycle.point)
        # 1. Endings the frame defines, checked before any new work.
        end_reason = self._ending(state, turn, pioneer, team, cycle)
        if end_reason:
            cycle.end(turn.round_no, end_reason)
            notes["cycle"] = None
            notes["cooldown_until"] = turn.round_no + REFRESH_ROUNDS
            return None, Plan(kind="task_end", purpose=f"任务结束：{end_reason}")

        if (cycle.submissions or cycle.description) and 'phaseTask' in state and not published:
            cycle.end(turn.round_no, "判题器已结束任务")
            notes["cycle"] = None
            notes["cooldown_until"] = turn.round_no + REFRESH_ROUNDS
            notes["solver_notes"] = {}
            return None, Plan(kind="task_end", purpose="任务已由判题器结算")

        here = self.on_point(state, team, pioneer.pos)
        if here is None:
            # Not fatal yet: the pioneer may simply be walking back.
            goal = min((cell for zone in self.own_points(state, team)
                        for cell in self.point_cells(zone)),
                       key=lambda pos: (distance(pioneer.pos, pos), pos.x, pos.y),
                       default=None)
            if goal is None:
                return None, Plan(kind="wait", purpose="没有可用任务点")
            command, plan = self._walk_to(turn, pioneer, goal, notes)
            return command, plan or Plan(kind="wait", purpose="返回任务点")

        # 2. Solve: the registry decides, the pipeline only enforces the frame.
        context = SolverContext(
            cycle=cycle,
            round_no=turn.round_no,
            phase_task=str(state.get("phaseTask") or cycle.description),
            llm_resp=str(state.get("llmResp") or ""),
            cmd_output=judge.last_result.output if judge else "",
            cmd_status=self._cmd_status(judge, pending),
            notes=notes.setdefault("solver_notes", {}),
            backpack=tuple(pioneer.backpack),
            is_day=is_day,
            cognitive_solver=cognitive_solver,
        )
        solved = self.registry.solve(context)
        if solved is None:
            # No idea yet: ask the model if we are allowed, then give up politely.
            if not notes.get("solver_notes", {}).get("llm_asked") and cycle.description:
                return None, Plan(kind="prompt",
                                  prompt=self._default_prompt(cycle), purpose="无可判定规则，交给 LLM")
            if cycle.timeout_rounds and cycle.rounds_left(turn.round_no) <= SUBMIT_MARGIN:
                cycle.end(turn.round_no, "超时无解")
                notes["cycle"] = None
                notes["cooldown_until"] = turn.round_no + REFRESH_ROUNDS
                return None, Plan(kind="task_end", purpose="超时且没有可提交的答案")
            return self._hold(turn, pioneer, notes)
        name, plan = solved
        notes["last_solver"] = name
        notes["last_plan"] = {"kind": plan.kind, "purpose": plan.purpose}

        if plan.kind == "submit":
            if plan.answer:
                cycle.record(turn.round_no, plan.answer, name)
                return ({"action": "submitAnswer", "taskAnswer": plan.answer},
                        plan)
            return self._hold(turn, pioneer, notes)
        if plan.kind in ("prompt", "cmd"):
            # These travel in the response, not in roleCommandMap: parking the
            # pioneer on the point keeps the task alive while we work.
            return None, plan
        if plan.kind == "give_up":
            cycle.end(turn.round_no, plan.reason or "主动放弃")
            notes["cycle"] = None
            notes["cooldown_until"] = turn.round_no + REFRESH_ROUNDS
            return None, plan
        return self._hold(turn, pioneer, notes)

    def _ending(self, state: dict[str, Any], turn: Turn, pioneer: Unit, team: str,
                cycle: TaskCycle) -> str:
        if pioneer.health <= 0:
            return "开拓者死亡"
        if cycle.timeout_rounds and cycle.rounds_left(turn.round_no) <= 0:
            return "任务超时"
        if cycle.point and min(distance(pioneer.pos,cell) for cell in
                               task_lifecycle.region_cells(cycle.point,self.own_points(state,team))) > HOLD_RANGE:
            # The accepted point owns this task. Entering another point cannot
            # preserve it, and the official hold distance has no extra slack.
            return "离开任务点范围"
        return ""

    @staticmethod
    def _cmd_status(judge: Any, pending: PendingRequest | None) -> str:
        if pending is not None and pending.kind == "cmd":
            return "pending"
        return getattr(getattr(judge, "last_result", None), "status", "empty")

    def _hold(self, turn: Turn, pioneer: Unit, notes: dict[str, Any]
              ) -> tuple[dict[str, Any] | None, Plan | None]:
        notes.setdefault("holds", 0)
        notes["holds"] = int(notes["holds"]) + 1
        return None, Plan(kind="wait", purpose="在任务点等待求解结果")

    def _walk_to(self, turn: Turn, pioneer: Unit, goal: Pos, notes: dict[str, Any]
                 ) -> tuple[dict[str, Any] | None, Plan | None]:
        # Within the ring the pioneer may accept directly; this check must come
        # first, because pathfinding to a neighbouring cell would be a no-op.
        if distance(pioneer.pos, goal) <= HOLD_RANGE:
            return {"action": "acceptTask"}, Plan(kind="accept", purpose="就位领取任务")
        step = walk_to_ring(turn, pioneer, goal)
        if step is None:
            return None, Plan(kind="wait", purpose="无法到达任务点")
        purpose = "前往己方任务点" if distance(step, goal) < distance(pioneer.pos, goal) \
            else "向任务点靠近（退避式）"
        return ({"action": "move", "targetPos": [step.dump()]},
                Plan(kind="move", purpose=purpose))

    @staticmethod
    def _default_prompt(cycle: TaskCycle) -> str:
        return ("请回答下面这个自动化任务，只输出答案本身，多个字段用 '字段=值' 并用 '; ' 分隔。\n"
                f"任务描述：\n{cycle.description}\n")


def new_task_notes() -> dict[str, Any]:
    """Fresh per-match pipeline memory."""
    return {"cycle": None, "cooldown_until": 0, "solver_notes": {}, "holds": 0}


__all__ = [
    "Plan", "SolverContext", "SolverRegistry", "TaskCycle", "TaskError",
    "TaskPipeline", "Submission", "REGISTRY", "register", "new_task_notes",
    "default_registry", "solver_keyword_fill", "solver_llm_ask",
    "solver_probe_command", "REFRESH_ROUNDS", "HOLD_RANGE",
]
