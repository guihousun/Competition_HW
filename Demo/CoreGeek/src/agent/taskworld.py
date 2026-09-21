"""Local stand-in for the judge's task machinery (demo fixture, not a rule).

The official judge owns task points, cooldowns, task texts, answer checking and
rewards. None of that is published, so this module **simulates** just enough of
it to exercise our side of the loop end to end: accept a task, hold the ring,
receive a description, submit an answer, get a pass rate and a reward, wait out
the 30-round refresh.

It is deliberately labelled a fixture:

* task texts are generated deterministically from the seed, so runs reproduce;
* the answer key is derived from the generated text, so the pass rate is honest
  about whether the strategy actually read the task;
* rewards use the published shape (``scoreReward`` / ``goldReward`` /
  ``timeoutRounds``) but the numbers are local, like the wave counts.

Official structure that *is* modelled exactly: 2 points per team, point 2 spans
two cells, 30-round refresh after an ending, acceptance only by a pioneer inside
the ring, and a timeout scoring the best pass rate submitted so far. The local
new-game budget is two points with three opportunities each. The timeout and
reward defaults below are observed public metadata from Issues 21/23/24, not
universal official constants; explicit case metadata remains authoritative.
"""
from __future__ import annotations

import random
import json
from copy import deepcopy
from typing import Any

from .protocol import Pos, distance, TWO_CELL_TASK_TYPES
from .tasks import REFRESH_ROUNDS, HOLD_RANGE, TaskPipeline

# Local task generation parameters (fixtures, not universal official numbers).
# The current observed profile has three finite opportunities at each of the
# two self-evolution points.  A point's cooldown only delays its next available
# opportunity; it never restores an opportunity that has already ended.
TASKS_PER_POINT = 3
DEFAULT_TIMEOUT = 15
DEFAULT_SCORE = 80
DEFAULT_GOLD = 80

# Explicit legacy worlds/replays are kept readable.  They are selected only by
# an old world with no rules metadata or by an explicit opt-in at construction;
# a new world never falls back to these values.
LEGACY_PROFILE = "legacy-local-v1"
LEGACY_TASKS_PER_POINT = 30
LEGACY_TIMEOUT = 25
LEGACY_SCORE = 50
LEGACY_GOLD = 30
TASK_WORLD_SCHEMA = "local-task-world/2"
OBSERVED_PROFILE = "observed-local-v2"
POINT_KINDS = ("自进化类1", "自进化类2")

_TEMPLATES = (
    ("机房巡检", "机房：{room} 温度：{a} 湿度：{b}", "机房={room}; 温度={a}; 湿度={b}"),
    ("接口探测", "服务：{room} 端口：{a} 协议：{b}", "服务={room}; 端口={a}; 协议={b}"),
    ("能耗统计", "区域：{room} 用电：{a} 用水：{b}", "区域={room}; 用电={a}; 用水={b}"),
)
_ROOMS = ("A3", "B7", "C1", "D9")
_PROTOCOLS = ("tcp", "udp", "http")


def _payload_for(rng: random.Random) -> tuple[str, str]:
    """Return (description, official-correct answer) for one generated task.

    The two templates must describe the same fields, otherwise the local fixture
    would grade a correct strategy as wrong. Both are filled from one dict.
    """
    title, description_template, answer_template = rng.choice(_TEMPLATES)
    fields = {"room": rng.choice(_ROOMS), "a": rng.randint(10, 99), "b": rng.randint(10, 99)}
    description = (f"【{title}】请读取下列现场信息并回报：\n"
                   + description_template.format(**fields)
                   + "\n作答格式：保留原中文字段名，将每个字段名与对应值用半角等号连接，"
                     "用半角分号分隔多个字段。只提交这些键值对，不要输出解释或其他内容。")
    return description, answer_template.format(**fields)


