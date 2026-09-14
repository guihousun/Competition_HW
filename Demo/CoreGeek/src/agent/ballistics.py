"""Official ballistics for the two line-fire weapons (任务书 §4.5.4).

The task book specifies both weapons geometrically, not as point damage:

* **加特林炮台** — level-many bullets, each with its own aim point. The aim points
  must all fall inside one 90° cone (any two directions measured from the tower
  differ by ≤ 90°, otherwise the whole attack is illegal). Each bullet travels
  the straight line between the two cell centres and is consumed by the *nearest*
  robot on that line, dealing 10 damage.
* **电磁狙击炮** — exactly one aim point. Energy (10/20/30 by level) travels the
  line and penetrates: every live robot on the path takes ``min(energy, hp)``
  damage and the energy is reduced by the damage dealt, until it runs out or the
  path ends.
* **火箭发射台** — missiles are not blocked and land where aimed (handled in the
  simulator, unchanged here).

This module is pure geometry and returns structured hit records: no state is
mutated, so the same functions can drive the simulator, the strategy's target
choice, and the viewer's trajectory rendering without three implementations
drifting apart.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

from .protocol import Pos, TOWER_TYPES, distance

# Fractions of one cell: a robot counts as being on a line when its centre is
# within half a cell of it (the same tolerance the published diagrams imply for
# "the bullet hits what stands on its path").
PATH_HALF_WIDTH = 0.5
GATLING_BULLET_DAMAGE = 10
# 任务书 §4.5.4: any two aim directions may differ by at most this much.
CONE_HALF_ANGLE_DEG = 90.0
# 电磁狙击炮 energy by level (任务书 §4.5.1: 10 / 20 / 30).
RAILGUN_ENERGY_BY_LEVEL = (10, 20, 30)
# A gatling tower fires one bullet per level (任务书 §4.5.4).
GATLING_TARGETS_BY_LEVEL = (1, 2, 3)


def cone_legal(salvos: Sequence[Pos], tower: Pos) -> bool:
    """True when every pair of aim directions fits inside one 90° cone.

    The rule is about directions from the tower, not about the target cells, so
    two bullets aimed at the same cell (or at cells that happen to be close) can
    still be illegal when the tower sits far away. Duplicate directions are fine;
    a bullet aimed at the tower itself is not, because it has no direction.
    """
    if len(salvos) <= 1:
        return True
    vectors: list[tuple[float, float]] = []
    for salvo in salvos:
        dx = float(salvo.x - tower.x)
        dy = float(salvo.y - tower.y)
        if dx == 0.0 and dy == 0.0:
            return False
        vectors.append((dx, dy))
    for i in range(len(vectors)):
        for j in range(i + 1, len(vectors)):
            angle = angle_between(vectors[i], vectors[j])
            if angle > CONE_HALF_ANGLE_DEG + 1e-9:
                return False
    return True


def angle_between(first: tuple[float, float], second: tuple[float, float]) -> float:
    """Angle in degrees between two 2D vectors, in [0, 180]."""
    import math

    dot = first[0] * second[0] + first[1] * second[1]
    norm = math.hypot(*first) * math.hypot(*second)
    if norm == 0.0:
        return 180.0
    cosine = max(-1.0, min(1.0, dot / norm))
    return math.degrees(math.acos(cosine))


def point_segment_distance(point: tuple[float, float], start: tuple[float, float],
                           end: tuple[float, float]) -> float:
    """Distance from `point` to the segment start-end, in cell units."""
    import math

    px, py = point
    ax, ay = start
    bx, by = end
    dx, dy = bx - ax, by - ay
    length_squared = dx * dx + dy * dy
    if length_squared == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / length_squared
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _robot_view(robot: Any) -> dict[str, Any] | None:
    """Normalise a robot record to ``{id, pos, health}``.

    The simulator stores robots as raw observation dicts, while ``Turn`` exposes
    them as typed records; the geometry must not care which one it is handed.
    """
    if isinstance(robot, dict):
        pos = robot.get("pos") or {}
        if isinstance(pos, dict):
            return {"id": robot.get("id"), "health": robot.get("health"), "pos": pos}
        return None
    robot_id = getattr(robot, "robot_id", None) or getattr(robot, "id", None)
    pos = getattr(robot, "pos", None)
    health = getattr(robot, "health", None)
    if pos is None or robot_id is None:
        return None
    return {"id": robot_id, "health": health,
            "pos": {"x": getattr(pos, "x", None), "y": getattr(pos, "y", None)}}


def robots_on_path(tower: Pos, target: Pos, robots: Iterable[Any]) -> list[dict[str, Any]]:
    """Live robots standing on the tower→target line, nearest first.

    Ordering is by distance from the tower, then by robot id, so the result is
    deterministic for a given board — a requirement for reproducible replays and
    for tests to be able to hand-check the outcome.
    """
    start = (float(tower.x), float(tower.y))
    end = (float(target.x), float(target.y))
    hits: list[tuple[float, int, dict[str, Any]]] = []
    for raw in robots:
        robot = _robot_view(raw)
        if robot is None or int(robot.get("health") or 0) <= 0:
            continue
        pos = robot["pos"]
        try:
            centre = (float(pos.get("x")), float(pos.get("y")))
        except (TypeError, ValueError):
            continue
        offset = point_segment_distance(centre, start, end)
        if offset > PATH_HALF_WIDTH + 1e-9:
            continue
        along = distance(tower, Pos(int(centre[0]), int(centre[1])))
        hits.append((along, int(robot.get("id") or 0), robot))
    hits.sort(key=lambda item: (item[0], item[1]))
    return [robot for _along, _id, robot in hits]


def gatling_volley(tower: Pos, level: int, salvos: Sequence[Pos],
                   robots: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Resolve one gatling attack.

    Returns ``{'legal': bool, 'reason': str, 'shots': [...]}``. Each shot records
    the bullet's path and the robot it was consumed by (or ``None`` when the
    bullet flew through empty ground). A robot can stop several bullets — each
    bullet is resolved independently, exactly as the rule describes.
    """
    level = max(1, min(int(level or 1), len(GATLING_TARGETS_BY_LEVEL)))
    if len(salvos) != GATLING_TARGETS_BY_LEVEL[level - 1]:
        return {"legal": False,
                "reason": f"加特林 Lv{level} 需要 {GATLING_TARGETS_BY_LEVEL[level - 1]} 个落点",
                "shots": []}
    if not cone_legal(salvos, tower):
        return {"legal": False, "reason": "落点不在同一个 90° 锥形内", "shots": []}
    roster = list(robots)
    shots: list[dict[str, Any]] = []
    for salvo in salvos:
        on_path = robots_on_path(tower, salvo, roster)
        victim = on_path[0] if on_path else None
        shots.append({
            "cell": salvo.dump(),
            "path": [tower.dump(), salvo.dump()],
            "robot": int(victim["id"]) if victim else None,
            "damage": GATLING_BULLET_DAMAGE if victim else 0,
            "blocked": bool(victim),
        })
    return {"legal": True, "reason": "", "shots": shots}


