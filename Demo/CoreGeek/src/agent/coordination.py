"""Per-observation team reservations, using only public fields (R02/R03/R06).

Reservations describe intended commands, never predicted settlement: sales and
task rewards cannot fund another action until they appear in the next observation.
"""
from typing import Any

from .market import shop_prices
from .protocol import Pos, Turn, TOWER_TYPES, WEAPON_BUILD_COST, WORKER, distance, move_command


def _neighbours(pos: Pos):
    return (Pos(pos.x + dx, pos.y + dy) for dx in (-1, 0, 1)
            for dy in (-1, 0, 1) if dx or dy)


def _reachable_area(turn: Turn, start: Pos, blocked: set[Pos]) -> int:
    """Size of the opening a yield actually creates, bounded by the public map."""
    seen, pending = {start}, [start]
    while pending:
        for pos in _neighbours(pending.pop()):
            if pos not in seen and pos not in blocked and turn.land(pos):
                seen.add(pos)
                pending.append(pos)
    return len(seen)


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
              commands: dict[int, dict[str, Any]]) -> dict[int, dict[str, Any]]:
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
    for uid, command in commands.items():
        cost = _cost(command, prices)
        if cost is None or cost > remaining:
            continue
        accepted[uid] = command
        remaining -= cost

    reserved = {Pos.load(pos) for cmd in accepted.values()
                if cmd.get('action') == 'build' for pos in cmd.get('targetPos', [])}
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
        if any(turn.land(pos) and pos not in blocked for pos in _neighbours(role.pos)):
            continue
        options = []
        for worker in roles:
            if (worker.kind != WORKER or worker.unit_id in commands
                    or worker.unit_id in accepted or worker.unit_id in controllers
                    or distance(worker.pos, role.pos) != 1):
                continue
            worker_blocked = turn.blocked(worker)
            free = [pos for pos in _neighbours(worker.pos)
                    if turn.land(pos) and pos not in worker_blocked and pos not in reserved]
            for target in free:
                after = (set(blocked) - {worker.pos}) | {target} | reserved
                area = _reachable_area(turn, role.pos, after)
                options.append((-area, worker.unit_id, target.x, target.y, target))
        if options:
            # Merely moving a worker one cell deeper into a one-cell corridor
            # recreates the blockage. Prefer a move that opens the largest region.
            _, uid, _, _, target = min(options)
            accepted[uid] = move_command(target)
            reserved.add(target)
    return accepted
