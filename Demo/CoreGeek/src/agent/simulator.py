"""Deterministic local approximation, NOT an official judger.

Seeded scenarios include lifecycle via scenarios.py. Enemy players stay still;
robot navigation and non-rocket ballistics are approximations. See docs/DEMO.md.
"""
from collections import Counter
from copy import deepcopy
from typing import Any

from .brain import decide, judge_local_tasks, plan_for_state, respond, llm_router_enabled
from .planner import PlannerState
from .sandbox import ResponseBuilder
from .protocol import (LAND, STATION, WALL, Pos, TOWER_TYPES, Turn, distance,
                       station_footprint)
from .scenarios import observation, prepare_round, ROBOT_STATS
from . import ballistics, treasure, turnactions


def cells(unit):
    p = Pos.load(unit['pos'])
    return station_footprint(p) if unit['roleType'] == 'station' else (p,)


def frame_view(state):
    """Local render snapshot: stable identities + last committed health.

    This is a local extension used by web/debug only. It lives under the
    private `_demo` key, which observation() strips, so the competition
    policy can never read it. Identities let the viewer tell a builder apart
    from the building it raised in the same cell; retype lets it animate
    data changes instead of guessing them.
    """
    meta = state.get('_demo')
    if meta is None:
        return None
    ledger = meta.setdefault('vis', {})
    units, retype, next_uid = [], {}, int(ledger.get('seq', 0))

    def note(unit, owner):
        nonlocal next_uid
        key = 'unit:%s:%s' % (owner, unit['id'])
        if key not in ledger:
            next_uid += 1
            ledger[key] = next_uid
        return {'uid': ledger[key], 'key': key, 'owner': owner, 'id': unit['id'],
                'pos': dict(unit['pos'])}

    for unit in state['teamOur'].get('roles') or ():
        if unit.get('health', 0) > 0:
            units.append(note(unit, 'own'))
        else:
            retype['unit:own:%s' % unit['id']] = {'health': 0}
    for unit in (state.get('teamEnemy') or {}).get('roles') or ():
        if unit.get('health', 0) > 0:
            units.append(note(unit, 'enemy'))
        else:
            retype['unit:enemy:%s' % unit['id']] = {'health': 0}
    for robot in (state.get('robot') or {}).get('roles') or ():
        if robot.get('health', 0) > 0:
            units.append(note(robot, 'robot'))
        else:
            retype['unit:robot:%s' % robot['id']] = {'health': 0}
    ledger['seq'] = next_uid
    return {'units': units, 'retype': retype, 'roundNo': int(state['roundNo'])}


# Damage sources that a viewer must not confuse with official ballistics.
SIMPLIFIED_SOURCES = ('rocket', 'gatling', 'railgun', 'robot')
# User high-confidence real-match clarification (2026-09-22): a robot keeps
# its base-directed route when every friendly role is more than three cells
# away. This pursuit/deviation radius is separate from combat attack range.
ROBOT_DEVIATION_RADIUS = 3


def _clear_building(state: dict[str, Any], record: dict[str, Any]) -> None:
    """Remove a destroyed building from the map zones so its cells become walkable.

    The unit stays in the role list with health 0 (the judge keeps reporting it),
    but the terrain entry goes: otherwise `land()` keeps refusing that cell for the
    rest of the match and a breached wall ring never opens.
    """
    info = state.get("mapInfo") or {}
    target = record.get("pos") or {}
    info["zones"] = [
        zone for zone in info.get("zones") or ()
        if not (record.get("kind") == zone.get("neutralType")
                and zone.get("pos") == target)
    ]


def _nearest_building_cell(turn: Turn, origin: Pos) -> Pos | None:
    """Closest standing building cell, used when a robot has no unit to attack."""
    best: tuple[int, int, int, Pos] | None = None
    for unit in turn.ours:
        if unit.kind not in ATTACKABLE_BUILDING_KINDS or unit.health <= 0:
            continue
        for cell in (turn.footprint(unit) if unit.kind == STATION else (unit.pos,)):
            key = (distance(origin, cell), cell.x, cell.y, cell)
            if best is None or key[:3] < best[:3]:
                best = key
    return best[3] if best else None


def _blocking_building(turn: Turn, origin: Pos, goal: Pos) -> Pos | None:
    """The building that occupies the step a robot wants to take toward `goal`.

    Returns the blocking building's cell, or ``None`` when the way is clear or the
    obstacle is not a building (a role or another robot, which the caller handles
    by attacking the unit itself). Only the next cell matters: a robot attacks what
    is in front of it, exactly as 任务书 §4.7.3 describes.
    """
    dx = (goal.x > origin.x) - (goal.x < origin.x)
    dy = (goal.y > origin.y) - (goal.y < origin.y)
    if dx == 0 and dy == 0:
        return None
    nxt = Pos(origin.x + dx, origin.y + dy)
    # Only buildings are attackable. Mine cells, vendors and shops block movement
    # too, but 任务书 §4.7.3 names units and buildings — not neutral scenery — and
    # the item rules are explicit that bombs and dizzy only affect robots.
    if _building_at(turn, nxt) is not None:
        return nxt
    return None


