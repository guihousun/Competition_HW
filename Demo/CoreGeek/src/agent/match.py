"""Two-team local match (任务书 §2, §4.2, §4.3).

The competition is 1v1: both teams receive the same round request and both return
commands for that same round, with no first-mover advantage. This module runs that
shape locally:

* one shared world holds both teams, so vision, occupancy and the settle pass all
  see the same board;
* each round, **both** sides are asked for commands from their own vision-filtered
  view — the request shape the judge would send each of them;
* the round is then settled once per side on the same world, so what one side does
  is visible to the other in the next round's request, and neither side's policy
  ever reads the other's hidden units.

What this is *not*: the official judge. Round settlement order, the anomaly limit
that stops scheduling, and the official victory rule are not reproduced — the
result is a local comparison of two strategies under the published rules, not an
official score. Anything this file reports must be labelled that way.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from . import planner, vision
from .brain import plan_for_state, respond
from .protocol import Pos, Turn
from .simulator import step

Policy = Callable[[dict[str, Any]], dict[str, Any]]

SIDE_A = "challenger"
SIDE_B = "defender"


def base_health(state: dict[str, Any], side: str) -> int:
    """Total health of a side's station, read from the world (not a view)."""
    team = state.get("teamOur") if (state.get("teamOur") or {}).get("type") == side \
        else state.get("teamEnemy")
    total = 0
    for role in (team or {}).get("roles") or ():
        if role.get("roleType") == "station":
            total += int(role.get("health") or 0)
    return total


def score_of(state: dict[str, Any], side: str) -> int:
    team = state.get("teamOur") if (state.get("teamOur") or {}).get("type") == side \
        else state.get("teamEnemy")
    return int((team or {}).get("totalScore") or 0)


