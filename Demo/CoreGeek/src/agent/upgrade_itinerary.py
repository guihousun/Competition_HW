"""Observation-derived worker shopping and voucher delivery, R02/R06.

One worker travels; one remains home. No hidden map or future wave inputs.
Costs include paths, buy/use rounds and a legal return to a sheltered gun post.
"""
from collections import deque
from .protocol import Pos, WALL, STATION, TOWER_TYPES, MEDICINE, WALL_FIXER, distance, buy_command, use_command, move_command
from .market import VOUCHER_TARGETS, can_upgrade, shop_prices
from .coordination import available_gold
from .defense_layout import wall_priority
from . import strategy_config, frontline


def routes(turn, role, start):
    blocked=turn.blocked(role)
    distances={start:0}; first={start:None}; queue=deque([start])
    while queue:
        cell=queue.popleft()
        for dx in (-1,0,1):
            for dy in (-1,0,1):
                if not (dx or dy):continue
                nxt=Pos(cell.x+dx,cell.y+dy)
                if nxt in distances or nxt in blocked or not turn.land(nxt):continue
                distances[nxt]=distances[cell]+1
                first[nxt]=nxt if cell==start else first[cell]
                queue.append(nxt)
    return distances,first


def stands(turn, role, pos):
    blocked=turn.blocked(role)
    return [cell for dx in (-1,0,1) for dy in (-1,0,1) if dx or dy
            for cell in (Pos(pos.x+dx,pos.y+dy),) if turn.land(cell) and cell not in blocked]


def priority(building):
    # Tactical priorities, never modified official prices/health/levels.
    config=strategy_config.get()
    threshold=config['maintenance']['base_emergency_fraction'] if config['enabled'] else .6
    if building.kind==STATION and building.health < (1500,3000,4500)[min(3,max(1,building.level))-1]*threshold:
        group=0
    elif building.kind in TOWER_TYPES:group=1 if building.level==1 else 2
    elif building.kind==STATION:group=3 if building.level==1 else 4
    else:group=5
    return group,building.level,0 if building.kind == "rocket" else 1,building.health,building.unit_id


def target_level(turn, gun):
    config = strategy_config.get()
    if not config['enabled']:
        return 3
    day = (turn.round_no-1)//130 + 1
    schedule = config['upgrades']['day_targets']
    levels = schedule[day-1] if day <= len(schedule) else config['upgrades']['late_weapon_target']
    guns = sorted(turn.weapons(), key=lambda g:(g.pos.y,g.pos.x,g.unit_id))
    index = next((i for i,g in enumerate(guns) if g.unit_id==gun.unit_id),0)
    return levels[min(index,2)]


def weapon_reserve(turn, payload):
    """Next usable weapon voucher, priced only from the actual shop quote."""
    if not any(kind == 'weaponShop' for kind in turn.zones.values()):
        return None
    prices = shop_prices(payload)
    for gun in sorted(turn.weapons(), key=priority):
        if gun.level >= target_level(turn, gun):
            continue
        for item, price in sorted(prices.items()):
            if price >= 0 and can_upgrade(item, gun.kind, gun.level):
                return {'building':gun.unit_id, 'voucher':item, 'gold':price, 'level':gun.level}
    return None


def purchase_allowed(turn, payload, item, commands):
    config = strategy_config.get()
    if config['enabled']:
        # Cap only new weapon investment; already held vouchers remain usable.
        if any(can_upgrade(item, kind, level) for kind in TOWER_TYPES for level in (1,2)):
            return any(can_upgrade(item,g.kind,g.level) and g.level < target_level(turn,g) for g in turn.weapons())
        # Real emergency front-wall upgrades may precede weapons; ordinary
        # late-game wear must not indefinitely block the day-four 333 target.
        pressure = any(w.pos in frontline.protected_walls(turn)
                       and 0 < w.health <= config['maintenance']['emergency_fraction']*(1000,1500,2000)[min(3,max(1,w.level))-1]
                       and can_upgrade(item,WALL,w.level) for w in turn.walls())
        if pressure:
            return True
    reserve = weapon_reserve(turn, payload)
    if reserve is None or item == reserve['voucher']:
        return True
    # Emergency maintenance retains its separate, existing eligibility checks.
    if item in (WALL_FIXER, MEDICINE):
        return True
    base = turn.station()
    if (base is not None and priority(base)[0] == 0 and can_upgrade(item,base.kind,base.level)):
        return True
    if item in VOUCHER_TARGETS:
        return False  # Finish quoted weapon upgrades before new non-emergency wall/base vouchers.
    price = shop_prices(payload).get(item)
    if price is None:
        return False
    held = {i for role in turn.ours if role.health > 0 for i in role.backpack}
    held.update(c.get('name') for c in commands.values() if c.get('action') == 'buy')
    budget = 0 if reserve['voucher'] in held else reserve['gold']
    return available_gold(turn,payload,commands) - price >= budget


