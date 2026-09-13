#!/usr/bin/env python3
"""离线 fixture 驱动器：不起服务，直接调 `App.handle(bytes) -> bytes`。

设计见 docs/design/code-design.md §16 第 3 项。

    py -3.13 tools/smoke.py                    # 跑内置的全部 fixture
    py -3.13 tools/smoke.py docs/request.txt   # 用真实样例跑一次并打印报文
    py -3.13 tools/smoke.py --serve 8080       # 起真服务（等价 python main3.py 8080）

为什么要有它：判题器是个黑盒，我们没法"打一局试试"。这个驱动器是我们**唯一**
能在本地断言"这一回合的响应合法、不抛异常、远快于 5 秒"的地方。
它**不**验证策略好坏——那需要真实对局数据。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

from coregeek.app import App  # noqa: E402
from coregeek.protocol import commands, model  # noqa: E402

SAMPLE = ROOT / "docs" / "request.txt"

#: 5 秒是判题器的硬闸门；留 10 倍余量做本地警戒线
WARN_MS = 500


def _sample() -> dict:
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def _variants() -> list[tuple[str, dict]]:
    """内置 fixture：覆盖样例本身、边界回合、以及"字段大面积缺失"。"""
    base = _sample()
    out: list[tuple[str, dict]] = [("官方样例 roundNo=85（夜晚）", base)]

    def mutate(name: str, **changes: object) -> None:
        payload = json.loads(json.dumps(base))
        payload.update(changes)
        out.append((name, payload))

    mutate("白天 roundNo=1", roundNo=1)
    mutate("白天末 roundNo=70", roundNo=70)
    mutate("入夜 roundNo=71", roundNo=71)
    mutate("半场边界 roundNo=131", roundNo=131)
    mutate("满场 roundNo=1300", roundNo=1300)
    mutate("gold=0", **{"teamOur": {**base["teamOur"], "goldNum": 0}})
    mutate("敌方不可见（teamEnemy 为空）", teamEnemy={})

    out.append(("空对象", {}))
    out.append(("非对象", []))
    out.append(("缺 teamOur", {"roundNo": 1}))
    out.append(("roles 为空", {"roundNo": 1, "teamOur": {"roles": []}}))
    out.append(("字段大面积缺失", {"roundNo": 3, "mapInfo": {}, "teamOur": {"roles": []}}))
    return out


def _check(label: str, body: bytes) -> list[str]:
    """断言响应是合法报文。返回问题列表（空 = 通过）。"""
    problems: list[str] = []
    try:
        doc = json.loads(body.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [f"响应不是合法 JSON：{exc}"]

    if not isinstance(doc, dict):
        return ["响应不是 JSON 对象"]

    for field in ("roleCommandMap", "prompt", "executeCmd"):
        if field not in doc:
            problems.append(f"缺顶层字段 {field}")
    if not isinstance(doc.get("roleCommandMap"), dict):
        problems.append("roleCommandMap 不是对象")
    if not isinstance(doc.get("prompt"), str):
        problems.append("prompt 不是字符串")
    if not isinstance(doc.get("executeCmd"), str):
        problems.append("executeCmd 不是字符串")

    for key, cmd in (doc.get("roleCommandMap") or {}).items():
        try:
            key_int = int(key)
        except (TypeError, ValueError):
            problems.append(f"roleCommandMap 的 key {key!r} 不是角色 ID")
            continue
        action = cmd.get("action") if isinstance(cmd, dict) else None
        if action not in commands.ACTION_CODES:
            problems.append(f"角色 {key_int} 的 action {action!r} 不在动作码全集内")
        # 形状校验（不依赖 Turn，所以这里只做与回合无关的那部分）
        if action in ("move", "collect", "remove", "build"):
            tp = cmd.get("targetPos")
            if not isinstance(tp, list) or len(tp) != 1:
                problems.append(f"角色 {key_int} 的 {action} 需要恰好 1 个 targetPos")
    return problems


def _fresh() -> App:
    """每个 fixture 都是**独立的一场**，必须用新的 App。

    复用同一个 App 会把"上一场跑了到第 1300 回合"带进下一场，
    于是 roundNo=1 被当成过期回合——那是 fixture 设计错误，
    不是策略错误（这个错已经犯过一次，写在这里当路标）。
    """
    return App(log_dir=str(ROOT / ".tmp-logs"), enable_log=False)


def _post(app: App, payload: dict) -> tuple[bytes, float]:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    t0 = time.perf_counter()
    body = app.handle(raw)
    return body, (time.perf_counter() - t0) * 1000


def run_fixtures() -> int:
    failures = 0
    worst = 0.0

    for label, payload in _variants():
        app = _fresh()
        try:
            body, ms = _post(app, payload)
        except Exception as exc:  # noqa: BLE001 - 这里抛异常就是 bug
            print(f"FAIL  {label}: handle() 抛了 {type(exc).__name__}: {exc}")
            failures += 1
            continue
        worst = max(worst, ms)
        failures += _report(label, body, ms)

    for label, ok, detail in _state_cases():
        if ok:
            print(f"ok    {label}  ({detail})")
        else:
            failures += 1
            print(f"FAIL  {label}: {detail}")

    if worst > WARN_MS:
        print(f"\n⚠️  最慢一次 {worst:.1f}ms，超过警戒线 {WARN_MS}ms（硬闸门是 5000ms）")
    print(f"\n最慢一次 {worst:.1f}ms / 硬闸门 5000ms")
    return failures


def _report(label: str, body: bytes, ms: float) -> int:
    problems = _check(label, body)
    if problems:
        print(f"FAIL  {label}  ({ms:.1f}ms)")
        for p in problems:
            print(f"      {p}")
        return 1
    n = len(json.loads(body)["roleCommandMap"])
    print(f"ok    {label}  指令 {n} 条  {ms:.1f}ms")
    return 0


def _state_cases() -> list[tuple[str, bool, str]]:
    """跨回合状态机的用例。这一块错了**不会报异常**，只会静默地不出招。"""
    out: list[tuple[str, bool, str]] = []
    base = _sample()

    # ① 幂等：同一回合重复 POST 必须原样回放
    app = _fresh()
    a, _ = _post(app, base)
    b, _ = _post(app, base)
    out.append(("同回合重复 POST 幂等", a == b, f"两次响应{'一致' if a == b else '不一致'}"))

    # ② 过期回合：不能把状态回退
    app = _fresh()
    _post(app, base)                       # roundNo=85
    _post(app, {**base, "roundNo": 84})    # 倒退 1 回合 → 过期
    snap = app.state.snapshot()
    out.append(
        ("小幅倒退的过期回合被忽略", snap["lastRound"] == 85, f"lastRound={snap['lastRound']} stale={snap['stale']}")
    )

    # ③ roundNo 重置且换边：必须开新一场（换边是最可靠的信号）
    app = _fresh()
    _post(app, base)                       # challenger, roundNo=85
    swapped = {**base, "roundNo": 1, "teamOur": {**base["teamOur"], "type": "defender"}}
    _post(app, swapped)
    snap = app.state.snapshot()
    out.append(
        ("换边后 roundNo 重置 → 开新一场", snap["lastRound"] == 1 and snap["match"] == 2,
         f"lastRound={snap['lastRound']} match={snap['match']}")
    )

    # ④ roundNo 重置但 type 没变：兜底信号必须兜住
    #    ⚠️ 这条若失效，下半场会**整场回空指令**且日志无异常。
    #    判据用 `match`（开了第 2 场）而不是比对响应字节——第 4 步 planner 是空的，
    #    正常响应与兜底响应**字节完全相同**，拿字节比对是测不出来的。
    app = _fresh()
    _post(app, {**base, "roundNo": 1300})
    _post(app, {**base, "roundNo": 1})
    snap = app.state.snapshot()
    out.append(
        ("roundNo 重置但 type 不变 → 兜底开新一场",
         snap["lastRound"] == 1 and snap["match"] == 2 and snap["stale"] == 0,
         f"lastRound={snap['lastRound']} match={snap['match']} stale={snap['stale']}")
    )

    # ⑤ 并发同一回合：两个线程同时进来，都必须拿到合法响应
    app = _fresh()
    results: list[bytes] = []
    lock = threading.Lock()

    def hit() -> None:
        r, _ = _post(app, base)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=hit) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok = len(results) == 4 and all(not _check("concurrent", r) for r in results)
    out.append(("并发 4 个同回合请求全部返回合法报文", ok, f"{len(results)} 个响应"))
    return out


def run_one(path: Path) -> int:
    app = App(log_dir=str(ROOT / ".tmp-logs"), enable_log=False)
    raw = path.read_bytes()
    t0 = time.perf_counter()
    body = app.handle(raw)
    ms = (time.perf_counter() - t0) * 1000

    doc = json.loads(body.decode("utf-8"))
    turn = model.load(json.loads(raw.decode("utf-8")))

    print(f"fixture : {path}")
    if turn is not None:
        print(
            f"round   : {turn.round_no}  "
            f"our={turn.our_type}  gold={turn.gold}  "
            f"roles={len(turn.our_roles)}  robots={len(turn.robots)}"
        )
        if turn.anomalies:
            print(f"anomaly : {len(turn.anomalies)}")
            for a in turn.anomalies:
                print(f"          - {a}")
    print(f"elapsed : {ms:.2f}ms")
    print("response:")
    print(json.dumps(doc, ensure_ascii=False, indent=2))

    problems = _check(str(path), body)
    for p in problems:
        print(f"FAIL  {p}")
    return 1 if problems else 0


def main(argv: list[str]) -> int:
    if "--serve" in argv:
        port = int(argv[argv.index("--serve") + 1])
        from coregeek.app import run

        run(port)
        return 0

    if argv and not argv[0].startswith("-"):
        return run_one(Path(argv[0]).resolve())

    print("=== fixture 驱动器 ===")
    failures = run_fixtures()
    print(f"\n{'FAILED' if failures else 'ALL PASS'}  ({failures} 个问题)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
