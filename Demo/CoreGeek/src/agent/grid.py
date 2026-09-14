from heapq import heappop, heappush
from itertools import count

from .protocol import Pos, Turn, Unit, distance

_STEPS = (
    (-1, -1), (-1, 0), (-1, 1),
    (0, -1), (0, 1),
    (1, -1), (1, 0), (1, 1),
)

# Search bound for one step of one role. The map has 1312 cells, so this only
# trips when the goal is unreachable and A* would otherwise flood the map.
# Returning None then means "cannot reach in one turn", which is exactly the
# decision the caller needs; it is not a rule change. The bound is generous
# enough that every reachable goal inside the build ring resolves normally.
MAX_EXPANSIONS = 1500
# A task errand crosses most of the map past two teams' units, a wall ring and a
# wave of robots. The default budget is right for ordinary local moves (it keeps
# the per-round cost low) but can run out on those long routes, which silently
# turned into a "no route" answer and a pioneer that wandered to the map edge.
ERRAND_EXPANSIONS = 12000


def next_step(turn: Turn, moving: Unit, goal: Pos, *,
              max_expansions: int | None = None) -> Pos | None:
    """First step of a shortest route from `moving` to `goal`.

    Callers repeat this every round, so *which* shortest route matters: A* returns
    whichever optimal path it happens to expand first, and that can start with a
    step that leaves the distance to the goal unchanged (measured live as a pioneer
    that appeared to shuffle sideways). The returned step is therefore chosen among
    the optimal first steps by "does it visibly close the gap", so repeated calls
    walk a straight, monotone route instead of an arbitrary shortest one.
    """
    if moving.pos == goal:
        return None
    blocked = turn.blocked(moving)
    limit = max_expansions if max_expansions is not None else MAX_EXPANSIONS
    reachable = _reachable_in_one_hop(turn, moving.pos, blocked)
    order = count()
    start = moving.pos
    frontier: list[tuple[int, int, int, Pos]] = [(distance(start, goal), 0, next(order), start)]
    came_from: dict[Pos, Pos] = {}
    best = {start: 0}
    seen: set[Pos] = set()
    expansions = 0

    while frontier:
        _, cost, _, current = heappop(frontier)
        if current in seen:
            continue
        if current == goal:
            return _pick_first_step(turn, start, goal, cost, came_from, reachable,
                                    max(limit, turn.width * turn.height))
        seen.add(current)
        expansions += 1
        if expansions > limit:
            return None
        for dx, dy in _STEPS:
            step = Pos(current.x + dx, current.y + dy)
            if step in blocked or not turn.land(step):
                continue
            new_cost = cost + 1
            if new_cost >= best.get(step, new_cost + 1):
                continue
            best[step] = new_cost
            came_from[step] = current
            heappush(
                frontier,
                (
                    new_cost + distance(step, goal),
                    new_cost,
                    next(order),
                    step,
                ),
            )
    return None


def _reachable_in_one_hop(turn: Turn, start: Pos, blocked: set[Pos]) -> list[Pos]:
    neighbours = []
    for dx, dy in _STEPS:
        step = Pos(start.x + dx, start.y + dy)
        if step not in blocked and turn.land(step):
            neighbours.append(step)
    return neighbours


def _pick_first_step(turn: Turn, start: Pos, goal: Pos, best_cost: int,
                     came_from: dict[Pos, Pos], neighbours: list[Pos],
                     limit: int) -> Pos:
    """Choose among the optimal first steps the one that closes the most distance.

    Each candidate needs its own shortest-path cost, and that search must be given
    the same effort as the caller allowed: with the default cap on a long route the
    probe simply reports "no path" for every candidate, which makes them all look
    equal and defeats the tie-break entirely. Falls back to the A* answer so the
    result stays deterministic for a given board.
    """
    optimal = [
        step for step in neighbours
        if 1 + _cost_to_goal(turn, step, goal, limit) == best_cost
    ]
    if not optimal:
        return _first_step(came_from, start, goal)
    return min(optimal, key=lambda step: (distance(step, goal), step.x, step.y))


def _cost_to_goal(turn: Turn, start: Pos, goal: Pos, limit: int) -> int:
    """Shortest step count from `start` to `goal`, or a large number if none."""
    if start == goal:
        return 0
    blocked = turn.blocked(_Probe(start))
    order = count()
    frontier = [(distance(start, goal), 0, next(order), start)]
    best = {start: 0}
    seen: set[Pos] = set()
    expansions = 0
    while frontier:
        _, cost, _, current = heappop(frontier)
        if current in seen:
            continue
        if current == goal:
            return cost
        seen.add(current)
        expansions += 1
        if expansions > limit:
            break
        for dx, dy in _STEPS:
            step = Pos(current.x + dx, current.y + dy)
            if step in blocked or not turn.land(step):
                continue
            new_cost = cost + 1
            if new_cost >= best.get(step, new_cost + 1):
                continue
            best[step] = new_cost
            heappush(frontier, (new_cost + distance(step, goal), new_cost,
                                next(order), step))
    return 10 ** 9


class _Probe:
    """Stands in for the moving unit when asking which cells are blocked."""

    def __init__(self, pos: Pos):
        self.pos = pos


def _first_step(came_from: dict[Pos, Pos], start: Pos, goal: Pos) -> Pos:
    current = goal
    while came_from[current] != start:
        current = came_from[current]
    return current