def _building_at(turn: Turn, cell: Pos):
    """Resolve an actual living building from roles, including every base cell.

    mapInfo.zones describes neutral sites and may omit buildings or name a base
    challengerBase/defenderBase. Roles are the source of identity and health.
    """
    return next((unit for unit in turn.ours
                 if unit.kind in ATTACKABLE_BUILDING_KINDS and unit.health > 0
                 and cell in turn.footprint(unit)), None)


def _building_alive(turn: Turn, cell: Pos, kind: str) -> bool:
    """Whether the building on `cell` is still standing.

    A destroyed building keeps its role entry with health 0, so the map zone can
    outlive it: attacking that cell would be attacking rubble, and the cell must be
    walkable instead.
    """
    unit = _building_at(turn, cell)
    return unit is not None and unit.kind == kind


ATTACKABLE_BUILDING_KINDS = (STATION, WALL) + TOWER_TYPES


def _adjacent_building(turn: Turn, origin: Pos) -> Pos | None:
    """A building directly next to the robot, when it has no unit to chase.

    A robot that appears inside or against a wall must still be able to break out
    rather than stand still forever.
    """
    neighbours = sorted(
        (Pos(origin.x + dx, origin.y + dy)
         for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy),
        key=lambda pos: (pos.x, pos.y),
    )
    for cell in neighbours:
        if _building_at(turn, cell) is not None:
            return cell
    return None


def resolve_moves(moves: dict[str, Pos], origins: dict[str, Pos],
                  blocked: set[Pos]) -> tuple[list[tuple[str, Pos]], list[tuple[str, Pos, str]]]:
    """Resolve simultaneous movement intents (任务书 §4.5.4 第5条).

    ``origins`` includes stationary roles; ``blocked`` contains only hard
    obstacles, never dynamic role origins. A hard obstacle wins even when an
    invalid input has a role on the same cell. Reject contests and pair swaps,
    then propagate blocked departures. Chains and cycles of three or more
    distinct movers can vacate their cells simultaneously (任务书 §4.2).
    The returned list is a final-position batch, not sequential move commands.
    The official swap clause names two roles; applying that refusal to robot
    pairs is a conservative local extension, documented in SIM_JOINT_MOVEMENT.
    """
    occupants: dict[Pos, set[str]] = {}
    for uid, pos in origins.items():
        occupants.setdefault(pos, set()).add(uid)
    goals: dict[Pos, list[str]] = {}
    for uid, target in moves.items():
        goals.setdefault(target, []).append(uid)
    reasons: dict[str, str] = {}
    for uid, target in moves.items():
        if target in blocked:
            reasons[uid] = "目标格被永久障碍占用"
        elif len(goals[target]) > 1:
            reasons[uid] = "目标格被其他单位同时争抢"
        elif uid not in origins or target == origins[uid]:
            reasons[uid] = "移动未离开原位置"
    for uid, target in moves.items():
        for other in occupants.get(target, ()):
            if (uid in origins and other != uid and other in moves
                    and moves[other] == origins[uid]):
                reasons.setdefault(uid, "两移动单位互换位置发生碰撞")
                reasons.setdefault(other, "两移动单位互换位置发生碰撞")
    departing = set(moves) - set(reasons)
    while True:
        stopped = {uid for uid in departing
                   if occupants.get(moves[uid], set()) - departing}
        if not stopped:
            break
        for uid in stopped:
            reasons[uid] = "目标格已被占用且本轮不会空出"
        departing.difference_update(stopped)
    return ([(uid, moves[uid]) for uid in sorted(departing)],
            [(uid, moves[uid], reasons[uid]) for uid in sorted(reasons)])


def _movement_terrain(turn: Turn) -> set[Pos]:
    """Neutral/mineral cells, including the existing two-cell task-point model."""
    from .taskworld import point_cells
    return {cell for pos, kind in turn.zones.items() if kind != LAND
            for cell in point_cells(kind, pos)}


from .combat_geometry import intervening_wall as _intervening_wall


def _hard_movement_cells(turn: Turn) -> set[Pos]:
    """Buildings and neutral cells never vacate; enemy players stay stationary."""
    blocked = _movement_terrain(turn)
    fixed = (tuple(u for u in turn.ours if u.kind not in ('worker', 'pioneer'))
             + turn.enemies)
    for unit in fixed:
        if unit.health > 0:
            blocked.update(turn.footprint(unit))
    return blocked


def _settle_joint_moves(state, role_moves: dict[str, Pos], robot_moves: dict[str, Pos]):
    """Apply one shared collision batch. Robot keys have a separate id space.

    Inputs are already chosen, adjacent intents; neither collisions nor their
    propagation may trigger a second path choice in this round.
    """
    turn = Turn.load(state)
    own = {str(u['id']): u for u in state['teamOur']['roles']}
    robots = {str(u['id']): u for u in (state.get('robot') or {}).get('roles') or ()}
    origins = {str(u.unit_id): u.pos for u in turn.controllable()}
    origins.update({'robot:' + str(u.robot_id): u.pos
                    for u in turn.robots if u.health > 0})
    moves = dict(role_moves)
    moves.update({'robot:' + uid: target for uid, target in robot_moves.items()})
    resolved, rejected = resolve_moves(moves, origins, _hard_movement_cells(turn))
    role_records, robot_records = [], []
    for key, target in resolved:
        is_robot = key.startswith('robot:')
        unit = robots[key[6:]] if is_robot else own[key]
        # All acceptance decisions above used the same original positions.
        unit['pos'] = target.dump()
        if is_robot:
            robot_records.append({'robot': unit['id'], 'kind': unit.get('roleType'),
                                  'from': origins[key].dump(), 'to': target.dump()})
        else:
            role_records.append({'a': 'move', 'id': unit['id'], 'kind': unit['roleType'],
                                 'from': origins[key].dump(), 'to': target.dump()})
    return role_records, robot_records, rejected