def new_world(state: dict[str, Any]) -> dict[str, Any]:
    """Deterministic local task world for one generated scenario."""
    seed = int(((state.get("_demo") or {}).get("seed")) or 1)
    rng = random.Random(seed * 7919 + 13)
    # The legacy profile is an explicit compatibility choice for local fixtures
    # that need to reproduce a pre-v2 world.  It is never inferred for a new
    # scenario from the absence of public task metadata.
    requested_profile = ((state.get("_demo") or {}).get("task_world_profile"))
    if requested_profile == LEGACY_PROFILE:
        profile = LEGACY_PROFILE
        tasks_per_point, timeout, score, gold = (
            LEGACY_TASKS_PER_POINT, LEGACY_TIMEOUT, LEGACY_SCORE, LEGACY_GOLD)
    else:
        profile = OBSERVED_PROFILE
        tasks_per_point, timeout, score, gold = (
            TASKS_PER_POINT, DEFAULT_TIMEOUT, DEFAULT_SCORE, DEFAULT_GOLD)
    points: dict[str, dict[str, Any]] = {}
    for team in ("challenger", "defender"):
        for index in (1, 2):
            key = f"{team}TaskPoint{index}"
            points[key] = {
                "tasks_left": tasks_per_point,
                "cooldown": 0,
                "active": None,
            }
    return {
        "seed": seed,
        "rng_state": rng.getstate(),
        "points": points,
        "rules": {
            "schema": TASK_WORLD_SCHEMA,
            "profile": profile,
            "tasks_per_point": tasks_per_point,
            "timeout_rounds": timeout,
            "score_reward": score,
            "gold_reward": gold,
        },
    }


def _point_key(zone_kind: str) -> str:
    return zone_kind


def _zone_lookup(state: dict[str, Any]) -> dict[str, Pos]:
    found: dict[str, Pos] = {}
    for zone in (state.get("mapInfo") or {}).get("zones") or ():
        kind = str(zone.get("neutralType", ""))
        if "TaskPoint" in kind:
            pos = Pos.load(zone["pos"])
            previous = found.get(kind)
            # Explicit horizontal point-2 cells share one left-hand anchor.
            # Input order must not shift its region to the right by one cell.
            if (kind in TWO_CELL_TASK_TYPES and previous is not None
                    and pos.y == previous.y and abs(pos.x - previous.x) == 1):
                found[kind] = min((previous, pos), key=lambda cell: cell.x)
            else:
                found[kind] = pos
    return found


def point_cells(kind: str, pos: Pos) -> tuple[Pos, ...]:
    """Task point 2 occupies two cells (任务书 §4.6.2)."""
    if kind.endswith("TaskPoint2"):
        return (pos, Pos(pos.x + 1, pos.y))
    return (pos,)


def _find_pioneer(state: dict[str, Any]) -> dict[str, Any] | None:
    for role in state["teamOur"].get("roles") or ():
        if role.get("roleType") == "pioneer" and int(role.get("health") or 0) > 0:
            return role
    return None


def _case_terms(case, world=None):
    """Resolve local terms, with explicit case metadata taking precedence.

    Worlds created before the rules metadata was added are legacy fixtures. We
    read them with their historical defaults without mutating the replay.
    """
    rules = (world or {}).get('rules') if isinstance(world, dict) else None
    if world is None:
        result = {'timeout': DEFAULT_TIMEOUT, 'score': DEFAULT_SCORE,
                  'gold': DEFAULT_GOLD}
    elif isinstance(rules, dict) and rules.get('schema') == TASK_WORLD_SCHEMA:
        result = {
            'timeout': int(rules.get('timeout_rounds') or DEFAULT_TIMEOUT),
            'score': int(rules.get('score_reward') or DEFAULT_SCORE),
            'gold': int(rules.get('gold_reward') or DEFAULT_GOLD),
        }
    else:
        result = {'timeout': LEGACY_TIMEOUT, 'score': LEGACY_SCORE,
                  'gold': LEGACY_GOLD}
    for target, source in (('timeout', 'timeout_rounds'), ('score', 'score_reward'), ('gold', 'gold_reward')):
        value = (case or {}).get(source)
        if type(value) is int and 1 <= value <= 10000:
            result[target] = value
    return result


