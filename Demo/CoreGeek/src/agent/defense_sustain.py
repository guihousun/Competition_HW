"""One public-state interior repair trip (strategy, R01/R02/R04/R06).

No damage prediction or sacrifice of currently useful attacks. The caller owns
fire targeting, excludes task-busy roles, and protects owned_roles through ALL
later fire allocation. Local inside geometry is the existing layout assumption.
"""
from collections import deque
from dataclasses import replace
import os

from . import home_defense, defense_layout
from .market import can_upgrade
from .protocol import (Pos, WALL, WALL_FIXER, WALL_UPGRADE_VOUCHER_1,
                       WALL_UPGRADE_VOUCHER_2, WORKER, ROUNDS_PER_DAY,
                       distance, move_command, use_command)

ENV = 'COMPETITION_HW_MAINTENANCE_V2'
REPAIR_ENTRY_FRACTION = .7  # Strategy tuning, not an official damage rule.
ITEMS = (WALL_FIXER, WALL_UPGRADE_VOUCHER_1, WALL_UPGRADE_VOUCHER_2)


def enabled():
    return os.environ.get(ENV, '').lower() in ('on', 'true', '1', 'yes')


def _integer(value, minimum=0):
    return type(value) is int and minimum <= value <= 2**63 - 1


def sanitize_memory(value):
    """Copy only one validated lease and its bounded use-confirmation fields."""
    result = {}
    if not isinstance(value, dict):
        return result
    if _integer(value.get('last_round')):
        result['last_round'] = value['last_round']
    lease = value.get('lease')
    if not isinstance(lease, dict):
        return result
    keys = ('role_id', 'target_id', 'return_weapon_id', 'issued_round')
    if (not all(_integer(lease.get(k)) for k in keys)
            or lease.get('item') not in ITEMS
            or lease.get('phase') not in ('to_wall', 'use', 'return')):
        return result
    clean = {k: lease[k] for k in keys}
    clean.update(item=lease['item'], phase=lease['phase'])
    if (all(_integer(lease.get(k)) for k in
            ('use_round', 'item_count', 'target_level', 'target_health'))
            and 1 <= lease['item_count'] <= 100
            and 1 <= lease['target_level'] <= 3):
        clean.update({k: lease[k] for k in ('use_round', 'item_count', 'target_level', 'target_health')})
    result['lease'] = clean
    return result


def _neighbours(pos):
    return (Pos(pos.x + dx, pos.y + dy) for dx in (-1, 0, 1)
            for dy in (-1, 0, 1) if dx or dy)


class _Board:
    def __init__(self, turn, role, claimed):
        # TaskPoint2's second cell is a published obstacle, even on old Turns.
        extra = {Pos(p.x + 1, p.y) for p, kind in turn.zones.items()
                 if kind in ('challengerTaskPoint2', 'defenderTaskPoint2')}
        blocked = turn.blocked(role) | set(claimed) | extra
        base = turn.station().pos
        self.free = {Pos(x, y) for x in range(base.x - 1, base.x + 3)
                     for y in range(base.y - 2, base.y + 2)
                     if turn.land(Pos(x, y)) and Pos(x, y) not in blocked
                     and home_defense.inside(turn, Pos(x, y))}
        self.cache = {}

    def search(self, start):
        if start in self.cache:
            return self.cache[start]
        distances, first = {}, {}
        queue = deque()
        if start in self.free:
            queue.append(start)
            distances[start], first[start] = 0, None
        while queue:
            pos = queue.popleft()
            for nxt in _neighbours(pos):
                if nxt not in self.free or nxt in distances:
                    continue
                distances[nxt] = distances[pos] + 1
                first[nxt] = nxt if pos == start else first[pos]
                queue.append(nxt)
        self.cache[start] = distances, first
        return distances, first


def _busy(commands):
    result = set()
    for key, command in commands.items():
        try:
            result.add(int(key))
        except (ValueError, TypeError):
            pass
        if isinstance(command, dict) and 'controllerId' in command:
            try:
                result.add(int(command['controllerId']))
            except (ValueError, TypeError):
                pass
    return result


