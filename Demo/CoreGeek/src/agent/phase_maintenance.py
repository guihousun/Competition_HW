"""Interior worker repair lease; strategy choices only (R01/R02/R03/R06).

The caller excludes the dedicated three-rocket operator, even on cooldown-only
rounds, and reserves returned owned_roles before any later fire assignment.
Entry/exit fractions are policy, not changed official health or repair amounts.
"""
from collections import deque
from copy import deepcopy

from . import home_defense, defense_layout
from .coordination import available_gold
from .market import shop_prices
from .protocol import Pos, WALL_FIXER, distance, move_command, use_command, buy_command


def sanitize_memory(value, last_round=None):
    """Keep only bounded lease fields when restoring untrusted persistent state."""
    if not isinstance(value, dict):
        return {}
    integer = lambda v: type(v) is int and 0 <= v < 2**63
    clean = {'last_round': value['last_round']} if integer(value.get('last_round')) else {}
    lease = value.get('lease')
    if not isinstance(lease, dict) or lease.get('phase') not in ('repair', 'return'):
        return clean
    if not all(integer(lease.get(k)) for k in ('owner', 'target', 'issued_round')):
        return clean
    home = lease.get('home')
    if not isinstance(home, dict) or not all(type(home.get(k)) is int and -10000 < home[k] < 10000 for k in ('x', 'y')):
        return clean
    row = {k: lease[k] for k in ('owner', 'target', 'issued_round', 'phase')}
    row['home'] = dict(x=home['x'], y=home['y'])
    if integer(lease.get('use_round')) and integer(lease.get('item_count')) and lease['item_count'] <= 100:
        if lease['use_round'] < lease['issued_round']:
            return clean
        row.update(use_round=lease['use_round'], item_count=lease['item_count'])
    if last_round is not None and (not integer(last_round) or row['issued_round'] > last_round
                                   or row.get('use_round', 0) > last_round):
        return clean
    clean['lease'] = row
    return clean


def _neighbours(p):
    return (Pos(p.x + x, p.y + y) for x in (-1, 0, 1)
            for y in (-1, 0, 1) if x or y)


def _busy(commands, excluded):
    result = {int(x) for x in excluded}
    for uid, command in commands.items():
        result.add(int(uid))
        if command.get('controllerId') is not None:
            result.add(int(command['controllerId']))
    return result


def _paths(turn, worker, commands):
    blocked = set(turn.blocked(worker))
    blocked.update(Pos.load(p) for cmd in commands.values()
                   if cmd.get('action') in ('move', 'build') for p in cmd.get('targetPos', ()))
    blocked.update(Pos(p.x + 1, p.y) for p, kind in turn.zones.items()
                   if kind in ('challengerTaskPoint2', 'defenderTaskPoint2'))
    dist, first, queue = {worker.pos: 0}, {worker.pos: None}, deque([worker.pos])
    if not home_defense.inside(turn, worker.pos):
        return {}, {}
    while queue:
        p = queue.popleft()
        for n in _neighbours(p):
            if n in dist or n in blocked or not turn.land(n) or not home_defense.inside(turn, n):
                continue
            dist[n], first[n] = dist[p] + 1, n if p == worker.pos else first[p]
            queue.append(n)
    return dist, first


def _fraction(wall):
    return wall.health / (1000, 1500, 2000)[wall.level - 1]


