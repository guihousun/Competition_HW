"""Per-match planner state for the judge-facing channels.

The official POST is stateless: every round carries a fresh observation and the
judge keeps the only durable state. Our side still needs a little memory to use
``prompt`` / ``executeCmd`` / ``lastCmdResult`` correctly, because a result
arrives **one round later** and the daily LLM allowance must survive across
requests.

This module keeps exactly that memory, keyed by the match's own identity (seed
of the scenario is not published, so the key is the observation digest). It holds
no game rules and no decisions: :mod:`agent.brain` asks for the state, uses it,
and writes it back, so the planner stays a pure function of (observation, state).
"""
from __future__ import annotations

import threading
import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field, asdict
from typing import Any

from .sandbox import CommandResult, JudgeState, LLM_DAILY_QUOTA, PendingRequest
from .tasks import TaskCycle, Submission
from .llm_router import LLMRouter
from .task_context import ContextStore, public_task_confirmed

# Top-level planner memory schema. A dump without this marker (older/newer/
# foreign) is restored conservatively: context is dropped and the ordinary LLM
# allowance is not silently reset to "free".
PLANNER_SCHEMA = "competition-hw-planner/1"


@dataclass
class PlannerState:
    """Everything we remember between two rounds of one match."""

    judge: JudgeState = field(default_factory=JudgeState)
    tasks: dict[str, Any] = field(default_factory=dict)
    # Shared cognitive-channel scheduler and bounded task context (P0b).
    llm_router: "LLMRouter | None" = None
    task_context: "ContextStore | None" = None
    team_agent: Any = None
    # Set when a restore could not be trusted, so the caller degrades explicitly.
    degraded: str | None = None
    # Round number of the last observation we handled, for cache validation.
    last_round: int = 0
    # Local counters that never affect the official protocol, only diagnostics.
    prompts_sent: int = 0
    commands_sent: int = 0
    _routed_key: str = field(default="", repr=False)
    _routed_fields: dict[str, Any] = field(default_factory=dict, repr=False)

    def routed_observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Expose only this task's associated replies to the legacy pipeline.

        Ingestion is shared by bookkeeping and planning, once per observation.
        Other owners consume their own router receipts; raw model text must not
        bypass that boundary through SolverContext or JudgeState.last_result.
        The original observation (including full trace text) is never modified.
        """
        confirmation = public_task_confirmed(payload, self.tasks.get("cycle"))
        raw_fields = {name: payload.get(name) for name in
                      ("roundNo", "llmResp", "lastCmdResult", "phaseTask", "errors")}
        key = hashlib.sha256(json.dumps(
            [raw_fields, confirmation.generation, confirmation.confirmed],
            ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        if key != self._routed_key:
            router = self.ensure_llm_router()
            previous = set(router.receipts)
            router.ingest(payload, confirmed_task=confirmation)
            allowed = {"llmResp": "", "lastCmdResult": ""}
            for receipt_id, receipt in router.receipts.items():
                if receipt_id in previous:
                    continue
                request = next((item for item in router.history.values()
                                if item.request_id == receipt.request_id), None)
                name = "llmResp" if receipt.kind == "prompt" else "lastCmdResult"
                if self.team_agent is not None:
                    self.team_agent.consume(request, receipt, payload.get(name) or "")
                if receipt.status != "received":
                    continue
                if (request is None or request.owner != "task"
                        or not confirmation.confirmed
                        or request.generation != confirmation.generation):
                    continue
                name = "llmResp" if receipt.kind == "prompt" else "lastCmdResult"
                # Use the original validated value, not the bounded preview.
                allowed[name] = payload.get(name) or ""
            self._routed_key, self._routed_fields = key, allowed
            self.judge.last_result = CommandResult("empty", None, "")
        return {**payload, **self._routed_fields}

    def ensure_llm_router(self) -> LLMRouter:
        if self.llm_router is None:
            self.llm_router = LLMRouter(self.judge)
        else:
            self.llm_router.judge = self.judge
        return self.llm_router

    def ensure_task_context(self) -> ContextStore:
        if self.task_context is None:
            self.task_context = ContextStore()
        return self.task_context

    def ensure_team_agent(self, payload):
        from .team_agent import TeamAgent
        owner = "team:" + hashlib.sha256(_match_key(payload).encode("utf-8")).hexdigest()
        if self.team_agent is None or self.team_agent.owner != owner:
            self.team_agent = TeamAgent(owner)
        return self.team_agent

    def dump(self) -> dict[str, Any]:
        """Serialise the memory so it can survive an HTTP round trip.

        The local debug page sends its state back on every step, and JSON cannot
        carry live objects. Without this the debug flow rebuilt a **fresh** planner
        each round, so the viewer was exercising a memoryless strategy instead of
        the one the judge sees — task cycles and cooldowns never persisted there
        while they did on the judge path.
        """
        cycle = self.tasks.get("cycle")
        return {
            "schema": PLANNER_SCHEMA,
            "degraded": self.degraded,
            "lastRound": int(self.last_round),
            "promptsSent": int(self.prompts_sent),
            "commandsSent": int(self.commands_sent),
            "tasks": {
                "cooldown_until": int(self.tasks.get("cooldown_until") or 0),
                "last_note": str(self.tasks.get("last_note") or ""),
                "last_error": str(self.tasks.get("last_error") or ""),
                "solver_notes": dict(self.tasks.get("solver_notes") or {}),
                "accept_retries": deepcopy(self.tasks.get('accept_retries') or {}),
                "acceptance_status": deepcopy(self.tasks.get('acceptance_status') or {}),
                "supervisor": deepcopy(self.tasks.get('supervisor') or {}),
                "cycle": (None if cycle is None else {
                    "point": dict(getattr(cycle, "point", {}) or {}),
                    "accepted_round": int(getattr(cycle, "accepted_round", 0) or 0),
                    "task_type": str(getattr(cycle, "task_type", "") or ""),
                    "description": str(getattr(cycle, "description", "") or ""),
                    "timeout_rounds": int(getattr(cycle, "timeout_rounds", 0) or 0),
                    "phase": str(getattr(cycle, "phase", "") or ""),
                    "last_answer": getattr(cycle, "last_answer", None),
                    "best_rate": float(getattr(cycle, "best_rate", 0.0) or 0.0),
                    "submissions": [asdict(item) for item in cycle.submissions],
                }),
            },
            "judge": {
                "llmUsedToday": int(self.judge.llm_used_today),
                "llmDay": int(self.judge.llm_day),
                "llmResponses": int(self.judge.llm_responses),
                "cmdRuns": int(self.judge.cmd_runs),
                "cmdFailures": int(self.judge.cmd_failures),
                "lastPrompt": str(self.judge.last_prompt or ""),
                "lastCommand": str(self.judge.last_command or ""),
                "pendingPrompt": asdict(self.judge.pending_prompt) if self.judge.pending_prompt else None,
                "pendingCmd": asdict(self.judge.pending_cmd) if self.judge.pending_cmd else None,
                "lastResult": asdict(self.judge.last_result),
            },
            "llmRouter": self.llm_router.dump() if self.llm_router is not None else None,
            "taskContext": self.task_context.dump() if self.task_context is not None else None,
            "teamAgent": self.team_agent.dump() if self.team_agent is not None else None,
        }

    def _fresh_tasks(self) -> dict[str, Any]:
        return {"cycle": None, "cooldown_until": 0, "solver_notes": {}}

    @classmethod
    def load(cls, dump: Any) -> "PlannerState":
        """Restore without trusting malformed transport data or freeing quota."""
        try:
            return cls._load(dump)
        except (TypeError, ValueError, OverflowError, RecursionError):
            state = cls()
            state.tasks = state._fresh_tasks()
            state.degraded = "malformed_planner"
            state.judge.llm_used_today = LLM_DAILY_QUOTA
            return state

    @classmethod
    def _load(cls, dump: Any) -> "PlannerState":
        """Rebuild a planner from :meth:`dump`, tolerating partial input.

        A non-dict input (an older summary, or a placeholder string from an earlier
        transport) yields an empty planner with the *same task shape* a fresh one
        has, so callers never have to special-case the keys.
        """
        state = cls()
        state.tasks = state._fresh_tasks()
        if not isinstance(dump, dict):
            state.degraded = "not_an_object"
            state.judge.llm_used_today = LLM_DAILY_QUOTA  # never reset unknown quota to free
            return state
        if dump.get("schema") != PLANNER_SCHEMA:
            state.degraded = ("unknown_schema" if isinstance(dump.get("schema"), str)
                              else "missing_schema")
            state.judge.llm_used_today = LLM_DAILY_QUOTA
            return state
        judge_record = dump.get("judge")
        if (not isinstance(judge_record, dict)
                or type(judge_record.get("llmUsedToday")) is not int
                or judge_record["llmUsedToday"] < 0
                or type(judge_record.get("llmDay")) is not int
                or judge_record["llmDay"] < 1):
            raise ValueError("unknown quota record")
        for name in ("lastRound", "promptsSent", "commandsSent"):
            if name in dump and (type(dump[name]) is not int or dump[name] < 0):
                raise ValueError("invalid planner counter")
        state.last_round = int(dump.get("lastRound") or 0)
        state.prompts_sent = int(dump.get("promptsSent") or 0)
        state.commands_sent = int(dump.get("commandsSent") or 0)
        tasks = dump.get("tasks") if isinstance(dump.get("tasks"), dict) else {}
        raw_cycle = tasks.get("cycle")
        cycle = None
        if isinstance(raw_cycle, dict):
            cycle = TaskCycle(
                point=dict(raw_cycle.get("point") or {}),
                accepted_round=int(raw_cycle.get("accepted_round") or 0),
                task_type=str(raw_cycle.get("task_type") or ""),
                description=str(raw_cycle.get("description") or ""),
                timeout_rounds=int(raw_cycle.get("timeout_rounds") or 0),
            )
            cycle.phase = str(raw_cycle.get("phase") or "")
            cycle.last_answer = raw_cycle.get("last_answer")
            cycle.best_rate = float(raw_cycle.get("best_rate") or 0.0)
            for item in raw_cycle.get('submissions') or []:
                if isinstance(item,dict) and isinstance(item.get('round_no'),int) and isinstance(item.get('answer'),str):
                    cycle.submissions.append(Submission(item['round_no'],item['answer'],str(item.get('source') or 'restored')))
        state.tasks = {
            "cycle": cycle,
            "cooldown_until": int(tasks.get("cooldown_until") or 0),
            "last_note": str(tasks.get("last_note") or ""),
            "last_error": str(tasks.get("last_error") or ""),
            "solver_notes": dict(tasks.get("solver_notes") or {}),
            "accept_retries": deepcopy(tasks.get('accept_retries')) if isinstance(tasks.get('accept_retries'),dict) else {},
            "acceptance_status": deepcopy(tasks.get('acceptance_status')) if isinstance(tasks.get('acceptance_status'),dict) else {},
            "supervisor": deepcopy(tasks.get('supervisor')) if isinstance(tasks.get('supervisor'),dict) else {},
        }
        judge = dump.get("judge") if isinstance(dump.get("judge"), dict) else {}
        state.judge.llm_used_today = int(judge.get("llmUsedToday") or 0)
        state.judge.llm_day = int(judge.get("llmDay") or 1)
        state.judge.llm_responses = int(judge.get("llmResponses") or 0)
        state.judge.cmd_runs = int(judge.get("cmdRuns") or 0)
        state.judge.cmd_failures = int(judge.get("cmdFailures") or 0)
        state.judge.last_prompt = str(judge.get("lastPrompt") or "")
        state.judge.last_command = str(judge.get("lastCommand") or "")
        for name, field_name in [('pendingPrompt', 'pending_prompt'), ('pendingCmd', 'pending_cmd')]:
            value = judge.get(name)
            if isinstance(value, dict) and isinstance(value.get('payload'), str) and isinstance(value.get('sent_round'), int):
                setattr(state.judge, field_name, PendingRequest(
                    str(value.get('kind') or ''), value['sent_round'], value['payload'],
                    str(value.get('purpose') or '')))
        if isinstance(judge.get('lastResult'), dict):
            from .sandbox import parse_command_result
            state.judge.last_result = parse_command_result(judge['lastResult'].get('raw'))
        if dump.get("teamAgent") is not None:
            from .team_agent import TeamAgent
            raw_agent = dump["teamAgent"]
            if not isinstance(raw_agent, dict) or not isinstance(raw_agent.get("owner"), str):
                raise ValueError("invalid coordinator owner")
            state.team_agent = TeamAgent.load(raw_agent, raw_agent["owner"])
        raw_router = dump.get("llmRouter")
        if raw_router is not None:
            router = LLMRouter(state.judge)
            router.load(raw_router)
            state.llm_router = router
            if router.degraded:
                state.degraded = router.degraded
        raw_context = dump.get("taskContext")
        if raw_context is not None:
            store = ContextStore()
            store.load(raw_context)
            state.task_context = store
            if store.degraded and state.degraded is None:
                state.degraded = store.degraded
        if state.degraded is None and isinstance(dump.get("degraded"), str):
            state.degraded = dump["degraded"]
        return state

    def note_round(self, round_no: int) -> None:
        """Per-round housekeeping: detect a new match and roll the game day over.

        With no official match ID, round one after a later round is our explicit
        local reset convention. Other backwards observations are stale and do not
        reset memory or quota. A replayed round one is indistinguishable from a
        new match on this interface; callers with lifecycle knowledge should use
        reset() or a fresh PlannerState rather than inventing an official ID.
        """
        round_no = int(round_no)
        if self.last_round and round_no < self.last_round and round_no != 1:
            return  # delayed observation, not evidence of a new match
        if self.last_round and round_no == 1 < self.last_round:
            self.tasks = {"cycle": None, "cooldown_until": 0, "solver_notes": {}}
            self.judge = JudgeState()
            self.llm_router = self.task_context = None
            self.team_agent = None
            self.degraded = None
            self.prompts_sent = self.commands_sent = 0
            self._routed_key, self._routed_fields = "", {}
        if self.degraded and not self.last_round:
            # A damaged snapshot first seen mid-match cannot get a free day just
            # because the default JudgeState started its day counter at one.
            self.judge.llm_day = (round_no - 1) // 130 + 1
            self.judge.llm_used_today = LLM_DAILY_QUOTA
        self.last_round = round_no
        self.judge.note_round(round_no)

    def note_submission(self, prompt: str | None, command: str | None,
                        round_no: int, in_task: bool) -> None:
        if prompt:
            # One accounting path: if the router already charged this round's
            # ordinary request, do not charge the same prompt a second time.
            router = self.llm_router
            already_charged = bool(router is not None and router.take_charge(round_no))
            if not already_charged:
                self.judge.consume_llm(in_task)
            self.judge.pending_prompt = PendingRequest("prompt", round_no, prompt)
            self.judge.last_prompt = prompt
            self.prompts_sent += 1
        if command:
            self.judge.pending_cmd = PendingRequest("cmd", round_no, command)
            self.judge.last_command = command
            self.commands_sent += 1

    def note_results(self, payload: dict[str, Any], round_no: int, *,
                     routed: bool = False) -> dict[str, Any]:
        """Fold the judge's previous answers into the state. Returns a summary."""
        summary: dict[str, Any] = {}
        from .sandbox import parse_command_result

        if routed:
            payload = self.routed_observation(payload)

        llm_resp = str(payload.get("llmResp") or "")
        if self.judge.pending_prompt is not None:
            if llm_resp and round_no > self.judge.pending_prompt.sent_round:
                self.judge.pending_prompt = None
                self.judge.llm_responses += 1
                summary["llm"] = "answered"
            elif self.judge.pending_prompt.expired(round_no):
                self.judge.pending_prompt = None
                summary["llm"] = "no-answer"
        raw = payload.get("lastCmdResult")
        if self.judge.pending_cmd is not None and raw not in (None, "") and round_no > self.judge.pending_cmd.sent_round:
            result = parse_command_result(raw)
            self.judge.last_result = result
            self.judge.pending_cmd = None
            if result.is_failure:
                self.judge.cmd_failures += 1
                summary["cmd"] = result.status
            else:
                self.judge.cmd_runs += 1
                summary["cmd"] = f"exit:{result.exit_code}"
        elif self.judge.pending_cmd is not None and self.judge.pending_cmd.expired(round_no):
            self.judge.pending_cmd = None
            self.judge.last_result = CommandResult("judger_error", None, "结果未返回")
            self.judge.cmd_failures += 1
            summary["cmd"] = "missing"
        return summary


