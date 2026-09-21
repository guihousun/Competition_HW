"""Reserve a common gun post without overriding tasks or active item use."""
from collections import deque

from . import home_defense, strategy_config
from .protocol import Pos, distance, move_command


def common_cells(turn):
    """Public geometry only; no transient role occupancy decides ownership."""
    # The compatibility flag historically said "three rockets", but the
    # current enabled strategy is one controller for the configured three
    # weapons (2 rocket + 1 railgun by default). Disabled/legacy diagnostics
    # retain their old two/three-rocket geometry.
    from . import strategy_config
    config = strategy_config.get()
    guns = (list(turn.weapons()) if config['enabled']
            and config['defense']['single_operator_three_rockets']
            else [g for g in turn.weapons() if g.kind == 'rocket'])
    base = turn.station()
    if len(guns) != 3 or base is None:
        return set(), set()
    permanent = {p for u in turn.ours + turn.enemies if u.health > 0
                 and u.kind not in ('worker', 'pioneer') for p in turn.footprint(u)}
    inner = {Pos(x, y) for x in range(base.pos.x-1, base.pos.x+3)
             for y in range(base.pos.y-2, base.pos.y+2)
             if turn.land(Pos(x, y)) and Pos(x, y) not in permanent}
    common = {p for p in inner if all(distance(p, g.pos) == 1 for g in guns)}
    return common, inner


def reserve(turn, commands, *, protected=(), business_goals=None):
    config = strategy_config.get()
    if not config['enabled'] or not config['defense']['single_operator_three_rockets']:
        return []
    workers = turn.workers()
    if not workers:
        return []
    owner = workers[0].unit_id
    common, inner = common_cells(turn)
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
        if current and landing in common and role.unit_id in (business_goals or {}):
            target,inside_only=business_goals[role.unit_id]
            from . import traffic
            blocked=set(turn.blocked(role))|common
            if inside_only and not turn.is_day and home_defense.inside(turn,role.pos):
                blocked |= {Pos(x,y) for x in range(turn.width) for y in range(turn.height)
                            if not home_defense.inside(turn,Pos(x,y))}
            blocked.update(Pos.load(p) for uid,cmd in commands.items() if uid!=role.unit_id
                           and cmd.get('action') in ('move','build') for p in cmd.get('targetPos',()))
            goals={p for p in traffic.neighbours(target) if turn.land(p) and p not in blocked
                   and (not inside_only or home_defense.inside(turn,p))}
            route=traffic.path(turn,role.pos,goals,blocked)
            if route and len(route)>1:commands[role.unit_id]=move_command(route[1])
            else:commands.pop(role.unit_id,None)
            notes.append({'role':role.unit_id,'reason':'reroute_business_around_common_post'})
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
    if turn.is_day and (turn.round_no-1) % 130 >= config['economy']['return_day_index']:
        notes.extend(_yield_idle_corridor(turn, commands, owner, common, inner, set(protected)))
    return notes


def _path(turn, start, goals, blocked, forbidden=()):
    """A bounded public eight-neighbour path, including its starting cell."""
    parents = {start: None}
    queue = deque([start])
    while queue:
        pos = queue.popleft()
        if pos in goals:
            path = []
            while pos is not None:
                path.append(pos); pos = parents[pos]
            return list(reversed(path))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if not (dx or dy): continue
                nxt = Pos(pos.x+dx, pos.y+dy)
                if (nxt in parents or nxt in blocked or nxt in forbidden
                        or not turn.land(nxt)):
                    continue
                parents[nxt] = pos; queue.append(nxt)
    return None


def _yield_idle_corridor(turn, commands, owner, common, inner, protected):
    """Move one idle blocker off a needed dusk route; never order the gunner
    through an occupied cell. A blocker may need two ordinary moves to get out
    of a one-cell corridor, and each subsequent snapshot is checked afresh.
    """
    gunner = next(r for r in turn.workers() if r.unit_id == owner)
    if gunner.pos in common:
        return []
    idle = {r.pos: r for r in turn.controllable()
            if r.unit_id != owner and r.unit_id not in protected and r.unit_id not in commands}
    if not idle:
        return []
    claimed = {Pos.load(p) for uid, cmd in commands.items() if uid != owner
               and cmd.get('action') in ('move', 'build') for p in cmd.get('targetPos', ())}
    blocked = turn.blocked(gunner) | claimed
    if _path(turn, gunner.pos, common, blocked):
        return []  # An actual free route already exists; nobody needs to yield.
    # This relaxed path identifies the blocker only. It never becomes a gunner
    # move command, so hypothetical removal cannot bypass simultaneous occupancy.
    route = _path(turn, gunner.pos, common, (blocked-set(idle)) | claimed)
    if not route:
        return []
    role = next((idle[p] for p in route if p in idle), None)
    if role is None:
        return []
    occupied = set(turn.blocked(role)) | claimed
    gunner_cmd = commands.get(owner, {})
    if gunner_cmd.get('action') == 'move':
        occupied.update(Pos.load(p) for p in gunner_cmd.get('targetPos', ()))
    goals = inner - common - set(route) - occupied
    escape = _path(turn, role.pos, goals, occupied, common)
    if not escape or len(escape) < 2:
        return [{'role': role.unit_id, 'reason': 'common_corridor_cannot_vacate_safely'}]
    commands[role.unit_id] = move_command(escape[1])
    return [{'role': role.unit_id, 'reason': 'yield_common_rocket_corridor',
             'blocked_at': role.pos.dump(), 'target': escape[-1].dump()}]
