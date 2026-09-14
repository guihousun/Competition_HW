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
the ring, and a timeout scoring the best pass rate submitted so far.
"""
from __future__ import annotations

import random
from copy import deepcopy
from typing import Any

from .protocol import Pos, distance
from .tasks import REFRESH_ROUNDS, HOLD_RANGE, TaskPipeline

# Local task generation parameters (fixtures, not official numbers). The point
# budget is set high enough that tasks stay available across a 1300-round match:
# with only a handful per point the demo ran out of tasks half way through and
# the task loop had nothing left to exercise.
TASKS_PER_POINT = 30
DEFAULT_TIMEOUT = 25
POINT_KINDS = ("自进化类1", "自进化类2")

_TEMPLATES = (
    ("机房巡检", "机房：A3 温度：{a} 湿度：{b}", "机房={room}; 温度={a}; 湿度={b}"),
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
                   + description_template.format(**fields))
    return description, answer_template.format(**fields)


def new_world(state: dict[str, Any]) -> dict[str, Any]:
    """Deterministic local task world for one generated scenario."""
    seed = int(((state.get("_demo") or {}).get("seed")) or 1)
    rng = random.Random(seed * 7919 + 13)
    points: dict[str, dict[str, Any]] = {}
    for team in ("challenger", "defender"):
        for index in (1, 2):
            key = f"{team}TaskPoint{index}"
            points[key] = {
                "tasks_left": TASKS_PER_POINT,
                "cooldown": 0,
                "active": None,
            }
    return {"seed": seed, "rng_state": rng.getstate(), "points": points}


def _point_key(zone_kind: str) -> str:
    return zone_kind


def _zone_lookup(state: dict[str, Any]) -> dict[str, Pos]:
    found: dict[str, Pos] = {}
    for zone in (state.get("mapInfo") or {}).get("zones") or ():
        kind = str(zone.get("neutralType", ""))
        if "TaskPoint" in kind:
            found[kind] = Pos.load(zone["pos"])
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


def player_tasks(state: dict[str, Any], world: dict[str, Any],
                 team: str) -> list[dict[str, Any]]:
    """The published ``playerTasks`` list for our team, rebuilt each round."""
    zones = _zone_lookup(state)
    tasks: list[dict[str, Any]] = []
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
            "scoreReward": int(active["score"]) if active else 50,
            "goldReward": int(active["gold"]) if active else 30,
            "isValid": bool(book["cooldown"] <= 0 and book["tasks_left"] > 0),
            "timeoutRounds": int(active["timeout"]) if active else DEFAULT_TIMEOUT,
        })
    return tasks


def _pass_rate(answer: str, expected: str) -> float:
    """字段级通过率（任务书 §六：正确字段数 / 全量字段数）。"""
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
            rate = _pass_rate(answer, active["answer"])
            active["best_rate"] = max(active["best_rate"], rate)
            if rate >= active["best_rate"]:
                active["best_answer"] = answer
            used = max(1, round_no - active["accepted"])
            # 任务书 §六: full credit adds 5 × 标准回合数 / 实际回合数 on top of
            # the reward; partial credit is the reward times the pass rate.
            if rate >= 1.0:
                score = int(active["score"] + 5 * active["timeout"] / used)
            else:
                score = int(active["score"] * rate)
            gold = int(active["gold"] * rate)
            state["teamOur"]["totalScore"] = int(state["teamOur"].get("totalScore") or 0) + score
            state["teamOur"]["goldNum"] = int(state["teamOur"].get("goldNum") or 0) + gold
            events.append(f"任务结算：通过率 {rate:.0%}，积分 +{score}，金币 +{gold}")
            report["rewards"] = {"rate": rate, "score": score, "gold": gold,
                                 "answer": answer, "expected": active["answer"],
                                 "bestRate": active["best_rate"]}
            active["pending_answer"] = None
            book["active"] = None
            book["cooldown"] = REFRESH_ROUNDS
            book["tasks_left"] = max(0, book["tasks_left"] - 1)
            report["ended"] = "completed"
            state["phaseTask"] = ""
            return report
        if expired or not in_ring:
            reason = "超时" if expired else "离开任务点范围"
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
        if world.pop('llm_demo_once', False):
            description = '【本地LLM演示】计算十七加二十五。返回一个键值对，字段名 result，值为阿拉伯整数，不要解释。'
            answer = 'result=42'
        events.append(f"任务生成序号 {ordinal}：{description.splitlines()[0]}")
        book["active"] = {
            "type": POINT_KINDS[index - 1],
            "accepted": round_no,
            "deadline": round_no + DEFAULT_TIMEOUT,
            "timeout": DEFAULT_TIMEOUT,
            "description": description,
            "answer": answer,
            "score": 50,
            "gold": 30,
            "best_rate": 0.0,
            "best_answer": "",
            "pending_answer": None,
        }
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
           "point_cells", "TASKS_PER_POINT", "DEFAULT_TIMEOUT"]
