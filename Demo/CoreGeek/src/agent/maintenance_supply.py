"""One currently justified repair proposal, using the team's purchase contract.

Strategy only (R01/R02/R03/R06). No stockpiling, future income, private fields,
or independent courier memory. The caller must stage/finalize through TripFrame.
"""
from . import defense_sustain, team_trip
from .coordination import available_gold
from .defense_layout import wall_priority
from .market import shop_prices
from .protocol import WALL_FIXER


def plan(turn, payload, commands, *, deadline=55, commitment=None, reserved_workers=()):
    report = {'phase': 'idle', 'reason': 'disabled', 'operation': 'repair', 'item': WALL_FIXER}
    if not defense_sustain.enabled():
        return None, report
    report['gold_available'] = available_gold(turn, payload, commands)
    if not turn.is_day or turn.station() is None or turn.station().health <= 0:
        report['reason'] = 'day_or_base_missing'
        return None, report
    busy = set(reserved_workers)
    for key, command in commands.items():
        try:
            busy.add(int(key))
            if command.get('controllerId') is not None:
                busy.add(int(command['controllerId']))
        except (TypeError, ValueError):
            continue
    if commitment is not None:
        if commitment.get('operation', 'upgrade') != 'repair':
            report['reason'] = 'existing_upgrade_owner'
            return None, report
        report.update(worker=commitment['owner'], building=commitment['target'])
        if commitment['owner'] in busy:
            report['reason'] = 'owner_reserved'
            return None, report
        cost = team_trip.evaluate_trip(turn, payload, commitment, commands=commands)
        report.update(reason=cost.reason, route=cost.report(), phase='return' if commitment.get('phase') == 'return' else cost.reason)
        # Late return is still a legal safety action, not permission to buy/use late.
        if cost.command and (cost.feasible or commitment.get('phase') == 'return'):
            return (commitment['owner'], cost.command), report
        return None, report
    if team_trip.threat(turn) or len(turn.weapons()) < 3:
        report['reason'] = 'threat_or_initial_weapons'
        return None, report
    walls = [w for w in turn.walls() if defense_sustain._candidate_wall(turn, w)]
    walls.sort(key=lambda w: (w.health / (1000, 1500, 2000)[w.level - 1],
                             wall_priority(w.pos, turn.station().pos, turn.width, turn.height)[0], w.unit_id))
    if not walls:
        report['reason'] = 'no_current_repair_need'
        return None, report
    all_workers = turn.workers()
    holders = [w for w in all_workers if WALL_FIXER in w.backpack]
    # An already carried kit is not duplicated because its owner is temporarily
    # reserved/unreachable. That reservation belongs to the caller to resolve.
    workers = [w for w in (holders or all_workers) if w.unit_id not in busy]
    if not workers:
        report['reason'] = 'holders_reserved' if holders else 'workers_reserved'
        return None, report
    price = shop_prices(payload).get(WALL_FIXER)
    report['price'] = price
    for wall in walls:
        choices = []
        for worker in workers:
            record = {'operation': 'repair', 'owner': worker.unit_id, 'target': wall.unit_id,
                      'item': WALL_FIXER, 'deadline': team_trip.deadline(turn, deadline)}
            cost = team_trip.evaluate_trip(turn, payload, record, commands=commands)
            if cost.feasible and cost.command:
                choices.append((cost.actions, worker.unit_id, worker, cost))
        if not choices:
            continue
        _, _, worker, cost = min(choices, key=lambda row: row[:2])
        report.update(phase=cost.reason, reason='current_damaged_front_wall', worker=worker.unit_id,
                      building=wall.unit_id, target=wall.pos.dump(), route=cost.report(),
                      quantity=0 if WALL_FIXER in worker.backpack else 1)
        return (worker.unit_id, cost.command), report
    report['reason'] = 'no_complete_repair_trip'
    return None, report