def player_tasks(state: dict[str, Any], world: dict[str, Any],
                 team: str) -> list[dict[str, Any]]:
    """The published ``playerTasks`` list for our team, rebuilt each round."""
    zones = _zone_lookup(state)
    tasks: list[dict[str, Any]] = []
    suite = world.get('agent_cases') or []
    upcoming = suite[int(world.get('generated') or 0) % len(suite)] if suite else None
    terms = _case_terms(upcoming, world)
    for index in (1, 2):
        kind = f"{team}TaskPoint{index}"
        pos = zones.get(kind)
        if pos is None or kind not in world["points"]:
            continue
        book = world["points"][kind]
        active = book["active"]
        tasks.append({
            "taskType": active["type"] if active else POINT_KINDS[index - 1],
            "taskPosition": pos.dump(),
            "coldDownRounds": int(book["cooldown"]),
            "scoreReward": int(active["score"]) if active else terms['score'],
            "goldReward": int(active["gold"]) if active else terms['gold'],
            "isValid": bool(book["cooldown"] <= 0 and book["tasks_left"] > 0),
            "timeoutRounds": int(active["timeout"]) if active else terms['timeout'],
        })
    return tasks


def _pass_rate(answer: str, expected: str) -> float:
    """Local fixture field grading, not a published universal judge algorithm."""
    def fields(text: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for chunk in str(text).replace("；", ";").split(";"):
            if "=" in chunk:
                key, _, value = chunk.partition("=")
                out[key.strip()] = value.strip().lower()
        return out

    want = fields(expected)
    got = fields(answer)
    if not want:
        return 0.0
    hits = sum(1 for key, value in want.items() if got.get(key) == value)
    return hits / len(want)


def _grade(answer: str, active: dict[str, Any]) -> float:
    """Fixture-specific output contracts; never make malformed JSON pass."""
    mode = active.get('grading', 'fields')
    expected = active['answer']
    if mode == 'exact':
        return float(answer == expected)
    if mode == 'json_fields':
        try:
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError('duplicate key')
                    result[key] = value
                return result
            def reject_constant(value):
                raise ValueError('nonfinite constant')
            want = json.loads(expected, object_pairs_hook=unique, parse_constant=reject_constant)
            got = json.loads(answer, object_pairs_hook=unique, parse_constant=reject_constant)
            if not isinstance(want, dict) or not want or not isinstance(got, dict):
                return 0.0
            # Extra fields violate this fixture's explicit output contract.
            if set(got) - set(want):
                return 0.0
            return sum(k in got and type(got[k]) is type(v) and got[k] == v
                       for k, v in want.items()) / len(want)
        except (TypeError, ValueError, RecursionError):
            return 0.0
    if mode == 'fields':
        return _pass_rate(answer, expected)
    return 0.0  # unknown local judge contract cannot produce success


def advance(state: dict[str, Any], world: dict[str, Any], events: list[str]) -> dict[str, Any]:
    """Run the judge-side step for one round. Returns a report for the viewer.

    Idempotent per round: a round may be judged from more than one call site (the
    simulator's own loop and the judge-facing entry), and judging twice would
    regenerate a task text under a strategy that already answered the previous
    one. The second call returns the first call's report untouched.
    """
    round_no = int(state.get("roundNo") or 1)
    if int(world.get("judged_round") or 0) == round_no and world.get("last_report"):
        return world["last_report"]
    report = _advance_once(state, world, events)
    world["judged_round"] = round_no
    world["last_report"] = report
    return report


def _advance_once(state: dict[str, Any], world: dict[str, Any],
                  events: list[str]) -> dict[str, Any]:
    report: dict[str, Any] = {"accepted": None, "ended": None, "rewards": None,
                              "phaseTask": ""}
    team = state["teamOur"].get("type", "challenger")
    round_no = int(state.get("roundNo") or 1)
    zones = _zone_lookup(state)
    # 1. Cooldowns tick down before anything else.
    for kind, book in world["points"].items():
        if book["cooldown"] > 0:
            book["cooldown"] -= 1

    # 2. Continue or end an active task for our point.
    active_book = None
    for index in (1, 2):
        kind = f"{team}TaskPoint{index}"
        book = world["points"].get(kind)
        if book and book["active"]:
            active_book = (kind, book)
            break
    if active_book is not None:
        kind, book = active_book
        active = book["active"]
        pioneer = _find_pioneer(state)
        pos = zones.get(kind)
        in_ring = False
        if pioneer is not None and pos is not None:
            in_ring = min(distance(Pos.load(pioneer["pos"]), cell)
                          for cell in point_cells(kind, pos)) <= HOLD_RANGE
        expired = active["deadline"] <= round_no
        submitted = state["lastRoundRoleActionResults"]
        command = (state.get("_engine") or {}).get("lastCommands") or {}
        answer = active.get("pending_answer")
        if answer is not None:
            rate = _grade(answer, active)
            if rate >= active["best_rate"]:
                active["best_answer"] = answer
            active["best_rate"] = max(active["best_rate"], rate)
            active["pending_answer"] = None
            report["submission"] = {"rate": rate, "bestRate": active["best_rate"]}
            # An incorrect/partial answer is feedback, not a task ending
            # (任务书 §五、§六; 接口 §1.7 errorCode 2). Keep the task open
            # so a solver can inspect feedback and submit an improved answer.
            # Rates are local judge diagnostics, never new official fields.
            if rate < 1.0:
                state.setdefault("errors", []).append({
                    "errorCode": 2, "description": "答案不正确或不完全正确"})
                events.append("任务答案未完全正确，继续作答")
            if rate >= 1.0:
                used = max(1, round_no - active["accepted"])
                score = int(active["score"] + 5 * active["timeout"] / used)
                gold = int(active["gold"])
                state["teamOur"]["totalScore"] = int(state["teamOur"].get("totalScore") or 0) + score
                state["teamOur"]["goldNum"] = int(state["teamOur"].get("goldNum") or 0) + gold
                events.append(f"任务结算：通过率 {rate:.0%}，积分 +{score}，金币 +{gold}")
                report["rewards"] = {"rate": rate, "score": score, "gold": gold,
                                     "answer": answer, "expected": active["answer"],
                                     "bestRate": active["best_rate"]}
                book["active"] = None
                book["cooldown"] = REFRESH_ROUNDS
                book["tasks_left"] = max(0, book["tasks_left"] - 1)
                report["ended"] = "completed"
                state["phaseTask"] = ""
                return report
        if expired or not in_ring:
            reason = "超时" if expired else ("开拓者死亡" if pioneer is None else "离开任务点范围")
            if expired:
                state.setdefault("errors", []).append({
                    "errorCode": 1, "description": "任务超时"})
            if active["best_rate"] > 0:
                score = int(active["score"] * active["best_rate"])
                gold = int(active["gold"] * active["best_rate"])
                state["teamOur"]["totalScore"] = int(state["teamOur"].get("totalScore") or 0) + score
                state["teamOur"]["goldNum"] = int(state["teamOur"].get("goldNum") or 0) + gold
                events.append(f"任务{reason}：按最高通过率 {active['best_rate']:.0%} 结算，积分 +{score}，金币 +{gold}")
                report["rewards"] = {"rate": active["best_rate"], "score": score, "gold": gold,
                                     "answer": active["best_answer"], "expected": active["answer"],
                                     "bestRate": active["best_rate"]}
            else:
                events.append(f"任务{reason}：没有可结算的答案")
            book["active"] = None
            book["cooldown"] = REFRESH_ROUNDS
            book["tasks_left"] = max(0, book["tasks_left"] - 1)
            report["ended"] = reason
            state["phaseTask"] = ""
            return report
        state["phaseTask"] = active["description"]
        report["phaseTask"] = active["description"]
        # Record a submission made this round for next-round scoring.
        if submitted and pioneer is not None:
            pioneer_id = str(pioneer["id"])
            if submitted.get(pioneer_id):
                issued = command.get(pioneer_id) or {}
                if issued.get("action") == "submitAnswer":
                    active["pending_answer"] = str(issued.get("taskAnswer") or "")
        return report

    # 3. Accept a new task if a pioneer is standing on a ready point.
    pioneer = _find_pioneer(state)
    if pioneer is None:
        state["phaseTask"] = ""
        return report
    for index in (1, 2):
        kind = f"{team}TaskPoint{index}"
        book = world["points"].get(kind)
        pos = zones.get(kind)
        if book is None or pos is None or book["cooldown"] > 0 or book["tasks_left"] <= 0:
            continue
        if min(distance(Pos.load(pioneer["pos"]), cell)
               for cell in point_cells(kind, pos)) > HOLD_RANGE:
            continue
        accepted = state["lastRoundRoleActionResults"].get(str(pioneer["id"]))
        command = (state.get("_engine") or {}).get("lastCommands") or {}
        if not accepted or (command.get(str(pioneer["id"])) or {}).get("action") != "acceptTask":
            continue
        # Deterministic per task, not per round: the description and the answer
        # key must describe the same fields, so both come from one generation
        # keyed by the task's own ordinal.
        ordinal = int(world.get("generated") or 0)
        world["generated"] = ordinal + 1
        rng = random.Random(world["seed"] * 104729 + ordinal * 7717 + 13)
        description, answer = _payload_for(rng)
        # Explicit local task suites support actual document/query loops. The
        # private environment, answer and future cases stay out of observations.
        suite = world.get('agent_cases') or []
        case = deepcopy(suite[ordinal % len(suite)]) if suite else None
        if case:
            description, answer = case['description'], case['answer']
        terms = _case_terms(case, world)
        if world.pop('llm_demo_once', False):
            description = '【本地LLM演示】计算十七加二十五。返回一个键值对，字段名 result，值为阿拉伯整数，不要解释。'
            answer = 'result=42'
        events.append(f"任务生成序号 {ordinal}：{description.splitlines()[0]}")
        book["active"] = {
            "type": POINT_KINDS[index - 1],
            "accepted": round_no,
            "deadline": round_no + terms['timeout'],
            "timeout": terms['timeout'],
            "description": description,
            "answer": answer,
            "score": terms['score'],
            "gold": terms['gold'],
            "best_rate": 0.0,
            "best_answer": "",
            "pending_answer": None,
        }
        if case:
            book['active'].update(grading=case.get('grading', 'exact'),
                                  fixture_id=case['id'],
                                  sandbox_fixture=case.get('sandbox_fixture', {}))
        state["phaseTask"] = description
        events.append(f"接取任务：{description.splitlines()[0]}")
        report["accepted"] = description
        report["phaseTask"] = description
        return report
    state["phaseTask"] = ""
    return report


def attach(state: dict[str, Any]) -> None:
    """Create the local task world for a freshly generated scenario."""
    meta = state.setdefault("_demo", {})
    # Imported snapshots and recordings already carry the task world that was
    # observed/generated at that time. Never replace it with today's defaults;
    # doing so would silently change remaining opportunities and rewards.
    if isinstance(meta.get("task_world"), dict):
        return
    meta["task_world"] = new_world(state)
    state["teamOur"]["playerTasks"] = player_tasks(state, meta["task_world"],
                                                   state["teamOur"]["type"])


def snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """Small, viewer-friendly view of the local task world."""
    world = (state.get("_demo") or {}).get("task_world")
    if not world:
        return {}
    team = state["teamOur"].get("type", "challenger")
    out: dict[str, Any] = {"points": []}
    for index in (1, 2):
        kind = f"{team}TaskPoint{index}"
        book = world["points"].get(kind)
        if not book:
            continue
        active = book["active"] or {}
        out["points"].append({
            "kind": kind,
            "cooldown": book["cooldown"],
            "tasksLeft": book["tasks_left"],
            "active": bool(book["active"]),
            "phaseTask": active.get("description", ""),
            "deadline": active.get("deadline"),
        })
    return out


__all__ = ["new_world", "advance", "attach", "snapshot", "player_tasks",
           "point_cells", "TASKS_PER_POINT", "DEFAULT_TIMEOUT", "DEFAULT_SCORE",
           "DEFAULT_GOLD", "LEGACY_PROFILE"]
