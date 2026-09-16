"""Public-state team route contracts (strategy; R01/R02/R03/R06).

Only an actually dispatched trip can constrain construction. Overlay pairs use
the SAME time slice: once the owner's action is selected, its exact conditional
next-round state and remaining deadline. A shadow is never a success receipt.
"""
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, replace
from heapq import heappop, heappush
from functools import lru_cache

from .coordination import available_gold
from .home_defense import inside
from .market import can_upgrade, shop_prices
from .protocol import (Pos, WALL, WALL_FIXER, CONTROLLABLE_TYPES, TOWER_TYPES, DAY_ROUNDS, ROUNDS_PER_DAY, distance,
                       move_command, buy_command, use_command)

MAX_ROUTE_OVERLAYS = 64  # compute bound; exhaustion defers construction


@lru_cache(maxsize=4096)
def neighbours(pos):
    # Only immutable coordinate geometry is shared across observations. Keep
    # the original ordering: BFS tie breaks depend on it; occupancy is not cached.
    return tuple(Pos(pos.x + dx, pos.y + dy) for dx in (-1, 0, 1)
                 for dy in (-1, 0, 1) if dx or dy)


def task_cells(turn):
    return {Pos(p.x + 1, p.y) for p, kind in turn.zones.items()
            if kind in ('challengerTaskPoint2', 'defenderTaskPoint2')}


