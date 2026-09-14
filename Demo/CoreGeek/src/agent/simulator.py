"""Deterministic local approximation, NOT an official judger.

Seeded scenarios include lifecycle via scenarios.py. Enemy players stay still;
robot navigation and non-rocket ballistics are approximations. See docs/DEMO.md.
"""
from collections import Counter
from copy import deepcopy
from typing import Any

from .brain import decide, judge_local_tasks, plan_for_state, respond
from .planner import PlannerState
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
    """Resolve one round of movement intents.

    Officially every role acts in the same round, so a cell that a role leaves
    this round is free for a role stepping into it. Rejecting anything whose
    destination happened to be occupied when the round *started* was the largest
    single source of "指令未执行" and is not what 任务书 §4.2 describes.

    The resolution is a fixpoint over "who is leaving":

    * two roles aimed at the same cell — nobody moves (目标争抢);
    * a role aimed at a cell held by a role that is *not* leaving — refused;
    * a role aimed at a cell held by a role that *is* leaving — allowed, and its
      own cell becomes free for the next pass (this is what lets a whole chain
      step forward in one round);
    * a swap — both move;
    * a cycle (A→B→C→A) — nobody moves, because no single step completes without a
      temporary overlap the grid does not allow.

    Successful movers are emitted in dependency order (the occupant of a target
    moves first), so applying them one at a time never writes a unit onto an
    occupied cell. Order is deterministic, so a replay is reproducible.

    Cell occupancy comes from `origins`; `blocked` is only the *terrain* that can
    never be entered. Passing occupied cells inside `blocked` would make a legal
    swap look like a step into a wall, so the two are kept separate here (any
    overlap is removed) to make the contract hard to get wrong.
    """
    def key(pos: Pos) -> tuple[int, int]:
        return (int(pos.x), int(pos.y))

    occupied_by: dict[tuple[int, int], str] = {}
    for uid, origin in origins.items():
        occupied_by.setdefault(key(origin), uid)
    blocked_keys = {key(pos) for pos in blocked} - set(occupied_by)
    goals: dict[tuple[int, int], list[str]] = {}
    for uid, target in moves.items():
        goals.setdefault(key(target), []).append(uid)

    contested = {target for target, uids in goals.items() if len(uids) > 1}

    # Reachability fixpoint. A move is possible when its destination is empty or is
    # held by a mover that is itself leaving. Removing impossible moves can make
    # others impossible (its holder is no longer leaving), so iterate.
    departing = {uid for uid, target in moves.items() if key(target) not in contested}
    changed = True
    while changed:
        changed = False
        # Snapshot before iterating: mutating `departing` mid-loop made the
        # membership test read a half-updated set and reject legal swaps.
        for uid in sorted(list(departing)):
            if uid not in departing:
                continue
            target = key(moves[uid])
            if target in blocked_keys and target not in occupied_by:
                departing.discard(uid)   # permanent terrain
                changed = True
                continue
            occupant = occupied_by.get(target)
            if occupant is not None and occupant != uid and occupant not in departing:
                departing.discard(uid)   # held by someone who stays put
                changed = True

    # Order the survivors so that applying them one at a time never writes a unit
    # onto an occupied cell. Greedily take any move whose destination is currently
    # free; committing it frees its own cell, which can unblock the next. A chain
    # therefore resolves end-first, and a swap resolves once one leg is taken.
    #
    # A cycle that frees nothing (A→B→C→A) never becomes available and is dropped:
    # the grid allows no temporary overlap, so no single step can complete.
    rejected_cycle: list[tuple[str, Pos, str]] = []
    order: list[str] = []
    pending = set(departing)
    current: dict[tuple[int, int], str] = dict(occupied_by)
    # A straight swap is officially legal: apply the two legs as a pair, since
    # neither can go first on its own. This is what lets two roles pass each other
    # in a corridor instead of one of them losing the round.
    for _ in range(len(pending)):
        pair = None
        for uid in sorted(pending):
            other = current.get(key(moves[uid]))
            if (other is None or other == uid or other not in pending
                    or current.get(key(moves[other])) != uid):
                continue
            if key(moves[uid]) in blocked_keys or key(moves[other]) in blocked_keys:
                continue
            pair = (uid, other)
            break
        if pair is None:
            break
        first, second = pair
        current.pop(key(origins[first]), None)
        current.pop(key(origins[second]), None)
        current[key(moves[first])] = first
        current[key(moves[second])] = second
        order.extend([first, second])
        pending.discard(first)
        pending.discard(second)
    guard = len(pending) + 2
    while pending and guard > 0:
        guard -= 1
        progressed = False
        for uid in sorted(pending):
            target = key(moves[uid])
            occupant = current.get(target)
            if occupant is not None and occupant != uid:
                continue
            if target in blocked_keys and occupant is None:
                continue
            origin = key(origins[uid])
            if current.get(origin) != uid or occupant == uid:
                continue
            current.pop(origin, None)
            current[target] = uid
            order.append(uid)
            pending.discard(uid)
            progressed = True
        if not progressed:
            break
    for uid in sorted(pending):
        rejected_cycle.append((uid, moves[uid], "移动意图形成闭环，本轮无法单步完成"))

    resolved: list[tuple[str, Pos]] = [(uid, moves[uid]) for uid in order]

    rejected: list[tuple[str, Pos, str]] = list(rejected_cycle)
    contested_ids: set[str] = set()
    for target in sorted(contested):
        for uid in sorted(goals[target]):
            rejected.append((uid, moves[uid], "目标格被其他角色同时争抢"))
            contested_ids.add(uid)
    moved = {uid for uid, _target in resolved}
    for uid in sorted(set(moves) - moved - contested_ids - {u for u, _p, _w in rejected_cycle}):
        target = key(moves[uid])
        reason = ("目标格被永久障碍占用"
                  if target in blocked_keys and target not in occupied_by
                  else "目标格已被占用且本轮不会空出")
        rejected.append((uid, moves[uid], reason))
    return resolved, rejected


