"""Bounded, serialisable task context for the shared cognitive channel (P0b).

Engineering/internal architecture only (R01/R02/R07). This module never touches
the judge response and never observes private simulator state: it assembles a
small, public-observation view of the current work so a prompt does not depend on
the platform remembering chat history.

Hard rules:

* everything here is **local memory**; no official response field is produced and
  no extra field is requested from the judge;
* public observations only — no ``_demo``, seed, future news, hidden answers or
  local judge state;
* every saved field is bounded in type, nesting, entry count **and** text length;
  load is strict and rejects malformed/oversized/newer data instead of silently
  dropping entries or trusting truthy strings;
* truncation is recorded explicitly. When no verified trace reference exists the
  context says the original is unavailable — it never implies an archived file;
* no hidden reasoning chain is requested or stored;
* malformed or unknown-schema input degrades **explicitly** and conservatively —
  context is dropped, not invented, and quota is never reset by a context load.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import json
import math
from typing import Any

SCHEMA = "competition-hw-task-context/1"
# Units are Unicode characters, not bytes; all caps are engineering limits.
TEXT_LIMIT = 4000          # raw task/source text stored per context
ITEM_TEXT_LIMIT = 400
ITEM_KEYS_LIMIT = 16
ITEM_LIST_LIMIT = 16
ITEM_DEPTH_LIMIT = 2
FACT_LIMIT = 24
TOOL_LIMIT = 8
FAILURE_LIMIT = 12
QUESTION_LIMIT = 8
QUESTION_TEXT_LIMIT = 200
TRACE_LIMIT = 8
TRACE_TEXT_LIMIT = 120
TOTAL_LIMIT = 16000        # stored context JSON cap
CONTEXT_LIMIT = 8000       # rendered context body inside a prompt
PROMPT_LIMIT = 12000       # full prompt budget (instruction + context + nonce room)
COMMAND_LIMIT = 2000       # raw executeCmd cap; oversized commands are rejected
INSTRUCTION_ROOM = 400     # header/instruction reserve inside PROMPT_LIMIT
NONCE_ROOM = 160           # request_id/nonce reserve inside PROMPT_LIMIT
# Team-wide bounded context store (old generations are evicted, never kept whole).
STORE_LIMIT = 6

_HEX64 = set("0123456789abcdef")
_FACT_KINDS = ("observed", "inferred", "assumed", "uncertain", "unclassified")


def sha256_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _clip(text: Any, limit: int) -> tuple[str, bool]:
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _is_hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX64


def task_generation(task_text: Any, accepted_round: Any) -> str | None:
    """Stable per-acceptance generation id: text digest + accept round.

    A different task text, or the same text accepted again in a later cycle, is a
    new generation, so an old answer can never be handed to a new task.
    """
    text = str(task_text or "")
    if not text:
        return None
    try:
        accepted = int(accepted_round or 0)
    except (TypeError, ValueError, OverflowError):
        accepted = 0
    return sha256_text(f"{accepted}|{text}")[:16]


def context_digest(*parts: Any) -> str:
    return sha256_text("|".join("" if part is None else str(part) for part in parts))[:16]


# ---------------------------------------------------------------------------
# bounded value normalisation (build + strict load share it)
# ---------------------------------------------------------------------------

def _bound_value(value: Any, depth: int = 0) -> tuple[Any, bool]:
    """Return ``(bounded_value, changed)``; never stringifies arbitrary objects."""
    if isinstance(value, str):
        clipped, truncated = _clip(value, ITEM_TEXT_LIMIT)
        return clipped, truncated
    if isinstance(value, bool) or value is None:
        return value, False
    if isinstance(value, int):
        return value, False
    if isinstance(value, float):
        return (value, False) if math.isfinite(value) else (None, True)
    if depth >= ITEM_DEPTH_LIMIT:
        return None, True
    if isinstance(value, (list, tuple)):
        items = []
        changed = len(value) > ITEM_LIST_LIMIT
        for item in list(value)[:ITEM_LIST_LIMIT]:
            bounded, item_changed = _bound_value(item, depth + 1)
            if bounded is None and item_changed:
                changed = True
                continue
            items.append(bounded)
        return items, changed
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        changed = len(value) > ITEM_KEYS_LIMIT
        for key in list(value)[:ITEM_KEYS_LIMIT]:
            if not isinstance(key, str):
                changed = True
                continue
            bounded, item_changed = _bound_value(value[key], depth + 1)
            if bounded is None and item_changed:
                changed = True
                continue
            result[key] = bounded
            changed = changed or item_changed
        return result, changed
    return None, True


def _normalize_item(item: Any, *, strict: bool = False) -> dict[str, Any] | None:
    """Bound one dict item. In strict mode any clipping/drop rejects the item."""
    if not isinstance(item, dict):
        return None
    bounded, changed = _bound_value(item)
    if not isinstance(bounded, dict) or not bounded:
        return None
    if strict and changed:
        return None
    return bounded


# ---------------------------------------------------------------------------
# public task validity
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TaskConfirmation:
    """Whether a task may be treated as confirmed, using public evidence only."""

    confirmed: bool
    generation: str | None
    source_digest: str | None
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def dump(self) -> dict[str, Any]:
        return {"confirmed": self.confirmed, "generation": self.generation,
                "source_digest": self.source_digest, "reason": self.reason,
                "evidence": dict(self.evidence)}


def _role_xy(role: dict[str, Any]) -> tuple[int, int] | None:
    """Real published coordinates only; a missing axis is not defaulted to zero."""
    pos = role.get("pos")
    if not isinstance(pos, dict):
        return None
    if "x" not in pos or "y" not in pos:
        return None
    try:
        return int(pos["x"]), int(pos["y"])
    except (TypeError, ValueError, OverflowError):
        return None


def _public_own_cells(payload: dict[str, Any], anchor: tuple[int, int]) -> tuple[tuple[Any, ...], str]:
    """Cells of an *own* task region, or ``()`` with a reason.

    Evidence must be a public own-team task zone (``<team>TaskPoint*``) or a
    clearly attributable entry in our own ``playerTasks`` list. An enemy zone, an
    absent ``playerTasks`` region, or a bare coordinate from a local cycle object
    is not evidence — a local cycle plus an arbitrary point cannot earn a task
    exemption.
    """
    from .protocol import Pos
    from .task_lifecycle import region_cells
    team = str((payload.get("teamOur") or {}).get("type") or "")
    zones = ((payload.get("mapInfo") or {}).get("zones")) or []
    own = []
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        kind = str(zone.get("neutralType") or "")
        if team and kind.startswith(team) and "TaskPoint" in kind:
            own.append(zone)
    for zone in own:
        try:
            if Pos.load(zone.get("pos")) == Pos(anchor[0], anchor[1]):
                return tuple(region_cells(zone["pos"], zones)), "own_zone"
        except (KeyError, TypeError, ValueError):
            continue
    published = (payload.get("teamOur") or {}).get("playerTasks")
    if isinstance(published, list):
        for entry in published:
            if not isinstance(entry, dict):
                continue
            position = entry.get("taskPosition")
            if not isinstance(position, dict):
                continue
            try:
                if (int(position.get("x")), int(position.get("y"))) == anchor:
                    return (Pos(anchor[0], anchor[1]),), "own_player_tasks"
            except (TypeError, ValueError, OverflowError):
                continue
    if own:
        return (), "anchor_not_own_region"
    return (), "no_public_own_region"


def public_task_confirmed(payload: Any, cycle: Any) -> TaskConfirmation:
    """Confirm a running task from public evidence only.

    A locally constructed cycle object is **not** enough: without a published
    ``phaseTask`` text there is no confirmation and therefore no task exemption.
    ``isValid=false`` on a point only means that point cannot be re-accepted; it
    never ends a task that is otherwise still published and in range. An unknown
    timeout is not treated as ``0`` (never expiring) nor assumed valid forever:
    confirmation then rests on the live published text plus the in-range pioneer.
    """
    text = str(payload.get("phaseTask") or "") if isinstance(payload, dict) else ""
    if not isinstance(payload, dict) or not text.strip():
        return TaskConfirmation(False, None, None, "no_published_task_text",
                                {"phaseTask": bool(text)})
    if cycle is None:
        return TaskConfirmation(False, task_generation(text, 0), None,
                                "no_cycle", {"phaseTask": True})
    if getattr(cycle, "phase", "") == "ended" or getattr(cycle, "ended_round", 0):
        return TaskConfirmation(False, None, None, "task_ended", {"phaseTask": True})
    generation = task_generation(text, getattr(cycle, "accepted_round", 0))
    digest = context_digest("task", generation, text)
    round_no = payload.get("roundNo")
    try:
        round_no = int(round_no) if round_no is not None else None
    except (TypeError, ValueError, OverflowError):
        round_no = None
    timeout = getattr(cycle, "timeout_rounds", 0) or 0
    try:
        timeout = int(timeout)
    except (TypeError, ValueError, OverflowError):
        timeout = 0
    evidence: dict[str, Any] = {"phaseTask": True, "timeout_rounds": timeout,
                                "timeout_known": timeout > 0}
    if round_no is not None:
        evidence["round"] = round_no
    if timeout > 0 and round_no is not None:
        accepted = int(getattr(cycle, "accepted_round", 0) or 0)
        if round_no >= accepted + timeout:
            return TaskConfirmation(False, generation, digest, "task_timeout", evidence)
    anchor = None
    point = getattr(cycle, "point", None)
    if isinstance(point, dict) and "x" in point and "y" in point:
        try:
            anchor = (int(point["x"]), int(point["y"]))
        except (TypeError, ValueError, OverflowError):
            anchor = None
    if anchor is None:
        return TaskConfirmation(False, generation, digest, "no_public_point", evidence)
    cells, region_reason = _public_own_cells(payload, anchor)
    evidence["region"] = region_reason
    if not cells:
        return TaskConfirmation(False, generation, digest, region_reason, evidence)
    team = (payload.get("teamOur") or {})
    for role in team.get("roles") or ():
        if not isinstance(role, dict) or role.get("roleType") != "pioneer":
            continue
        health = role.get("health")
        if isinstance(health, bool) or health is None:
            continue
        try:
            if not (float(health) > 0 and math.isfinite(float(health))):
                continue
        except (TypeError, ValueError):
            continue
        xy = _role_xy(role)
        if xy is None:
            continue
        from .protocol import Pos, distance
        from .tasks import HOLD_RANGE
        here = Pos(xy[0], xy[1])
        if min(distance(here, cell) for cell in cells) <= HOLD_RANGE:
            evidence["pioneer"] = {"role_id": role.get("id"), "in_range": True}
            return TaskConfirmation(True, generation, digest, "confirmed", evidence)
    evidence["pioneer"] = {"in_range": False}
    return TaskConfirmation(False, generation, digest, "pioneer_not_on_task", evidence)


# ---------------------------------------------------------------------------
# bounded context envelope
# ---------------------------------------------------------------------------

def _normalize_items(items: Any, limit: int, *, strict: bool = False
                     ) -> tuple[list[dict[str, Any]], int, int]:
    kept: list[dict[str, Any]] = []
    dropped = 0
    clipped = 0
    for item in items or ():
        if isinstance(item, dict):
            _bounded, changed = _bound_value(item)
            if changed:
                clipped += 1
        normalized = _normalize_item(item, strict=strict)
        if normalized is None or len(kept) >= limit:
            dropped += 1
            continue
        kept.append(normalized)
    return kept, dropped, clipped


@dataclass
class ContextEnvelope:
    """A bounded, JSON-safe view for one request. No hidden reasoning."""

    owner: str
    generation: str
    source_digest: str
    task_text: str = ""
    task_text_sha256: str = ""
    task_text_truncated: bool = False
    answer_contract: dict[str, Any] = field(default_factory=dict)
    facts: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    trace_refs: list[dict[str, Any]] = field(default_factory=list)
    truncation: dict[str, Any] = field(default_factory=dict)
    schema: str = SCHEMA

    def dump(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "generation": self.generation,
            "source_digest": self.source_digest,
            "task_text": self.task_text,
            "task_text_sha256": self.task_text_sha256,
            "task_text_truncated": self.task_text_truncated,
            "answer_contract": dict(self.answer_contract),
            "facts": [dict(item) for item in self.facts],
            "tool_results": [dict(item) for item in self.tool_results],
            "failures": [dict(item) for item in self.failures],
            "open_questions": list(self.open_questions),
            "trace_refs": [dict(item) for item in self.trace_refs],
            "truncation": dict(self.truncation),
        }

    @classmethod
    def load(cls, raw: Any) -> tuple["ContextEnvelope | None", str]:
        """Strict load. Returns ``(envelope, reason)``; never raises.

        Unknown/newer schemas, oversized or wrongly-typed fields, truthy-string
        flags and malformed entries all reject the whole envelope, so a caller
        degrades to deterministic planning instead of silently starting from an
        empty — or worse, fabricated — context.
        """
        if not isinstance(raw, dict):
            return None, "not_an_object"
        schema = raw.get("schema")
        if schema != SCHEMA:
            return None, "unknown_schema" if isinstance(schema, str) else "missing_schema"
        for key in ("owner", "generation", "source_digest"):
            if not isinstance(raw.get(key), str):
                return None, f"missing_{key}"
        text = raw.get("task_text")
        if not isinstance(text, str) or len(text) > TEXT_LIMIT:
            return None, "task_text_out_of_bounds"
        truncated = raw.get("task_text_truncated")
        if not isinstance(truncated, bool):
            return None, "task_text_truncated_not_bool"
        digest = raw.get("task_text_sha256")
        if not _is_hex64(digest):
            return None, "task_text_sha256_invalid"
        if not truncated and sha256_text(text) != digest:
            return None, "task_text_sha256_mismatch"
        contract = raw.get("answer_contract")
        if not isinstance(contract, dict):
            return None, "answer_contract_not_object"
        bounded_contract, contract_changed = _bound_value(contract)
        if not isinstance(bounded_contract, dict) or contract_changed:
            return None, "answer_contract_out_of_bounds"
        lists: dict[str, list[dict[str, Any]]] = {}
        for key, limit in (("facts", FACT_LIMIT), ("tool_results", TOOL_LIMIT),
                           ("failures", FAILURE_LIMIT)):
            value = raw.get(key)
            if not isinstance(value, list):
                return None, f"{key}_not_list"
            if len(value) > limit:
                return None, f"{key}_too_many"
            normalized = []
            for item in value:
                bounded = _normalize_item(item, strict=True)
                if bounded is None:
                    return None, f"{key}_malformed_entry"
                normalized.append(bounded)
            lists[key] = normalized
        questions = raw.get("open_questions")
        if not isinstance(questions, list) or len(questions) > QUESTION_LIMIT:
            return None, "open_questions_invalid"
        bounded_questions = []
        for question in questions:
            if not isinstance(question, str) or len(question) > QUESTION_TEXT_LIMIT:
                return None, "open_questions_invalid"
            bounded_questions.append(question)
        traces = raw.get("trace_refs")
        if not isinstance(traces, list) or len(traces) > TRACE_LIMIT:
            return None, "trace_refs_invalid"
        bounded_traces = []
        for trace in traces:
            bounded = _normalize_item(trace, strict=True)
            if bounded is None:
                return None, "trace_refs_invalid"
            bounded_traces.append(bounded)
        truncation = raw.get("truncation")
        if not isinstance(truncation, dict):
            return None, "truncation_invalid"
        bounded_truncation, truncation_changed = _bound_value(truncation)
        if not isinstance(bounded_truncation, dict) or truncation_changed:
            return None, "truncation_invalid"
        candidate = cls(
            owner=raw["owner"], generation=raw["generation"],
            source_digest=raw["source_digest"], task_text=text,
            task_text_sha256=digest, task_text_truncated=truncated,
            answer_contract=bounded_contract, facts=lists["facts"],
            tool_results=lists["tool_results"], failures=lists["failures"],
            open_questions=bounded_questions, trace_refs=bounded_traces,
            truncation=bounded_truncation)
        if len(json.dumps(candidate.dump(), ensure_ascii=False)) > TOTAL_LIMIT:
            return None, "context_too_large"
        return candidate, "ok"

    def check(self) -> dict[str, Any]:
        """Integrity report: does the stored text still match its digest?"""
        return {"task_text_sha256_matches": (not self.task_text_truncated
                                             and sha256_text(self.task_text) == self.task_text_sha256),
                "task_text_truncated": self.task_text_truncated,
                "trace_refs": len(self.trace_refs),
                "truncation": dict(self.truncation),
                "facts": len(self.facts), "tool_results": len(self.tool_results)}

    def _has_loss(self) -> bool:
        if self.task_text_truncated:
            return True
        for key, value in self.truncation.items():
            if key.endswith("_dropped") or key == "items_clipped":
                try:
                    if int(value or 0) > 0:
                        return True
                except (TypeError, ValueError):
                    return True
        return False

    def _truncation_footer(self) -> str:
        if self.trace_refs:
            refs = []
            for trace in self.trace_refs[:TRACE_LIMIT]:
                name = trace.get("event_id") or trace.get("run_id") or trace.get("name")
                if name:
                    refs.append(str(name))
            if refs:
                return "内容已截断；已验证trace引用: " + ", ".join(sorted(set(refs)))
        return "内容已截断；本上下文未保存原始全文，也没有已验证的trace引用（原始内容不可用）"

    def prompt_view(self, limit: int = CONTEXT_LIMIT) -> str:
        """Bounded prompt body; states incompleteness instead of implying a file."""
        lines: list[str] = [f"[{self.owner} generation={self.generation} "
                            f"source={self.source_digest}]"]
        title = "任务原文" if self.owner == "task" else "公开来源原文"
        lines.append(f"{title} (sha256={self.task_text_sha256[:16]}"
                     f"{', 已截断' if self.task_text_truncated else ''}):")
        lines.append(self.task_text or "(无原文)")
        if self.answer_contract:
            lines.append("答案格式约束:")
            for key in sorted(self.answer_contract):
                lines.append(f"- {key}: {self.answer_contract[key]}")
        if self.facts:
            lines.append("已观测事实与推断:")
            for fact in self.facts:
                kind = fact.get("kind")
                label = kind if kind in _FACT_KINDS else "unclassified"
                marks = []
                if fact.get("conflict"):
                    marks.append("冲突")
                if fact.get("uncertain"):
                    marks.append("不确定")
                suffix = ("[" + "/".join(marks) + "]") if marks else ""
                lines.append(f"- [{label}]{suffix} {fact.get('text', '')} "
                             f"(round={fact.get('round', '?')}, source={fact.get('source', '?')})")
        if self.tool_results:
            lines.append("工具结果摘要:")
            for item in self.tool_results:
                suffix = " [截断]" if item.get("truncated") else ""
                trace = item.get("trace_ref") or "-"
                lines.append(f"- {item.get('summary', '')}{suffix} "
                             f"(round={item.get('round', '?')}, trace={trace})")
        if self.failures:
            lines.append("失败记录:")
            for item in self.failures:
                lines.append(f"- {item.get('reason', '')} (round={item.get('round', '?')})")
        if self.open_questions:
            lines.append("待确认问题:")
            for question in self.open_questions:
                lines.append(f"- {question}")
        if self._has_loss():
            footer = self._truncation_footer()
            lines.append("截断信息: " + json.dumps(self.truncation, ensure_ascii=False, sort_keys=True))
        else:
            footer = ""
        body = "\n".join(lines)
        limit = max(1, int(limit))
        if not footer:
            return body if len(body) <= limit else body[:limit]
        separator = "\n"
        room = limit - len(footer) - len(separator)
        if room <= 0:
            return footer[:limit]
        if len(body) <= room:
            return body + separator + footer
        return body[:room] + separator + footer


def render_prompt(envelope: ContextEnvelope, *, instruction: str = "",
                  nonce: str = "", limit: int = PROMPT_LIMIT) -> str:
    """Assemble a full prompt within ``PROMPT_LIMIT``, reserving header/nonce room.

    The context body receives ``limit - instruction - nonce`` characters, so a
    valid bounded context plus its instruction header always fits. Units are
    characters. A raw ``executeCmd`` that is too long is rejected by the router,
    never chopped into a different command.
    """
    limit = max(1, int(limit))
    header = (instruction or "请按题目要求作答，只输出答案本身。")[:INSTRUCTION_ROOM]
    nonce_line = f"\nrequest_id: {nonce}" if nonce else ""
    reserve = len(header) + len(nonce_line) + 2
    body = envelope.prompt_view(max(1, limit - reserve))
    text = f"{header}\n{body}{nonce_line}"
    return text if len(text) <= limit else text[:limit]


def build_context(owner: str, generation: str, source_digest: str, *,
                  task_text: Any = "", answer_contract: Any = None,
                  facts: Any = None, tool_results: Any = None,
                  failures: Any = None, open_questions: Any = None,
                  trace_refs: Any = None) -> ContextEnvelope:
    """Assemble a bounded envelope, recording every truncation it performs."""
    clipped_text, text_truncated = _clip(task_text, TEXT_LIMIT)
    bounded_contract, _contract_changed = _bound_value(answer_contract or {})
    if not isinstance(bounded_contract, dict):
        bounded_contract = {}
    kept_facts, facts_dropped, facts_clipped = _normalize_items(facts, FACT_LIMIT)
    kept_tools, tools_dropped, tools_clipped = _normalize_items(tool_results, TOOL_LIMIT)
    kept_failures, failures_dropped, failures_clipped = _normalize_items(failures, FAILURE_LIMIT)
    questions: list[str] = []
    questions_dropped = 0
    for question in open_questions or ():
        if not isinstance(question, str):
            questions_dropped += 1
            continue
        if len(questions) >= QUESTION_LIMIT:
            questions_dropped += 1
            continue
        questions.append(_clip(question, QUESTION_TEXT_LIMIT)[0])
    traces, traces_dropped, _traces_clipped = _normalize_items(trace_refs, TRACE_LIMIT)
    truncation = {
        "text_truncated": text_truncated,
        "facts_dropped": facts_dropped,
        "tools_dropped": tools_dropped,
        "failures_dropped": failures_dropped,
        "questions_dropped": questions_dropped,
        "traces_dropped": traces_dropped,
        "items_clipped": (facts_clipped + tools_clipped + failures_clipped),
    }
    envelope = ContextEnvelope(
        owner=str(owner), generation=str(generation), source_digest=str(source_digest),
        task_text=clipped_text, task_text_sha256=sha256_text(str(task_text or "")),
        task_text_truncated=text_truncated, answer_contract=bounded_contract,
        facts=kept_facts, tool_results=kept_tools, failures=kept_failures,
        open_questions=questions, trace_refs=traces, truncation=truncation)
    # Aggregate cap: drop trailing entries from the biggest lists until it fits.
    while len(json.dumps(envelope.dump(), ensure_ascii=False)) > TOTAL_LIMIT:
        if envelope.facts:
            envelope.facts.pop()
            envelope.truncation["facts_dropped"] += 1
        elif envelope.tool_results:
            envelope.tool_results.pop()
            envelope.truncation["tools_dropped"] += 1
        elif envelope.failures:
            envelope.failures.pop()
            envelope.truncation["failures_dropped"] += 1
        elif envelope.trace_refs:
            envelope.trace_refs.pop()
            envelope.truncation["traces_dropped"] += 1
        else:
            break
    return envelope


class ContextStore:
    """Team-level bounded context memory; evicts whole old generations."""

    def __init__(self, limit: int = STORE_LIMIT) -> None:
        self.limit = max(1, int(limit))
        self._items: "OrderedDict[str, ContextEnvelope]" = OrderedDict()
        self.dropped = 0
        self.degraded: str | None = None

    @staticmethod
    def _key(owner: Any, generation: Any) -> str:
        return f"{owner}:{generation}"

    def put(self, envelope: ContextEnvelope) -> None:
        if not isinstance(envelope, ContextEnvelope):
            return
        key = self._key(envelope.owner, envelope.generation)
        self._items[key] = envelope
        self._items.move_to_end(key)
        while len(self._items) > self.limit:
            self._items.popitem(last=False)
            self.dropped += 1

    def get(self, owner: str, generation: str) -> ContextEnvelope | None:
        return self._items.get(self._key(owner, generation))

    def drop(self, owner: str, generation: str) -> None:
        self._items.pop(self._key(owner, generation), None)

    def reset(self) -> None:
        self._items.clear()
        self.dropped = 0
        self.degraded = None

    def dump(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "limit": self.limit, "dropped": int(self.dropped),
                "items": [item.dump() for item in self._items.values()]}

    def load(self, raw: Any) -> str:
        """Strict restore; returns a reason. Unknown schema degrades, never resets quota."""
        self._items.clear()
        self.degraded = None
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
            self.degraded = "unknown_schema"
            return self.degraded
        limit = raw.get("limit")
        dropped = raw.get("dropped")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            self.degraded = "malformed"
            return self.degraded
        if not isinstance(dropped, int) or isinstance(dropped, bool) or dropped < 0:
            self.degraded = "malformed"
            return self.degraded
        self.limit = limit
        self.dropped = dropped
        rejected = 0
        for item in raw.get("items") or ():
            envelope, reason = ContextEnvelope.load(item)
            if envelope is None:
                rejected += 1
                continue
            self.put(envelope)
        if rejected:
            self.degraded = f"rejected_items={rejected}"
        return self.degraded or "ok"


__all__ = ["SCHEMA", "TEXT_LIMIT", "CONTEXT_LIMIT", "PROMPT_LIMIT", "COMMAND_LIMIT",
           "TOTAL_LIMIT", "STORE_LIMIT", "TaskConfirmation", "ContextEnvelope",
           "ContextStore", "sha256_text", "task_generation", "context_digest",
           "public_task_confirmed", "build_context", "render_prompt"]