def _allocate_robot_moves(turn, walkers, obstacles):
    """Local AI intent reservations, never sequential position settlement.

    Front ranks choose first, with round-rotated stable IDs within each rank.
    A robot may follow an already planned departure, but never assumes that an
    unplanned/staying robot will vacate. Final player/robot collisions are still
    resolved once by the shared resolver, without retrying failed intentions.
    """
    ranks = {}
    for row in walkers:
        robot, origin, goal = row[:3]
        ranks.setdefault(distance(origin, goal) if goal is not None else 0, []).append(row)
    unvacated = {row[1] for row in walkers}
    reserved, moves = set(), {}
    for rank in sorted(ranks):
        group = sorted(ranks[rank], key=lambda row: row[0]['id'])
        offset = (turn.round_no - 1) % len(group)
        for row in group[offset:] + group[:offset]:
            robot, origin, goal = row[:3]
            base_directed = bool(row[3]) if len(row) > 3 else False
            options = [Pos(origin.x + dx, origin.y + dy)
                       for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy]
            options = [q for q in options if turn.land(q) and q not in obstacles
                       and q not in unvacated and q not in reserved
                       and (goal is None or distance(q, goal) < distance(origin, goal))]
            if not options:
                continue
            target = min(options, key=lambda q: (
                distance(q, goal) if goal is not None else 0,
                abs(q.x - goal.x) + abs(q.y - goal.y) if base_directed and goal is not None else 0,
                q.x, q.y))
            moves[str(robot['id'])] = target
            reserved.add(target)
            unvacated.discard(origin)
    return moves


def _stable_base_goal(turn, origin):
    """Choose one deterministic entry cell on the station's approach side.

    Re-selecting the nearest cell of a 2x2 station every round can alternate
    between its upper and lower cells and create an artificial down-then-up
    path.  The side is chosen from the current approach vector, while the
    entry cell on that side is canonical and therefore remains stable.
    """
    base = turn.station()
    if base is None:
        return None
    cells = turn.footprint(base)
    xmin, xmax = min(c.x for c in cells), max(c.x for c in cells)
    ymin, ymax = min(c.y for c in cells), max(c.y for c in cells)
    cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
    dx, dy = origin.x - cx, origin.y - cy
    if abs(dx) >= abs(dy):
        return Pos(xmax if dx >= 0 else xmin, ymax)
    return Pos(xmax, ymax if dy >= 0 else ymin)


def _plan_robot_actions(state):
    """Local collision-aware AI, reading one pre-movement snapshot.

    Acquisition/path tie-breaks are local assumptions, not official AI. Selecting
    and locking a ranged attack before player movement is also a local timing
    assumption; the rule text does not specify that cross-action ordering.
    """
    turn = Turn.load(state)
    roles = state['teamOur']['roles']
    by_id = {str(u['id']): u for u in roles}
    robots = (state.get('robot') or {}).get('roles') or []
    hard_blocked = _hard_movement_cells(turn)
    robot_moves, robot_attacks = {}, []
    walkers = []
    if not turn.is_day:
        for robot in robots:
            if robot['health'] <= 0 or robot.get('abnormalState') == 'dizzy':
                continue
            # Do not redirect robots explicitly assigned to the other team.
            if robot.get('targetTeam', state['teamOur'].get('type')) != state['teamOur'].get('type'):
                continue
            p = Pos.load(robot['pos'])
            # Buildings are handled as obstacles, not as unit targets: a robot
            # attacks the wall in its way rather than "the nearest unit" (which
            # would often *be* that wall, and would blur two different behaviours
            # into one record type).
            units = [u for u in roles if u['health'] > 0
                     and u['roleType'] not in ATTACKABLE_BUILDING_KINDS
                     and min(distance(p, c) for c in cells(u)) <= ROBOT_DEVIATION_RADIUS]
            victim = None
            goal = None
            if units:
                victim = min(units, key=lambda u: min(distance(p, c) for c in cells(u)))
                goal = min(cells(victim), key=lambda c: distance(p, c))
            else:
                # User-confirmed behaviour: advance on the base and only
                # deviate for a nearby role within three cells. The exact
                # tie-break among several nearby roles remains local.
                base = turn.station()
                demo_profile = isinstance(state.get('_demo'), dict) and 'profile' in state['_demo']
                goal = (_stable_base_goal(turn, p) if base is not None and demo_profile
                        else (min(turn.footprint(base), key=lambda c: (distance(p, c), abs(p.x-c.x)+abs(p.y-c.y), c.x, c.y))
                              if base is not None else _nearest_building_cell(turn, p)))
            screening_wall = None
            if victim is not None and goal is not None and distance(p, goal) <= 3:
                screening_wall = _intervening_wall(turn, p, goal)
            if victim is not None and goal is not None and distance(p, goal) <= 3 and screening_wall is None:
                power = ROBOT_STATS.get(robot.get('roleType'), (40,5,1))[1]
                robot_attacks.append({'robot': robot['id'], 'kind': robot.get('roleType'),
                                      'victim': victim['id'], 'damage': power,
                                      'from': p.dump(), 'to': goal.dump()})
                continue
            # No unit in reach: hit whatever building is in the way. 任务书 §4.7.3
            # has robots attack blocking units *and buildings*, so a wall must be
            # breakable even when nothing else is nearby.
            if screening_wall is not None:
                blocked_by = screening_wall
            elif goal is not None:
                blocked_by = _blocking_building(turn, p, goal)
            else:
                blocked_by = _adjacent_building(turn, p)
            if blocked_by is not None:
                power = ROBOT_STATS.get(robot.get('roleType'), (40,5,1))[1]
                # Key the damage by the building's unit id: robot records and role
                # records use different id spaces, and a cell-keyed Counter silently
                # never matches a unit when the damage is committed.
                target_building = _building_at(turn, blocked_by)
                building_unit = by_id.get(str(target_building.unit_id)) if target_building else None
                if building_unit is None:
                    continue
                robot_attacks.append({'robot': robot['id'], 'kind': robot.get('roleType'),
                                      'building': int(building_unit['id']), 'damage': power,
                                      'buildingKind': building_unit['roleType'],
                                      'from': p.dump(), 'to': blocked_by.dump()})
                continue
            # Mark base-directed walkers so their tie-break can favor a
            # straight approach without changing the historical local order
            # used by independent robot-intent tests.
            walkers.append((robot, p, goal,
                            victim is None and turn.station() is not None
                            and isinstance(state.get('_demo'), dict)
                            and 'profile' in state['_demo']))
    # First classify actions for the entire unchanged snapshot. Attacking,
    # stunned and inactive robots are known not to vacate; route around them.
    # Plan non-conflicting robot destinations before the joint resolver. This
    # local AI coordination avoids repeatedly choosing an identical failed
    # destination set; it is not permission to ignore an actual collision.
    walker_ids = {row[0]['id'] for row in walkers}
    known_stationary = {Pos.load(robot['pos']) for robot in robots
                        if robot['health'] > 0 and robot['id'] not in walker_ids}
    obstacles = hard_blocked | known_stationary
    robot_moves = _allocate_robot_moves(turn, walkers, obstacles)
    return robot_moves, robot_attacks