def plan(turn, payload, commands, *, start=12, deadline=55, commitment=None, reserved_workers=()):
    if commitment is not None:
        from .team_trip import evaluate_trip
        cost = evaluate_trip(turn,payload,commitment,commands=commands)
        worker = next((w for w in turn.workers() if w.unit_id==commitment['owner']),None)
        report = dict(phase='idle',reason=cost.reason,worker=commitment['owner'],
                      building=commitment['target'],voucher=commitment['item'],
                      committed=True,route=cost.report())
        returning=commitment.get('phase')=='return'
        if commitment.get('operation')=='stock':
            report.update(operation='stock',quantity=commitment['quantity'])
        if worker is None or worker.unit_id in commands or cost.command is None or (not cost.feasible and not returning):
            if returning:report['phase']='return_wait'
            return None,report
        report['phase'] = ('return_to_post' if returning else 'use' if cost.command['action']=='use' else 'buy' if cost.command['action']=='buy'
                           else 'return_with_voucher' if commitment['item'] in worker.backpack else 'to_shop')
        return (worker.unit_id,cost.command),report
    shops=sorted((p for p,k in turn.zones.items() if k=='weaponShop'),key=lambda p:(p.x,p.y))
    gold=available_gold(turn,payload,commands)
    report={'phase':'idle','reason':None,'gold_available':gold,'weapon_reserve':weapon_reserve(turn,payload),'shop_count':len(shops),
            'vendor_count':sum(k=='vendor' for k in turn.zones.values())}
    def stop(reason):report['reason']=reason;return None,report
    if not turn.is_day:return stop('night_defence')
    index=(turn.round_no-1)%130
    if index>=deadline:return stop('return_before_night')
    workers=[w for w in turn.workers() if w.unit_id not in commands and w.unit_id not in reserved_workers]
    base = turn.station()
    config = strategy_config.get()
    wall_emergency = any(w.pos in frontline.protected_walls(turn)
                         and w.health <= config['maintenance']['emergency_fraction']*(1000,1500,2000)[min(3,max(1,w.level))-1]
                         for w in turn.walls()) if config['enabled'] else False
    def building_priority(b):
        rank = wall_priority(b.pos, base.pos, turn.width, turn.height)[0] if base and b.kind == WALL else 0
        if config['enabled'] and b.kind == WALL:
            tier = frontline.wall_tier(turn,b.pos)
            # Center two, outer front two, then corners, including mixed levels.
            # An urgent front-six group retains its emergency spending escape.
            return (0.5 if wall_emergency and b.pos in frontline.protected_walls(turn) else 5,tier)+priority(b)[1:]
        return (priority(b)[0],rank)+priority(b)[1:]
    retired_rear=set(frontline.rear_walls(turn)) if config['enabled'] else set()
    buildings=sorted((b for b in turn.ours if b.health>0 and b.level<3
                      and b.kind in (STATION,WALL)+TOWER_TYPES
                      and not (b.kind==WALL and b.pos in retired_rear)),key=building_priority)
    # Bound planning cost on crowded wall maps; retain all base/tower targets.
    wall_ids={b.unit_id for b in buildings if b.kind==WALL}
    allowed_walls=[b.unit_id for b in buildings if b.kind==WALL][:4]
    buildings=[b for b in buildings if b.unit_id not in wall_ids or b.unit_id in allowed_walls]
    def action(worker,building,item,phase,cmd,price=None):
        report.update(phase=phase,reason=phase,worker=worker.unit_id,building=building.unit_id,
                      voucher=item,target=building.pos.dump(),price=price)
        return (worker.unit_id,cmd),report
    def full_cost(worker,building,item):
        from .team_trip import evaluate_trip, deadline as absolute_deadline
        return evaluate_trip(turn,payload,dict(owner=worker.unit_id,target=building.unit_id,
            item=item,deadline=absolute_deadline(turn,deadline)),commands=commands)
    from .team_trip import threat
    visible_danger=threat(turn)
    # Actual inventory has priority; no shop presence/quote is needed to use it.
    carried=False
    for building in buildings:
        for worker in workers:
            for item in sorted(set(worker.backpack)&set(VOUCHER_TARGETS)):
                carried=True
                if not can_upgrade(item,building.kind,building.level):continue
                cost=full_cost(worker,building,item)
                if cost.feasible and (not visible_danger or (cost.command['action']=='use' and cost.actions==1)):
                    report['route']=cost.report()
                    return action(worker,building,item,'use' if cost.command['action']=='use' else 'return_with_voucher',cost.command)
    if carried:return stop('held_voucher_deferred_for_threat' if visible_danger else 'held_voucher_no_reachable_eligible_target')
    if len(turn.weapons())<3:return stop('initial_weapons_first')
    if not shops:return stop('weapon_shop_not_observed')
    if index<start:return stop('before_shopping_window')
    if len(turn.workers())<2:return stop('keep_last_worker_home')
    if any(r.health>0 for r in turn.robots):return stop('visible_robots_defend_first')
    prices=shop_prices(payload)
    if not prices:return stop('no_shop_prices')
    held={item for unit in turn.ours if unit.health>0 for item in unit.backpack}
    held.update(cmd.get('name') for cmd in commands.values() if cmd.get('action')=='buy')
    if held & set(VOUCHER_TARGETS):return stop('voucher_already_in_team')
    candidates=[(b,item,prices[item]) for b in buildings for item in sorted(prices)
                if can_upgrade(item,b.kind,b.level) and (b.kind not in TOWER_TYPES or b.level < target_level(turn,b)) and 0<=prices[item]<=gold
                and purchase_allowed(turn,payload,item,commands)]
    if not candidates:return stop('saving_for_weapon_upgrade' if report['weapon_reserve'] and gold < report['weapon_reserve']['gold'] else 'no_affordable_upgrade')
    for building,item,price in candidates:
        options=[]
        for worker in workers:
            if worker.backpack_full:continue
            cost=full_cost(worker,building,item)
            if cost.feasible:options.append((cost.actions,worker.unit_id,worker,cost))
        if options:
            _,_,worker,cost=min(options,key=lambda x:x[:2])
            report['route']=cost.report()
            return action(worker,building,item,'buy' if cost.command['action']=='buy' else 'to_shop',cost.command,price)
    return stop('trip_unreachable_full_or_too_late')
