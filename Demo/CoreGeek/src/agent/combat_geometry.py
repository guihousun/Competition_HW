"""Public-observation wall geometry shared by display simulator and risk checks."""

def intervening_wall(turn, origin, goal):
    """First live friendly wall intersected by the robot-to-role centre segment.

    S04: a wall screens roles from robot attacks. Corner-only contact is not a
    passage through a cell (local geometric convention, not an official detail).
    """
    hits = []
    for wall in turn.walls():
        enter, leave = 0.0, 1.0
        for start, delta, cell in ((origin.x, goal.x - origin.x, wall.pos.x),
                                   (origin.y, goal.y - origin.y, wall.pos.y)):
            if delta == 0:
                if not cell - .5 < start < cell + .5:
                    leave = -1
                    break
            else:
                lo, hi = sorted(((cell - .5 - start) / delta, (cell + .5 - start) / delta))
                enter, leave = max(enter, lo), min(leave, hi)
        if leave - enter > 1e-12 and leave > 0 and enter < 1:
            hits.append((enter, wall.unit_id, wall.pos))
    return min(hits)[2] if hits else None

