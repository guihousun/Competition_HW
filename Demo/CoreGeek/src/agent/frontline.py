"""Public-geometry front-four and corner-two strategy groups (R02/R03, D01).

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


def wall_tier(turn, pos):
    """Zero for front-four, one for their corners, two for other walls."""
    front, corners = wall_groups(turn)
    return 0 if pos in front else 1 if pos in corners else 2