_STATES: dict[str, PlannerState] = {}
_LOCK = threading.RLock()
# Fixed lock striping bounds memory while serialising complete transactions for
# the same public team identity. Locks stay outside serialised planner objects.
_PLANNING_LOCKS = tuple(threading.RLock() for _ in range(16))
MAX_TRACKED_MATCHES = 8


def planning_lock(payload: dict[str, Any]):
    digest = hashlib.sha256(_match_key(payload).encode("utf-8")).digest()
    return _PLANNING_LOCKS[digest[0] % len(_PLANNING_LOCKS)]


def _match_key(payload: dict[str, Any]) -> str:
    """Identify a match from published fields only.

    The seed is simulator-private and must not be needed here. The map *layout* is
    deliberately not part of the key either: zones change during a match (mines
    deplete and respawn), and a key that moves would silently reset the planner's
    memory — quota, cooldowns and pending judge requests — on every such round.
    Team identity plus map dimensions are stable for the whole match.
    """
    team = payload.get("teamOur") or {}
    info = payload.get("mapInfo") or {}
    identity = team.get('teamId') or team.get('teamName') or 'team'
    return f"{identity}|{team.get('type')}|{info.get('width')}x{info.get('height')}"


def state_for(payload: dict[str, Any]) -> PlannerState:
    key = _match_key(payload)
    with _LOCK:
        state = _STATES.get(key)
        if state is None:
            # A process first seeing a mid-match request has no evidence about
            # ordinary calls already made today. Do not grant a fresh allowance.
            first_round = type(payload.get("roundNo")) is int and payload["roundNo"] == 1
            state = PlannerState() if first_round else PlannerState.load(None)
            _STATES[key] = state
            while len(_STATES) > MAX_TRACKED_MATCHES:
                _STATES.pop(next(iter(_STATES)))
        return state


def reset(payload: dict[str, Any] | None = None) -> None:
    """Forget one match (or all of them) — used by tests and the reset button."""
    with _LOCK:
        if payload is None:
            _STATES.clear()
            return
        _STATES.pop(_match_key(payload), None)


def tracked() -> int:
    with _LOCK:
        return len(_STATES)


__all__ = ["PlannerState", "state_for", "reset", "tracked", "MAX_TRACKED_MATCHES"]
