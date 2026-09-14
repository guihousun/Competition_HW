"""Work during observed quiet nights, without predicting future waves.

R01/R02/R03/R06: one action per role, public observations only, no night builds.
The four-cell work radius is a conservative strategy choice, not a game rule.
"""
from collections import Counter

from .coordination import available_gold
from .grid import next_step
from .market import can_upgrade, shop_prices, vendor_prices, VOUCHER_TARGETS
from .protocol import (Pos, Turn, Unit, WALL, STATION, TOWER_TYPES, MEDICINE,
                       WALL_FIXER, distance, use_command, buy_command,
                       sell_command, collect_command, move_command)

WORK_RADIUS = 4
STONE_RESERVE = 10
ORE_BATCH = 20


def _near(turn, role, kind):
    return any(zone == kind and distance(role.pos, pos) == 1
               for pos, zone in turn.zones.items())


def _maintenance(turn, role):
    if role.health <= 154:
        yield MEDICINE, None
    buildings = sorted((u for u in turn.ours if u.health > 0
                        and distance(role.pos, u.pos) == 1
                        and u.kind in (STATION, WALL) + TOWER_TYPES),
                       key=lambda u: (0 if u.kind == STATION else 2 if u.kind == WALL else 1,
                                      u.health, u.unit_id))
    for building in buildings:
        for item in sorted(VOUCHER_TARGETS):
            if can_upgrade(item, building.kind, building.level):
                yield item, building.pos
        wall_threshold = (700, 1050, 1400)[min(3, max(1, building.level)) - 1]
        if building.kind == WALL and building.health <= wall_threshold:
            yield WALL_FIXER, building.pos


def plan(turn: Turn, payload: dict, pairs: tuple) -> dict:
    """Return extra worker actions; absence leaves the ordinary defence plan in charge."""
    robot_info = payload.get('robot')
    # Missing observations are not evidence that a wave has been cleared.
    if (turn.is_day or (turn.round_no - 1) % 130 == 70
            or not isinstance(robot_info, dict) or not isinstance(robot_info.get('roles'), list)
            or any(robot.health > 0 for robot in turn.robots)):
        return {}
    workers = turn.workers()
    if not workers:
        return {}
    # Visible hostile crew near home is also a reason to keep defending.
    if any(enemy.health > 0 and enemy.kind in ('worker', 'pioneer')
           and any(distance(enemy.pos, worker.pos) <= 8 for worker in workers)
           for enemy in turn.enemies):
        return {}
    commands, serviced = {}, set()
    prices = shop_prices(payload)
    held = {item for worker in workers for item in worker.backpack}
    for role in workers:
        options = list(_maintenance(turn, role))
        for item, target in options:
            if item in role.backpack and (target is None or target not in serviced):
                commands[role.unit_id] = use_command(item, target)
                if target is not None:
                    serviced.add(target)
                break
        if role.unit_id in commands:
            continue
        if _near(turn, role, 'vendor'):
            vendor = vendor_prices(payload)
            for material, amount in sorted(Counter(role.backpack).items()):
                surplus = amount - (STONE_RESERVE if material == 'stone' else 0)
                if surplus > 0 and vendor.get(material, 0) > 0:
                    commands[role.unit_id] = sell_command(material, surplus)
                    break
        if role.unit_id in commands or role.backpack_full:
            continue
        if _near(turn, role, 'weaponShop'):
            for item, target in options:
                if item in held or target in serviced:
                    continue
                price = prices.get(item)
                if price is not None and 0 <= price <= available_gold(turn, payload, commands):
                    commands[role.unit_id] = buy_command(item)
                    held.add(item)
                    break

    # One stable worker may leave its post; the other must already be at a tower.
    # No persistent job or simulator seed is needed to resume/abort this work.
    scout = workers[-1]
    posts = {role.unit_id: tower for role, tower in pairs}
    post = posts.get(scout.unit_id)
    guard_ready = any(role.unit_id != scout.unit_id and role.unit_id in posts
                      and distance(role.pos, posts[role.unit_id].pos) <= 1 for role in workers)
    if scout.unit_id in commands or post is None or not guard_ready:
        return commands
    if scout.backpack_full or distance(scout.pos, post.pos) > WORK_RADIUS:
        return commands
    mines = [(pos, kind) for pos, kind in turn.zones.items()
             if kind in ('stone', 'iron', 'copper')
             and distance(pos, post.pos) <= WORK_RADIUS
             and ((kind == 'stone' and scout.backpack.count('stone') < STONE_RESERVE)
                  or (kind != 'stone' and len(scout.backpack) < ORE_BATCH
                      and vendor_prices(payload).get(kind, 0) > 0))]
    mines.sort(key=lambda pair: (pair[1] != 'stone', distance(scout.pos, pair[0]), pair[0].x, pair[0].y))
    blocked = turn.blocked(scout)
    for mine, _kind in mines:
        if distance(scout.pos, mine) == 1:
            commands[scout.unit_id] = collect_command(mine)
            return commands
        stands = [Pos(mine.x + dx, mine.y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                  if dx or dy]
        stands.sort(key=lambda pos: (distance(scout.pos, pos), pos.x, pos.y))
        for stand in stands:
            if not turn.land(stand) or stand in blocked or distance(stand, post.pos) > WORK_RADIUS:
                continue
            step = next_step(turn, scout, stand)
            if step is not None and distance(step, post.pos) <= WORK_RADIUS:
                commands[scout.unit_id] = move_command(step)
                return commands
    return commands