def deadline(turn, index):
    return ((turn.round_no - 1) // 130) * 130 + 1 + index


def threat(turn):
    return any(r.health > 0 for r in turn.robots) or any(
        r.health > 0 and r.kind in CONTROLLABLE_TYPES + TOWER_TYPES for r in turn.enemies)


@dataclass(frozen=True)
class TripCost:
    feasible: bool
    actions: int | None
    budget: int
    reason: str
    command: dict | None = None

    def report(self):
        return dict(feasible=self.feasible, remaining_actions=self.actions,
                    budget=self.budget,
                    slack=None if self.actions is None else self.budget-self.actions,
                    reason=self.reason)


def evaluate_trip(turn, payload, commitment, *, actor_positions=None,
                  added_walls=(), commands=None):
    """Cost to the SAME target AND a legal sheltered weapon post afterward.

    No alternate building, speculative future income, assumed consumption or
    mine reserves. Real occupants are static obstacles unless explicitly moved
    by a supplied, separately checked construction overlay.
    """
    budget = commitment['deadline'] - turn.round_no
    def reject(reason, actions=None):
        return TripCost(False, actions, budget, reason)
    returning = commitment.get('phase') == 'return'
    operation = commitment.get('operation','upgrade')
    if (operation not in ('upgrade','repair') or (operation=='repair' and commitment['item']!=WALL_FIXER)
            or (operation=='upgrade' and commitment['item']==WALL_FIXER)):
        return reject('unsupported_operation')
    if not turn.is_day or (budget <= 0 and not returning):
        return reject('deadline')
    owner = next((w for w in turn.workers() if w.unit_id == commitment['owner']), None)
    target = next((u for u in turn.ours if u.health > 0 and u.unit_id == commitment['target']), None)
    item = commitment['item']
    if owner is None or (target is None and not returning):
        return reject('owner_or_target_missing')
    eligible = (target is not None and target.kind==WALL and 1<=target.level<=3
                and 0<target.health<(1000,1500,2000)[target.level-1]) if operation=='repair' else (
                    target is not None and can_upgrade(item,target.kind,target.level))
    if not returning and not eligible:
        return reject('target_no_longer_eligible')
    held = item in owner.backpack
    price = shop_prices(payload).get(item)
    if not returning and not held and (price is None or price < 0 or price > available_gold(
            turn, payload, commands or {}, replacing=owner.unit_id) or owner.backpack_full):
        return reject('purchase_unavailable')
    blocked = set(turn.blocked(owner)) | task_cells(turn) | set(added_walls)
    for uid, pos in (actor_positions or {}).items():
        actor = next((u for u in turn.controllable() if u.unit_id == uid), None)
        if actor is None or actor.unit_id == owner.unit_id:
            continue
        blocked.discard(actor.pos)
        blocked.add(pos)
    for uid, command in (commands or {}).items():
        if uid != owner.unit_id and uid not in (actor_positions or {}) and command.get('action') in ('move', 'build'):
            blocked.update(Pos.load(p) for p in command.get('targetPos', ()))

    def bfs(start):
        distances, first, queue = {start: 0}, {start: None}, deque([start])
        while queue:
            cell = queue.popleft()
            for nxt in neighbours(cell):
                if nxt in distances or nxt in blocked or not turn.land(nxt):
                    continue
                distances[nxt] = distances[cell] + 1
                first[nxt] = nxt if cell == start else first[cell]
                queue.append(nxt)
        return distances, first

    # Reverse return graph. Once sheltered, returning may not exit the ring
    # again merely to reach another gun; exterior starts may enter normally.
    base=turn.station()
    inside_region=({p for cell in turn.footprint(base) for p in (cell,*neighbours(cell))}
                   if base is not None else set())
    homes = {p for gun in turn.weapons() for p in neighbours(gun.pos)
             if p in inside_region and turn.land(p) and p not in blocked}
    return_cost = {p:0 for p in homes}; home_step = {p:None for p in homes}
    queue = deque(sorted(homes,key=lambda p:(p.x,p.y)))
    while queue:
        cell = queue.popleft()
        for predecessor in neighbours(cell):
            if (predecessor in return_cost or predecessor in blocked or not turn.land(predecessor)
                    or (predecessor in inside_region and cell not in inside_region)):
                continue
            return_cost[predecessor]=return_cost[cell]+1
            home_step[predecessor]=cell
            queue.append(predecessor)
    if returning:
        actions = return_cost.get(owner.pos)
        if actions is None:
            return reject('return_blocked')
        command = move_command(home_step[owner.pos]) if actions else None
        return TripCost(actions<=budget,actions,budget,
                        'returned' if actions==0 else 'return' if actions<=budget else 'late_return',command)

    stands = [p for p in neighbours(target.pos) if turn.land(p) and p not in blocked and p in return_cost]
    if not stands:
        return reject('return_unreachable')
    outward, first = bfs(owner.pos)
    options = []
    if held:
        for stand in stands:
            if stand in outward:
                command = (use_command(item, target.pos) if outward[stand] == 0
                           else move_command(first[stand]))
                options.append((outward[stand]+1+return_cost[stand], outward[stand], stand.x, stand.y, command))
    else:
        # Weighted reverse search: a use stand's initial cost includes use AND
        # its real return. Nearest-to-target alone can pick a stranded stand.
        delivery = {p:1+return_cost[p] for p in stands}
        pending = []
        for p,n in delivery.items():heappush(pending,(n,p.x,p.y))
        while pending:
            cost,x,y=heappop(pending);cell=Pos(x,y)
            if cost!=delivery[cell]:continue
            for nxt in neighbours(cell):
                if nxt in blocked or not turn.land(nxt) or cost+1>=delivery.get(nxt,10**9):
                    continue
                delivery[nxt]=cost+1;heappush(pending,(cost+1,nxt.x,nxt.y))
        shops = sorted((p for p, kind in turn.zones.items() if kind == 'weaponShop'), key=lambda p:(p.x,p.y))
        for shop in shops:
            for stand in neighbours(shop):
                if stand not in outward or stand not in delivery:
                    continue
                actions = outward[stand]+1+delivery[stand]
                command = buy_command(item,1) if outward[stand] == 0 else move_command(first[stand])
                options.append((actions,outward[stand],stand.x,stand.y,command))
    if not options:
        return reject('route_unreachable')
    actions, _, _, _, command = min(options,key=lambda v:v[:4])
    if actions > budget:
        return reject('route_exceeds_budget',actions)
    return TripCost(True,actions,budget,'delivery' if held else 'procurement',command)


def evaluate_after_action(turn, payload, commitment, command, commands, *, actor_positions=None, added_walls=()):
    """One shared conditional next-round slice for new AND active trips.

    An emitted first move is fixed, not replaced with a different shortest
    first step after seeing the teammate's candidate. None means one real wait
    round. This shadow is never persisted as a successful action receipt.
    """
    owner=next((w for w in turn.workers() if w.unit_id==commitment['owner']),None)
    if owner is None:
        return TripCost(False,None,commitment['deadline']-turn.round_no-1,'owner_or_target_missing')
    if commitment.get('operation')=='repair' and command and command.get('action') in ('buy','use'):
        valid=command.get('name')==WALL_FIXER and commitment['item']==WALL_FIXER
        if command['action']=='buy':
            price=shop_prices(payload).get(WALL_FIXER)
            valid=valid and command.get('num',1)==1 and WALL_FIXER not in owner.backpack and not owner.backpack_full
            valid=valid and price is not None and 0<=price<=available_gold(turn,payload,commands,replacing=owner.unit_id)
            valid=valid and any(kind=='weaponShop' and distance(owner.pos,p)==1 for p,kind in turn.zones.items())
        else:
            target=next((u for u in turn.walls() if u.unit_id==commitment['target']),None)
            valid=valid and target is not None and WALL_FIXER in owner.backpack
            valid=valid and target is not None and distance(owner.pos,target.pos)==1 and command.get('targetPos')==[target.pos.dump()]
        if not valid:
            return TripCost(False,None,commitment['deadline']-turn.round_no-1,'repair_first_action_invalid')
    start=owner.pos
    after_record=dict(commitment)
    if command and command.get('action')=='move':
        owner=replace(owner,pos=Pos.load(command['targetPos'][0]))
    elif command and command.get('action')=='buy':
        owner=replace(owner,backpack=owner.backpack+(commitment['item'],))
    elif command and command.get('action')=='use':
        after_record['phase']='return'
    positions={uid:Pos.load(c['targetPos'][0]) for uid,c in commands.items()
               if uid!=owner.unit_id and c.get('action')=='move'}
    positions.update(actor_positions or {})
    positions.pop(owner.unit_id,None)
    walls={Pos.load(p) for c in commands.values() if c.get('action')=='build'
           for p in c.get('targetPos',())} | set(added_walls)
    occupied=turn.blocked(next(w for w in turn.workers() if w.unit_id==owner.unit_id)) | task_cells(turn)
    if (owner.pos in walls or owner.pos in positions.values()
            or (owner.pos!=start and (distance(start,owner.pos)!=1 or owner.pos in occupied or not turn.land(owner.pos)))):
        return TripCost(False,None,commitment['deadline']-turn.round_no-1,'first_action_conflict')
    after_turn=replace(turn,round_no=turn.round_no+1,
        is_day=turn.round_no%ROUNDS_PER_DAY<DAY_ROUNDS,
        gold=available_gold(turn,payload,commands),
        ours=tuple(owner if u.unit_id==owner.unit_id else u for u in turn.ours))
    return evaluate_trip(after_turn,payload,after_record,actor_positions=positions,added_walls=walls)


class RouteGuard:
    """Bounded per-observation memo, applied to build stands AND emitted steps."""
    def __init__(self, turn, payload, commitment, commands=(), *, final=False):
        self.turn, self.payload = turn, payload
        self.commitment = commitment
        self.commands = dict(commands)
        self.owner_action = self.commands.get(commitment['owner']) if commitment else None
        self.advance = final or self.owner_action is not None
        self.before = self.evaluate() if commitment else None
        self.cache = {}
        self.rejected = 0
        self.limit_hit = False

    def evaluate(self, **overlays):
        if self.advance:
            return evaluate_after_action(self.turn,self.payload,self.commitment,
                self.owner_action,self.commands,**overlays)
        return evaluate_trip(self.turn,self.payload,self.commitment,commands=self.commands,**overlays)

    def check(self, owner, stand, walls=()):
        if self.commitment is None or owner == self.commitment['owner']:
            return True, 0
        key = owner, stand, frozenset(walls)
        if key not in self.cache:
            if len(self.cache) >= MAX_ROUTE_OVERLAYS:
                self.limit_hit = True
                return False,0
            self.cache[key] = self.evaluate(actor_positions={owner:stand},added_walls=key[2])
        after = self.cache[key]
        allowed = after.feasible
        if self.commitment.get('phase')=='return' and after.actions is not None:
            allowed = allowed or (self.before.actions is None or after.actions<=self.before.actions)
        if not allowed:
            self.rejected += 1
        increase = max(0,after.actions-self.before.actions) if after.actions is not None and self.before.actions is not None else 0
        return allowed, increase

    def allows_command(self, role, command):
        action = command.get('action')
        stand = Pos.load(command['targetPos'][0]) if action == 'move' else role.pos
        walls = [Pos.load(command['targetPos'][0])] if action == 'build' else ()
        return self.check(role.unit_id,stand,walls)[0]

    def report(self):
        return {'active': self.commitment is not None,
                'target': self.commitment['target'] if self.commitment else None,
                'before': self.before.report() if self.before else None,
                'time_slice':'after_fixed_action' if self.advance else 'current_observation',
                'rejected_candidates': self.rejected,'evaluated_overlays':len(self.cache),
                'search_limit_hit':self.limit_hit}


def clean_memory(value):
    """Small untrusted transport records; malformed records are dropped."""
    result = {}
    if not isinstance(value,dict):
        return result
    for kind in ('purchase','construction'):
        row = value.get(kind)
        if not isinstance(row,dict):
            continue
        if any(type(row.get(k)) is not int for k in ('owner','issued_round','last_round','deadline')):
            continue
        returning = kind=='purchase' and row.get('phase')=='return'
        last_limit = ((row['issued_round']-1)//ROUNDS_PER_DAY)*ROUNDS_PER_DAY+DAY_ROUNDS+1 if returning else row['deadline']
        if (row['owner'] < 0 or not 1 <= row['issued_round'] <= row['last_round'] < last_limit
                or not row['issued_round'] < row['deadline'] <= 1301):
            continue
        common = {k:row[k] for k in ('owner','issued_round','last_round','deadline')}
        if kind == 'purchase':
            if (type(row.get('target')) is not int or type(row.get('level')) is not int
                    or type(row.get('count')) is not int or not 0 <= row['count'] <= 100
                    or not isinstance(row.get('item'),str) or len(row['item']) > 80
                    or row.get('phase','acquire') not in ('acquire','return')
                    or row.get('last_action','') not in ('','move','buy','use')):
                continue
            operation = row.get('operation','upgrade')
            if operation not in ('upgrade','repair') or (operation=='upgrade' and row['item']==WALL_FIXER):
                continue
            if operation=='repair':
                target_pos=row.get('target_pos')
                if (row['item']!=WALL_FIXER or not 1<=row['level']<=3
                        or type(row.get('target_health')) is not int or not 0<row['target_health']<=2000
                        or not isinstance(target_pos,dict) or set(target_pos)!={'x','y'}
                        or any(type(target_pos[k]) is not int or not 0<=target_pos[k]<limit
                               for k,limit in (('x',41),('y',32)))
                        or ('use_round' in row and (type(row['use_round']) is not int
                            or not row['issued_round']<=row['use_round']<=row['last_round']))):
                    continue
            common.update({k:row[k] for k in ('target','level','count','item')})
            common.update(phase=row.get('phase','acquire'),last_action=row.get('last_action',''))
            # Do not add an operation field to legacy upgrade records: their
            # default-off serialization and byte ordering remain unchanged.
            if 'operation' in row:
                common['operation']=operation
            if operation=='repair':
                common.update(target_health=row['target_health'],target_pos=deepcopy(row['target_pos']))
                if 'use_round' in row:common['use_round']=row['use_round']
        else:
            cells = row.get('walls')
            if (row.get('phase') not in ('work','return') or not isinstance(cells,list)
                    or not 1 <= len(cells) <= 10 or any(not isinstance(p,dict)
                        or set(p) != {'x','y'} or any(type(p[k]) is not int or not 0 <= p[k] < (41 if k=='x' else 32)
                        for k in ('x','y')) for p in cells)):
                continue
            common.update(walls=deepcopy(cells),phase=row['phase'])
        result[kind] = common
    if result.get('purchase',{}).get('owner') == result.get('construction',{}).get('owner'):
        result.pop('construction',None)
    return result


class TripFrame:
    """Stage decisions separately; only final emitted commands start a trip."""
    def __init__(self, turn, payload, memory, *, purchase_deadline=67, construction_deadline=55):
        self.turn, self.payload = turn,payload
        self.purchase_deadline = deadline(turn,purchase_deadline)
        self.construction_deadline = deadline(turn,construction_deadline)
        self.memory = clean_memory(memory)
        self.events, self.pending = [], {}
        self._new_deferred = False
        live = {w.unit_id:w for w in turn.workers()}
        for kind,row in list(self.memory.items()):
            reason = None
            if row['owner'] not in live:
                reason = 'owner_missing'
            elif not turn.is_day:
                reason = 'deadline'
            elif turn.round_no-row['last_round'] not in (0,1) or (turn.round_no-1)//130 != (row['last_round']-1)//130:
                reason = 'observation_discontinuity'
            elif kind=='construction' and turn.round_no>=row['deadline']:
                reason='deadline'
            elif kind == 'purchase':
                if row['phase']=='acquire':
                    target = next((u for u in turn.ours if u.unit_id==row['target'] and u.health>0),None)
                    count = live[row['owner']].backpack.count(row['item'])
                    if row.get('operation')=='repair':
                        self.observe_repair(row,target,count)
                    elif target is None or target.level != row['level']:
                        confirmed = (target is not None and target.level>row['level']
                                     and count<row['count'] and row['last_action']=='use')
                        self.begin_return(row,'upgrade_confirmed_observed' if confirmed else 'target_changed_observed')
                    elif row['count'] and count<row['count']:
                        self.begin_return(row,'item_absent_observed')
                if threat(turn):
                    self.begin_return(row,'visible_threat')
                elif turn.round_no>=row['deadline']:
                    self.begin_return(row,'deadline')
                if row['phase']=='acquire':
                    cost = evaluate_trip(turn,payload,row)
                    if not cost.feasible:
                        self.begin_return(row,cost.reason)
                    else:
                        count = live[row['owner']].backpack.count(row['item'])
                        if count != row['count']:
                            self.events.append({'kind':kind,'event':'inventory_observed',
                                'owner':row['owner'],'before':row['count'],'after':count})
                        row['count'] = count
            if reason:
                self.cancel(kind,reason)
                continue
            row['last_round'] = turn.round_no
            if kind=='purchase' and row['phase']=='return':
                worker=live[row['owner']]
                if inside(turn,worker.pos) and any(distance(worker.pos,g.pos)==1 for g in turn.weapons()):
                    self.cancel(kind,'returned_observed')
            if kind == 'construction':
                if threat(turn):
                    row['phase']='return'
                    self.events.append({'kind':kind,'event':'return','reason':'visible_threat','owner':row['owner']})
                remaining = set(Pos.load(p) for p in row['walls']) - {w.pos for w in turn.walls()}
                if not remaining:
                    row['phase'] = 'return'
                worker = live[row['owner']]
                if row['phase']=='return' and inside(turn,worker.pos) and any(distance(worker.pos,g.pos)==1 for g in turn.weapons()):
                    self.cancel(kind,'returned_observed')

    @property
    def purchase(self):
        return self.memory.get('purchase')

    @property
    def construction(self):
        return self.memory.get('construction')

    def cancel(self,kind,reason):
        if kind in self.memory:
            self.events.append({'kind':kind,'event':'cancel','reason':reason,'owner':self.memory[kind]['owner']})
            self.memory.pop(kind)

    def begin_return(self,row,reason):
        if row.get('phase')!='return':
            self.events.append({'kind':'purchase','event':'return','reason':reason,'owner':row['owner']})
            row['phase']='return'

    def observe_repair(self,row,target,count):
        """Consumption is an exit signal, never sufficient proof of success.

        A boolean previous-round use receipt plus actual consumption confirms
        repair even if same-round attacks hide its healing in net wall HP.
        Missing/contradictory receipts stay unknown; no inferred bonus stock.
        """
        consumed=count<row['count']
        current=(target is not None and target.kind==WALL and target.level==row['level']
                 and target.pos.dump()==row['target_pos'])
        attempted=(row.get('last_action')=='use' and row.get('use_round')==self.turn.round_no-1)
        receipts=self.payload.get('lastRoundRoleActionResults')
        feedback=receipts.get(str(row['owner'])) if isinstance(receipts,dict) else None
        if consumed:
            confirmed=current and attempted and feedback is True
            self.begin_return(row,'repair_confirmed_observed' if confirmed else 'repair_consumed_unconfirmed')
        elif not current:
            self.begin_return(row,'repair_target_changed_observed')
        elif attempted and feedback is not False:
            # A claimed success without consumption, or no attributable result,
            # is not a safe basis for retrying/buying. Retain the real inventory.
            self.begin_return(row,'repair_use_unconfirmed')
        elif attempted:
            self.events.append({'kind':'purchase','event':'repair_use_failed_observed','owner':row['owner']})

    def stage_purchase(self, report, proposal):
        if proposal is None:
            return
        owner,command = proposal
        target = next((u for u in self.turn.ours if u.unit_id==report.get('building')),None)
        worker = next((w for w in self.turn.workers() if w.unit_id==owner),None)
        prior = self.purchase
        operation=report.get('operation','upgrade')
        item=report.get('item',report.get('voucher')) if 'operation' in report else report.get('voucher')
        if (worker is None or not item or operation not in ('upgrade','repair')
                or (operation=='upgrade' and item==WALL_FIXER)
                or ('item' in report and report.get('voucher',report['item'])!=report['item'])):
            return
        if operation=='repair' or (prior and prior.get('operation')=='repair'):
            if (operation!='repair' or item!=WALL_FIXER or command.get('action') not in ('move','buy','use')
                    or (command.get('action') in ('buy','use') and command.get('name')!=WALL_FIXER)
                    or (command.get('action')=='buy' and command.get('num',1)!=1)
                    or (prior and (prior.get('operation')!='repair' or prior['owner']!=owner
                                   or prior['target']!=report.get('building') or prior['item']!=item))
                    or (prior and prior['phase']=='return' and command.get('action')!='move')
                    or (self.construction and self.construction['owner']==owner)
                    or (self.pending.get('construction',({},))[0].get('owner')==owner)):
                return
            if not (prior and prior['phase']=='return') and (
                    target is None or target.kind!=WALL or not 1<=target.level<=3
                    or not 0<target.health<(1000,1500,2000)[target.level-1]):
                return
        if operation=='repair' and command.get('action')=='use' and (
                command.get('targetPos')!=[target.pos.dump()] or WALL_FIXER not in worker.backpack):
            return
        if prior and prior['phase']=='return':
            record = dict(prior,last_round=self.turn.round_no,last_action=command['action'],
                          count=worker.backpack.count(prior['item']))
        elif target is None:
            return
        else:
            record = dict(owner=owner,target=target.unit_id,item=item,level=target.level,
                      count=worker.backpack.count(item),issued_round=(prior or {}).get('issued_round',self.turn.round_no),
                      last_round=self.turn.round_no,deadline=(prior or {}).get('deadline',self.purchase_deadline),
                      phase='acquire',last_action=command['action'])
            if 'operation' in report:record['operation']=operation
            if operation=='repair':
                record.update(target_health=target.health,target_pos=target.pos.dump())
                if command['action']=='use':record['use_round']=self.turn.round_no
        self.pending['purchase'] = record,deepcopy(command)

    def stage_construction(self, owner, command, report):
        prior = self.construction
        walls = (prior or {}).get('walls') or report.get('planned_walls')
        if not walls:
            return
        record = dict(owner=owner,walls=deepcopy(walls),phase='return' if report['phase']=='return' else 'work',
                      issued_round=(prior or {}).get('issued_round',self.turn.round_no),last_round=self.turn.round_no,
                      deadline=(prior or {}).get('deadline',self.construction_deadline))
        self.pending['construction'] = record,deepcopy(command)

    def finalize(self,commands):
        for kind,(record,proposed) in self.pending.items():
            if commands.get(record['owner']) != proposed:
                continue
            if kind=='purchase' and self.purchase is None:
                cost=self.prospective_new(record,proposed,commands)
                if not cost.feasible:
                    self.defer_new(record,cost)
                    continue
            if kind not in self.memory:
                self.events.append({'kind':kind,'event':'issued','owner':record['owner']})
            self.memory[kind] = record
        return clean_memory(self.memory)

    def prospective_new(self,record,proposed,commands):
        """Conditional after-settlement proof; none of this is persisted as fact.

        Include the exact proposed first step, not a newly recomputed shortcut.
        The real contract still stores the CURRENT bag and target level.
        """
        return evaluate_after_action(self.turn,self.payload,record,proposed,commands)

    def defer_new(self,record,cost):
        if not self._new_deferred:
            self.events.append({'kind':'purchase','event':'defer','owner':record['owner'],
                'reason':'new_trip_conflicts_final_actions','route':cost.report()})
            self._new_deferred=True

    def protect_final(self,commands):
        """A late task move/reservation may change the earlier route proof.

        Drop only the unsafe non-courier worker action. Existing reconcile
        already forbids moving into any current occupant, so dropping an
        accepted move cannot create a same-round move into its old position.
        This does not claim protection from future opponent actions.
        """
        result = dict(commands)
        if self.purchase is None:
            pending=self.pending.get('purchase')
            if pending:
                record,proposed=pending
                if result.get(record['owner'])==proposed:
                    cost=self.prospective_new(record,proposed,result)
                    if not cost.feasible:
                        result.pop(record['owner'],None)
                        self.defer_new(record,cost)
            return result
        for worker in self.turn.workers():
            if worker.unit_id==self.purchase['owner']:
                continue
            command = result.get(worker.unit_id,{})
            if command.get('action') not in ('move','build'):
                continue
            guard = RouteGuard(self.turn,self.payload,self.purchase,
                {uid:c for uid,c in result.items() if uid!=worker.unit_id},final=True)
            if not guard.allows_command(worker,command):
                result.pop(worker.unit_id,None)
                self.events.append({'kind':'construction','event':'defer',
                                    'reason':'final_team_route_budget','owner':worker.unit_id})
        return result
