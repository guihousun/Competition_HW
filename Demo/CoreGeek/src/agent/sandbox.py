"""Judge-facing transport: LLM prompts, sandbox commands, and their results.

The judge owns two extra response fields beyond ``roleCommandMap`` (接口文档 §2):

* ``prompt``      — sent to the judge's LLM; 3 calls per game day unless a task
                    is being executed, when the quota does not apply and the
                    call is not counted (接口文档 §1.7).
* ``executeCmd``  — a shell/python command run in the judge's sandbox; at most
                    15 s, no external network, available only while a task is in
                    progress (接口文档 §2.1).

Results come back **one round later** in ``lastCmdResult`` / ``llmResp`` with a
documented format:

    "[exitCode:N]\\n<output>" | "[TIMEOUT]\\n<partial>" |
    "[JUDGER_ERROR]\\n<reason>" | trailing "[TRUNCATED]"

This module is pure transport and bookkeeping: it never decides what to ask, so
the strategy stays the only place where behaviour is chosen. Sandbox failures
and command timeouts are explicitly **not** team exceptions, so they are parsed
and reported as their own outcome instead of being folded into the error count.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Documented markers (接口文档 §1.1).
EXIT_CODE = "exitCode"
TIMEOUT = "TIMEOUT"
JUDGER_ERROR = "JUDGER_ERROR"
TRUNCATED = "[TRUNCATED]"
# Sandbox command budget, official.
SANDBOX_TIMEOUT_SECONDS = 15
# Daily LLM allowance outside task execution (接口文档 §1.7).
LLM_DAILY_QUOTA = 3
ROUNDS_PER_DAY = 130
# How many rounds to wait for a result before considering it lost. The judge
# answers "last round", so 1 is the norm; 2 tolerates a dropped round.
RESULT_WAIT_ROUNDS = 2


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Parsed ``lastCmdResult``."""

    status: str  # exit | timeout | judger_error | empty
    exit_code: int | None = None
    output: str = ""
    truncated: bool = False
    raw: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "exit" and self.exit_code == 0

    @property
    def is_failure(self) -> bool:
        """True for a sandbox-level failure (never a team exception)."""
        return self.status in ("timeout", "judger_error")

    def summary(self) -> str:
        if self.status == "empty":
            return "未提交命令"
        if self.status == "timeout":
            return f"命令超时（{SANDBOX_TIMEOUT_SECONDS}s 上限），部分输出 {len(self.output)} 字符"
        if self.status == "judger_error":
            return f"判题器侧异常：{self.output or '无说明'}"
        tail = "（已截断）" if self.truncated else ""
        return f"退出码 {self.exit_code}{tail}，输出 {len(self.output)} 字符"


def parse_command_result(raw: Any) -> CommandResult:
    """Parse the documented ``lastCmdResult`` string.

    Unknown shapes degrade to ``judger_error`` rather than being treated as a
    success, so a format change can never look like a working sandbox.
    """
    if raw is None or (isinstance(raw, str) and raw == ""):
        return CommandResult(status="empty", raw="")
    text = str(raw)
    truncated = TRUNCATED in text
    if truncated:
        text = text.replace(TRUNCATED, "").rstrip("\n")
    head, _, body = text.partition("\n")
    head = head.strip()
    if head.startswith(f"[{EXIT_CODE}:"):
        inner = head[len(EXIT_CODE) + 2:].rstrip("]")
        try:
            code = int(inner)
        except ValueError:
            return CommandResult("judger_error", None, text, truncated, str(raw))
        return CommandResult("exit", code, body, truncated, str(raw))
    if head.startswith(f"[{TIMEOUT}]"):
        return CommandResult("timeout", None, body, truncated, str(raw))
    if head.startswith(f"[{JUDGER_ERROR}]"):
        return CommandResult("judger_error", None, body, truncated, str(raw))
    return CommandResult("judger_error", None, text, truncated, str(raw))


@dataclass
class PendingRequest:
    """A prompt or sandbox command awaiting its result."""

    kind: str  # "prompt" | "cmd"
    sent_round: int
    payload: str
    purpose: str = ""

    def expired(self, round_no: int, wait: int = RESULT_WAIT_ROUNDS) -> bool:
        return round_no - self.sent_round >= wait


@dataclass
class JudgeState:
    """Per-team bookkeeping for the judge-facing channels.

    Held under the simulator's private ``_demo`` area, so it can never leak into
    the policy's own observation: the strategy sees a fresh object each round and
    only the fields the official protocol publishes.
    """

    llm_used_today: int = 0
    llm_day: int = 1
    pending_prompt: PendingRequest | None = None
    pending_cmd: PendingRequest | None = None
    last_prompt: str = ""
    last_command: str = ""
    last_result: CommandResult = field(default_factory=lambda: CommandResult("empty"))
    llm_responses: int = 0
    cmd_runs: int = 0
    cmd_failures: int = 0

    def note_round(self, round_no: int) -> None:
        """Reset the daily LLM allowance on the first round of a game day."""
        day = (round_no - 1) // ROUNDS_PER_DAY + 1
        if day != self.llm_day:
            self.llm_day = day
            self.llm_used_today = 0

    def llm_available(self, in_task: bool) -> bool:
        """Inside a task the daily limit does not apply (接口文档 §1.7)."""
        if in_task:
            return True
        return self.llm_used_today < LLM_DAILY_QUOTA

    def consume_llm(self, in_task: bool) -> None:
        if not in_task:
            self.llm_used_today += 1


class ResponseBuilder:
    """Builds the official response object without losing the command map."""

    def __init__(self) -> None:
        self.commands: dict[str, dict[str, Any]] = {}
        self.prompt: str | None = None
        self.execute: str | None = None

    def build(self) -> dict[str, Any]:
        """The response to POST. Empty optional fields are omitted entirely."""
        response: dict[str, Any] = {"roleCommandMap": self.commands}
        if self.prompt:
            response["prompt"] = self.prompt
        if self.execute:
            response["executeCmd"] = self.execute
        return response


def shell(command: str, *, sandbox_prefix: str = "") -> str:
    """Wrap a command for the judge sandbox.

    The judge runs shell/python itself; this helper only makes sure a generated
    command is a single line and free of NUL bytes, so nothing about the sandbox
    protocol is asserted locally that the judge has not published.
    """
    clean = " ".join(str(command).split())
    if "\x00" in clean:
        raise ValueError("command must not contain NUL")
    return f"{sandbox_prefix}{clean}"
