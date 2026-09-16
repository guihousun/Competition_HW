"""Per-observation team reservations, using only public fields (R02/R03/R06).

Reservations describe intended commands, never predicted settlement: sales and
task rewards cannot fund another action until they appear in the next observation.
"""
from typing import Any

from .market import shop_prices
from .protocol import (Pos, Turn, CONTROLLABLE_TYPES, TOWER_TYPES, WEAPON_BUILD_COST,
                       WORKER, distance, move_command)


def _neighbours(pos: Pos):
    return (Pos(pos.x + dx, pos.y + dy) for dx in (-1, 0, 1)
            for dy in (-1, 0, 1) if dx or dy)


def _reachable_cells(turn: Turn, start: Pos, blocked: set[Pos]) -> set[Pos]:
    """Public connected component; diagonals obey the same movement rules."""
    seen, pending = {start}, [start]
    while pending:
        for pos in _neighbours(pending.pop()):
            if pos not in seen and pos not in blocked and turn.land(pos):
                seen.add(pos)
                pending.append(pos)
    return seen


def _reachable_area(turn: Turn, start: Pos, blocked: set[Pos]) -> int:
    return len(_reachable_cells(turn, start, blocked))


def _cost(command: dict[str, Any], prices: dict[str, int]) -> int | None:
    if command.get('action') == 'buy':
        price = prices.get(command.get('name'))
        amount = command.get('num', 1)
        if (price is None or price < 0 or type(amount) is not int or amount < 1):
            return None
        return price * amount
    if command.get('action') == 'build' and command.get('name') in TOWER_TYPES:
        return WEAPON_BUILD_COST
    return 0


def available_gold(turn: Turn, payload: dict[str, Any],
                   commands: dict[int, dict[str, Any]], *, replacing: int | None = None) -> int:
    """Recompute from live intents so replacing/removing one releases its cost."""
    remaining = turn.gold
    prices = shop_prices(payload)
    for uid, command in commands.items():
        if uid == replacing:
            continue
        cost = _cost(command, prices)
        if cost is None:
            return 0  # no guessed price for an unpublished item
        remaining -= cost
    return max(0, remaining)


