"""Observation-derived worker shopping and voucher delivery, R02/R06.

One worker travels; one remains home. No hidden map or future wave inputs.
Costs are path lengths on the currently observed board, plus buy/use rounds.
"""
from collections import deque
from .protocol import Pos, WALL, STATION, TOWER_TYPES, distance, buy_command, use_command, move_command
from .market import VOUCHER_TARGETS, can_upgrade, shop_prices
from .coordination import available_gold


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
    if building.kind==STATION and building.health < (1500,3000,4500)[min(3,max(1,building.level))-1]*.6:
        group=0
    elif building.kind in TOWER_TYPES:group=1 if building.level==1 else 3
    elif building.kind==STATION:group=2 if building.level==1 else 4
    else:group=5
    return group,building.level,building.health,building.unit_id


def plan(turn, payload, commands, *, start=12, deadline=55):
    shops=sorted((p for p,k in turn.zones.items() if k=='weaponShop'),key=lambda p:(p.x,p.y))
    gold=available_gold(turn,payload,commands)
    report={'phase':'idle','reason':None,'gold_available':gold,'shop_count':len(shops),
            'vendor_count':sum(k=='vendor' for k in turn.zones.values())}
    def stop(reason):report['reason']=reason;return None,report
    if not turn.is_day:return stop('night_defence')
    index=(turn.round_no-1)%130
    if index>=deadline:return stop('return_before_night')
    workers=[w for w in turn.workers() if w.unit_id not in commands]
    buildings=sorted((b for b in turn.ours if b.health>0 and b.level<3 and b.kind in (STATION,WALL)+TOWER_TYPES),key=priority)
    # Bound planning cost on crowded wall maps; retain all base/tower targets.
    buildings=[b for b in buildings if b.kind!=WALL]+[b for b in buildings if b.kind==WALL][:4]
    cache={}
    def route(worker,pos):
        key=(worker.unit_id,pos)
        if key not in cache:cache[key]=routes(turn,worker,pos)
        return cache[key]
    def action(worker,building,item,phase,cmd,price=None):
        report.update(phase=phase,reason=phase,worker=worker.unit_id,building=building.unit_id,
                      voucher=item,target=building.pos.dump(),price=price)
        return (worker.unit_id,cmd),report
    # Actual inventory has priority; no shop presence/quote is needed to use it.
    carried=False
    for worker in workers:
        for item in sorted(set(worker.backpack)&set(VOUCHER_TARGETS)):
            carried=True
            for building in buildings:
                if not can_upgrade(item,building.kind,building.level):continue
                if distance(worker.pos,building.pos)<=1:
                    return action(worker,building,item,'use',use_command(item,building.pos))
                dist,first=route(worker,worker.pos)
                choices=[p for p in stands(turn,worker,building.pos) if p in dist and index+dist[p]+1<=deadline]
                if choices:
                    goal=min(choices,key=lambda p:(dist[p],p.x,p.y))
                    if first[goal] is not None:return action(worker,building,item,'return_with_voucher',move_command(first[goal]))
    if carried:return stop('held_voucher_no_reachable_eligible_target')
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
                if can_upgrade(item,b.kind,b.level) and 0<=prices[item]<=gold]
    if not candidates:return stop('no_affordable_upgrade')
    for building,item,price in candidates:
        options=[]
        for worker in workers:
            if worker.backpack_full:continue
            if not any(other.unit_id!=worker.unit_id and
                       any(distance(other.pos,tower.pos)<=4 for tower in turn.weapons())
                       for other in turn.workers()):continue
            dist,first=route(worker,worker.pos)
            destinations=stands(turn,worker,building.pos)
            for shop in shops:
                for stand in stands(turn,worker,shop):
                    if stand not in dist:continue
                    back,_=route(worker,stand)
                    returning=min((back[p] for p in destinations if p in back),default=10**9)
                    total=dist[stand]+1+returning+1
                    if index+total<=deadline:
                        options.append((total,dist[stand],worker.unit_id,stand.x,stand.y,worker,stand,shop))
        if options:
            *_,worker,stand,shop=min(options,key=lambda x:x[:5])
            if distance(worker.pos,shop)<=1:
                return action(worker,building,item,'buy',buy_command(item,1),price)
            return action(worker,building,item,'to_shop',move_command(route(worker,worker.pos)[1][stand]),price)
    return stop('trip_unreachable_full_or_too_late')