def step(payload, commands=None, *, external_response=None):
    """Advance the environment; an external response bypasses local policy.

    ``commands`` keeps the older manual-debug semantics. The explicit external
    mode is for HTTP agent/replay validation: no local strategy, model request,
    receipt consumption or preview is run alongside the external decision.
    """
    if external_response is not None:
        if (commands is not None or not isinstance(external_response, dict)
                or set(external_response) - {'roleCommandMap', 'prompt', 'executeCmd'}
                or not isinstance(external_response.get('roleCommandMap'), dict)
                or any(not isinstance(k, str) or not isinstance(v, dict)
                       for k, v in external_response['roleCommandMap'].items())
                or any(k in external_response and not isinstance(external_response[k], str)
                       for k in ('prompt', 'executeCmd'))):
            raise ValueError('invalid_external_response')
    state = deepcopy(payload)
    turn = Turn.load(state)
    roles = state['teamOur']['roles']
    stations = [u for u in roles if u['roleType'] == 'station']
    if state.get('_demo', {}).get('finished') or turn.round_no > 1300 or not any(u['health'] > 0 for u in stations):
        return {'state': state, 'events': ['模拟已结束'], 'executed': {},
                'roleCommandMap': {}, 'done': True}
    # Plan with the same pipeline the judge entry uses, so a local run exercises
    # the real strategy (tasks included) rather than a reduced copy.
    meta = state.setdefault('_demo', {})
    planner_state = meta.get('planner') if external_response is None else PlannerState()
    if not isinstance(planner_state, PlannerState):
        # A state that travelled over HTTP carries the planner as a plain dict (it
        # cannot carry an object). Rebuild it instead of starting empty, so the local
        # viewer runs the same strategy as the judge path rather than a memoryless
        # copy of it.
        planner_state = PlannerState.load(planner_state)
        meta['planner'] = planner_state
    if external_response is None:
        planner_state.note_round(turn.round_no)
        planner_state.note_results(state, turn.round_no, routed=llm_router_enabled())
        planning = plan_for_state(state, planner_state, judge_tasks=False)
    else:
        planning = ResponseBuilder()
        planning.commands = deepcopy(external_response['roleCommandMap'])
        planning.prompt = external_response.get('prompt')
        planning.execute = external_response.get('executeCmd')
        meta['planner'] = None  # external agent owns its memory; do not invent a local copy
        meta['decision_source'] = 'external-response'
    # Judge replies belong to the previous request, not all future tasks.
    state['llmResp'] = ''
    state['lastCmdResult'] = ''
    # Errors describe the preceding operation, not every future observation.
    # The planner has consumed them above; this step publishes fresh feedback.
    state['errors'] = []
    state['lastSummonTreasureResult'] = 0
    plan_commands = planning.commands
    if external_response is None:
        planner_state.note_submission(planning.prompt, planning.execute, turn.round_no,
                                      bool(planner_state.tasks.get('cycle')))
        planner_state.last_round = turn.round_no
    commands = plan_commands if commands is None else commands
    by_id = {str(u['id']): u for u in roles}
    events, outcomes, damage = [], {}, Counter()
    actions = []
    alive_before = {str(u['id']): int(u.get('health') or 0) for u in roles}
    robots_before = {str(r['id']): {'health': int(r.get('health') or 0),
                                    'pos': Pos.load(r['pos']), 'type': r.get('roleType')}
                     for r in (state.get('robot') or {}).get('roles') or ()}
    gold_before = int(state['teamOur'].get('goldNum') or 0)
    occupied = set(turn.occupied_cells())  # already excludes dead units
    terrain = _movement_terrain(turn)
    blocked = occupied | terrain
    robots = (state.get('robot') or {}).get('roles') or []
    moves = {}
    spent_controllers = set()
    fired = set()
    collected = Counter()
    built_sites = set()
    # Deferred work: battle items resolve before robot movement, summon orders
    # land in the next wave, so both are queued instead of applied inline.
    effects = {'battle_items': [], 'summons': [], 'summons_used_today': 0}
    battle_records = []
    for uid, command in commands.items():
        unit = by_id.get(uid)
        outcomes[uid] = False
        if not unit or unit['health'] <= 0:
            continue
        pos = Pos.load(unit['pos'])
        target = Pos.load(command['targetPos'][0]) if command.get('targetPos') else None
        action = command['action']
        if action == 'move' and target and unit['roleType'] in ('worker', 'pioneer'):
            # Occupancy is not checked here: a cell held by a role that leaves this
            # round is legal (任务书 §4.2), and resolve_moves() decides that with the
            # full set of intents in hand. Only terrain is refused up front.
            if distance(pos, target) == 1 and turn.land(target) and target not in terrain:
                moves[uid] = target
        elif action == 'collect' and target and unit['roleType'] == 'worker':
            bag = unit.setdefault('backpack', [])
            kind = turn.zones.get(target)
            from .local_world_news import resource_paused
            if resource_paused(state, kind, turn.round_no):
                events.append(f'{uid} 采集未执行：本地新闻事件导致 {kind} 暂停开采')
                continue
            if distance(pos, target) == 1 and kind in ('stone', 'iron', 'copper') and len(bag) < unit.get('backPackCapability', 100):
                bag.append(kind)
                collected[(target.x, target.y, kind)] += 1
                outcomes[uid] = True
                events.append(f'{uid} 采集 {kind} +1')
                actions.append({'a': 'collect', 'id': unit['id'], 'kind': kind,
                                'from': pos.dump(), 'to': target.dump(),
                                'bag': len(bag), 'cap': unit.get('backPackCapability', 0)})
        elif action == 'build' and turn.is_day and target and unit['roleType'] == 'worker':
            kind = command.get('name')
            station = turn.station()
            radius = min(distance(target, p) for p in station_footprint(station.pos)) if station else -1
            # Task book 4.5.1: a new level-1 weapon covers the existing weapon
            # at that cell. Replacement still costs 25 gold but no extra slot.
            previous = next((u for u in roles if u['health'] > 0 and u['roleType'] in TOWER_TYPES
                             and Pos.load(u['pos']) == target), None) if kind in TOWER_TYPES else None
            replaceable = False
            if previous is not None and target not in terrain:
                others = [u for u in turn.ours + turn.enemies if u.health > 0 and u.unit_id != previous['id']]
                replaceable = not any(target in (station_footprint(u.pos) if u.kind == 'station' else (u.pos,))
                                      for u in others)
            if (target in built_sites or (target in blocked and not replaceable)
                    or not turn.land(target) or distance(pos, target) != 1):
                continue
            bag = unit.setdefault('backpack', [])
            if kind == 'wall' and radius == 2 and 'stone' in bag:
                bag.remove('stone')
            elif kind in TOWER_TYPES and radius == 1 and state['teamOur']['goldNum'] >= 25 and sum(u['health'] > 0 and u['roleType'] in TOWER_TYPES for u in roles) - int(previous is not None) < 3:
                state['teamOur']['goldNum'] -= 25
            else:
                continue
            prefix = 10000 if state['teamOur'].get('type') == 'challenger' else 20000
            start = prefix + {'gatling':20, 'railgun':30, 'rocket':40}.get(kind, 0)
            if kind == 'wall':
                start = 40000 if prefix == 10000 else 41000
            replaced = [previous['id']] if previous is not None else []
            if previous is not None:
                roles[:] = [u for u in roles if u['id'] != previous['id']]
            used_ids = {u['id'] for u in roles if u['health'] > 0}
            new_id = next(i for i in range(start, start+(1000 if kind=='wall' else 3)) if i not in used_ids)
            roles[:] = [u for u in roles if u['id'] != new_id]
            roles.append({'id': new_id, 'roleType': kind, 'pos': target.dump(),
                          'health': 1000, 'level': 1, 'cooldown': 0})
            blocked.add(target)
            built_sites.add(target)
            outcomes[uid] = True
            events.append(f'{uid} 建造 {kind} @ ({target.x}, {target.y})')
            actions.append({'a': 'build', 'id': unit['id'], 'kind': kind,
                            'newId': new_id, 'from': pos.dump(), 'to': target.dump(),
                            'replaced': replaced})
        elif action == 'remove' and target and unit['roleType'] == 'worker':
            try:
                record = turnactions.remove_wall(state, unit, target=target)
            except turnactions.Refusal as refusal:
                events.append(f'{uid} 拆除未执行：{refusal}')
            else:
                outcomes[uid] = True
                actions.append(record)
                events.append(f"{uid} 拆除围墙 @ ({target.x}, {target.y})（不退还石头）")
        elif action == 'sell':
            material = command.get('name')
            amount = int(command.get('num') or 1)
            try:
                record = turnactions.sell(state, unit, material=material, amount=amount)
            except (
                turnactions.Refusal,
            ) as refusal:
                events.append(f'{uid} 贩卖未执行：{refusal}')
            else:
                outcomes[uid] = True
                actions.append(record)
                events.append(f"{uid} 卖出 {material}×{amount}，+{record['gold']} 金币")
        elif action == 'buy':
            item = command.get('name')
            amount = int(command.get('num') or 1)
            try:
                record = turnactions.buy(state, unit, item=item, amount=amount)
            except turnactions.Refusal as refusal:
                events.append(f'{uid} 购买未执行：{refusal}')
            else:
                outcomes[uid] = True
                actions.append(record)
                events.append(f"{uid} 购买 {item}×{amount}，-{abs(record['gold'])} 金币")
        elif action == 'use':
            item = command.get('name')
            try:
                record = turnactions.use(state, unit, item=item, target=target,
                                         effects=effects)
            except turnactions.Refusal as refusal:
                events.append(f'{uid} 使用 {item} 未生效：{refusal}')
            else:
                outcomes[uid] = True
                actions.append(record)
                events.append(f"{uid} 使用 {item}（{turnactions.describe_effect(record)}）")
        elif action == 'acceptTask' and unit['roleType'] == 'pioneer':
            # The judge fixture owns the task text and the answer key; here we
            # only verify the official precondition (own point, pioneer alive).
            zone = turnactions.own_task_point(state, pos)
            if zone is None:
                events.append(f'{uid} 领取任务未执行：不在己方任务点周围一格内')
            else:
                outcomes[uid] = True
                actions.append({'a': 'acceptTask', 'id': unit['id'],
                                'from': pos.dump(), 'to': pos.dump(),
                                'point': dict(zone['pos'])})
                events.append(f"{uid} 在己方任务点领取任务")
        elif action == 'submitAnswer' and unit['roleType'] == 'pioneer':
            answer = str(command.get('taskAnswer') or '')
            if not answer.strip() or not state.get('phaseTask'):
                events.append(f'{uid} 提交答案未执行：没有已领取的任务或答案为空')
            else:
                outcomes[uid] = True
                actions.append({'a': 'submitAnswer', 'id': unit['id'],
                                'answer': answer[:200], 'from': pos.dump(),
                                'to': pos.dump()})
                events.append(f"{uid} 提交任务答案（{len(answer)} 字符）")
        elif action == 'summonTreasure' and unit['roleType'] == 'pioneer':
            # Interface 2.2: a legal failed sacrifice still spends the items.
            offered = command.get('item') or []
            if isinstance(offered, str):
                offered = [offered]
            try:
                if target is None or len(command.get('targetPos') or []) != 1:
                    raise treasure.Refusal('召唤宝藏需要一个目标位置')
                record = treasure.summon(
                    state, side=state['teamOur'].get('type', ''), pioneer_id=unit['id'],
                    site=target, items=offered, round_no=turn.round_no)
            except treasure.Refusal as refusal:
                events.append(f'{uid} 召唤宝藏未执行：{refusal}')
            else:
                outcomes[uid] = True
                actions.append({'a': 'summonTreasure', 'id': unit['id'],
                                'from': pos.dump(), 'to': dict(record['site']),
                                'items': record['items'], 'score': record['score'],
                                'gold': record['gold'], 'result': record['result']})
                outcome_label = '开启宝藏' if record['result'] == 1 else f"未获宝藏，结果码 {record['result']}"
                events.append(
                    f"{uid} 献祭 {'、'.join(record['items'])}，{outcome_label} "
                    f"（积分 +{record['score']}，金币 +{record['gold']}）")
        elif action == 'attack' and not turn.is_day and unit['roleType'] in TOWER_TYPES:
            controller = by_id.get(str(command.get('controllerId')))
            if not controller or controller['roleType'] not in ('worker', 'pioneer') or controller['health'] <= 0 or controller['id'] in spent_controllers or str(controller['id']) in commands or distance(pos, Pos.load(controller['pos'])) > 1 or unit.get('cooldown', 0) > 0:
                continue
            tower = next(u for u in turn.weapons() if str(u.unit_id) == uid)
            targets = [Pos.load(p) for p in command.get('targetPos', [])]
            count = 1 if unit['roleType'] == 'railgun' else max(1, min(unit.get('level', 1), 3))
            if not targets or len(targets) != count or any(distance(pos, p) > tower.range_of_attack() for p in targets):
                continue
            if unit['roleType'] in ('gatling', 'railgun'):
                # Official straight-line ballistics (任务书 §4.5.4), including the
                # gatling's 90° cone and the railgun's energy penetration.
                volley = ballistics.tower_volley(unit['roleType'], pos,
                                                 int(unit.get('level') or 1), targets, robots)
                if not volley.get('legal'):
                    events.append(f"{uid} 开火非法：{volley.get('reason')}")
                    continue
                spent_controllers.add(controller['id'])
                outcomes[uid] = True
                for robot_id, amount in ballistics.damage_map(volley).items():
                    damage[robot_id] += amount
                salvos = [{'cell': shot['cell'], 'path': shot.get('path'),
                           'hits': ([{'robot': shot['robot'], 'damage': shot['damage'],
                                      'cell': shot['cell']}] if shot.get('robot') is not None else []),
                           'blocked': shot.get('blocked', False)}
                          for shot in volley.get('shots') or []]
                if unit['roleType'] == 'railgun':
                    salvos = [{'cell': targets[0].dump(), 'path': volley.get('path'),
                               'hits': [{'robot': hit['robot'], 'damage': hit['damage'],
                                         'cell': hit['cell'], 'energyLeft': hit['energyLeft']}
                                        for hit in volley.get('hits') or []],
                               'energy': volley.get('energy'), 'remaining': volley.get('remaining')}]
                hits = sum(len(salvo['hits']) for salvo in salvos)
                total = sum(hit['damage'] for salvo in salvos for hit in salvo['hits'])
                events.append(f'{uid} 开火：{hits} 次命中 / {total} 伤害（直线弹道，沿路径命中）')
                actions.append({'a': 'attack', 'id': unit['id'], 'kind': unit['roleType'],
                                'level': int(unit.get('level') or 1), 'from': pos.dump(),
                                'controller': controller['id'], 'salvos': salvos,
                                'source': 'ballistics'})
                continue
            spent_controllers.add(controller['id'])
            salvos = []
            for target in targets:
                salvo = []
                for robot in robots:
                    d = distance(target, Pos.load(robot['pos']))
                    if d == 0:
                        damage[robot['id']] += 20
                        salvo.append({'robot': robot['id'], 'damage': 20, 'cell': target.dump()})
                    elif d == 1:
                        damage[robot['id']] += 10
                        salvo.append({'robot': robot['id'], 'damage': 10, 'cell': target.dump()})
                salvos.append({'cell': target.dump(), 'path': [pos.dump(), target.dump()],
                               'hits': salvo, 'blocked': False})
            unit['cooldown'] = 3
            fired.add(uid)
            outcomes[uid] = True
            events.append(f'{uid} 开火（火箭：中心20，周围8格10，不被阻挡）')
            actions.append({'a': 'attack', 'id': unit['id'], 'kind': unit['roleType'],
                            'level': int(unit.get('level') or 1), 'from': pos.dump(),
                            'controller': controller['id'], 'salvos': salvos,
                            'source': 'missile'})
    # Items resolve before robot movement (任务书 §4.6.3 note), so a bomb or a
    # dizzy lands on robots that then lose their turn.
    battle_records = turnactions.apply_battle_items(state, effects)
    for record in battle_records:
        if record['a'] == 'bomb':
            events.append(f"范围炸弹命中机器人 {record['robot']}，伤害 {record['damage']}")
        else:
            events.append(f"眩晕法宝命中机器人 {record['robot']}，眩晕 {record['rounds']} 回合")
    actions.extend(battle_records)
    # Attacks and paths are selected before either side's movement is written.
    # The AI remains a local assumption; collision outcomes use official R02.
    robot_intents, robot_attacks = _plan_robot_actions(state)
    role_damage, building_damage = Counter(), Counter()
    for record in robot_attacks:
        if 'victim' in record:
            role_damage[record['victim']] += record['damage']
            target_id = record['victim']
        else:
            building_damage[record['building']] += record['damage']
            target_id = record['building']
        events.append(f"机器人 {record['robot']} 攻击 {target_id}，伤害 {record['damage']}")
    role_moves, robot_moves, rejected = _settle_joint_moves(state, moves, robot_intents)
    for record in role_moves:
        uid = str(record['id'])
        outcomes[uid] = True
        events.append(f"{uid} 移动至 ({record['to']['x']}, {record['to']['y']})")
    actions.extend(role_moves)
    for uid, target, why in rejected:
        events.append(f'{uid} 移动未执行：{why}')
    # Retain existing settlement order: shots read pre-movement positions and
    # their damage commits at turn end; lethally shot robots still take a turn.
    for unit in roles:
        unit['health'] = max(0, unit['health'] - role_damage[unit['id']] - building_damage[unit['id']])
    building_deaths = [
        {'id': unit['id'], 'kind': unit['roleType'], 'pos': unit['pos']}
        for unit in roles
        if alive_before.get(str(unit['id']), 0) > 0 and unit['health'] <= 0
        and unit['roleType'] in ('wall',) + TOWER_TYPES
    ]
    for record in building_deaths:
        events.append(f"建筑 {record['id']}（{record['kind']}）被机器人摧毁")
        # A destroyed building must stop blocking its cell. Leaving the zone entry
        # behind (and the role in the list) would keep the cell impassable for the
        # rest of the match, so a broken wall ring would never actually open.
        _clear_building(state, record)
    unit_deaths = [{'id': u['id'], 'kind': u['roleType'], 'pos': u['pos']}
                   for u in roles
                   if alive_before.get(str(u['id']), 0) > 0 and u['health'] <= 0]
    robot_deaths = []
    for robot in robots:
        previous = robot['health']
        robot['health'] = max(0, previous - damage[robot['id']])
        if previous > 0 and robot['health'] == 0:
            score = ROBOT_STATS.get(robot.get('roleType'), (40,5,1))[2]
            state['teamOur']['totalScore'] = state['teamOur'].get('totalScore', 0) + score
            if '_demo' in state:
                state['_demo']['kills'] = int(state['_demo'].get('kills') or 0) + 1
            robot_deaths.append({'robot': robot['id'], 'kind': robot.get('roleType'),
                                 'pos': robot['pos'], 'score': score})
        if damage[robot['id']]:
            events.append(f"机器人 {robot['id']} 受到 {damage[robot['id']]} 伤害")
    for unit in roles:
        if str(unit['id']) not in fired and unit.get('cooldown', 0) > 0:
            unit['cooldown'] -= 1
    fresh_dizzy = {record['robot'] for record in battle_records if record['a'] == 'dizzy'}
    for record in turnactions.tick_dizzy(state, freshly_dizzy=fresh_dizzy):
        events.append(f"机器人 {record['robot']} 眩晕结束")
    skipped = []
    for uid, ok in outcomes.items():
        if not ok:
            events.append(f'{uid} 指令未执行（简化规则校验或碰撞）')
            skipped.append(uid)
    state['lastRoundRoleActionResults'] = outcomes
    done = turn.round_no >= 1300 or not any(u['health'] > 0 for u in stations)
    state['roundNo'] = min(1300, turn.round_no + 1)
    before_view = (state.get('_demo') or {}).get('vis_prev')
    gold_after = int(state['teamOur'].get('goldNum') or 0)
    meta = state.get('_demo')
    spawned = []
    if meta is not None:
        # A generated scenario carries a full lifecycle; an imported request
        # snapshot may carry `_demo` without it, so never assume the keys.
        meta['elapsed'] = int(meta.get('elapsed') or 0) + 1
        meta['finished'] = done
        # Summon orders bought now land on the opponent's next night (R06).
        for team, load in turnactions.summon_load(effects, state['teamOur'].get('type')).items():
            pending = meta.setdefault('summon_load', {})
            for kind, amount in load.items():
                pending[kind] = int(pending.get(kind, 0)) + amount
        for (x,y,kind), amount in collected.items():
            key = f'{x},{y}'
            mines = meta.setdefault('mines', {})
            mines[key] = mines.get(key, 10) - amount
            if mines[key] <= 0:
                state['mapInfo']['zones'] = [z for z in state['mapInfo']['zones'] if z['pos'] != {'x':x,'y':y}]
                meta['refresh'] = meta.get('refresh', []) + [kind]
                del mines[key]
        if turn.round_no % 130 == 0 and any(u['health'] > 0 for u in stations):
            state['teamOur']['totalScore'] += 10*(turn.round_no//130)
        if not done:
            spawned = prepare_round(state, events)
    # One local render frame per round: what actually happened, derived from the
    # same pass that produced the state. Never used by the competition policy.
    if meta is not None:
        # Keep the engine-owned records the local judge fixture reads (last
        # round's commands) outside the published fields.
        engine = state.setdefault('_engine', {})
        engine['lastCommands'] = {str(k): v for k, v in commands.items()}
    # Judge the round with the commands that really ran, so the task fixture
    # publishes phaseTask/playerTasks exactly where the official judge would.
    judge_local_tasks(state, commands, planner_state)
    frame = {'round': turn.round_no,
             'actions': actions,
             'robotMoves': robot_moves,
             'robotAttacks': robot_attacks,
             'unitDeaths': unit_deaths,
             'robotDeaths': robot_deaths,
             'spawned': spawned,
             'skipped': skipped,
             'gold': {'before': gold_before, 'after': gold_after},
             'view': frame_view(state)}
    if before_view is not None:
        frame['moved'] = [{'uid': cur['uid'], 'key': cur['key'], 'from': prev['pos'], 'to': cur['pos']}
                          for prev, cur in _pair_positions(before_view, frame['view'])]
        frame['changed'] = [{'key': key, 'fields': fields}
                            for key, fields in _pair_fields(before_view, frame['view'])]
    if meta is not None:
        meta['vis_prev'] = frame['view']
    # The official response for the *new* state: same pipeline as the judge path,
    # so the preview a user sees is the command map the judge would receive. It is
    # planned without committing, so a preview can never accept or end a task.
    if done or external_response is not None:
        preview: dict[str, Any] = {}
    else:
        preview = plan_for_state(state, planner_state, commit=False,
                                 judge_tasks=False).commands
    return {'state': state, 'executed': commands, 'events': events or ['本回合无行动'],
            'roleCommandMap': preview, 'done': done,
            'frame': frame, 'judgeRequest': {k: v for k, v in planning.build().items()
                                            if k in ('prompt', 'executeCmd')}}


def _pair_positions(before, after):
    """Yield (previous, current) for each identity present in both frames."""
    index = {unit['key']: unit for unit in (before or {}).get('units') or ()}
    for unit in (after or {}).get('units') or ():
        previous = index.get(unit['key'])
        if previous is not None:
            yield previous, unit


def _pair_fields(before, after):
    """Report value changes for identities that exist but changed kind/owner."""
    index = {unit['key']: unit for unit in (before or {}).get('units') or ()}
    for unit in (after or {}).get('units') or ():
        previous = index.get(unit['key'])
        if previous is not None and previous != unit:
            fields = [name for name in ('owner', 'id') if previous[name] != unit[name]]
            if fields:
                yield unit['key'], fields
