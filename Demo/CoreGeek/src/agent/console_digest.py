"""Bounded, thread-safe console digest for competition turns.

Engineering diagnostics only (DEV rules R01/R08): this module never changes the
judge response, the protocol, the strategy or the action budget. It consumes the
structured per-turn summaries produced by :mod:`agent.diagnostics` and prints a
small number of actionable lines instead of one JSON blob per round. The JSONL
trace stays independent and still records every captured turn.

Modes (environment ``COMPETITION_HW_CONSOLE``):

* ``compact`` (default) — identity/start, phase changes, first anomalies,
  base-damage thresholds, supervisor transitions and periodic rollups;
* ``full`` — the historical one-line JSON ``response`` summary per turn;
* ``off`` — no console summary lines at all (trace unaffected).

The digest is observation-only: a globally visible robot is not a threat to us,
a disappearing robot is not a death, an issued attack is not proven damage, and
missing/invalid data stays unknown instead of becoming zero. Streams are kept
separate by available run/team/base identity and are reset on a round restart;
at most :data:`MAX_STREAMS` windows are retained, with no whole histories.
"""
from __future__ import annotations

import atexit
from collections import Counter, OrderedDict
import hashlib
import json
import logging
import os
import threading
from typing import Any

LOGGER = logging.getLogger("agent.console")

MODE_ENV = "COMPETITION_HW_CONSOLE"
MODES = ("compact", "full", "off")
DEFAULT_MODE = "compact"
ROLLUP_ROUNDS = 20
MAX_STREAMS = 8
BASE_THRESHOLDS = (1000, 750, 500, 250, 100)
MAX_ACTION_TOKENS = 6
MAX_REASON_TOKENS = 4
MAX_CODE_TOKENS = 4

_state_lock = threading.Lock()
_singleton: "ConsoleDigest | None" = None


# ---------------------------------------------------------------------------
# small, dependency-free readers
# ---------------------------------------------------------------------------

