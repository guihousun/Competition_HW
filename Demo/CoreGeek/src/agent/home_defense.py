"""Worker positioning inside the planned wall ring (strategy, R02/R03, D01)."""
from collections import deque

from .defense_layout import footprint_bounds
from .protocol import Pos


def inside(turn, pos):
    base = turn.station()
    if base is None:
        return False
    xmin, xmax, ymin, ymax = footprint_bounds(base.pos)
    return xmin - 1 <= pos.x <= xmax + 1 and ymin - 1 <= pos.y <= ymax + 1


def step_inside(turn, role, targets=None, claimed=()):
    """Nearest reachable interior goal; an inside worker never routes outside.

    Outside starts may use the observed rear opening. Other roles, walls and
    reserved landing cells remain obstacles; eight-way movement is unchanged.
    """
    if turn.station() is None:
        return None
    confined = inside(turn, role.pos)
    goals = None if targets is None else set(targets)
    blocked = turn.blocked(role) | set(claimed)
    queue = deque([role.pos])
    first = {role.pos: None}
    while queue:
        cell = queue.popleft()
        if inside(turn, cell) and (goals is None or cell in goals):
            return first[cell]
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if not (dx or dy):
                    continue
                nxt = Pos(cell.x + dx, cell.y + dy)
                if (nxt in first or nxt in blocked or not turn.land(nxt)
                        or (confined and not inside(turn, nxt))):
                    continue
                first[nxt] = nxt if cell == role.pos else first[cell]
                queue.append(nxt)
    return None


def tower_step(turn, role, tower, claimed=()):
    goals = [Pos(tower.pos.x + dx, tower.pos.y + dy)
             for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy]
    return step_inside(turn, role, goals, claimed)


def status(turn, commands, *, quiet=False):
    if turn.is_day:
        return []
    return [{'worker': r.unit_id,
             'state': ('inside_planned_ring' if inside(turn, r.pos) else
                       'outside_' + commands[r.unit_id]['action'] if r.unit_id in commands
                       else 'quiet_outside_hold' if quiet
                       else 'return_blocked' if turn.station() else 'base_missing')}
            for r in turn.workers()]
