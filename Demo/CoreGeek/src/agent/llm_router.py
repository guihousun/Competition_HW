"""Shared cognitive-channel router: budget, in-flight ownership and receipts (P0b).

Engineering/internal architecture only (R01/R02/R07; 接口文档 §1.1/§1.3.2/§1.7/§2.1).
This module decides *which* cognitive request (prompt/``executeCmd``) may be sent
for the team this round and owns the in-flight/receipt bookkeeping. It never
touches the judge response, never implements a task-solving algorithm and never
calls the network, a model SDK or a subprocess.

Quota discipline — one accounting path:

* ``agent.sandbox.JudgeState`` stays the ordinary daily quota source of truth;
  this router only reads ``llm_available`` and calls the same ``consume_llm``.
  There is no second daily counter here.
* A prompt is charged exactly once, when it is actually emitted. Preview/offer
  and requests replaced by a higher-priority proposal are never charged.
* ``executeCmd`` does **not** consume the LLM allowance: the quota gate applies
  to prompts only.

Exemption discipline:

* a task request is emitted with the task exemption **only** when public evidence
  confirms the task this round; a caller's ``in_task`` hint can never confer it;
* an unconfirmed task request is dropped rather than launched to solve a task
  that does not exist.

Correlation discipline:

* the official protocol has **no** guaranteed request id, so attribution relies
  on a single in-flight request plus round order;
* an internal nonce is a *verified* correlation only when it matches structurally
  (a delimited ``nonce=<value>`` token) in the reply — never a substring;
* wrong-nonce, late, empty, duplicate/stale-generation and unsolicited replies are
  quarantined, never guessed into the current task; the same answer text is not
  globally de-duplicated.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import json
import re
import secrets
from typing import Any

from .task_context import (COMMAND_LIMIT, PROMPT_LIMIT, TEXT_LIMIT,
                           TaskConfirmation, context_digest, sha256_text)

SCHEMA = "competition-hw-llm-router/1"
OWNERS = ("task", "news", "treasure")
KINDS = ("prompt", "cmd")
STATES = ("queued", "emitted", "waiting", "received", "rejected", "expired", "obsolete")
BUDGET_ORDINARY = "ordinary"
BUDGET_TASK_EXEMPT = "task_exempt"
# Engineering wait/backoff values, not an official model timeout.
WAIT_ROUNDS = 3
MAX_QUEUE = 8
MAX_RECEIPTS = 16
MAX_QUARANTINE = 8
# Payload budgets (characters). A prompt carries a bounded context plus its
# instruction/nonce header; a raw command is kept well under the prompt budget.
# Oversized payloads are rejected at offer/load time — never silently chopped.
PROMPT_PAYLOAD_LIMIT = PROMPT_LIMIT
COMMAND_PAYLOAD_LIMIT = COMMAND_LIMIT
# Correlation shapes. The shape decides the matcher; there is no substring fallback.
CORRELATION_SHAPES = ("plan_json", "command_output", "answer_text", "text")
JSON_TOKEN_KEYS = ("request_id", "nonce")
COMMAND_HEADER_TEMPLATE = "[REQUEST {}]"
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MODEL_PLAN_KINDS = ("inspect", "run", "check", "answer", "need_info", "give_up")
MODEL_PLAN_FIELDS = ("kind", "reason", "command", "answer", "evidence_ids")
MODEL_PLAN_LIMIT = 4000

_FORBIDDEN_PLAN_KEYS = ("roleCommandMap", "prompt", "executeCmd", "taskAnswer", "answer_text")


def payload_limit(kind: str) -> int:
    return COMMAND_PAYLOAD_LIMIT if kind == "cmd" else PROMPT_PAYLOAD_LIMIT


def _finite_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _no_duplicate_keys(pairs: Any) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise ValueError("duplicate key")
        seen.add(key)
    return dict(pairs)


def verify_json_token(text: str, token: str) -> bool:
    """Strict top-level JSON correlation: ``request_id``/``nonce`` must equal token.

    A JSON object with a different top-level id, duplicate keys, or the token
    mentioned only inside explanatory text does **not** correlate. This never
    claims the platform supports ids: it only checks a token we asked for.
    """
    if not NONCE_RE.match(token):
        return False
    text = text.strip()
    if not text or len(text) > PROMPT_PAYLOAD_LIMIT:
        return False
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except (ValueError, RecursionError):
        return False
    if not isinstance(data, dict):
        return False
    present = {key: data[key] for key in JSON_TOKEN_KEYS if key in data}
    if not present:
        return False
    return all(value == token for value in present.values())


def verify_command_header(text: str, token: str) -> bool:
    """Command output correlation: an explicit wrapper header, anchored to line 1."""
    if not NONCE_RE.match(token):
        return False
    lines = text.splitlines()
    if not lines:
        return False
    return lines[0].strip() == COMMAND_HEADER_TEMPLATE.format(token)


@dataclass
class Request:
    """One proposed/emitted cognitive request. Internal id, never an official id."""

    request_id: str
    owner: str
    generation: str
    source_digest: str
    kind: str
    payload: str
    budget_class: str = BUDGET_ORDINARY
    purpose: str = ""
    expected_result_shape: str = "text"
    sent_round: int = 0
    queued_round: int = 0
    status: str = "queued"
    attempt: int = 1
    charged: bool = False
    uncertain: bool = False
    validated_in_task: bool = False
    requested_in_task: bool = False
    nonce: str = ""
    content_key: str = ""
    priority: int = 100

    def dump(self) -> dict[str, Any]:
        return {"request_id": self.request_id, "owner": self.owner,
                "generation": self.generation, "source_digest": self.source_digest,
                "kind": self.kind, "payload": self.payload,
                "budget_class": self.budget_class, "purpose": self.purpose,
                "expected_result_shape": self.expected_result_shape,
                "sent_round": self.sent_round, "queued_round": self.queued_round,
                "status": self.status, "attempt": self.attempt,
                "charged": self.charged, "uncertain": self.uncertain,
                "validated_in_task": self.validated_in_task,
                "requested_in_task": self.requested_in_task,
                "nonce": self.nonce, "content_key": self.content_key,
                "priority": self.priority}

    @classmethod
    def load(cls, raw: Any) -> "Request | None":
        if not isinstance(raw, dict):
            return None
        kind = raw.get("kind")
        if not isinstance(kind, str) or kind not in KINDS:
            return None
        payload = raw.get("payload")
        if not isinstance(payload, str) or len(payload) > payload_limit(kind):
            return None
        for key in ("charged", "uncertain", "validated_in_task", "requested_in_task"):
            if key in raw and not isinstance(raw[key], bool):
                return None
        for key in ("generation", "source_digest", "purpose", "expected_result_shape",
                    "nonce", "content_key"):
            if key in raw and not isinstance(raw[key], str):
                return None
        shape = str(raw.get("expected_result_shape") or "text")
        if shape not in CORRELATION_SHAPES:
            return None
        try:
            request = cls(
                request_id=str(raw["request_id"]), owner=str(raw["owner"]),
                generation=str(raw.get("generation") or ""),
                source_digest=str(raw.get("source_digest") or ""),
                kind=kind, payload=payload,
                budget_class=str(raw.get("budget_class") or BUDGET_ORDINARY),
                purpose=str(raw.get("purpose") or ""),
                expected_result_shape=shape,
                sent_round=_finite_int(raw.get("sent_round")),
                queued_round=_finite_int(raw.get("queued_round")),
                status=str(raw.get("status") or "queued"),
                attempt=max(1, _finite_int(raw.get("attempt"), 1)),
                charged=raw.get("charged", False), uncertain=raw.get("uncertain", False),
                validated_in_task=raw.get("validated_in_task", False),
                requested_in_task=raw.get("requested_in_task", False),
                nonce=str(raw.get("nonce") or ""), content_key=str(raw.get("content_key") or ""),
                priority=_finite_int(raw.get("priority"), 100))
        except (KeyError, TypeError, ValueError):
            return None
        if request.owner not in OWNERS or request.status not in STATES:
            return None
        if request.budget_class not in (BUDGET_ORDINARY, BUDGET_TASK_EXEMPT):
            return None
        if request.nonce and not NONCE_RE.match(request.nonce):
            return None
        return request


@dataclass
class Receipt:
    """A processed platform reply. The bounded preview plus the genuine digest."""

    request_id: str | None
    round_no: int
    kind: str
    status: str          # received | quarantined | rejected
    reason: str
    source: str = "platform"
    text: str = ""
    text_sha256: str = ""
    text_truncated: bool = False

    def dump(self) -> dict[str, Any]:
        return {"request_id": self.request_id, "round_no": self.round_no,
                "kind": self.kind, "status": self.status, "reason": self.reason,
                "source": self.source, "text": self.text,
                "text_sha256": self.text_sha256, "text_truncated": self.text_truncated}

    @classmethod
    def load(cls, raw: Any) -> "Receipt | None":
        if not isinstance(raw, dict):
            return None
        if not isinstance(raw.get("text_truncated"), bool):
            return None
        for key in ("text", "text_sha256", "reason", "source"):
            if key in raw and not isinstance(raw[key], str):
                return None
        try:
            return cls(request_id=(str(raw["request_id"]) if raw.get("request_id") else None),
                       round_no=_finite_int(raw.get("round_no")), kind=str(raw.get("kind") or ""),
                       status=str(raw.get("status") or ""), reason=str(raw.get("reason") or ""),
                       source=str(raw.get("source") or "platform"),
                       text=str(raw.get("text") or ""),
                       text_sha256=str(raw.get("text_sha256") or ""),
                       text_truncated=raw["text_truncated"])
        except (TypeError, ValueError):
            return None


class LLMRouter:
    """Team-level scheduler for the shared cognitive channel."""

    def __init__(self, judge: Any = None, *, allow_prompt: bool = True,
                 allow_command: bool = True) -> None:
        self.judge = judge
        self.allow_prompt = bool(allow_prompt)
        self.allow_command = bool(allow_command)
        self.degraded: str | None = None
        self.day_index = 1
        self.active_generation: str | None = None
        self.last_round = 0
        self.queue: list[Request] = []
        self.pending: dict[str, Request | None] = {"prompt": None, "cmd": None}
        self.history: "OrderedDict[str, Request]" = OrderedDict()
        self.receipts: "OrderedDict[str, Receipt]" = OrderedDict()
        self.quarantine: list[Receipt] = []
        self.uncertain_reserved = 0
        self.sent_count = 0
        self._counter = 0
        self._receipt_seq = 0
        self._charged_round: int | None = None
        self._emitted_round: int | None = None
        self.blocked_day: int = 0
        self.blocked_generation: str = ""

    # -- quota helpers ------------------------------------------------------

    def _ordinary_available(self) -> bool:
        judge = self.judge
        if judge is None or not hasattr(judge, "llm_available"):
            return False
        try:
            return bool(judge.llm_available(in_task=False))
        except Exception:  # noqa: BLE001 - scheduler must never break planning
            return False

    def ordinary_used(self) -> int | None:
        judge = self.judge
        if judge is None:
            return None
        value = getattr(judge, "llm_used_today", None)
        return _finite_int(value, 0) if value is not None else None

    def take_charge(self, round_no: Any) -> bool:
        """Consume the "router already charged this round" marker exactly once."""
        if self._charged_round is None:
            return False
        same = _finite_int(round_no) == self._charged_round
        self._charged_round = None
        return same

    # -- round bookkeeping --------------------------------------------------

    def note_round(self, round_no: Any) -> dict[str, Any]:
        round_no = _finite_int(round_no)
        info: dict[str, Any] = {"round": round_no, "restart": False}
        if self.last_round and round_no and round_no < self.last_round:
            if round_no != 1:
                info["stale"] = True
                info["day_index"] = self.day_index
                return info
            # A new match: drop in-flight/queued work instead of carrying it over.
            for request in self.queue:
                request.status = "obsolete"
            self.queue = []
            for kind in KINDS:
                if self.pending[kind] is not None:
                    self.pending[kind].status = "obsolete"
                self.pending[kind] = None
            self.uncertain_reserved = 0
            info["restart"] = True
        self.last_round = round_no or self.last_round
        if round_no:
            self.day_index = (round_no - 1) // 130 + 1
        info["day_index"] = self.day_index
        return info

    # -- proposing ----------------------------------------------------------

    def offer(self, owner: str, generation: Any, source_digest: Any, *,
              kind: str, payload: Any, purpose: str = "",
              expected_result_shape: str = "text", in_task: bool = False,
              priority: int = 100, nonce: str = "", operation_id: str = "") -> Request | None:
        """Queue a request. Never charges quota and never changes the response.

        An oversized payload is **rejected**, never silently chopped: a chopped
        command could execute a different action and a chopped prompt can lose an
        output constraint.
        """
        if self.degraded:
            return None
        if owner not in OWNERS or kind not in KINDS:
            return None
        if not isinstance(payload, str) or not payload:
            return None
        if len(payload) > payload_limit(kind):
            return None
        if expected_result_shape not in CORRELATION_SHAPES:
            return None
        if nonce and not NONCE_RE.match(nonce):
            return None
        if not isinstance(operation_id, str) or len(operation_id) > 160:
            return None
        generation = str(generation or "none")
        content_key = context_digest(owner, generation, kind, payload)
        if operation_id:
            # A fresh controller operation may intentionally reread the same
            # document. Repeated offers of that operation still deduplicate.
            # This local identity is not an official result-correlation token.
            content_key = context_digest(owner, generation, kind, payload, operation_id)
        for existing in self.queue:
            if existing.content_key == content_key and existing.status == "queued":
                return existing
        for slot in self.pending.values():
            if slot is not None and slot.content_key == content_key and slot.status == "waiting":
                return slot
        recent = self.history.get(content_key)
        if recent is not None and recent.status in ("waiting", "received"):
            return recent
        self._counter += 1
        request = Request(
            request_id=f"r{self._counter:04d}-{content_key}",
            owner=owner, generation=generation, source_digest=str(source_digest or "none"),
            kind=kind, payload=payload, budget_class=BUDGET_ORDINARY,
            purpose=str(purpose or "")[:200], expected_result_shape=str(expected_result_shape or "text")[:80],
            queued_round=self.last_round, priority=_finite_int(priority, 100),
            requested_in_task=bool(in_task), nonce=nonce, content_key=content_key)
        self.queue.append(request)
        while len(self.queue) > MAX_QUEUE:
            dropped = self.queue.pop(0)
            dropped.status = "obsolete"
        return request

    # -- selection ----------------------------------------------------------

    def _confirmed_for(self, request: Request, confirmed_task: TaskConfirmation | None) -> bool:
        if request.owner != "task":
            return False
        if confirmed_task is None or not confirmed_task.confirmed:
            return False
        if confirmed_task.generation and request.generation != confirmed_task.generation:
            return False
        return True

    def select(self, observation: Any, *,
               confirmed_task: TaskConfirmation | None = None,
               judge: Any = None) -> Request | None:
        """Pick at most one new cognitive request for this round, or ``None``."""
        if judge is not None:
            self.judge = judge
        if self.degraded:
            return None
        if self._emitted_round == self.last_round:
            return None
        if confirmed_task is not None and confirmed_task.confirmed and confirmed_task.generation:
            self.active_generation = confirmed_task.generation
        eligible: list[Request] = []
        for request in list(self.queue):
            if request.status != "queued":
                continue
            if request.owner == "task":
                if not self._confirmed_for(request, confirmed_task):
                    # Never launch model work for a task that is not confirmed.
                    request.status = "obsolete"
                    continue
                request.budget_class = BUDGET_TASK_EXEMPT
                request.validated_in_task = True
            else:
                request.budget_class = BUDGET_ORDINARY
                request.validated_in_task = False
            if request.kind == "prompt" and not self.allow_prompt:
                continue
            if request.kind == "prompt" and (
                    (request.owner == "task" and request.generation == self.blocked_generation)
                    or (request.budget_class == BUDGET_ORDINARY and self.day_index == self.blocked_day)):
                continue
            if request.kind == "cmd" and not self.allow_command:
                continue
            if request.kind == "cmd" and not self._confirmed_for(request, confirmed_task):
                continue  # executeCmd only while a confirmed task is running
            if request.kind == "prompt" and request.budget_class != BUDGET_TASK_EXEMPT \
                    and not self._ordinary_available():
                continue  # ordinary allowance spent: deterministic strategy continues
            eligible.append(request)
        if not eligible:
            return None
        eligible.sort(key=lambda item: (item.priority, item.owner, item.generation,
                                        item.request_id))
        for request in eligible:
            slot = "cmd" if request.kind == "cmd" else "prompt"
            if self.pending[slot] is None:
                return request
        return None

    # -- emission -----------------------------------------------------------

    def mark_emitted(self, request: Request | None, *, round_no: Any = None,
                     judge: Any = None) -> bool:
        """Record an actually-emitted request and charge the ordinary quota once.

        Refuses to overwrite an existing in-flight request, and re-validates the
        task exemption so a caller hint can never smuggle one through.
        """
        if request is None or request.status != "queued":
            return False
        round_no = _finite_int(round_no) or self.last_round
        if round_no == self._emitted_round:
            return False
        if request.kind == "prompt" and request.owner == "task" and request.generation == self.blocked_generation:
            return False
        if judge is not None:
            self.judge = judge
        slot = "cmd" if request.kind == "cmd" else "prompt"
        if self.pending[slot] is not None:
            return False
        if request.owner == "task" and request.budget_class == BUDGET_TASK_EXEMPT \
                and not request.validated_in_task:
            request.budget_class = BUDGET_ORDINARY
        if request.kind == "prompt" and request.budget_class == BUDGET_ORDINARY:
            if (round_no - 1) // 130 + 1 == self.blocked_day:
                return False
            if not self._ordinary_available():
                return False
            if self.judge is not None:
                try:
                    self.judge.consume_llm(in_task=False)
                except Exception:  # noqa: BLE001
                    return False
            request.charged = True
            self._charged_round = round_no
        request.sent_round = round_no
        request.status = "waiting"
        self._emitted_round = round_no
        self.pending[slot] = request
        self.queue = [item for item in self.queue if item is not request]
        self.history[request.content_key] = request
        self.sent_count += 1
        self._trim_history()
        return True

    def mark_uncertain(self, request: Request | None) -> bool:
        """Conservative reservation when the platform write outcome is unknown."""
        if request is None:
            return False
        request.uncertain = True
        if request.kind == "prompt" and request.budget_class == BUDGET_ORDINARY and not request.charged:
            if self.judge is not None:
                try:
                    self.judge.consume_llm(in_task=False)
                except Exception:  # noqa: BLE001
                    return False
            request.charged = True
            self.uncertain_reserved += 1
        return True

    # -- receipts -----------------------------------------------------------

    def ingest(self, observation: Any, *,
               confirmed_task: TaskConfirmation | None = None) -> dict[str, Any]:
        """Fold public replies into router state; never guesses an attribution.

        The current generation is resolved from this observation *before*
        association, so a reply can never be attributed to a stale generation.
        """
        info: dict[str, Any] = {"received": 0, "expired": 0, "quarantined": 0,
                                "same_round": 0, "stale": 0, "restart": False}
        if not isinstance(observation, dict):
            return info
        round_no = _finite_int(observation.get("roundNo"))
        if self.last_round and 1 < round_no < self.last_round:
            info["stale"] = 1
            return info
        if confirmed_task is not None and confirmed_task.generation:
            if confirmed_task.confirmed:
                self.active_generation = confirmed_task.generation
            elif self.active_generation and confirmed_task.generation == self.active_generation:
                self.active_generation = None
        info["restart"] = bool(self.note_round(round_no).get("restart"))
        errors = observation.get("errors")
        if isinstance(errors, list) and any(isinstance(e, dict) and e.get("errorCode") == 5 for e in errors):
            pending = self.pending["prompt"]
            if pending is not None and round_no > pending.sent_round:
                # The error concerns the previously sent prompt, including when
                # its receipt arrives on the next day. Never refund uncertainty.
                self.blocked_day = (pending.sent_round - 1) // 130 + 1
                if pending.owner == "task":
                    self.blocked_generation = pending.generation
                pending.status = "rejected"
                self._receipt(pending, round_no, "prompt", "rejected", "official_quota_error")
                self.history[pending.content_key] = pending
                self.pending["prompt"] = None
            elif pending is None:
                self.blocked_day = self.day_index
        llm_text = observation.get("llmResp")
        self._ingest_channel(observation, round_no, "prompt",
                             "" if llm_text in (None, "") else str(llm_text), info,
                             confirmed_task)
        raw_command = observation.get("lastCmdResult")
        self._ingest_channel(observation, round_no, "cmd",
                             "" if raw_command in (None, "") else str(raw_command), info,
                             confirmed_task)
        return info

    def _ingest_channel(self, observation: dict[str, Any], round_no: int, kind: str,
                        text: str, info: dict[str, Any],
                        confirmed_task: TaskConfirmation | None) -> None:
        pending = self.pending[kind]
        if not text:
            if pending is not None and round_no and round_no >= pending.sent_round + WAIT_ROUNDS:
                pending.status = "expired"
                self._receipt(pending, round_no, kind, "expired", "no_reply_within_wait")
                self.history[pending.content_key] = pending
                self.pending[kind] = None
                info["expired"] += 1
            return
        if pending is None:
            self._quarantine(None, round_no, kind, "no_pending_request", text)
            info["quarantined"] += 1
            return
        if round_no and round_no <= pending.sent_round:
            # A same-round payload belongs to an earlier request, not this one.
            info["same_round"] += 1
            return
        if round_no and pending.sent_round and round_no > pending.sent_round + WAIT_ROUNDS:
            pending.status = "expired"
            self._receipt(pending, round_no, kind, "expired", "late_reply_after_wait", text)
            self.history[pending.content_key] = pending
            self.pending[kind] = None
            info["expired"] += 1
            info["quarantined"] += 1
            return
        # A stale generation cannot feed the current task (task-owned channels).
        if pending.owner == "task" and self.active_generation \
                and pending.generation != self.active_generation:
            pending.status = "obsolete"
            self._receipt(pending, round_no, kind, "quarantined", "stale_generation", text)
            self.history[pending.content_key] = pending
            self.pending[kind] = None
            info["stale"] += 1
            info["quarantined"] += 1
            return
        if pending.owner == "task" and confirmed_task is not None \
                and not confirmed_task.confirmed:
            pending.status = "obsolete"
            self._receipt(pending, round_no, kind, "quarantined",
                          "task_no_longer_confirmed", text)
            self.history[pending.content_key] = pending
            self.pending[kind] = None
            info["stale"] += 1
            info["quarantined"] += 1
            return
        if pending.nonce:
            ok, source = self._correlate(text, pending)
            if not ok:
                self._quarantine(pending, round_no, kind, source, text)
                info["quarantined"] += 1
                return
        else:
            source = "single_inflight"
        pending.status = "received"
        self._receipt(pending, round_no, kind, "received", source, text)
        self.history[pending.content_key] = pending
        self.pending[kind] = None
        info["received"] += 1

    def received_results(self, kind: str | None = None) -> list[Receipt]:
        """Bounded verified results (bounded preview + genuine digest) for the agent."""
        out = [receipt for receipt in self.receipts.values()
               if receipt.status == "received" and (kind is None or receipt.kind == kind)]
        return out[-MAX_RECEIPTS:]

    @staticmethod
    def new_nonce() -> str:
        """Fresh internal correlation token for our own output (not an official id)."""
        return "n" + secrets.token_hex(8)

    @staticmethod
    def _correlate(text: str, request: Request) -> tuple[bool, str]:
        """Correlation decided by the expected result shape, never substrings."""
        if not request.nonce:
            return True, "single_inflight"
        shape = request.expected_result_shape
        if shape == "plan_json":
            return ((True, "verified_json_token") if verify_json_token(text, request.nonce)
                    else (False, "request_id_mismatch"))
        if shape == "command_output":
            return ((True, "verified_command_header") if verify_command_header(text, request.nonce)
                    else (False, "command_header_mismatch"))
        # A required token with no structural matcher must not be accepted loosely.
        return False, "unsupported_correlation_shape"

    def _receipt(self, request: Request | None, round_no: int, kind: str, status: str,
                 reason: str, text: str = "") -> Receipt:
        preview = text if len(text) <= TEXT_LIMIT else text[:TEXT_LIMIT]
        receipt = Receipt(request_id=(request.request_id if request is not None else None),
                          round_no=round_no, kind=kind, status=status, reason=reason,
                          text=preview, text_sha256=sha256_text(text),
                          text_truncated=len(text) > TEXT_LIMIT)
        self._receipt_seq += 1
        key = f"{self._receipt_seq}:{receipt.request_id or '-'}@{round_no}:{kind}"
        self.receipts[key] = receipt
        self.receipts.move_to_end(key)
        while len(self.receipts) > MAX_RECEIPTS:
            self.receipts.popitem(last=False)
        return receipt

    def _quarantine(self, request: Request | None, round_no: int, kind: str,
                    reason: str, text: str) -> Receipt:
        receipt = self._receipt(request, round_no, kind, "quarantined", reason, text)
        self.quarantine.append(receipt)
        while len(self.quarantine) > MAX_QUARANTINE:
            self.quarantine.pop(0)
        return receipt

    def _trim_history(self) -> None:
        while len(self.history) > MAX_RECEIPTS:
            self.history.popitem(last=False)

    # -- model plan validation ---------------------------------------------

    @staticmethod
    def parse_model_plan(raw: Any) -> dict[str, Any] | None:
        """Strictly parse a model JSON plan. No ``eval``, no official fields.

        ``roleCommandMap``/``prompt``/``executeCmd`` and other response-field
        lookalikes are rejected, so a forged plan can never become an official
        action. Unknown/unsupported kinds and oversized payloads are rejected.
        """
        if isinstance(raw, str):
            text = raw.strip()
            if not text or len(text) > MODEL_PLAN_LIMIT:
                return None
            try:
                data = json.loads(text)
            except (ValueError, RecursionError):
                return None
        elif isinstance(raw, dict):
            if len(json.dumps(raw, ensure_ascii=False)) > MODEL_PLAN_LIMIT:
                return None
            data = raw
        else:
            return None
        if not isinstance(data, dict):
            return None
        if any(key in data for key in _FORBIDDEN_PLAN_KEYS):
            return None
        if any(key not in MODEL_PLAN_FIELDS for key in data):
            return None
        kind = data.get("kind")
        if kind not in MODEL_PLAN_KINDS:
            return None
        plan: dict[str, Any] = {"kind": kind, "reason": str(data.get("reason") or "")[:200]}
        for key in ("command", "answer"):
            if key in data:
                value = data[key]
                if not isinstance(value, str) or len(value) > MODEL_PLAN_LIMIT:
                    return None
                plan[key] = value
        if "evidence_ids" in data:
            ids = data["evidence_ids"]
            if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
                return None
            plan["evidence_ids"] = ids[:16]
        if kind in ("run",) and not plan.get("command"):
            return None
        if kind == "answer" and not plan.get("answer"):
            return None
        return plan

    # -- persistence --------------------------------------------------------

    def dump(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "degraded": self.degraded,
            "day_index": self.day_index,
            "active_generation": self.active_generation,
            "last_round": self.last_round,
            "queue": [item.dump() for item in self.queue],
            "pending_prompt": self.pending["prompt"].dump() if self.pending["prompt"] else None,
            "pending_command": self.pending["cmd"].dump() if self.pending["cmd"] else None,
            "history": [item.dump() for item in self.history.values()],
            "receipts": [item.dump() for item in self.receipts.values()],
            "quarantine": [item.dump() for item in self.quarantine],
            "uncertain_reserved": self.uncertain_reserved,
            "sent_count": self.sent_count,
            "counter": self._counter,
            "charged_round": self._charged_round,
            "emitted_round": self._emitted_round,
            "blocked_day": self.blocked_day,
            "blocked_generation": self.blocked_generation,
            "allow_prompt": self.allow_prompt,
            "allow_command": self.allow_command,
        }

    def load(self, raw: Any) -> str:
        """Strict restore. Unknown schema degrades explicitly; never raises."""
        self.reset()
        if not isinstance(raw, dict):
            self.degraded = "not_an_object"
            return self.degraded
        if raw.get("schema") != SCHEMA:
            self.degraded = "unknown_schema"
            return self.degraded
        for key in ("allow_prompt", "allow_command"):
            if key in raw and not isinstance(raw[key], bool):
                self.degraded = "malformed"
                return self.degraded
        if raw.get("degraded") is not None and not isinstance(raw.get("degraded"), str):
            self.degraded = "malformed"
            return self.degraded
        queue = raw.get("queue")
        if not isinstance(queue, list) or len(queue) > MAX_QUEUE:
            self.degraded = "malformed"
            return self.degraded
        for key, limit in (("history", MAX_RECEIPTS), ("receipts", MAX_RECEIPTS),
                           ("quarantine", MAX_QUARANTINE)):
            if not isinstance(raw.get(key), list) or len(raw[key]) > limit:
                self.degraded = "malformed"
                return self.degraded
        self.day_index = max(1, _finite_int(raw.get("day_index"), 1))
        self.active_generation = (str(raw["active_generation"])
                                  if isinstance(raw.get("active_generation"), str) else None)
        self.last_round = _finite_int(raw.get("last_round"))
        self.uncertain_reserved = max(0, _finite_int(raw.get("uncertain_reserved")))
        self.sent_count = max(0, _finite_int(raw.get("sent_count")))
        self._counter = max(0, _finite_int(raw.get("counter")))
        charged_round = raw.get("charged_round")
        self._charged_round = (_finite_int(charged_round) if charged_round is not None else None)
        emitted_round = raw.get("emitted_round")
        self._emitted_round = (_finite_int(emitted_round) if emitted_round is not None else None)
        self.blocked_day = max(0, _finite_int(raw.get("blocked_day")))
        self.blocked_generation = str(raw.get("blocked_generation") or "")[:160]
        self.allow_prompt = raw.get("allow_prompt", True)
        self.allow_command = raw.get("allow_command", True)
        rejected = 0
        for item in queue:
            request = Request.load(item)
            if request is None or request.status != "queued":
                rejected += 1
                continue
            self.queue.append(request)
        for key, slot in (("pending_prompt", "prompt"), ("pending_command", "cmd")):
            request = Request.load(raw.get(key))
            if request is None:
                if raw.get(key) is not None:
                    rejected += 1
                continue
            request.status = "waiting"
            self.pending[slot] = request
            self.history[request.content_key] = request
        for item in raw.get("history") or ():
            request = Request.load(item)
            if request is None:
                rejected += 1
                continue
            self.history[request.content_key] = request
        for item in raw.get("receipts") or ():
            receipt = Receipt.load(item)
            if receipt is None:
                rejected += 1
                continue
            self._receipt_seq += 1
            self.receipts[f"{self._receipt_seq}:{receipt.request_id or '-'}"] = receipt
        for item in raw.get("quarantine") or ():
            receipt = Receipt.load(item)
            if receipt is None:
                rejected += 1
                continue
            self.quarantine.append(receipt)
        while len(self.receipts) > MAX_RECEIPTS:
            self.receipts.popitem(last=False)
        del self.quarantine[:-MAX_QUARANTINE]
        while len(self.history) > MAX_RECEIPTS:
            self.history.popitem(last=False)
        if raw.get("degraded"):
            # A previously degraded state must not be relaxed by a round trip.
            self.degraded = str(raw["degraded"])
        elif rejected:
            self.degraded = f"rejected_records={rejected}"
        return self.degraded or "ok"

    def reset(self) -> None:
        self.degraded = None
        self.day_index = 1
        self.active_generation = None
        self.last_round = 0
        self.queue = []
        self.pending = {"prompt": None, "cmd": None}
        self.history = OrderedDict()
        self.receipts = OrderedDict()
        self.quarantine = []
        self.uncertain_reserved = 0
        self.sent_count = 0
        self._counter = 0
        self._receipt_seq = 0
        self._charged_round = None
        self._emitted_round = None
        self.blocked_day = 0
        self.blocked_generation = ""


__all__ = ["SCHEMA", "OWNERS", "KINDS", "STATES", "BUDGET_ORDINARY",
           "BUDGET_TASK_EXEMPT", "WAIT_ROUNDS", "MAX_QUEUE", "CORRELATION_SHAPES",
           "PROMPT_PAYLOAD_LIMIT", "COMMAND_PAYLOAD_LIMIT", "payload_limit",
           "verify_json_token", "verify_command_header",
           "Request", "Receipt", "LLMRouter"]