def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _num(value: Any) -> float | None:
    """Finite float or None (bools rejected); shared by plan/cooldown readers."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _text(value: Any, limit: int = 24) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value if len(value) <= limit else value[:limit]


def _range(lo: Any, hi: Any) -> str | None:
    low, high = _num(lo), _num(hi)
    if low is None and high is None:
        return None
    if low is None:
        return f"{int(high)}"
    if high is None:
        return f"{int(low)}"
    low_i, high_i = int(low), int(high)
    return f"{low_i}..{high_i}" if low_i != high_i else str(low_i)


def _counter_text(counter: Counter, limit: int) -> str | None:
    if not counter:
        return None
    items = sorted(counter.items(), key=lambda item: (-item[1], str(item[0])))[:limit]
    return ",".join(f"{key}:{count}" for key, count in items)


def _tokens(pairs: list[tuple[str, Any]]) -> str:
    parts: list[str] = []
    for key, value in pairs:
        if value is None or value == "":
            continue
        if key == "digest":
            parts.append("digest")        # leading marker for every console line
            parts.append(str(value))
            continue
        parts.append(f"{key}={value}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# mode + stream identity
# ---------------------------------------------------------------------------

def console_mode(env: Any = None) -> str:
    """Normalize ``COMPETITION_HW_CONSOLE``; unknown values fall back to compact."""
    raw = env if env is not None else os.environ.get(MODE_ENV, "")
    value = str(raw).strip().lower() if raw is not None else ""
    return value if value in MODES else DEFAULT_MODE


def stream_token(request: Any = None, event_id: Any = None) -> str:
    """Bounded run + team/base identity key; never a judge run id claim.

    The team/base half reuses :func:`agent.telemetry.stream_key` so console and
    trace agree. A missing half stays ``unknown`` instead of being invented.
    """
    run = None
    if isinstance(event_id, str) and event_id:
        run = event_id.split(":", 1)[0][:16] or None
    base = None
    if request is not None:
        try:
            from .telemetry import stream_key
            base = stream_key(request)
        except Exception:  # noqa: BLE001 - identity must never break logging
            base = None
    if run is None and base is None:
        return "unknown"
    return f"{run or 'unknown'}:{base or 'unknown'}"


def _stream_of(summary: dict[str, Any], stream: Any = None) -> str:
    if isinstance(stream, str) and stream:
        return stream
    run = None
    event = summary.get("event")
    if isinstance(event, str) and event:
        run = event.split(":", 1)[0][:16] or None
    base = summary.get("stream") or summary.get("stream_key")
    base = base if isinstance(base, str) and base else None
    if run is None and base is None:
        return "unknown"
    return f"{run or 'unknown'}:{base or 'unknown'}"


def _label(stream: str) -> str:
    run, _, base = stream.partition(":")
    run = "?" if run in ("", "unknown") else run[:6]
    base = "?" if base in ("", "unknown") else base[:6]
    return f"{run}/{base}"


def _fingerprint(summary: dict[str, Any]) -> str | None:
    """Deterministic bounded fingerprint of one summary (dedup safety net)."""
    try:
        payload = json.dumps(summary, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# per-stream window state
# ---------------------------------------------------------------------------

def _new_window() -> dict[str, Any]:
    return {
        "seq": 0,
        "window_turns": 0,
        "first_round": None,
        "last_round": None,
        "last_event": None,
        "last_fingerprint": None,
        "phase": None,
        "events": set(),
        "thresholds": set(),
        "since_rollup": 0,
        "rollup_start": None,
        "actions": Counter(),
        "empty": Counter(),
        "errors": 0,
        "error_codes": Counter(),
        "false_results": 0,
        "previous_hp": None,
        "gold": None,
        "score": None,
        "weapon_levels": None,
        "controllers": Counter(),
        "hp_start": None,
        "hp_last": None,
        "hp_min": None,
        "hp_max": None,
        "damage": 0,
        "walls_min": None,
        "walls_max": None,
        "robots_min": None,
        "robots_max": None,
        "ready_min": None,
        "ready_max": None,
        "cooling": 0,
        "cmds": 0,
        "plan_max": None,
        "supervisor": None,
        "acceptance": None,
    }


def _note_bounds(window: dict[str, Any], key: str, value: Any) -> None:
    number = _as_int(value)
    if number is None:
        return
    low_key, high_key = key + "_min", key + "_max"
    window[low_key] = number if window[low_key] is None else min(window[low_key], number)
    window[high_key] = number if window[high_key] is None else max(window[high_key], number)


class ConsoleDigest:
    """Thread-safe, bounded aggregator. ``observe`` is pure: it returns lines."""

    def __init__(self, *, rollup_rounds: int = ROLLUP_ROUNDS,
                 max_streams: int = MAX_STREAMS) -> None:
        self.rollup_rounds = max(1, int(rollup_rounds))
        self.max_streams = max(1, int(max_streams))
        self._lock = threading.Lock()
        self._windows: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

    # -- public API ---------------------------------------------------------

    def observe(self, summary: Any, stream: Any = None) -> list[str]:
        if not isinstance(summary, dict):
            return []
        key = _stream_of(summary, stream)
        with self._lock:
            window = self._windows.get(key)
            lines: list[str] = []
            if window is not None and self._is_duplicate(summary, window):
                window["duplicates"] = window.get("duplicates", 0) + 1
                self._touch(key)
                return []
            restart = window is not None and self._is_restart(summary, window)
            if window is None or restart:
                if restart and window["window_turns"]:
                    # Close the old segment so its pending totals are not lost.
                    lines.append(self._rollup(key, window, final=True))
                previous = window["last_round"] if restart else None
                self._windows[key] = window = _new_window()
                self._touch(key)
                if restart:
                    lines.append(_tokens([("digest", "restart"), ("s", _label(key)),
                                          ("r", _as_int(summary.get("round"))),
                                          ("after", previous)]))
                else:
                    lines.append(self._start_line(key, summary))
            lines.extend(self._absorb(key, window, summary))
            lines.extend(self._evict())
            self._touch(key)
        return lines

    def flush(self, stream: Any = None) -> list[str]:
        """Emit the final partial window(s). A killed process may not reach here."""
        with self._lock:
            if isinstance(stream, str) and stream:
                keys = [stream] if stream in self._windows else []
            else:
                keys = list(self._windows)
            lines = []
            for key in keys:
                window = self._windows.pop(key, None)
                if window and window["window_turns"]:
                    lines.append(self._rollup(key, window, final=True))
        return lines

    def reset(self, stream: Any = None) -> None:
        with self._lock:
            if isinstance(stream, str) and stream:
                self._windows.pop(stream, None)
            else:
                self._windows.clear()

    def stream_count(self) -> int:
        with self._lock:
            return len(self._windows)

    # -- internals ----------------------------------------------------------

    def _touch(self, key: str) -> None:
        self._windows.move_to_end(key)

    def _evict(self) -> list[str]:
        lines: list[str] = []
        while len(self._windows) > self.max_streams:
            key, window = self._windows.popitem(last=False)
            if window["window_turns"]:
                lines.append(self._rollup(key, window, final=True))
        return lines

    def _is_duplicate(self, summary: dict[str, Any], window: dict[str, Any]) -> bool:
        """A repeated round number is not enough: content must repeat exactly.

        Dedup requires either the same non-empty event id with an identical
        bounded fingerprint, or (when no event id is present) the exact same
        summary content. A changed payload in the same round is a real request
        and keeps its counts and first-anomaly evidence.
        """
        fingerprint = _fingerprint(summary)
        if fingerprint is None:
            return False
        event = summary.get("event")
        has_event = isinstance(event, str) and bool(event)
        last_event = window.get("last_event")
        if has_event:
            return (isinstance(last_event, str) and bool(last_event)
                    and event == last_event
                    and fingerprint == window.get("last_fingerprint"))
        if isinstance(last_event, str) and last_event:
            return False
        return fingerprint == window.get("last_fingerprint")

    def _is_restart(self, summary: dict[str, Any], window: dict[str, Any]) -> bool:
        round_no, last = _as_int(summary.get("round")), window.get("last_round")
        return round_no is not None and last is not None and round_no < last

    def _start_line(self, key: str, summary: dict[str, Any]) -> str:
        return _tokens([
            ("digest", f"s={_label(key)}"),
            ("r", _as_int(summary.get("round"))),
            ("start", "1"),
            ("phase", _text(summary.get("phase"))),
            ("base", _as_int(summary.get("base_hp"))),
            ("lvl", _as_int(summary.get("base_level"))),
            ("team", _as_int(summary.get("team_roles_live"))),
            ("workers", _as_int(summary.get("workers_live"))),
            ("pioneers", _as_int(summary.get("pioneers_live"))),
            ("weapons", _head(summary.get("weapon_counts")) or _as_int(summary.get("weapons_live"))),
            ("ready", _as_int(summary.get("weapons_ready"))),
            ("walls", _as_int(summary.get("walls_live"))),
            ("robots", _as_int(summary.get("robots_visible"))),
            ("target_us", _as_int(summary.get("robots_targeting_us"))),
            ("gold", _as_int(summary.get("gold"))),
            ("score", _as_int(summary.get("score"))),
        ])

    def _absorb(self, key: str, window: dict[str, Any], summary: dict[str, Any]) -> list[str]:
        lines: list[str] = []
        round_no = _as_int(summary.get("round"))
        if window["first_round"] is None:
            window["first_round"] = round_no
        if window["window_turns"] == 0:
            window["rollup_start"] = round_no
        window["last_round"] = round_no
        window["seq"] += 1
        window["window_turns"] += 1
        window["since_rollup"] += 1
        event = summary.get("event")
        window["last_event"] = event if isinstance(event, str) and event else None
        window["last_fingerprint"] = _fingerprint(summary)

        actions = summary.get("action_counts")
        if isinstance(actions, dict):
            for name, count in actions.items():
                number = _as_int(count)
                if number:
                    window["actions"][str(name)] += number
        commands = _as_int(summary.get("commands"))
        if commands is not None:
            window["cmds"] += commands
        false_results = _as_int(summary.get("action_results_false"))
        if false_results:
            window["false_results"] += false_results
        for name in ('gold', 'score'):
            window[name] = _as_int(summary.get(name))
        levels = summary.get('weapon_levels')
        window['weapon_levels'] = _counter_text(Counter(levels), 6) if isinstance(levels, dict) else None
        assignments = (summary.get('controllers') or {}).get('issued_attacks') or []
        for assignment in assignments:
            uid = _as_int(assignment.get('controller')) if isinstance(assignment, dict) else None
            if uid is not None:
                window['controllers'][str(uid)] += 1
        self._absorb_errors(window, summary)
        self._absorb_base(window, summary)
        _note_bounds(window, "walls", summary.get("walls_live"))
        _note_bounds(window, "robots", summary.get("robots_visible"))
        _note_bounds(window, "ready", summary.get("weapons_ready"))
        plan_ms = _num(summary.get("plan_ms"))
        if plan_ms is not None:
            window["plan_max"] = plan_ms if window["plan_max"] is None else max(window["plan_max"], plan_ms)

        if commands == 0:
            reason = _text(summary.get("empty_reason"), 40) or "unknown"
            window["empty"][reason] += 1
            if "empty" not in window["events"]:
                window["events"].add("empty")
                lines.append(self._event_line(key, "first_empty", round_no, [
                    ("reason", reason),
                    ("base", _as_int(summary.get("base_hp"))),
                    ("ready", _as_int(summary.get("weapons_ready"))),
                    ("robots", _as_int(summary.get("robots_visible"))),
                ]))
        if _num(summary.get("weapons_ready")) == 0 and (_as_int(summary.get("weapons_live")) or 0) > 0:
            window["cooling"] += 1

        phase = _text(summary.get("phase"))
        if phase is not None:
            if window["phase"] is None:
                window["phase"] = phase
            elif phase != window["phase"]:
                window["phase"] = phase
                lines.append(_tokens([("digest", "phase"), ("s", _label(key)), ("r", round_no),
                                      ("now", phase),
                                      ("base", _as_int(summary.get("base_hp"))),
                                      ("robots", _as_int(summary.get("robots_visible")))]))
        lines.extend(self._absorb_decision(key, window, summary))
        lines.extend(self._absorb_anomalies(key, window, summary, round_no))
        if window["since_rollup"] >= self.rollup_rounds:
            lines.append(self._rollup(key, window, final=False))
            self._reset_counters(window)
        return lines

    def _reset_counters(self, window: dict[str, Any]) -> None:
        """Start a fresh aggregation window; phase/anomaly memory is retained."""
        for key, value in (("window_turns", 0), ("since_rollup", 0), ("errors", 0),
                           ("false_results", 0), ("damage", 0), ("cooling", 0), ("cmds", 0)):
            window[key] = value
        for key in ("actions", "empty", "error_codes", "controllers"):
            window[key] = Counter()
        for key in ("hp_start", "hp_last", "hp_min", "hp_max", "walls_min", "walls_max",
                    "robots_min", "robots_max", "ready_min", "ready_max", "plan_max"):
            window[key] = None

    def _absorb_errors(self, window: dict[str, Any], summary: dict[str, Any]) -> None:
        total = _as_int(summary.get("judge_errors_total"))
        if total is None:
            # Legacy summaries (pre-categorization) only carried this count.
            legacy = _as_int(summary.get("protocol_errors"))
            if legacy:
                window["errors"] += legacy
            return
        if total:
            window["errors"] += total
        codes = summary.get("errors_by_code")
        if isinstance(codes, dict):
            for code, count in codes.items():
                number = _as_int(count)
                if number:
                    window["error_codes"][str(_as_int(code) if _as_int(code) is not None else code)] += number

    def _absorb_base(self, window: dict[str, Any], summary: dict[str, Any]) -> None:
        health = _as_int(summary.get("base_hp"))
        if health is None:
            return
        if window["hp_start"] is None:
            window["hp_start"] = health
        if window["previous_hp"] is not None and health < window["previous_hp"]:
            window["damage"] += window["previous_hp"] - health
        window["previous_hp"] = health
        window["hp_last"] = health
        window["hp_min"] = health if window["hp_min"] is None else min(window["hp_min"], health)
        window["hp_max"] = health if window["hp_max"] is None else max(window["hp_max"], health)

    def _absorb_anomalies(self, key: str, window: dict[str, Any], summary: dict[str, Any],
                          round_no: Any) -> list[str]:
        lines: list[str] = []
        errors = _as_int(summary.get("judge_errors_total"))
        if errors is None:
            errors = _as_int(summary.get("protocol_errors")) or 0
        if errors and "errors" not in window["events"]:
            window["events"].add("errors")
            lines.append(self._event_line(key, "first_errors", round_no, [
                ("total", errors),
                ("command", _as_int(summary.get("command_errors"))),
                ("task", _as_int(summary.get("task_errors"))),
                ("llm", _as_int(summary.get("llm_quota_errors"))),
                ("network", _as_int(summary.get("network_errors"))),
            ]))
        false_results = _as_int(summary.get("action_results_false"))
        if false_results and "action_false" not in window["events"]:
            window["events"].add("action_false")
            lines.append(self._event_line(key, "first_action_false", round_no, [("n", false_results)]))
        robots = _as_int(summary.get("robots_visible"))
        if robots and "robots" not in window["events"]:
            window["events"].add("robots")
            lines.append(self._event_line(key, "first_robots", round_no, [
                ("visible", robots),
                ("target_us", _as_int(summary.get("robots_targeting_us"))),
            ]))
        if summary.get("empty_reason") == "decision_exception" or summary.get("decision_error"):
            if "decision_exception" not in window["events"]:
                window["events"].add("decision_exception")
                lines.append(self._event_line(key, "first_decision_exception", round_no, []))
        if summary.get("empty_reason") == "invalid_input" and "invalid_input" not in window["events"]:
            window["events"].add("invalid_input")
            lines.append(self._event_line(key, "first_invalid_input", round_no, []))
        if window["damage"] and "base_damage" not in window["events"]:
            window["events"].add("base_damage")
            lines.append(self._event_line(key, "first_base_damage", round_no, [
                ("hp", _as_int(summary.get("base_hp"))),
                ("observed_damage", window["damage"]),
            ]))
        state = summary.get("base_health_state")
        if state == "observed_zero" and "base_dead" not in window["events"]:
            window["events"].add("base_dead")
            lines.append(_tokens([("digest", "critical"), ("s", _label(key)), ("r", round_no),
                                  ("base_observed_zero", "1")]))
        if summary.get("empty_reason") == "no_live_controllers" and "no_controllers" not in window["events"]:
            window["events"].add("no_controllers")
            lines.append(_tokens([("digest", "critical"), ("s", _label(key)), ("r", round_no),
                                  ("no_live_controllers", "1")]))
        health = _as_int(summary.get("base_hp"))
        if health is not None:
            crossed = [t for t in BASE_THRESHOLDS if health <= t and t not in window["thresholds"]]
            if crossed:
                threshold = min(crossed)
                window["thresholds"].update(crossed)
                lines.append(_tokens([("digest", "base_low"), ("s", _label(key)), ("r", round_no),
                                      ("hp", health), ("threshold", threshold)]))
        return lines

    def _absorb_decision(self, key: str, window: dict[str, Any],
                         summary: dict[str, Any]) -> list[str]:
        report = summary.get("decision")
        if not isinstance(report, dict):
            return []
        lines: list[str] = []
        supervisor = report.get("supervisor")
        if isinstance(supervisor, dict):
            current = (_text(supervisor.get("mode")), _text(supervisor.get("reason")),
                       supervisor.get("reserve_pioneer"),
                       _as_int(supervisor.get("return_steps_lower_bound")),
                       _as_int(supervisor.get("relevant_robots")),
                       _as_int(supervisor.get("ready_workers")),
                       _as_int(supervisor.get("visible_pressure_hp")))
            if any(value is not None for value in current) and current[:3] != window["supervisor"]:
                window["supervisor"] = current[:3]
                lines.append(_tokens([("digest", "supervisor"), ("s", _label(key)),
                                      ("r", _as_int(summary.get("round"))),
                                      ("mode", current[0]), ("reason", current[1]),
                                      ("reserve_pioneer", current[2]), ("advice", supervisor.get("recommended_reserve")),
                                      ("return_steps", current[3]),
                                      ("relevant_robots", current[4]),
                                      ("ready_workers", current[5]),
                                      ("pressure_hp", current[6])]))
        task = report.get("task")
        if isinstance(task, dict):
            status = task.get("acceptance_status") if isinstance(task.get("acceptance_status"), dict) else {}
            current = (
                _text(task.get("phase"), 40),
                task.get("cycle_active") if isinstance(task.get("cycle_active"), bool) else None,
                _text(task.get("plan_kind"), 40),
                task.get("pending_command") if isinstance(task.get("pending_command"), bool) else _text(task.get("pending_command"), 40),
                task.get("pending_prompt") if isinstance(task.get("pending_prompt"), bool)
                else _text(task.get("pending_prompt"), 40),
                _text(status.get("phase"), 40),
                _text(status.get("reason"), 40),
                _as_int(status.get("retry_after")),
            )
            if any(value is not None for value in current) and current != window["acceptance"]:
                window["acceptance"] = current
                lines.append(_tokens([("digest", "task"), ("s", _label(key)),
                                      ("r", _as_int(summary.get("round"))),
                                      ("phase", current[0]), ("cycle_active", current[1]),
                                      ("plan", current[2]), ("pending_cmd", current[3]),
                                      ("pending_prompt", current[4]),
                                      ("accept", current[5]), ("accept_reason", current[6]),
                                      ("retry_after", current[7])]))
        return lines

    def _event_line(self, key: str, name: str, round_no: Any,
                    extra: list[tuple[str, Any]]) -> str:
        return _tokens([("digest", "anomaly"), ("s", _label(key)), ("what", name),
                        ("r", round_no), *extra])

    def _rollup(self, key: str, window: dict[str, Any], *, final: bool) -> str:
        start = _as_int(window.get("rollup_start"))
        end = _as_int(window.get("last_round"))
        span = None
        if start is not None and end is not None:
            span = f"{start}..{end}" if start != end else str(end)
        pairs: list[tuple[str, Any]] = [("digest", "rollup"), ("s", _label(key)), ("r", span),
                                        ("turns", window["window_turns"])]
        if final:
            pairs.append(("final", "1"))
        pairs += [
            ("phase", window["phase"]),
            ("gold", window["gold"]), ("score", window["score"]),
            ("weapons", window["weapon_levels"]),
            ("controllers", _counter_text(window["controllers"], 3)),
            ("hp_min", window["hp_min"]),
            ("hp_last", window["hp_last"]),
            ("dmg", window["damage"] or None),
            ("walls", _range(window["walls_min"], window["walls_max"])),
            ("robots", _range(window["robots_min"], window["robots_max"])),
            ("ready", _range(window["ready_min"], window["ready_max"])),
            ("cooling", window["cooling"] or None),
            ("cmds", window["cmds"] or None),
            ("actions", _counter_text(window["actions"], MAX_ACTION_TOKENS)),
            ("accept", window["actions"].get("acceptTask") or None),
            ("submit", window["actions"].get("submitAnswer") or None),
            ("empty", _counter_text(window["empty"], MAX_REASON_TOKENS)),
            ("errors", window["errors"] or None),
            ("codes", _counter_text(window["error_codes"], MAX_CODE_TOKENS)),
            ("false", window["false_results"] or None),
            ("plan_max", None if window["plan_max"] is None else round(window["plan_max"], 1)),
        ]
        return _tokens(pairs)


def _head(counts: Any) -> str | None:
    """Compact ``g/r/rk`` live weapon counts, or None when unknown."""
    if not isinstance(counts, dict):
        return None
    parts = []
    for kind in ("gatling", "railgun", "rocket"):
        number = _as_int(counts.get(kind))
        if number is not None:
            parts.append(f"{kind}:{number}")
    return "/".join(parts) if parts else None


# ---------------------------------------------------------------------------
# module-level entry points
# ---------------------------------------------------------------------------

def _digest() -> ConsoleDigest:
    global _singleton
    if _singleton is None:
        with _state_lock:
            if _singleton is None:
                _singleton = ConsoleDigest()
                atexit.register(_flush_at_exit)
    return _singleton


def _default_emit(line: str) -> None:
    LOGGER.info("%s", line)


def _safe_send(send, line: str) -> None:
    try:
        send(line)
    except Exception:  # noqa: BLE001 - a broken sink must never break a turn
        return


def _flush_at_exit() -> None:
    try:
        if _singleton is not None:
            for line in _singleton.flush():
                _safe_send(_default_emit, line)
    except Exception:  # noqa: BLE001
        return


def flush(emitter=None) -> list[str]:
    """Flush partial console windows (normal exit only; kill may lose them)."""
    send = emitter if callable(emitter) else _default_emit
    try:
        lines = _digest().flush()
    except Exception:  # noqa: BLE001
        return []
    for line in lines:
        _safe_send(send, line)
    return lines


def emit_summary(summary: Any, emitter=None, stream: Any = None) -> list[str]:
    """Emit one summary through the configured console mode. Never raises.

    ``emitter`` is the injectable sink (diagnostics passes its own); ``full``
    restores the historical ``response <json>`` line, ``off`` silences only this
    console stream, ``compact`` (default) uses the bounded digest.
    """
    send = emitter if callable(emitter) else _default_emit
    try:
        mode = console_mode()
    except Exception:  # noqa: BLE001
        mode = DEFAULT_MODE
    if mode == "off":
        return []
    if mode == "full":
        try:
            line = "response " + json.dumps(summary, ensure_ascii=False, sort_keys=True,
                                            separators=(",", ":"))
        except Exception:  # noqa: BLE001
            return []
        _safe_send(send, line)
        return [line]
    if not isinstance(summary, dict):
        return []
    try:
        lines = _digest().observe(summary, stream=stream)
    except Exception:  # noqa: BLE001
        return []
    for line in lines:
        _safe_send(send, line)
    return lines


def reset(stream: Any = None) -> None:
    try:
        _digest().reset(stream)
    except Exception:  # noqa: BLE001
        return