def _matching(turn, weapons, roles):
    """Exact tiny bipartite matching: one stationary role per ready weapon."""
    weapons = sorted({w.unit_id: w for w in weapons}.values(), key=lambda w: w.unit_id)
    edges = {w.unit_id: [r.unit_id for r in roles if r.health > 0
                        and home_defense.inside(turn, r.pos)
                        and distance(r.pos, w.pos) == 1] for w in weapons}

    def assign(index, used, mapping):
        if index == len(weapons):
            return mapping
        wid = weapons[index].unit_id
        for uid in edges[wid]:
            if uid not in used:
                found = assign(index + 1, used | {uid}, {**mapping, wid: uid})
                if found is not None:
                    return found
        return None

    return assign(0, set(), {})


def _coverage(turn, role, busy, ready, actions, post):
    others = [r for r in turn.controllable() if r.unit_id != role.unit_id
              and r.unit_id not in busy]
    match = _matching(turn, ready, others)
    if match is None:
        return 'fire_coverage', {}
    # Checking cumulative due groups catches one worker alternating two rockets:
    # individual guns may be coverable but not simultaneously by distinct roles.
    cooling = [w for w in turn.weapons() if w.cooldown > 0]
    for window in sorted({w.cooldown for w in cooling}):
        due = tuple(ready) + tuple(w for w in cooling if w.cooldown <= window)
        if _matching(turn, due, others) is not None:
            continue
        if actions > window:
            return 'cooldown_window', {'window': window, 'coverage': match}
        if _matching(turn, due, others + [replace(role, pos=post)]) is None:
            return 'return_coverage', {'window': window, 'coverage': match}
    return None, {'coverage': match}


def _valid_item(role, wall, item):
    return item in role.backpack and (item == WALL_FIXER or
                                      can_upgrade(item, wall.kind, wall.level))


def _candidate_wall(turn, wall):
    return (wall.kind == WALL and 1 <= wall.level <= 3
            and 0 < wall.health <= REPAIR_ENTRY_FRACTION * (1000, 1500, 2000)[wall.level - 1]
            and defense_layout.wall_priority(wall.pos, turn.station().pos,
                                             turn.width, turn.height)[0] <= 1)


