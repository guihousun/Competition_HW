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
from copy import deepcopy
from dataclasses import dataclass, field, asdict
from typing import Any

from .sandbox import CommandResult, JudgeState, PendingRequest
from .tasks import TaskCycle, Submission


@dataclass
class PlannerState:
    """Everything we remember between two rounds of one match."""

    judge: JudgeState = field(default_factory=JudgeState)
    tasks: dict[str, Any] = field(default_factory=dict)
    # Round number of the last observation we handled, for cache validation.
    last_round: int = 0
    # Local counters that never affect the official protocol, only diagnostics.
    prompts_sent: int = 0
    commands_sent: int = 0

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
        }

    @classmethod
    def load(cls, dump: Any) -> "PlannerState":
        """Rebuild a planner from :meth:`dump`, tolerating partial input.

        A non-dict input (an older summary, or a placeholder string from an earlier
        transport) yields an empty planner with the *same task shape* a fresh one
        has, so callers never have to special-case the keys.
        """
        state = cls()
        state.tasks = {"cycle": None, "cooldown_until": 0, "solver_notes": {}}
        if not isinstance(dump, dict):
            return state
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
        return state

    def note_round(self, round_no: int) -> None:
        """Per-round housekeeping: detect a new match and roll the game day over.

        A match key cannot distinguish two matches played with the same team id and
        map size, which is exactly what a benchmark loop does. The round counter
        can: it only ever moves forward inside a match (任务书 §4.2), so a round
        number that goes backwards means the previous match ended and this memory
        belongs to a match that no longer exists. Without this reset, the last
        task's 30-round cooldown leaks into the next run and silently blocks every
        task acceptance there.
        """
        round_no = int(round_no)
        if self.last_round and round_no < self.last_round:
            self.tasks = {"cycle": None, "cooldown_until": 0, "solver_notes": {}}
            self.judge = JudgeState()
        self.last_round = round_no
        self.judge.note_round(round_no)

    def note_submission(self, prompt: str | None, command: str | None,
                        round_no: int, in_task: bool) -> None:
        if prompt:
            self.judge.consume_llm(in_task)
            self.judge.pending_prompt = PendingRequest("prompt", round_no, prompt)
            self.judge.last_prompt = prompt
            self.prompts_sent += 1
        if command:
            self.judge.pending_cmd = PendingRequest("cmd", round_no, command)
            self.judge.last_command = command
            self.commands_sent += 1

    def note_results(self, payload: dict[str, Any], round_no: int) -> dict[str, Any]:
        """Fold the judge's previous answers into the state. Returns a summary."""
        summary: dict[str, Any] = {}
        from .sandbox import parse_command_result

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
MAX_TRACKED_MATCHES = 8


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
            state = PlannerState()
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
