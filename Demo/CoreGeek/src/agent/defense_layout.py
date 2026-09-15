"""Defence layout: which side the enemy is expected from, and where walls/towers go.

Category: strategy optimization (local geometry prior). Rules referenced:
R02 (地图/视野/调度: 41x32, base 2x2 with ``pos`` the upper-left corner, Chebyshev
distance) and R03 (角色/建筑与资源: walls cost one stone, three towers globally,
towers need a controller in an adjacent cell).

What this module is allowed to know
-----------------------------------
Only **public observation geometry**: the map width/height and the base footprint
(``station_footprint``). The "primary approach" is a strategy *prior* derived from
"side bases expect a horizontal approach from the interior" — it is **not** knowledge of
future spawns, of the local simulator's wave generation, or of any hidden field
(no ``_demo``/seed/colour/robot type). It can be wrong for a given night; see the
limitations in ``docs/ISSUE_12_DEFENCE.md``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from .protocol import Pos

# Ring radius measured as Chebyshev distance from the base footprint (2x2).
# Kept identical to the previous wall ring so the footprint is unchanged (D01).
WALL_RADIUS = 2
# Weapon sites sit one cell outside the footprint, as the local build-region
# assumption (D01) has always had it.
WEAPON_RADIUS = 1
# Cells deliberately left open in the wall ring so our own roles can leave.
# Two cells only *reduce* the single-cell vulnerability: two robots can still
# block both, and one robot plus terrain can still trap a role. That is why the
# opening is also required to be terrain-connected *and* placed on the rear.
EXIT_WIDTH = 2
EAST, WEST, NORTH, SOUTH = "E", "W", "N", "S"
HORIZONTAL = (EAST, WEST)
VERTICAL = (NORTH, SOUTH)
# Wall-building priority per approach: front edge first, then the two flanks,
# then the rear. The rear is last because that is where the exit is cut.
SIDE_ORDER = {
    EAST: (EAST, NORTH, SOUTH, WEST),
    WEST: (WEST, NORTH, SOUTH, EAST),
    NORTH: (NORTH, EAST, WEST, SOUTH),
    SOUTH: (SOUTH, EAST, WEST, NORTH),
}
OPPOSITE = {EAST: WEST, WEST: EAST, NORTH: SOUTH, SOUTH: NORTH}


def primary_approach(base_pos: Pos, width: int, height: int) -> str:
    """Which map border the enemy is expected to arrive from, as a prior.

    Side bases expect an inward horizontal approach, consistent with the fixed
    approach reported in Issues 12/19. Vertical displacement must not rotate a
    right-side base's front north merely because it is near the bottom edge.
    A footprint intersecting the map's centre column uses the vertical interior
    direction instead. This is a strategy prior, not a claim about exact spawns.

    This reads only ``mapInfo.width/height`` and the base position, so it is
    stable across identical snapshots and does not change as towers/walls appear.
    """
    xs = [base_pos.x, base_pos.x + 1]
    ys = [base_pos.y - 1, base_pos.y]
    base_cx = (min(xs) + max(xs)) / 2.0
    base_cy = (min(ys) + max(ys)) / 2.0
    map_cx = (width - 1) / 2.0
    map_cy = (height - 1) / 2.0
    dx = map_cx - base_cx
    dy = map_cy - base_cy
    if max(xs) < map_cx:
        return EAST
    if min(xs) > map_cx:
        return WEST
    if dy:
        return NORTH if dy > 0 else SOUTH
    return EAST if dx >= 0 else WEST


def footprint_bounds(base_pos: Pos) -> tuple[int, int, int, int]:
    xs = [base_pos.x, base_pos.x + 1]
    ys = [base_pos.y - 1, base_pos.y]
    return min(xs), max(xs), min(ys), max(ys)


def _edge_cells(base_pos: Pos, side: str) -> tuple[Pos, ...]:
    """The ring cells on one side, in a canonical ascending order.

    Corners belong to the north/south rows, so the four sides partition the ring
    outline exactly once. North/south run left-to-right by x, east/west
    bottom-to-top by y.
    """
    xmin, xmax, ymin, ymax = footprint_bounds(base_pos)
    if side == SOUTH:
        return tuple(Pos(x, ymin - WALL_RADIUS)
                     for x in range(xmin - WALL_RADIUS, xmax + WALL_RADIUS + 1))
    if side == NORTH:
        return tuple(Pos(x, ymax + WALL_RADIUS)
                     for x in range(xmin - WALL_RADIUS, xmax + WALL_RADIUS + 1))
    if side == WEST:
        return tuple(Pos(xmin - WALL_RADIUS, y)
                     for y in range(ymin - WALL_RADIUS + 1, ymax + WALL_RADIUS))
    if side == EAST:
        return tuple(Pos(xmax + WALL_RADIUS, y)
                     for y in range(ymin - WALL_RADIUS + 1, ymax + WALL_RADIUS))
    raise ValueError(f"unknown side {side!r}")


def geometric_ring(base_pos: Pos) -> tuple[Pos, ...]:
    """Every perimeter cell, ignoring terrain (used to model a finished ring)."""
    return tuple(cell for side in (SOUTH, NORTH, WEST, EAST)
                 for cell in _edge_cells(base_pos, side))


def _footprint_distance_to(cell: Pos, base_pos: Pos) -> int:
    xmin, xmax, ymin, ymax = footprint_bounds(base_pos)
    dx = max(xmin - cell.x, 0, cell.x - xmax)
    dy = max(ymin - cell.y, 0, cell.y - ymax)
    return max(dx, dy)


def _footprint_cells(base_pos: Pos) -> set[Pos]:
    xmin, xmax, ymin, ymax = footprint_bounds(base_pos)
    return {Pos(x, y) for x in range(xmin, xmax + 1) for y in range(ymin, ymax + 1)}


@dataclass(frozen=True, slots=True)
class InteriorGraph:
    """The cells inside the ring, their adjacency, and which touch the opening.

    Built once per snapshot: the interior is at most twelve cells, so the
    per-candidate check below is a walk over a tiny graph instead of a fresh
    flood of the neighbourhood for every tower combination.
    """

    cells: tuple[Pos, ...]
    neighbours: dict[Pos, tuple[Pos, ...]]
    gateways: frozenset[Pos]


def interior_graph(base_pos: Pos, exit_cells: Iterable[Pos],
                   land: Callable[[Pos], bool]) -> InteriorGraph:
    """Model the cells one step out from the base and how they connect.

    Only cells at exactly ``WEAPON_RADIUS`` matter: with the ring finished, any
    route from the interior to the outside has to pass through an opening cell,
    so "an inner cell reaches the way out" is exactly "it reaches a cell next to
    the opening through inner cells".
    """
    opening = set(exit_cells)
    xmin, xmax, ymin, ymax = footprint_bounds(base_pos)
    cells = tuple(sorted(
        (Pos(x, y)
         for x in range(xmin - WEAPON_RADIUS, xmax + WEAPON_RADIUS + 1)
         for y in range(ymin - WEAPON_RADIUS, ymax + WEAPON_RADIUS + 1)
         if _footprint_distance_to(Pos(x, y), base_pos) == WEAPON_RADIUS
         and land(Pos(x, y))),
        key=lambda pos: (pos.x, pos.y)))
    members = set(cells)
    # Movement is eight-way (R02: Chebyshev distance), so a role may step
    # diagonally past a tower corner. Modelling the interior with orthogonal
    # steps only would call a cell trapped that a role can actually leave.
    neighbours = {
        cell: tuple(other for other in _neighbours_of(cell) if other in members)
        for cell in cells
    }
    gateways = frozenset(
        cell for cell in cells
        if any(neighbour in opening for neighbour in _neighbours_of(cell)))
    return InteriorGraph(cells=cells, neighbours=neighbours, gateways=gateways)


def _neighbours_of(cell: Pos) -> tuple[Pos, ...]:
    return tuple(Pos(cell.x + dx, cell.y + dy)
                 for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy)


def interior_reachability(graph: InteriorGraph,
                          obstacles: Iterable[Pos] = ()) -> tuple[bool, int]:
    """Whether our own buildings leave every inner cell a way out.

    ``exit_is_usable`` only asks whether the opening itself leads outside. Once
    towers (or terrain, or an earlier layout's walls) stand inside the ring they
    can cut the interior into pockets, and a role standing in one is trapped even
    though the opening is fine. Returns ``(every_inner_cell_reaches_a_gateway,
    trapped_count)``.
    """
    blocked = set(obstacles)
    reached = {cell for cell in graph.gateways if cell not in blocked}
    queue = list(reached)
    while queue:
        current = queue.pop()
        for neighbour in graph.neighbours.get(current, ()):
            if neighbour in reached or neighbour in blocked:
                continue
            reached.add(neighbour)
            queue.append(neighbour)
    usable = trapped = 0
    for cell in graph.cells:
        if cell in blocked:
            continue
        usable += 1
        if cell not in reached:
            trapped += 1
    return (usable > 0 and trapped == 0), trapped


def exit_is_usable(base_pos: Pos, exit_cells: Iterable[Pos],
                   land: Callable[[Pos], bool], *,
                   standing_walls: Iterable[Pos] = ()) -> bool:
    """Whether an opening really lets our roles out once the ring is finished.

    Being land is not enough: a rear opening can lead straight into water, a
    neutral zone or any other blocked strip, and a completed ring would then seal
    the interior. This models the *finished* ring — every perimeter cell is a
    wall except the opening — and floods over land from the opening, requiring it
    to reach both an interior cell and a cell just outside the ring. Walls that
    already stand are treated as blocked too, so an opening that a previous layout
    walled over does not count as usable.

    The flood is breadth-first and confined to the base's neighbourhood (footprint
    distance at most ``WALL_RADIUS + 2``), so it is a bounded local search rather
    than a walk over an unbounded plane, and it cannot be starved by diving away
    from the base.
    """
    from collections import deque

    opening = tuple(exit_cells)
    if not opening:
        return False
    standing = set(standing_walls)
    open_cells = [cell for cell in opening if cell not in standing]
    if not open_cells:
        return False
    blocked = (set(geometric_ring(base_pos)) - set(opening)) | standing
    horizon = WALL_RADIUS + 2
    seen: set[Pos] = set()
    queue: deque[Pos] = deque()
    for cell in open_cells:
        if land(cell) and cell not in blocked and cell not in seen:
            seen.add(cell)
            queue.append(cell)
    if not queue:
        return False
    reached_in = False
    reached_out = False
    while queue:
        current = queue.popleft()
        cell_distance = _footprint_distance_to(current, base_pos)
        if cell_distance <= WEAPON_RADIUS:
            reached_in = True
        if cell_distance > WALL_RADIUS:
            reached_out = True
        if reached_in and reached_out:
            return True
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                nxt = Pos(current.x + dx, current.y + dy)
                if nxt in seen or nxt in blocked or not land(nxt):
                    continue
                if _footprint_distance_to(nxt, base_pos) > horizon:
                    continue
                seen.add(nxt)
                queue.append(nxt)
    return reached_in and reached_out


def _side_candidates(base_pos: Pos, side: str, *,
                     land: Callable[[Pos], bool]) -> list[tuple[Pos, ...]]:
    """Openings on one side, most central first, then its single-cell fallback."""
    cells = tuple(cell for cell in _edge_cells(base_pos, side) if land(cell))
    ordered: list[tuple[Pos, ...]] = []
    seen: set[tuple[Pos, ...]] = set()
    for width in range(EXIT_WIDTH, 0, -1):
        if len(cells) < width:
            continue
        ideal = (len(cells) - width) / 2.0
        runs = []
        for start in range(len(cells) - width + 1):
            run = cells[start:start + width]
            if any(not _adjacent(a, b) for a, b in zip(run, run[1:])):
                continue
            runs.append((abs(start - ideal), start, run))
        for _offset, _start, run in sorted(runs):
            if run not in seen:
                seen.add(run)
                ordered.append(run)
    return ordered


def choose_exit(base_pos: Pos, side_order: Iterable[str], *,
                land: Callable[[Pos], bool] | None = None,
                standing_walls: Iterable[Pos] = ()) -> tuple[Pos, ...]:
    """The opening to leave unwalled, preferring the rear and a usable one.

    Candidates are every run of ``EXIT_WIDTH`` adjacent ring cells, then every
    single cell, on each side in the reverse of the wall priority (rear, flanks,
    front), ranked by how many of the run's cells are actually open — a cell a
    previous layout already walled over does not count, so a fully open flank
    beats a rear opening that is half bricked up — then by width, then rear-first
    preference, then centrality.

    Nothing is ever filled in here: a cell that already has a wall is simply never
    rebuilt, so a legacy layout cannot be made worse. An empty result only happens
    when the base has no perimeter at all.

    The search short-circuits on the first acceptable candidate, so the common
    case (the rear opening is clear) costs exactly one connectivity check.
    """
    keep = land if land is not None else (lambda _pos: True)
    standing = frozenset(standing_walls)
    preference = list(reversed(list(side_order)))
    candidates: list[tuple[tuple, tuple[Pos, ...]]] = []
    for pref_index, side in enumerate(preference):
        for order_index, run in enumerate(_side_candidates(base_pos, side, land=keep)):
            open_count = sum(1 for cell in run if cell not in standing)
            key = (-open_count, -len(run), pref_index, order_index)
            candidates.append((key, run))
    candidates.sort(key=lambda entry: entry[0])
    for key, run in candidates:
        open_count = -key[0]
        if open_count == 0:
            break  # nothing beyond this can be usable either
        if exit_is_usable(base_pos, run, keep, standing_walls=standing):
            return run
    return candidates[0][1] if candidates else ()


def ring_by_side(base_pos: Pos, *,
                 land: Callable[[Pos], bool] | None = None) -> dict[str, tuple[Pos, ...]]:
    """The intended radius-2 perimeter, grouped by side (corners in N/S rows)."""
    keep = land if land is not None else (lambda _pos: True)
    return {side: tuple(cell for cell in _edge_cells(base_pos, side) if keep(cell))
            for side in (SOUTH, NORTH, WEST, EAST)}


def ring_cells(base_pos: Pos, *, land: Callable[[Pos], bool] | None = None) -> tuple[Pos, ...]:
    """Every intended perimeter cell, in a stable order (S, N, W, E)."""
    grouped = ring_by_side(base_pos, land=land)
    return tuple(cell for side in (SOUTH, NORTH, WEST, EAST) for cell in grouped[side])


def _adjacent(a: Pos, b: Pos) -> bool:
    """Orthogonally adjacent (the exit is a straight opening)."""
    return abs(a.x - b.x) + abs(a.y - b.y) == 1


@dataclass(frozen=True, slots=True)
class Layout:
    """A deterministic defence layout for one base/map snapshot."""

    approach: str
    side_order: tuple[str, ...]
    ring: tuple[Pos, ...]
    exit_cells: tuple[Pos, ...]
    wall_order: tuple[Pos, ...]
    exit_usable: bool = True


def layout(base_pos: Pos, width: int, height: int, *,
           land: Callable[[Pos], bool] | None = None,
           standing_walls: Iterable[Pos] = ()) -> Layout:
    """Build the full layout for one base/map snapshot.

    ``ring`` is the intended perimeter filtered to land, ``exit_cells`` is the
    deliberate opening, and ``wall_order`` is exactly ``ring`` minus the opening
    in build priority order — so the ring has no accidental holes.

    ``land`` defaults to the map bounds, so an opening can never be placed off the
    map; a caller that passes its own predicate is expected to include the bounds
    check too (``Turn.land`` does).

    ``standing_walls`` lets the choice avoid an opening that is already walled
    (a ring from an earlier layout), so a usable exit is retained instead of the
    crew being sealed in.
    """
    walls = frozenset(standing_walls)
    in_bounds = (land if land is not None
                 else (lambda pos: 0 <= pos.x < width and 0 <= pos.y < height))
    approach = primary_approach(base_pos, width, height)
    side_order = SIDE_ORDER[approach]
    grouped = ring_by_side(base_pos, land=in_bounds)
    ring = tuple(cell for side in (SOUTH, NORTH, WEST, EAST) for cell in grouped[side])
    exit_cells = choose_exit(base_pos, side_order, land=in_bounds, standing_walls=walls)
    opening = set(exit_cells)
    wall_order = tuple(cell for side in side_order
                       for cell in grouped[side] if cell not in opening)
    usable = exit_is_usable(base_pos, exit_cells, in_bounds, standing_walls=walls)
    return Layout(approach=approach, side_order=side_order, ring=ring,
                  exit_cells=exit_cells, wall_order=wall_order, exit_usable=usable)


def side_of(cell: Pos, base_pos: Pos) -> tuple[str, ...]:
    """Which side(s) a cell lies on relative to the base footprint.

    Corner cells belong to two sides; the result is ordered so callers can rank a
    corner by the better of the two without ambiguity.
    """
    xmin, xmax, ymin, ymax = footprint_bounds(base_pos)
    sides: list[str] = []
    if cell.x >= xmax + 1:
        sides.append(EAST)
    elif cell.x <= xmin - 1:
        sides.append(WEST)
    if cell.y >= ymax + 1:
        sides.append(NORTH)
    elif cell.y <= ymin - 1:
        sides.append(SOUTH)
    return tuple(sides) or ("CENTER",)


def side_rank(cell: Pos, base_pos: Pos, side_order: tuple[str, ...]) -> tuple[int, int]:
    """(best rank among the cell's sides, corner penalty) for tower placement.

    A pure-side cell beats a corner with the same best rank, so towers prefer the
    middle of the approach-facing edge.
    """
    sides = side_of(cell, base_pos)
    ranks = [side_order.index(side) for side in sides if side in side_order]
    if not ranks:
        return len(side_order), 0
    return min(ranks), 0 if len(sides) == 1 else 1


def weapon_cells(base_pos: Pos, *, land: Callable[[Pos], bool] | None = None) -> tuple[Pos, ...]:
    """Cells one ring out from the footprint (the assumed weapon ring, D01)."""
    keep = land if land is not None else (lambda _pos: True)
    xmin, xmax, ymin, ymax = footprint_bounds(base_pos)
    cells = []
    for x in range(xmin - WEAPON_RADIUS, xmax + WEAPON_RADIUS + 1):
        for y in range(ymin - WEAPON_RADIUS, ymax + WEAPON_RADIUS + 1):
            pos = Pos(x, y)
            if _in_footprint(pos, base_pos):
                continue
            if keep(pos):
                cells.append(pos)
    return tuple(cells)


def _in_footprint(cell: Pos, base_pos: Pos) -> bool:
    xmin, xmax, ymin, ymax = footprint_bounds(base_pos)
    return xmin <= cell.x <= xmax and ymin <= cell.y <= ymax


def exit_guard_cells(base_pos: Pos, exit_cells: Iterable[Pos]) -> frozenset[Pos]:
    """Cells that must stay clear so the exit stays usable.

    A tower placed next to the opening would block the only way out for our own
    roles, so those cells are avoided when new tower sites are chosen.
    """
    guard: set[Pos] = set()
    for cell in exit_cells:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                guard.add(Pos(cell.x + dx, cell.y + dy))
    return frozenset(guard)