def night_plan(turn, memory, commands, ready_weapons=(), excluded_roles=(), config=None, payload=None):
    """Stage one worker's move/use/return; finalize against actual final output.

    Only public observation changes confirm repairs. A successful dispatch alone
    is not consumption/success; repeated observations do not trigger a new use.
    ready_weapons is accepted for caller compatibility, never assumed coverable.
    """
    cfg = config or {}
    result = dict(commands={}, owned_roles=[], claimed=[], report=dict(phase='idle', reason='no_candidate'))
    if not cfg.get('enabled', True):
        result['report']['reason'] = 'disabled'
        return result
    clean = sanitize_memory(memory, last_round=turn.round_no)
    if turn.round_no < clean.get('last_round', 0) or clean.get('lease', {}).get('issued_round', 0) > turn.round_no:
        clean = {}
    memory.clear()
    memory.update(clean)
    previous = deepcopy(memory)
    memory['last_round'] = turn.round_no
    result['_previous'] = previous
    base = turn.station()
    if turn.is_day or not base or base.health <= 0:
        memory.pop('lease', None)
        result['report']['reason'] = 'day_or_base_missing'
        return result
    old_lease = memory.get('lease')
    if old_lease and (turn.round_no > previous.get('last_round', turn.round_no) + 1
                      or (old_lease['issued_round'] - 1) // 130 != (turn.round_no - 1) // 130):
        memory.pop('lease', None)
        result['report']['reason'] = 'observation_gap_or_expired_night'
        return result
    busy = _busy(commands, excluded_roles)
    workers = {w.unit_id: w for w in turn.workers()}
    walls = {w.unit_id: w for w in turn.walls() if 1 <= w.level <= 3}
    entry, exit_at = cfg.get('entry_fraction', .55), cfg.get('exit_fraction', .85)
    lease = memory.get('lease')
    if lease and (lease.get('owner') not in workers or lease.get('owner') in busy):
        memory.pop('lease', None)
        result['report']['reason'] = 'owner_dead_or_excluded'
        return result
    if not lease:
        if (turn.round_no - 1) // 130 + 1 < cfg.get('from_day', 4):
            result['report']['reason'] = 'before_maintenance_phase'
            return result
        candidates = []
        for worker in workers.values():
            if worker.unit_id in busy or WALL_FIXER not in worker.backpack:
                continue
            dist, _ = _paths(turn, worker, commands)
            for wall in walls.values():
                if _fraction(wall) > entry:
                    continue
                stands = [p for p in _neighbours(wall.pos) if p in dist]
                if stands:
                    priority = defense_layout.wall_priority(wall.pos, base.pos, turn.width, turn.height)[0]
                    candidates.append((priority, _fraction(wall), min(dist[p] for p in stands), worker.unit_id, wall.unit_id))
        if not candidates:
            return result
        _, _, _, uid, wid = min(candidates)
        lease = dict(owner=uid, target=wid, home=workers[uid].pos.dump(), phase='repair', issued_round=turn.round_no)
        memory['lease'] = lease
    worker = workers[lease['owner']]
    wall = walls.get(lease['target'])
    result['owned_roles'] = [worker.unit_id]
    report = result['report']
    report.update(worker=worker.unit_id, target=lease['target'], phase=lease['phase'])
    if 'use_round' in lease:
        if turn.round_no <= lease['use_round']:
            report['reason'] = 'await_next_observation'
            return result
        consumed = worker.backpack.count(WALL_FIXER) < lease['item_count']
        accepted = (payload or {}).get('lastRoundRoleActionResults', {}).get(str(worker.unit_id))
        if accepted is None:
            accepted = (payload or {}).get('lastRoundRoleActionResults', {}).get(worker.unit_id)
        confirmed = turn.round_no == lease['use_round'] + 1 and consumed and accepted is True
        report['use_feedback'] = 'confirmed' if confirmed else 'unconfirmed'
        if not confirmed:
            lease['phase'] = 'return'
        lease.pop('use_round')
        lease.pop('item_count')
    if wall is None or WALL_FIXER not in worker.backpack or (wall and _fraction(wall) >= exit_at):
        lease['phase'] = 'return'
    dist, first = _paths(turn, worker, commands)
    if lease['phase'] == 'return':
        home = Pos.load(lease['home'])
        if worker.pos == home:
            memory.pop('lease', None)
            result['owned_roles'] = []
            report.update(phase='released', reason='returned_observed')
            return result
        goal = home if home in dist else None
        report.update(phase='return', reason='returning' if goal else 'return_blocked')
    else:
        stands = [p for p in _neighbours(wall.pos) if p in dist]
        goal = min(stands, key=lambda p: (dist[p], p.x, p.y)) if stands else None
        report.update(phase='repair', reason='repair_needed' if goal else 'inside_route_blocked')
        if goal == worker.pos:
            result['commands'][worker.unit_id] = use_command(WALL_FIXER, wall.pos)
            lease.update(use_round=turn.round_no, item_count=worker.backpack.count(WALL_FIXER))
    if goal is not None and goal != worker.pos:
        result['commands'][worker.unit_id] = move_command(first[goal])
        result['claimed'] = [first[goal]]
    return result


def finalize(memory, result, commands):
    """Do not record a dropped use or start a lease from a rejected proposal."""
    for uid, proposed in result.get('commands', {}).items():
        if commands.get(uid, commands.get(str(uid))) == proposed:
            continue
        old = result.get('_previous', {}).get('lease')
        if not old:
            memory.pop('lease', None)
            result['owned_roles'] = []
        elif memory.get('lease'):
            memory['lease'].update(phase='return')
            memory['lease'].pop('use_round', None)
            memory['lease'].pop('item_count', None)
        result['report']['reason'] = 'dispatch_rejected'
        result['commands'], result['claimed'] = {}, []


def restock_plan(turn, payload, commands, excluded_roles=(), config=None):
    """Shop-adjacent daytime restock only; caller owns travel/deadline policy."""
    cfg = config or {}
    report = dict(reason='not_at_shop_or_stock_sufficient', item=WALL_FIXER)
    if not cfg.get('enabled', True) or not turn.is_day:
        return None, dict(report, reason='disabled_or_night')
    price = shop_prices(payload).get(WALL_FIXER)
    if price is None or price < 0:
        return None, dict(report, reason='price_unavailable')
    busy = _busy(commands, excluded_roles)
    gold = max(0, available_gold(turn, payload, commands) - cfg.get('reserve_gold', 0))
    for worker in turn.workers():
        if worker.unit_id in busy or worker.backpack_full:
            continue
        if not any(kind == 'weaponShop' and distance(worker.pos, p) == 1 for p, kind in turn.zones.items()):
            continue
        missing = max(0, cfg.get('stock_target', 2) - worker.backpack.count(WALL_FIXER))
        capacity = max(0, (worker.capacity or 0) - len(worker.backpack))
        amount = min(missing, capacity, gold // price if price else missing)
        if amount:
            return (worker.unit_id, buy_command(WALL_FIXER, amount)), dict(report, reason='stock_for_later_night', amount=amount, cost=amount * price)
    return None, report
