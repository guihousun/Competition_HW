"""Official visibility (任务书 §4.3).

The request we receive is already filtered by the judge — our code should never
see an enemy unit it is not allowed to see. Publishing that rule locally keeps
three things honest:

* the strategy is only ever trained/tested on legal information, so a local run
  cannot quietly depend on a god view;
* the viewer can show *why* a unit is visible (shared vision vs. global), which is
  the difference between "the picture is empty" and "the picture is filtered";
* the rule lives in one place, so a future change to vision distance is a
  one-line change rather than a hunt through the simulator.

The rule, exactly as published:

* every friendly unit has vision 4 and the team shares it;
* enemy **bases and walls** are globally visible, regardless of distance;
* **robots** are globally visible;
* every other enemy unit is visible only while inside our shared vision.
"""
from __future__ import annotations

from typing import Any, Iterable

from .protocol import GLOBAL_VISIBLE_TYPES, VISION_DISTANCE, Pos, distance

# Why a unit appears in an observation (used by the viewer and by tests).
REASON_VISION = "vision"
REASON_GLOBAL_TYPE = "global-type"
REASON_ROBOT = "robot"
# Robot role types end with this suffix in the published protocol.
ROBOT_SUFFIX = "Robot"


def friendly_cells(state: dict[str, Any]) -> list[Pos]:
    """Cells covered by our shared vision: one entry per living friendly unit."""
    cells: list[Pos] = []
    for role in (state.get("teamOur") or {}).get("roles") or ():
        if int(role.get("health") or 0) <= 0:
            continue
        pos = role.get("pos") or {}
        try:
            cells.append(Pos(int(pos["x"]), int(pos["y"])))
        except (KeyError, TypeError, ValueError):
            continue
    return cells


def is_visible(unit: dict[str, Any], watcher_cells: Iterable[Pos], *,
               kind_field: str = "roleType") -> bool:
    """Whether one enemy unit is visible to a team holding `watcher_cells`."""
    kind = str(unit.get(kind_field) or "")
    if kind in GLOBAL_VISIBLE_TYPES or kind.endswith(ROBOT_SUFFIX):
        return True
    pos = unit.get("pos") or {}
    try:
        target = Pos(int(pos["x"]), int(pos["y"]))
    except (KeyError, TypeError, ValueError):
        return False
    return any(distance(cell, target) <= VISION_DISTANCE for cell in watcher_cells)


def visibility_reason(unit: dict[str, Any], watcher_cells: Iterable[Pos],
                      *, kind_field: str = "roleType") -> str:
    """``''`` when hidden, otherwise why it is in the observation."""
    kind = str(unit.get(kind_field) or "")
    if kind in GLOBAL_VISIBLE_TYPES:
        return REASON_GLOBAL_TYPE
    if kind.endswith(ROBOT_SUFFIX):
        return REASON_ROBOT
    cells = list(watcher_cells)
    if not is_visible(unit, cells, kind_field=kind_field):
        return ""
    return REASON_VISION


def visible_enemies(state: dict[str, Any]) -> list[dict[str, Any]]:
    """The enemy units the observation may contain, in their original order."""
    cells = friendly_cells(state)
    return [
        unit for unit in (state.get("teamEnemy") or {}).get("roles") or ()
        if is_visible(unit, cells)
    ]


def filter_observation(state: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the request with enemy units trimmed to what we may see.

    Only ``teamEnemy.roles`` is touched. Robots are globally visible and are left
    alone; so is ``mapInfo``, which is public terrain rather than a unit list.
    """
    trimmed = dict(state)
    enemy = state.get("teamEnemy") or {}
    visible = visible_enemies(state)
    if len(visible) != len(enemy.get("roles") or ()):
        trimmed["teamEnemy"] = dict(enemy, roles=visible)
    return trimmed


def contact_report(state: dict[str, Any]) -> dict[str, Any]:
    """Small summary for the viewer: what we can see and what we cannot."""
    cells = friendly_cells(state)
    roles = (state.get("teamEnemy") or {}).get("roles") or ()
    visible = [unit for unit in roles if is_visible(unit, cells)]
    return {
        "visionDistance": VISION_DISTANCE,
        "globalTypes": list(GLOBAL_VISIBLE_TYPES),
        "watchers": len(cells),
        "enemyTotal": len(roles),
        "enemyVisible": len(visible),
        "enemyHidden": len(roles) - len(visible),
        "visible": [
            {"id": unit.get("id"), "roleType": unit.get("roleType"),
             "reason": visibility_reason(unit, cells)}
            for unit in visible
        ],
    }


def side_view(state: dict[str, Any], side: str, *,
              vision_filter: bool = True,
              keep_private: bool = False) -> dict[str, Any]:
    """A request-shaped view of a two-team world from one side's perspective.

    ``state`` is a world where ``teamOur`` and ``teamEnemy`` each hold a real team
    (see :mod:`agent.match`). This function swaps them when asked for the other
    side, so the same policy engine can drive either team without a second
    implementation.

    ``vision_filter`` decides what happens to the opposing team's unit list:

    * ``True`` (default) — apply the side's own vision, for anything a *policy*
      will read. Neither side can see the other's hidden units or private fields.
    * ``False`` — keep the opponent's units intact. The local settle pass needs
      them as physical obstacles and targets; filtering there would let a role
      walk through an enemy it simply cannot see, which is a simulation artefact
      rather than the rule.

    ``keep_private`` keeps the simulator's own ``_``-prefixed bookkeeping (seed,
      pressure, wave counters). The settle pass needs it — without it the wave
      generator decides the scenario is an imported snapshot and spawns nothing,
      so a two-team match would run with no robots and no mines at all. Anything a
      policy reads must leave this ``False``.
    """
    if keep_private:
        source = state
    else:
        source = {key: value for key, value in state.items() if not key.startswith('_')}
    team = source.get("teamOur") or {}
    if side == team.get("type"):
        swapped = source
    else:
        swapped = dict(source)
        swapped["teamOur"] = source.get("teamEnemy") or {}
        swapped["teamEnemy"] = source.get("teamOur") or {}
    return filter_observation(swapped) if vision_filter else swapped


__all__ = [
    "REASON_GLOBAL_TYPE", "REASON_ROBOT", "REASON_VISION", "ROBOT_SUFFIX",
    "contact_report", "filter_observation", "friendly_cells", "is_visible",
    "side_view", "visibility_reason", "visible_enemies",
]
