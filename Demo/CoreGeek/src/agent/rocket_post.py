"""Reserve a common gun post without overriding tasks or active item use."""
from collections import deque

from . import home_defense, strategy_config
from .protocol import Pos, distance, move_command


def reserve(turn, commands, *, protected=()):
    config = strategy_config.get()
    if not config['enabled'] or not config['defense']['single_operator_three_rockets']:
        return []
    workers = turn.workers()
    guns = [g for g in turn.weapons() if g.kind == 'rocket']
    base = turn.station()
    if not workers or len(guns) != 3 or base is None:
        return []
    owner = workers[0].unit_id
    permanent = {p for u in turn.ours + turn.enemies if u.health > 0
                 and u.kind not in ('worker', 'pioneer') for p in turn.footprint(u)}
    inner = {Pos(x, y) for x in range(base.pos.x-1, base.pos.x+3)
             for y in range(base.pos.y-2, base.pos.y+2)
             if turn.land(Pos(x, y)) and Pos(x, y) not in permanent}
    common = {p for p in inner if all(distance(p, g.pos) == 1 for g in guns)}
    if not common:
        return []
    notes = []
    for role in turn.controllable():
        if role.unit_id == owner or role.unit_id in protected:
            continue
        current = commands.get(role.unit_id)
        landing = (Pos.load(current['targetPos'][0]) if current
                   and current.get('action') == 'move' else None)
        # Keep active tasks, repairs, trades and actual movement away from the
        # post. Idle occupants can vacate via the real rear opening by day.
        if current and (current.get('action') != 'move' or landing not in common):
            continue
        if role.pos not in common and landing not in common:
            continue
        claimed = {Pos.load(p) for uid, cmd in commands.items() if uid != role.unit_id
                   and cmd.get('action') in ('move', 'build') for p in cmd.get('targetPos', ())}
        blocked = turn.blocked(role) | claimed
        goals = inner - common - blocked
        queue, first = deque([role.pos]), {role.pos: None}
        confined = not turn.is_day and home_defense.inside(turn, role.pos)
        step = None
        while queue:
            cell = queue.popleft()
            if cell in goals:
                step = first[cell]
                break
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if not (dx or dy):
                        continue
                    nxt = Pos(cell.x+dx, cell.y+dy)
                    if (nxt in first or nxt in blocked or nxt in common
                            or not turn.land(nxt)
                            or (confined and not home_defense.inside(turn, nxt))):
                        continue
                    first[nxt] = nxt if cell == role.pos else first[cell]
                    queue.append(nxt)
        if step is not None:
            commands[role.unit_id] = move_command(step)
            notes.append({'role': role.unit_id, 'reason': 'yield_common_rocket_post'})
        elif landing in common:
            # Already on another safe inner cell: do not walk into the gunner's
            # post merely because an older return planner chose it as nearest.
            if role.pos in goals:
                commands.pop(role.unit_id, None)
            notes.append({'role': role.unit_id, 'reason': 'common_post_route_blocked'})
        else:
            notes.append({'role': role.unit_id, 'reason': 'common_post_cannot_vacate_safely'})
    return notes