def reconcile(turn: Turn, payload: dict[str, Any],
              commands: dict[int, dict[str, Any]], *,
              day_yield_deadline: int | None = None) -> dict[int, dict[str, Any]]:
    """Last planning pass, after task/treasure overrides, before official output.

    Earlier spending intents keep priority. For a shared free destination, rotate
    priority by public round number over sorted living role IDs; losers wait and
    replan next round. We conservatively avoid all currently occupied cells,
    including swaps and chains, without guessing enemy or robot future moves.
    Non-conflicting movement and non-movement actions retain their original intent.
    This is policy arbitration, not a replacement for the judge's collision rules.
    """
    accepted = {}
    remaining = turn.gold
    prices = shop_prices(payload)
    living = {str(role.unit_id) for role in turn.controllable()}
    direct = {str(uid) for uid in commands if str(uid) in living}
    controllers_used = set()
    for uid, command in commands.items():
        if command.get('action') == 'attack':
            controller = str(command.get('controllerId'))
            # Explicit role work (including a task override) releases its weapon
            # claim. One controller cannot operate two weapons in the same turn.
            if controller not in living or controller in direct or controller in controllers_used:
                continue
            controllers_used.add(controller)
        cost = _cost(command, prices)
        if cost is None or cost > remaining:
            continue
        accepted[uid] = command
        remaining -= cost

    reserved = {Pos.load(pos) for cmd in accepted.values()
                if cmd.get('action') == 'build' for pos in cmd.get('targetPos', [])}
    # The second cell of a two-cell task point is occupied too (R02/R07).
    reserved.update(Pos(pos.x + 1, pos.y) for pos, kind in turn.zones.items()
                    if kind in ('challengerTaskPoint2', 'defenderTaskPoint2'))
    roles = list(turn.controllable())
    if roles:
        offset = (turn.round_no - 1) % len(roles)
        roles = roles[offset:] + roles[:offset]
    for role in roles:
        command = accepted.get(role.unit_id, {})
        if command.get('action') != 'move':
            continue
        targets = command.get('targetPos', [])
        target = Pos.load(targets[0]) if len(targets) == 1 else None
        if (target is None or distance(role.pos, target) != 1
                or not turn.land(target) or target in turn.blocked(role)
                or target in reserved):
            accepted.pop(role.unit_id)
        else:
            reserved.add(target)
    # A role boxed in by idle workers needs an opening, not another rejected
    # greedy step into a wall. Move at most one idle worker aside per blocked
    # mover. Never interrupt an action, a task hold, or a tower controller.
    controllers = {int(cmd['controllerId']) for cmd in accepted.values()
                   if cmd.get('action') == 'attack' and 'controllerId' in cmd}
    for role in roles:
        if commands.get(role.unit_id, {}).get('action') != 'move' or role.unit_id in accepted:
            continue
        blocked = turn.blocked(role)
        has_space = any(turn.land(pos) and pos not in blocked for pos in _neighbours(role.pos))
        # A free dead-end cell does not mean the intended journey is reachable.
        # Expand the old fully-boxed recovery only during peaceful daytime;
        # task holders and active workers/controllers remain protected below.
        threat = any(r.health > 0 for r in turn.robots) or any(
            r.health > 0 and r.kind in CONTROLLABLE_TYPES + TOWER_TYPES for r in turn.enemies)
        extended = (turn.is_day and not threat and day_yield_deadline is not None
                    and (turn.round_no - 1) % 130 + 2 < day_yield_deadline)
        if has_space and not extended:
            continue
        before_area = _reachable_area(turn, role.pos, set(blocked) | reserved)
        idle = [worker for worker in roles if worker.kind == WORKER
                and worker.unit_id not in commands and worker.unit_id not in accepted
                and worker.unit_id not in controllers
                and (extended or distance(worker.pos, role.pos) == 1)]
        # Upper bound after relocating idle crew: removing them entirely may
        # connect the component, but they still occupy distinct cells afterward.
        # This also avoids a costly search on an already open map.
        possible = _reachable_cells(turn, role.pos,
                                    (set(blocked) - {w.pos for w in idle}) | reserved)
        idle = [w for w in idle if w.pos in possible]
        if len(possible) - len(idle) <= before_area:
            continue
        options = []
        first_steps = []
        for worker in idle:
            worker_blocked = turn.blocked(worker)
            free = [pos for pos in _neighbours(worker.pos)
                    if turn.land(pos) and pos not in worker_blocked and pos not in reserved]
            for target in free:
                after = (set(blocked) - {worker.pos}) | {target} | reserved
                area = _reachable_area(turn, role.pos, after)
                first_steps.append((worker, target, after))
                if area > before_area:
                    options.append((-area, worker.unit_id, target.x, target.y, target))
        if not options and extended:
            # Two idle workers can block one another. Prove progress after two
            # SEQUENTIAL legal moves, but emit only the first. The next real
            # observation must independently revalidate any subsequent yield;
            # no assumed successful move, simultaneous swap, or cached script.
            for worker, target, after in first_steps:
                for second in idle:
                    if second.unit_id == worker.unit_id:
                        continue
                    second_blocked = (set(turn.blocked(second)) - {worker.pos}) | {target} | reserved
                    for next_target in _neighbours(second.pos):
                        if not turn.land(next_target) or next_target in second_blocked:
                            continue
                        final = (after - {second.pos}) | {next_target}
                        area = _reachable_area(turn, role.pos, final)
                        if area > before_area:
                            options.append((-area, worker.unit_id, target.x, target.y, target))
        if options:
            # Merely moving a worker one cell deeper into a one-cell corridor
            # recreates the blockage. Prefer a move that opens the largest region.
            _, uid, _, _, target = min(options)
            accepted[uid] = move_command(target)
            reserved.add(target)
    return accepted