def plan(turn, memory: dict, pairs, commands: dict, *, payload=None, claimed=(),
         ready_weapons=(), excluded_roles=(), enabled_override=None):
    """Mutate only the small memory; return this module's commands/reservations.

    ready_weapons are current legal-target/cooldown-zero weapons from the caller.
    Claims are Pos values. Failed/unconfirmed uses abort to return on the next
    observation; repeating the issuing observation does not confirm success.
    """
    result = {'commands': {}, 'owned_roles': [], 'claimed': [], 'report': {
        'phase': 'idle', 'reason': 'no_candidate', 'rejections': [],
        'scope': 'current_coverage_and_cooldown_only_no_dps_or_emergency_sacrifice'}}
    report = result['report']
    active = enabled() if enabled_override is None else bool(enabled_override)
    if not active:
        report['reason'] = 'disabled'
        return result  # Default OFF must not change legacy planner dumps.
    result['_previous_memory'] = sanitize_memory(memory)
    clean = sanitize_memory(memory)
    previous_round = clean.get('last_round')
    if turn.round_no < clean.get('last_round', 0):
        clean = {}
    memory.clear()
    memory.update(clean)
    memory['last_round'] = turn.round_no
    if turn.is_day or turn.station() is None or turn.station().health <= 0:
        memory.pop('lease', None)
        report['reason'] = 'day_or_base_missing'
        return result

    live = {u.unit_id: u for u in turn.ours if u.health > 0}
    guns = {w.unit_id: w for w in turn.weapons()}
    ready = tuple(guns[w.unit_id] for w in ready_weapons
                  if w.unit_id in guns and guns[w.unit_id].cooldown == 0)
    busy = _busy(commands) | set(excluded_roles)
    claimed = frozenset(claimed)
    breached = any(r.health > 0 and home_defense.inside(turn, r.pos)
                   for r in turn.robots) or any(
        r.health > 0 and r.kind in ('worker', 'pioneer')
        and home_defense.inside(turn, r.pos) for r in turn.enemies)

    def emit(role, command, phase, **details):
        result['owned_roles'] = [role.unit_id]
        result['commands'] = {role.unit_id: command}
        if command['action'] == 'move':
            result['claimed'] = [Pos.load(command['targetPos'][0])]
        report.update(phase=phase, worker=role.unit_id, **details)
        return result

    def return_home(role, board, lease, reason):
        lease['phase'] = 'return'
        for key in ('use_round', 'item_count', 'target_level', 'target_health'):
            lease.pop(key, None)
        dist, first = board.search(role.pos)
        options = [(dist[p], w.unit_id != lease['return_weapon_id'], w.unit_id,
                    p.x, p.y, p) for w in guns.values() for p in _neighbours(w.pos)
                   if p in dist]
        if not options:
            result['owned_roles'] = [role.unit_id]
            report.update(phase='return', reason='return_blocked', cause=reason,
                          worker=role.unit_id)
            return result
        # Prefer posts that actually restore the observed matching, including
        # cooling guns. In particular, a shared-rocket worker must not stop at
        # an adjacent but insufficient post just because its walk is shorter.
        others = [r for r in turn.controllable() if r.unit_id != role.unit_id
                  and r.unit_id not in busy]
        due = ready + tuple(w for w in guns.values() if w.cooldown > 0)
        covered = [entry for entry in options if _matching(
            turn, due, others + [replace(role, pos=entry[-1])]) is not None]
        if covered:
            options = covered
        original = [entry for entry in options if not entry[1]]
        _, _, wid, _, _, post = min(original or options)
        if dist[post] == 0:
            memory.pop('lease', None)
            report.update(phase='released', reason='at_post', cause=reason,
                          worker=role.unit_id, return_weapon_id=wid)
            return result
        return emit(role, move_command(first[post]), 'return', reason=reason,
                    remaining_actions=dist[post], return_weapon_id=wid, post=post.dump())

    def trip(role, wall, gun, board):
        outward, first = board.search(role.pos)
        choices = []
        for stand in _neighbours(wall.pos):
            if stand not in outward:
                continue
            back, _ = board.search(stand)
            for post in _neighbours(gun.pos):
                if post in back:
                    choices.append((outward[stand] + 1 + back[post],
                                    outward[stand], stand.x, stand.y, post.x, post.y,
                                    stand, post, first[stand]))
        return sorted(choices)

    lease = memory.get('lease')
    if lease:
        role = live.get(lease['role_id'])
        if role is None or role.kind != WORKER or lease['role_id'] in busy:
            memory.pop('lease', None)
            report.update(phase='cancelled', worker=lease['role_id'],
                          reason='lease_role_busy' if lease['role_id'] in busy
                          else 'lease_role_dead')
            return result
        board = _Board(turn, role, claimed)
        wall = live.get(lease['target_id'])
        reason = None
        if previous_round is not None and turn.round_no > previous_round + 1:
            reason = 'observation_gap'
        if ((lease['issued_round'] - 1) // ROUNDS_PER_DAY
                != (turn.round_no - 1) // ROUNDS_PER_DAY):
            reason = 'expired_night'
        if lease.get('use_round', turn.round_no) < turn.round_no:
            consumed = role.backpack.count(lease['item']) < lease['item_count']
            upgraded = (lease['item'] == WALL_FIXER or wall is not None
                        and wall.level > lease['target_level'])
            continuous = turn.round_no == lease['use_round'] + 1
            feedback = (payload or {}).get('lastRoundRoleActionResults', {})
            accepted = feedback.get(str(role.unit_id), feedback.get(role.unit_id)) is True
            reason = ('use_confirmed' if continuous and accepted and consumed and upgraded
                      else 'use_unknown_consumed' if consumed else 'use_unconfirmed')
        if reason or lease['phase'] == 'return':
            return return_home(role, board, lease, reason or 'returning')
        if breached or wall is None or not _candidate_wall(turn, wall):
            return return_home(role, board, lease, 'breach_or_target_invalid')
        if not _valid_item(role, wall, lease['item']):
            return return_home(role, board, lease, 'item_unavailable')
        selected = [(role, wall, lease['item'], guns.get(lease['return_weapon_id']), board)]
    else:
        if breached:
            report['reason'] = 'visible_inside_breach'
            return result
        posts = {r.unit_id: g.unit_id for r, g in pairs if g.unit_id in guns}
        selected = []
        for role in turn.workers():
            if role.unit_id in busy or not home_defense.inside(turn, role.pos):
                continue
            board = _Board(turn, role, claimed)
            home = guns.get(posts.get(role.unit_id))
            homes = [home] if home is not None else list(guns.values())
            for wall in turn.walls():
                if _candidate_wall(turn, wall):
                    for item in ITEMS:
                        if _valid_item(role, wall, item):
                            selected.extend((role, wall, item, g, board) for g in homes)

    options = []
    for role, wall, item, gun, board in selected:
        if gun is None:
            report['rejections'].append('return_weapon_missing')
            continue
        paths = trip(role, wall, gun, board)
        if not paths:
            report['rejections'].append('route_or_return_blocked')
        for path in paths:
            actions, out, _, _, _, _, stand, post, first = path
            if actions > ROUNDS_PER_DAY - (turn.round_no - 1) % ROUNDS_PER_DAY:
                report['rejections'].append('night_budget')
                continue
            reason, detail = _coverage(turn, role, busy, ready, actions, post)
            if reason:
                report['rejections'].append(reason)
                continue
            options.append((actions, role.unit_id, wall.unit_id, ITEMS.index(item),
                            gun.unit_id, stand.x, stand.y, post.x, post.y,
                            role, wall, item, gun, out, post, first, detail))
    report['rejections'] = sorted(set(report['rejections']))
    if not options:
        if lease:
            return return_home(role, board, lease, 'trip_no_longer_feasible')
        report['reason'] = 'no_feasible_trip'
        return result

    (actions, _, _, _, _, _, _, _, _, role, wall, item, gun,
     out, post, first, detail) = min(options, key=lambda option: option[:9])
    if not lease:
        lease = {'role_id': role.unit_id, 'target_id': wall.unit_id, 'item': item,
                 'phase': 'to_wall', 'return_weapon_id': gun.unit_id,
                 'issued_round': turn.round_no}
        memory['lease'] = lease
    lease['phase'] = 'to_wall' if out else 'use'
    if not out:
        lease.update(use_round=turn.round_no, item_count=role.backpack.count(item),
                     target_level=wall.level, target_health=wall.health)
    return emit(role, move_command(first) if out else use_command(item, wall.pos),
                lease['phase'], reason='feasible_current_coverage', target_id=wall.unit_id,
                item=item, remaining_actions=actions, return_actions=actions-out-1,
                post=post.dump(), **detail)


def finalize(memory, result, commands):
    """Validate actual dispatch without discarding an existing owner's return.

    The caller passes the final int-key command map after all arbitration. A
    dropped new proposal creates no lease. A dropped existing action keeps its
    owner, removes speculative use markers and safely returns on observation.
    Observation-driven cancellations/releases already made by plan are retained.
    """
    proposed = result.get('commands') or {}
    if not proposed:
        return
    owner, command = next(iter(proposed.items()))
    actual = commands.get(owner, commands.get(str(owner)))
    if actual == command:
        return
    previous = result.get('_previous_memory', {}).get('lease')
    if previous is None or previous.get('role_id') != owner:
        memory.pop('lease', None)
        result['owned_roles'] = []
    elif memory.get('lease', {}).get('role_id') == owner:
        memory['lease']['phase'] = 'return'
        for key in ('use_round', 'item_count', 'target_level', 'target_health'):
            memory['lease'].pop(key, None)
        result['owned_roles'] = [owner]
    result['commands'] = {}
    result['claimed'] = []
    result['report'].update(reason='dispatch_rejected', phase='return' if previous else 'cancelled')