class TwoTeamMatch:
    """Drives both teams over the same world, one official round at a time."""

    def __init__(self, world: dict[str, Any], *,
                 policies: dict[str, Policy] | None = None,
                 max_rounds: int = 1300):
        self.world = world
        self.max_rounds = max_rounds
        self.sides = ((world.get("teamOur") or {}).get("type") or SIDE_A,
                      (world.get("teamEnemy") or {}).get("type") or SIDE_B)
        # Each side gets its own planner memory; neither can read the other's.
        self.states = {side: planner.PlannerState() for side in self.sides}
        # Each side also needs its own simulator bookkeeping: the local task
        # fixture publishes phaseTask and playerTasks into `_demo`, and one shared
        # copy means whichever side settles last overwrites the other's published
        # request — a completed task would score into the wrong team's total.
        shared = world.get("_demo") or {}
        mirror = world.get("_mirror_demo")
        self.private: dict[str, dict[str, Any]] = {}
        # Which sides received their *own* recorded `_demo` (as opposed to a copy of
        # the first side's). A legacy layout without an owner may be adopted only
        # when it came from that side's own bookkeeping.
        self._own_layout_source: dict[str, bool] = {}
        for index, side in enumerate(self.sides):
            own = shared if index == 0 else (mirror if isinstance(mirror, dict) else None)
            # Deep copy: the fixed spawn pool and wave source are mutable local
            # state, and a shallow copy would let one side's night overwrite the
            # other side's pool or make both sides spawn from one base's columns.
            self.private[side] = deepcopy(own or shared or {})
            self._own_layout_source[side] = own is not None
        # A fixed spawn pool is per side (its own base's attack side). Rebuild any
        # pool that belongs to the other side instead of sharing it; a pool already
        # owned by this side is kept as-is, custom points included.
        for side in self.sides:
            self._ensure_spawn_layout(side)
        # The judge sends each team its own round-level fields (phaseTask,
        # playerTasks, lastCmdResult, lastRoundRoleActionResults). One shared copy
        # in the world means the side that settles last overwrites the other's
        # request, so one team ends up reading the other team's task text — or no
        # task at all — and its task loop stalls. Each side therefore keeps its own.
        self.published: dict[str, dict[str, Any]] = {side: {} for side in self.sides}
        self.policies = dict(policies or {})
        self.log: list[dict[str, Any]] = []
        self.finished = False
        self.winner: str | None = None
        self.reason = ""

    # -- per-side spawn isolation -----------------------------------------
    def _side_projection(self, side: str) -> dict[str, Any]:
        projection = vision.side_view(self.world, side, vision_filter=False,
                                      keep_private=True)
        projection["_demo"] = self.private[side]
        return projection

    def _expected_first_column(self, side: str) -> int | None:
        turn = Turn.load(self._side_projection(side))
        station = turn.station()
        if station is None:
            return None
        from .scenarios import spawn_column_pool
        first_x, _columns, _slots = spawn_column_pool(turn)
        return first_x

    def _ensure_spawn_layout(self, side: str) -> None:
        """Guarantee this side's private fixed pool belongs to *its* base.

        Ownership is carried by the layout's ``ownerTeam`` field, never inferred
        from the default column coordinate: a caller may legally configure a
        custom pool (say blue at x=25) that must survive. A pool with no owner is
        adopted only when it came from this side's own bookkeeping (a legacy
        snapshot), and never rebuilt just to look newer. A pool owned by the other
        side is rebuilt for this side.
        """
        from .scenarios import SPAWN_SCHEMA, configure_spawns
        private = self.private[side]
        layout = private.get("spawn_layout")
        if isinstance(layout, dict) and layout.get("schema") == SPAWN_SCHEMA:
            owner = layout.get("ownerTeam")
            if owner == side:
                return
            if owner in (None, "") and self._own_layout_source.get(side):
                layout["ownerTeam"] = side  # legacy pool from this side's own state
                return
        if self._expected_first_column(side) is None:
            return
        configure_spawns(self._side_projection(side))

    # -- one round ---------------------------------------------------------
    def request_for(self, side: str) -> dict[str, Any]:
        """The request the judge would send this side (vision filtered).

        The round's published fields are overlaid from this side's own copy, so a
        team reads the task text and command results the judge sent *it*.
        """
        payload = vision.side_view(self.world, side)
        for key, value in self.published.get(side, {}).items():
            payload[key] = value
        return payload

    def commands_for(self, side: str, payload: dict[str, Any]) -> dict[str, Any]:
        """The command map for one side.

        ``respond`` returns the whole response envelope (``roleCommandMap`` plus
        the optional judge channels), so the map is taken out of it here — handing
        the envelope to the settle pass would make every unit id look unknown.
        """
        policy = self.policies.get(side)
        response = policy(payload) if policy is not None else respond(payload)
        if not isinstance(response, dict):
            return {}
        return dict(response.get("roleCommandMap") or {})

    def round(self) -> dict[str, Any]:
        """Collect both sides' commands, then settle the round for both.

        Both command sets are gathered from the *same* snapshot before either is
        settled, which is what "no first-mover advantage" (任务书 §2) means here:
        neither policy can react to the other's move in the same round.

        Settlement then runs once per side on that snapshot, and each side's own
        team object is written back to the shared world. Keeping the two writes
        separate is deliberate: gold, backpacks, score and unit lists belong to one
        team, and merging two settle passes field by field is how a two-team loop
        silently corrupts state.
        """
        if self.finished:
            return self.result()
        round_no = int(self.world.get("roundNo") or 1)
        snapshot = {key: value for key, value in self.world.items()}
        requests = {side: self.request_for(side) for side in self.sides}
        commands = {side: self.commands_for(side, payload)
                    for side, payload in requests.items()}

        executed: dict[str, Any] = {}
        events: list[str] = []
        for side in self.sides:
            # keep_private: the local settle pass needs the simulator's own
            # bookkeeping. Without it the wave generator treats the projection as
            # an imported snapshot and spawns no robots and no mines, so a
            # two-team match would run with no pressure on either side.
            projection = vision.side_view(snapshot, side, vision_filter=False,
                                          keep_private=True)
            projection["_demo"] = self.private[side]
            outcome = step(projection, commands[side])
            settled = outcome["state"]
            self.world[self._slot(side)] = settled.get("teamOur") or {}
            other = self._slot(side, other=True)
            if not (self.world.get(other) or {}).get("roles"):
                self.world[other] = settled.get("teamEnemy") or {}
            # Everything else the judge publishes this round (phaseTask,
            # lastCmdResult, llmResp, the round's action results) has to come back
            # too. Keeping only the team object silently dropped them, and the task
            # pipeline — which reads phaseTask and feeds lastCmdResult back to its
            # solver — then sat waiting for an answer that could never arrive.
            for key, value in settled.items():
                if key in ('teamOur', 'teamEnemy'):
                    continue
                if key == '_demo':
                    self.private[side] = value
                    if not self.private[side].get('seed'):
                        self.private[side]['seed'] = snapshot.get('_demo', {}).get('seed')
                    continue
                self.world[key] = value
                # Remember what this side was told, so its next request is the one
                # the judge would have sent it rather than whatever the other side
                # settled afterwards.
                if key.startswith('last') or key in ('phaseTask', 'llmResp', 'robot',
                                                     'mapInfo', 'robotAtkInfo',
                                                     'lastSummonTreasureResult'):
                    self.published[side][key] = value
            executed[side] = outcome.get("executed") or {}
            events.extend(f"[{side}] {line}" for line in outcome.get("events") or ())
        # The world's own metadata mirrors the first side's, so the viewer and the
        # single-team diagnostics keep their familiar shape; the other side's copy
        # is parked under a separate key that no policy request ever reads.
        self.world["_demo"] = dict(self.private[self.sides[0]])
        self.world["_mirror_demo"] = dict(self.private[self.sides[1]])

        self.world["roundNo"] = round_no + 1
        entry = {
            "round": round_no,
            "score": {side: score_of(self.world, side) for side in self.sides},
            "baseHp": {side: base_health(self.world, side) for side in self.sides},
            "commands": {side: sorted(commands[side]) for side in self.sides},
            "executed": {side: sorted(executed[side]) for side in self.sides},
            "events": events,
        }
        self.log.append(entry)
        self._check_end(round_no)
        return entry

    def _slot(self, side: str, *, other: bool = False) -> str:
        """Which key of the world holds this side's team object."""
        first = (self.world.get("teamOur") or {}).get("type")
        is_first = (side == first) != other
        return "teamOur" if is_first else "teamEnemy"

    def _check_end(self, round_no: int) -> None:
        """End conditions the published material states: a broken base, or the
        round limit (任务书 §4.2: 130 rounds per day over 10 days)."""
        alive = {side: base_health(self.world, side) for side in self.sides}
        dead = [side for side, hp in alive.items() if hp <= 0]
        if dead and len(dead) < len(self.sides):
            self.finished = True
            self.winner = next(side for side in self.sides if side not in dead)
            self.reason = f"{'、'.join(dead)} 基地被摧毁"
        elif round_no >= self.max_rounds:
            self.finished = True
            self.reason = f"达到 {self.max_rounds} 回合上限"

    # -- results -----------------------------------------------------------
    def run(self, rounds: int | None = None) -> dict[str, Any]:
        limit = rounds if rounds is not None else self.max_rounds
        while not self.finished and len(self.log) < limit:
            self.round()
        return self.result()

    def result(self) -> dict[str, Any]:
        scores = {side: score_of(self.world, side) for side in self.sides}
        hp = {side: base_health(self.world, side) for side in self.sides}
        if self.winner is None and self.finished:
            # A draw is a real outcome in a 1v1 with simultaneous scheduling.
            self.winner = max(self.sides, key=lambda side: (scores[side], hp[side])) \
                if scores[self.sides[0]] != scores[self.sides[1]] else None
        return {
            "finished": self.finished,
            "rounds": len(self.log),
            "scores": scores,
            "baseHp": hp,
            "winner": self.winner,
            "reason": self.reason or ("进行中" if not self.finished else "回合结束"),
            "local": True,
        }

    def frame(self) -> dict[str, Any]:
        """A viewer-friendly snapshot of both sides, from the world itself."""
        from .simulator import frame_view
        view = frame_view(self.world)
        return {
            "roundNo": int(self.world.get("roundNo") or 1),
            "scores": {side: score_of(self.world, side) for side in self.sides},
            "baseHp": {side: base_health(self.world, side) for side in self.sides},
            "vision": {side: vision.contact_report(vision.side_view(self.world, side))
                       for side in self.sides},
            "view": view,
        }


__all__ = ["SIDE_A", "SIDE_B", "TwoTeamMatch", "base_health", "score_of"]