def step(payload, commands=None):
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
    planner_state = meta.get('planner')
    if not isinstance(planner_state, PlannerState):
        # A state that travelled over HTTP carries the planner as a plain dict (it
        # cannot carry an object). Rebuild it instead of starting empty, so the local
        # viewer runs the same strategy as the judge path rather than a memoryless
        # copy of it.
        planner_state = PlannerState.load(planner_state)
        meta['planner'] = planner_state
    planner_state.note_round(turn.round_no)
    planner_state.note_results(state, turn.round_no)
    planning = plan_for_state(state, planner_state, judge_tasks=False)
    # Judge replies belong to the previous request, not all future tasks.
    state['llmResp'] = ''
    state['lastCmdResult'] = ''
    # Errors describe the preceding operation, not every future observation.
    # The planner has consumed them above; this step publishes fresh feedback.
    state['errors'] = []
    state['lastSummonTreasureResult'] = 0
    plan_commands = planning.commands
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
    occupied = set(turn.occupied_cells())
    # `occupied_cells()` counts every listed unit, including ones at 0 health, so a
    # destroyed building (whose role entry the judge keeps reporting) would hold its
    # cell forever. A dead unit holds nothing: the role list keeps the record, the
    # ground is free. Terrain is separate and still takes precedence for buildings.
    for unit in turn.ours + turn.enemies:
        if unit.health <= 0 and unit.pos in occupied:
            occupied.discard(unit.pos)
    # Terrain is kept apart from occupancy: a cell held by a role that leaves this
    # round is a legal destination, but a wall or a base never is.
    terrain = {p for p, kind in turn.zones.items() if kind != 'land'}
    blocked = occupied | terrain
    robots = (state.get('robot') or {}).get('roles') or []
    moves = {}
    spent_controllers = set()
    fired = set()
    collected = Counter()
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
            if target in blocked or not turn.land(target) or distance(pos, target) != 1:
                continue
            bag = unit.setdefault('backpack', [])
            if kind == 'wall' and radius == 2 and 'stone' in bag:
                bag.remove('stone')
            elif kind in TOWER_TYPES and radius == 1 and state['teamOur']['goldNum'] >= 25 and sum(u['health'] > 0 and u['roleType'] in TOWER_TYPES for u in roles) < 3:
                state['teamOur']['goldNum'] -= 25
            else:
                continue
            prefix = 10000 if state['teamOur'].get('type') == 'challenger' else 20000
            start = prefix + {'gatling':20, 'railgun':30, 'rocket':40}.get(kind, 0)
            if kind == 'wall':
                start = 40000 if prefix == 10000 else 41000
            used_ids = {u['id'] for u in roles if u['health'] > 0}
            new_id = next(i for i in range(start, start+(1000 if kind=='wall' else 3)) if i not in used_ids)
            replaced = [u['id'] for u in roles if u['id'] == new_id and u['health'] > 0]
            roles[:] = [u for u in roles if u['id'] != new_id]
            roles.append({'id': new_id, 'roleType': kind, 'pos': target.dump(),
                          'health': 1000, 'level': 1, 'cooldown': 0})
            blocked.add(target)
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
    # Movement: intents are collected first and then resolved as one set, so a
    # role may step into a cell that another role is leaving this same round.
    # Rejecting those outright was the single largest source of "指令未执行"
    # (measured: hundreds per match) and is not what the rules describe — 任务书
    # §4.2 has both teams' roles act in the same round.
    origins = {uid: Pos.load(by_id[uid]['pos']) for uid in moves}
    resolved, rejected = resolve_moves(moves, origins, terrain)
    for uid, target in resolved:
        by_id[uid]['pos'] = target.dump()
        outcomes[uid] = True
        events.append(f'{uid} 移动至 ({target.x}, {target.y})')
        actions.append({'a': 'move', 'id': by_id[uid]['id'], 'kind': by_id[uid]['roleType'],
                        'from': origins[uid].dump(), 'to': target.dump()})
    for uid, target, why in rejected:
        events.append(f'{uid} 移动未执行：{why}')
    # Items resolve before robot movement (任务书 §4.6.3 note), so a bomb or a
    # dizzy lands on robots that then lose their turn.
    battle_records = turnactions.apply_battle_items(state, effects)
    for record in battle_records:
        if record['a'] == 'bomb':
            events.append(f"范围炸弹命中机器人 {record['robot']}，伤害 {record['damage']}")
        else:
            events.append(f"眩晕法宝命中机器人 {record['robot']}，眩晕 {record['rounds']} 回合")
    actions.extend(battle_records)
    role_damage = Counter()
    building_damage = Counter()
    robot_moves = []
    robot_attacks = []
    # Sequential greedy robot movement: deliberately conservative and deterministic.
    if not turn.is_day:
        refreshed = Turn.load(state)
        obstacles = set(refreshed.occupied_cells()) | {p for p, k in turn.zones.items() if k != 'land'}
        # Dead units hold nothing: a destroyed building's cell is rubble to walk
        # over, and the role list still carries its record at 0 health.
        for unit in refreshed.ours + refreshed.enemies:
            if unit.health <= 0:
                obstacles.discard(unit.pos)
        # 任务书 §4.7.3: "机器人会攻击阻挡其移动的单位（包括角色/建筑）". A robot
        # blocked by a building therefore attacks *that building* rather than
        # picking the nearest unit — which is what makes a wall ring a delaying
        # shield instead of an absolute one, and lets a robot break through to the
        # base behind it.
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
                     and u['roleType'] not in ATTACKABLE_BUILDING_KINDS]
            victim = None
            goal = None
            if units:
                victim = min(units, key=lambda u: min(distance(p, c) for c in cells(u)))
                goal = min(cells(victim), key=lambda c: distance(p, c))
            else:
                # Nothing to chase: head for the nearest building instead of
                # wandering. Without this a robot with no unit in the list picked
                # an arbitrary free cell each round (every candidate scored as
                # "equally far" from a None goal) and drifted off the board.
                goal = _nearest_building_cell(refreshed, p)
            if victim is not None and goal is not None and distance(p, goal) <= 3:
                power = ROBOT_STATS.get(robot.get('roleType'), (40,5,1))[1]
                role_damage[victim['id']] += power
                events.append(f"机器人 {robot['id']} 攻击 {victim['id']}，伤害 {power}")
                robot_attacks.append({'robot': robot['id'], 'kind': robot.get('roleType'),
                                      'victim': victim['id'], 'damage': power,
                                      'from': p.dump(), 'to': goal.dump()})
                continue
            # No unit in reach: hit whatever building is in the way. 任务书 §4.7.3
            # has robots attack blocking units *and buildings*, so a wall must be
            # breakable even when nothing else is nearby.
            if goal is not None:
                blocked_by = _blocking_building(refreshed, p, goal)
            else:
                blocked_by = _adjacent_building(refreshed, p)
            if blocked_by is not None:
                power = ROBOT_STATS.get(robot.get('roleType'), (40,5,1))[1]
                # Key the damage by the building's unit id: robot records and role
                # records use different id spaces, and a cell-keyed Counter silently
                # never matches a unit when the damage is committed.
                target_building = _building_at(refreshed, blocked_by)
                building_unit = by_id.get(str(target_building.unit_id)) if target_building else None
                if building_unit is None:
                    continue
                building_damage[int(building_unit['id'])] += power
                events.append(f"机器人 {robot['id']} 攻击建筑 {building_unit['id']}，伤害 {power}")
                robot_attacks.append({'robot': robot['id'], 'kind': robot.get('roleType'),
                                      'building': int(building_unit['id']), 'damage': power,
                                      'buildingKind': building_unit['roleType'],
                                      'from': p.dump(), 'to': blocked_by.dump()})
                continue
            options = [Pos(p.x + dx, p.y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy]
            options = [q for q in options if refreshed.land(q) and q not in obstacles
                       and (goal is None or distance(q, goal) < distance(p, goal))]
            if options:
                q = min(options, key=lambda c: (distance(c, goal) if goal is not None else 0,
                                                c.x, c.y))
                obstacles.discard(p)
                obstacles.add(q)
                robot['pos'] = q.dump()
                robot_moves.append({'robot': robot['id'], 'kind': robot.get('roleType'),
                                    'from': p.dump(), 'to': q.dump()})
    # Official order: shots resolve at pre-movement positions, damage commits
    # at turn end. A robot hit lethally can still take its current turn.
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
    if done:
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
