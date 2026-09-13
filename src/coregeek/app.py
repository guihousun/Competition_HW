"""组装根：把各层接起来，并守好那条唯一会出局的红线。

设计见 docs/design/code-design.md §5（安全响应管线）/ §7（并发安全）。

红线的三个来源（判题器只认这三类异常，累计 5 次该队不再被调度）：
  ① 连接超时（>10s）/ 响应超时（>5s）
  ② 响应格式错
  ③ 指令非法

对应这里的三道防线：
  ① `handle()` 用**工作线程 + 决策预算**包裹 planner，超时立刻回空指令。
     空指令集是合法的，且不计异常——宁可放弃这一个回合。
  ② 出参一定由 `json.dumps` 生成，且三个顶层字段**永远都在**。
  ③ 所有指令都过 `protocol/commands.py` 的校验器（见那里的注释）。

⚠️ 请求路径上**没有**任何阻塞 IO：
  - 日志是 `LogStore.submit()`（有界队列 + 后台线程），队列满就丢日志；
  - LLM 与沙盒都只是响应里的**字符串字段**，判题器在它自己的进程里执行；
  - 没有网络调用、没有 fsync。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .domain import calendar, planner
from .domain.intent import Intent
from .infra import config
from .infra.logstore import LogStore, console
from .protocol import commands, model
from .transport import server

__all__ = ["App", "Decision", "MatchState"]


# ══ 决策结果 ═════════════════════════════════════════════════════════
@dataclass
class Decision:
    """一次决策的完整产物。planner 在第 5 步接上，之前这里是空的。"""

    intents: list[Intent] = field(default_factory=list)
    prompt: str = ""
    execute_cmd: str = ""
    #: 各阶段耗时（毫秒），进日志用于定位 5 秒闸门
    phases: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# ══ 回合闸门 ═════════════════════════════════════════════════════════
CACHED = "cached"      # 同一回合重复到达 → 原样回放，不再推进状态
PROCEED = "proceed"    # 正常处理
STALE = "stale"        # 比已处理过的回合还旧 → 丢弃，避免状态回退


class MatchState:
    """整场比赛的跨回合状态，**按回合幂等**。

    ⚠️ 这里有一个会让整个半场归零的坑，必须写清楚：

    任务书只说"每场比赛进行两轮，下半场双方互换位置"，**没说 `roundNo` 是否重置**。
    如果它重置，而下半场我们又恰好用 `roundNo < last_round` 判"过期回合"，
    那么下半场的每一次请求都会被当成过期而**回一份空指令**——
    比赛照常跑完，我们一枪不开，日志上还看不出任何异常。

    所以判"新一场"用三条独立的信号，任一条成立即重置：
      1. **会话键变了**（`teamId` 或 `teamOur.type`）——换边最可靠的信号；
      2. `roundNo == 1` —— 明确的"从头开始"；
      3. `roundNo` 比 `last_round` 倒退超过 `MATCH_RESET_GAP` —— 兜底。

    只有"倒退很少"才当作过期重放。这样两种语义下都对：
      - roundNo 连续 → 永不触发重置，倒退即过期；
      - roundNo 重置 → 第 1 条或第 2 条命中，正常开新半场。

    `epoch` 用来丢弃**上一场**遗留的在途工作线程的结果：重置时自增，
    提交时必须带对 epoch，否则迟到者会把旧状态写进新半场。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.reset_to(("", ""))
        self.matches = 0
        self.seen_rounds = 0
        self.duplicate_rounds = 0
        self.stale_rounds = 0

    def reset_to(self, key: tuple[str, str]) -> None:
        with self._lock:
            self.key = key
            self.epoch = getattr(self, "epoch", 0) + 1
            self.last_round = -1
            self._cache: dict[int, bytes] = {}
            self.matches = getattr(self, "matches", 0) + 1
            self.duplicate_rounds = getattr(self, "duplicate_rounds", 0)

    def begin(self, round_no: int, key: tuple[str, str]) -> tuple[str, bytes | None]:
        """进入一个回合。返回 `(裁决, 可回放响应)`。"""
        with self._lock:
            if key != self.key:
                # 换边 / 换对手 → 新一场。旧缓存与旧回合号全部作废。
                self.reset_to(key)
                return PROCEED, None

            hit = self._cache.get(round_no)
            if hit is not None:
                self.duplicate_rounds += 1
                return CACHED, hit

            if round_no < self.last_round:
                backwards = self.last_round - round_no
                if round_no <= 1 or backwards >= config.MATCH_RESET_GAP:
                    self.reset_to(key)  # 兜底信号：roundNo 重置但 type 没变
                    return PROCEED, None
                self.stale_rounds += 1
                return STALE, None

            if round_no == self.last_round:
                self.duplicate_rounds += 1  # 同一回合的并发重入，重算即可
            return PROCEED, None

    def commit(self, round_no: int, body: bytes, epoch: int) -> bool:
        """提交决策结果。epoch 不匹配（上一场的迟到线程）或回合更旧 → 丢弃。"""
        with self._lock:
            if epoch != self.epoch or round_no < self.last_round:
                return False
            self.last_round = round_no
            self.seen_rounds += 1
            self._cache[round_no] = body
            if len(self._cache) > 64:
                for old in sorted(self._cache)[:-64]:
                    self._cache.pop(old, None)
            return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "key": list(self.key),
                "match": self.matches,
                "lastRound": self.last_round,
                "seen": self.seen_rounds,
                "dup": self.duplicate_rounds,
                "stale": self.stale_rounds,
            }