def railgun_volley(tower: Pos, level: int, target: Pos,
                   robots: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Resolve one railgun attack with energy penetration.

    Energy is ``10/20/30`` for Lv1/2/3. Walking the path from the tower outward,
    each robot absorbs ``min(remaining energy, its current health)`` and the
    energy drops by the damage dealt; the shot stops when the energy is spent.
    """
    level = max(1, min(int(level or 1), len(RAILGUN_ENERGY_BY_LEVEL)))
    energy = RAILGUN_ENERGY_BY_LEVEL[level - 1]
    remaining = energy
    hits: list[dict[str, Any]] = []
    for robot in robots_on_path(tower, target, robots):
        if remaining <= 0:
            break
        health = int(robot.get("health") or 0)
        damage = min(remaining, health)
        if damage <= 0:
            continue
        hits.append({"robot": int(robot["id"]), "damage": damage,
                     "cell": dict(robot.get("pos") or {}),
                     "health": health, "energyLeft": remaining - damage})
        remaining -= damage
    return {"legal": True, "reason": "", "energy": energy, "remaining": remaining,
            "hits": hits, "path": [tower.dump(), target.dump()]}


def tower_volley(kind: str, tower: Pos, level: int, salvos: Sequence[Pos],
                 robots: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Dispatch to the right resolver; unknown kinds are refused, never guessed."""
    if kind == "gatling":
        return gatling_volley(tower, level, salvos, robots)
    if kind == "railgun":
        if len(salvos) != 1:
            return {"legal": False, "reason": "电磁狙击炮只能指定 1 个落点", "hits": []}
        return railgun_volley(tower, level, salvos[0], robots)
    if kind in TOWER_TYPES:
        # Rockets: aim points are not blocked, resolved by the caller.
        return {"legal": True, "reason": "", "missile": [salvo.dump() for salvo in salvos]}
    return {"legal": False, "reason": f"未知武器类型 {kind}", "shots": [], "hits": []}


def damage_map(volley: dict[str, Any]) -> dict[int, int]:
    """Total damage per robot id from a resolved volley (gatling or railgun)."""
    totals: dict[int, int] = {}
    for shot in volley.get("shots") or ():
        robot_id = shot.get("robot")
        if robot_id is not None and shot.get("damage"):
            totals[int(robot_id)] = totals.get(int(robot_id), 0) + int(shot["damage"])
    for hit in volley.get("hits") or ():
        robot_id = hit.get("robot")
        if robot_id is not None and hit.get("damage"):
            totals[int(robot_id)] = totals.get(int(robot_id), 0) + int(hit["damage"])
    return totals


def missing_robot_fields(robots: Iterable[dict[str, Any]]) -> list[str]:
    """Fields the ballistics need but the observation did not provide.

    Called by the simulator so a future protocol change shows up as a named gap
    instead of silently degrading to point damage.
    """
    required = ("id", "pos", "health")
    missing: list[str] = []
    for robot in robots:
        for field in required:
            if field not in robot and field not in missing:
                missing.append(field)
    return missing


__all__ = [
    "CONE_HALF_ANGLE_DEG", "GATLING_BULLET_DAMAGE", "PATH_HALF_WIDTH",
    "RAILGUN_ENERGY_BY_LEVEL", "angle_between", "cone_legal", "damage_map",
    "gatling_volley", "missing_robot_fields", "point_segment_distance",
    "railgun_volley", "robots_on_path", "tower_volley",
]
