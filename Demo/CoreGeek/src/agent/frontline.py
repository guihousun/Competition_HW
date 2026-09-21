"""Public-geometry center/front/corner and rear groups (R02/R03, D01).

The radius-two perimeter and approach are existing local layout assumptions,
not an assertion about undocumented official building-zone coordinates.
"""
from .defense_layout import footprint_bounds, primary_approach, WALL_RADIUS
from .protocol import Pos


def wall_groups(turn):
    """Return planned front-four and front-corner-two land cells, even unbuilt.

    Rotate the same six-cell edge for all four approaches; missing/non-land
    cells are omitted, never replaced with new cells outside the perimeter.
    """
    base = turn.station()
    if base is None:
        return (), ()
    xmin, xmax, ymin, ymax = footprint_bounds(base.pos)
    left, right = xmin - WALL_RADIUS, xmax + WALL_RADIUS
    bottom, top = ymin - WALL_RADIUS, ymax + WALL_RADIUS
    approach = primary_approach(base.pos, turn.width, turn.height)
    if approach in ('E', 'W'):
        x = right if approach == 'E' else left
        front = tuple(Pos(x, y) for y in range(bottom + 1, top))
        corners = (Pos(x, bottom), Pos(x, top))
    else:
        y = top if approach == 'N' else bottom
        front = tuple(Pos(x, y) for x in range(left + 1, right))
        corners = (Pos(left, y), Pos(right, y))
    return (tuple(p for p in front if turn.land(p)),
            tuple(p for p in corners if turn.land(p)))


def protected_walls(turn):
    """Reserved front-six positions, independent of standing-wall presence."""
    front, corners = wall_groups(turn)
    return set(front) | set(corners)


def center_walls(turn):
    """The two geometric middle front cells; do not shift around missing land."""
    base = turn.station()
    if base is None:
        return ()
    xmin, xmax, ymin, ymax = footprint_bounds(base.pos)
    approach = primary_approach(base.pos, turn.width, turn.height)
    if approach in ('E', 'W'):
        x = xmax + WALL_RADIUS if approach == 'E' else xmin - WALL_RADIUS
        cells = (Pos(x, ymin), Pos(x, ymax))
    else:
        y = ymax + WALL_RADIUS if approach == 'N' else ymin - WALL_RADIUS
        cells = (Pos(xmin, y), Pos(xmax, y))
    return tuple(p for p in cells if turn.land(p))


def rear_walls(turn):
    """Entire rear perimeter edge including its two corners, even if unbuilt.

    This is the row/column opposite primary_approach, not the rear half of each
    flank. Terrain omissions never create replacement wall cells elsewhere.
    """
    base = turn.station()
    if base is None:
        return ()
    xmin, xmax, ymin, ymax = footprint_bounds(base.pos)
    left, right = xmin - WALL_RADIUS, xmax + WALL_RADIUS
    bottom, top = ymin - WALL_RADIUS, ymax + WALL_RADIUS
    approach = primary_approach(base.pos, turn.width, turn.height)
    if approach in ('E', 'W'):
        x = left if approach == 'E' else right
        cells = tuple(Pos(x, y) for y in range(bottom, top+1))
    else:
        y = bottom if approach == 'N' else top
        cells = tuple(Pos(x, y) for x in range(left, right+1))
    return tuple(p for p in cells if turn.land(p))


def wall_tier(turn, pos):
    """Center two, outer front two, front corners, then all other walls."""
    front, corners = wall_groups(turn)
    return 0 if pos in center_walls(turn) else 1 if pos in front else 2 if pos in corners else 3