# ══ 组装根 ═══════════════════════════════════════════════════════════
class App:
    def __init__(
        self,
        *,
        log_dir: str | None = None,
        budget_ms: float | None = None,
        enable_log: bool = True,
    ) -> None:
        self.state = MatchState()
        self.budget_s = (budget_ms if budget_ms is not None else config.DECIDE_BUDGET_S * 1000.0) / 1000.0
        self.log = LogStore(log_dir or config.LOG_DIR) if enable_log else None
        if self.log is not None:
            self.log.start()
        self.rounds_served = 0
        self.fallbacks = 0
        #: 跨回合粘性状态（岗位分工、角色↔武器配对）。只在决策线程里动，
        #: 由 `_run_with_budget` 的 join 串行化；换边时随 epoch 一起作废。
        self.memory = planner.PlanMemory()
        #: 见过几场新对局（= 粘性状态被作废过几次）。供 selfcheck / 日志观测。
        #: **首发 payload 也算一场** —— 进程启动时会话是空的，第一条请求正是在建立它。
        self.sessions_seen = 0

    # ── 生命周期 ────────────────────────────────────────────────────
    def shutdown(self) -> None:
        if self.log is not None:
            self.log.close()

    # ── 请求入口 ────────────────────────────────────────────────────
    def handle(self, raw: bytes) -> bytes:
        """`bytes -> bytes`。**永不抛异常，永远返回合法 JSON。**

        `raw` 用 bytes 而不是 dict，是为了让 fixture 驱动器和未来的
        `tools/replay.py` 能原样回放线上抓到的报文（含编码问题）。
        """
        started = time.monotonic()
        try:
            payload = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except (ValueError, AttributeError) as exc:
            console(f"app: malformed body ({exc})")
            return self._fallback(0, f"json: {exc}")

        if not isinstance(payload, dict):
            return self._fallback(0, "body is not an object")

        # 解析放在闸门之前：会话键（teamId / our_type）只有解析后才知道。
        # 这是纯 CPU 的 ~1ms，换来的是不必解析两遍。
        turn = model.load(payload)
        round_no = turn.round_no if turn is not None else _as_int(payload.get("roundNo"), 0)
        key = (turn.team_id, turn.our_type) if turn is not None else ("", "")

        epoch_before = self.state.epoch
        verdict, cached = self.state.begin(round_no, key)
        if self.state.epoch != epoch_before:
            # 换边 / 新一场：角色 id 会变，粘性状态必须整体作废。
            # 不清的话，`PlanMemory.jobs` 会把上一场的岗位安到本场的角色 id 上，
            # 症状是"换边后第一个工人死活不去筑墙"这种极难倒查的行为。
            self.memory.reset_match()
            self.sessions_seen += 1
        if verdict is CACHED and cached is not None:
            return cached
        if verdict is STALE:
            return self._fallback(round_no, "stale round")

        epoch = self.state.epoch
        decided = self._run_with_budget(turn, started)
        if decided is None:
            return self._fallback(round_no, "decision budget exceeded")

        result, decision = decided
        body = self._serialise(result, decision)
        if round_no > 0:
            self.state.commit(round_no, body, epoch)
        self.rounds_served += 1

        if turn is not None:
            self._log_round(turn, result, decision, body, started)
        return body

    # ── 决策预算 ────────────────────────────────────────────────────
    def _run_with_budget(
        self, turn: model.Turn | None, started: float
    ) -> tuple[commands.EncodeResult, Decision] | None:
        """在工作线程里跑决策，超预算就放弃。

        为什么值得多一个线程：5 秒是**硬闸门**，超了就是一次异常。
        而决策里将来会有 LLM 回复解析、技能库检索这类可能退化变慢的代码，
        一旦某回合慢下来，就地放弃比赌它能在 5 秒内跑完划算得多。
        """
        if turn is None:
            d = Decision(
                phases={"total": _ms(started, time.monotonic())},
                notes=["model.load() returned None"],
            )
            return commands.EncodeResult(), d

        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["value"] = self._decide(turn, started)
            except Exception as exc:  # noqa: BLE001 - planner 的任何 bug 都不能出局
                box["error"] = f"{type(exc).__name__}: {exc}"

        worker = threading.Thread(target=run, name="decide", daemon=True)
        worker.start()
        worker.join(timeout=self.budget_s)

        if worker.is_alive():
            console(f"app: decision budget {self.budget_s:.2f}s exceeded")
            return None
        if "error" in box:
            console(f"app: decide failed ({box['error']})")
            return None
        return box.get("value")

    def _decide(
        self, turn: model.Turn, started: float
    ) -> tuple[commands.EncodeResult, Decision]:
        """真正的流水线：决策 → 编码 → 校验。"""
        t1 = time.monotonic()
        decision = self._plan(turn)
        t2 = time.monotonic()
        result = commands.encode_all(decision.intents, turn)
        t3 = time.monotonic()

        decision.phases.update(
            plan=_ms(t1, t2), encode=_ms(t2, t3), total=_ms(started, t3)
        )
        if turn.anomalies:
            decision.notes.append(f"payload anomalies: {len(turn.anomalies)}")
        return result, decision

    def _plan(self, turn: model.Turn) -> Decision:
        """跑 `domain/planner.py`，拿到本回合的 Intent 列表。

        ⚠️ `self.memory` 是**跨回合且可变**的，但它只在决策线程里被读写 ——
        而 `_run_with_budget` 保证同一时刻只有一个决策线程在跑（它 join 了）。
        真正并发的只有 HTTP 线程，它们在 `MatchState.begin` 就被回合闸门挡住了。
        """
        d = Decision()
        d.intents = list(planner.plan(turn, self.memory))
        d.notes.append(
            f"round={turn.round_no} night={calendar.is_night(turn.round_no)} "
            f"intents={len(d.intents)}"
        )
        return d

    # ── 出参与日志 ──────────────────────────────────────────────────
    def _serialise(self, result: commands.EncodeResult, decision: Decision) -> bytes:
        """三个顶层字段**永远都在**。demo 只发了 `roleCommandMap` 一个。"""
        body = {
            "roleCommandMap": result.commands,
            "prompt": decision.prompt or "",
            "executeCmd": decision.execute_cmd or "",
        }
        return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _fallback(self, round_no: int, why: str) -> bytes:
        self.fallbacks += 1
        console(f"app: fallback at round {round_no} ({why})")
        return server.FALLBACK_BODY

    def _log_round(
        self,
        turn: model.Turn,
        result: commands.EncodeResult,
        decision: Decision,
        body: bytes,
        started: float,
    ) -> None:
        if self.log is None:
            return
        record: dict[str, Any] = {
            "roundNo": turn.round_no,
            "within": calendar.within(turn.round_no),
            "night": calendar.is_night(turn.round_no),
            "elapsedMs": round(_ms(started, time.monotonic()), 2),
            "phases": decision.phases,
            "session": self.state.snapshot(),
            "notes": decision.notes,
            "rejected": result.rejected,
            "warnings": result.warnings,
            "commandCount": len(result.commands),
            "response": body.decode("utf-8"),
        }
        if result.rejected or result.warnings:
            console(
                f"round {turn.round_no}: rejected={len(result.rejected)} "
                f"warn={len(result.warnings)}"
            )
        self.log.submit(record)

    # ── 供 smoke / replay 直接调用 ──────────────────────────────────
    def decide_only(self, payload: dict[str, Any]) -> Decision:
        """只跑决策、不落盘、不碰状态。`tools/smoke.py` 与 `tools/replay.py` 用。"""
        turn = model.load(payload)
        if turn is None:
            d = Decision()
            d.notes.append("model.load() returned None")
            return d
        return self._plan(turn)


# ══ 小工具 ═══════════════════════════════════════════════════════════
def _ms(a: float, b: float) -> float:
    return round((b - a) * 1000.0, 3)


def _as_int(v: Any, default: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        return default
    return v


def run(port: int, host: str = "0.0.0.0") -> None:
    app = App()
    console(f"coregeek starting | log={config.LOG_DIR} encrypt={config.LOG_ENCRYPT}")
    server.serve(app, port, host)
