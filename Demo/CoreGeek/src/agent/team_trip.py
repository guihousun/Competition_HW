"""Public-state team route contracts (strategy; R01/R02/R03/R06).

Only an actually dispatched trip can constrain construction. Costs for before
and after overlays use the SAME observation round and absolute deadline. An
overlay is a planning counterfactual, never confirmation that a move succeeded.
"""
from collections import deque
from copy import deepcopy
from dataclasses import dataclass

from .coordination import available_gold
from .home_defense import inside
from .market import can_upgrade, shop_prices
from .protocol import (Pos, CONTROLLABLE_TYPES, TOWER_TYPES, distance,
                       move_command, buy_command, use_command)

MAX_ROUTE_OVERLAYS = 64  # compute bound; exhaustion defers construction


def neighbours(pos):
    return (Pos(pos.x + dx, pos.y + dy) for dx in (-1, 0, 1)
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
    """Cost to the SAME target: buy if absent, deliver/use if actually held.

    No alternate building, speculative future income, assumed consumption or
    mine reserves. Real occupants are static obstacles unless explicitly moved
    by a supplied, separately checked construction overlay.
    """
    budget = commitment['deadline'] - turn.round_no
    def reject(reason, actions=None):
        return TripCost(False, actions, budget, reason)
    if not turn.is_day or budget <= 0:
        return reject('deadline')
    owner = next((w for w in turn.workers() if w.unit_id == commitment['owner']), None)
    target = next((u for u in turn.ours if u.health > 0 and u.unit_id == commitment['target']), None)
    item = commitment['item']
    if owner is None or target is None:
        return reject('owner_or_target_missing')
    if not can_upgrade(item, target.kind, target.level):
        return reject('target_no_longer_eligible')
    held = item in owner.backpack
    price = shop_prices(payload).get(item)
    if not held and (price is None or price < 0 or price > available_gold(
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

    stands = [p for p in neighbours(target.pos) if turn.land(p) and p not in blocked]
    outward, first = bfs(owner.pos)
    options = []
    if held:
        for stand in stands:
            if stand in outward:
                command = (use_command(item, target.pos) if outward[stand] == 0
                           else move_command(first[stand]))
                options.append((outward[stand]+1, 0, stand.x, stand.y, command))
    else:
        # Reverse multi-source distances are exact on the undirected move grid.
        home = {p: 0 for p in stands}; queue = deque(stands)
        while queue:
            cell = queue.popleft()
            for nxt in neighbours(cell):
                if nxt in home or nxt in blocked or not turn.land(nxt):
                    continue
                home[nxt] = home[cell]+1; queue.append(nxt)
        shops = sorted((p for p, kind in turn.zones.items() if kind == 'weaponShop'), key=lambda p:(p.x,p.y))
        for shop in shops:
            for stand in neighbours(shop):
                if stand not in outward or stand not in home:
                    continue
                actions = outward[stand]+1+home[stand]+1
                command = buy_command(item,1) if outward[stand] == 0 else move_command(first[stand])
                options.append((actions,outward[stand],stand.x,stand.y,command))
    if not options:
        return reject('route_unreachable')
    actions, _, _, _, command = min(options,key=lambda v:v[:4])
    if actions > budget:
        return reject('route_exceeds_budget',actions)
    return TripCost(True,actions,budget,'delivery' if held else 'procurement',command)


class RouteGuard:
    """Bounded per-observation memo, applied to build stands AND emitted steps."""
    def __init__(self, turn, payload, commitment, commands=()):
        self.turn, self.payload = turn, payload
        self.commitment = commitment
        self.commands = dict(commands)
        self.before = evaluate_trip(turn,payload,commitment,commands=self.commands) if commitment else None
        self.cache = {}
        self.rejected = 0
        self.limit_hit = False

    def check(self, owner, stand, walls=()):
        if self.commitment is None or owner == self.commitment['owner']:
            return True, 0
        key = owner, stand, frozenset(walls)
        if key not in self.cache:
            if len(self.cache) >= MAX_ROUTE_OVERLAYS:
                self.limit_hit = True
                return False,0
            self.cache[key] = evaluate_trip(self.turn,self.payload,self.commitment,
                actor_positions={owner:stand},added_walls=key[2],commands=self.commands)
        after = self.cache[key]
        if not after.feasible:
            self.rejected += 1
        increase = max(0,after.actions-self.before.actions) if after.actions is not None and self.before.actions is not None else 0
        return after.feasible, increase

    def allows_command(self, role, command):
        action = command.get('action')
        stand = Pos.load(command['targetPos'][0]) if action == 'move' else role.pos
        walls = [Pos.load(command['targetPos'][0])] if action == 'build' else ()
        return self.check(role.unit_id,stand,walls)[0]

    def report(self):
        return {'active': self.commitment is not None,
                'target': self.commitment['target'] if self.commitment else None,
                'before': self.before.report() if self.before else None,
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
        if row['owner'] < 0 or not 1 <= row['issued_round'] <= row['last_round'] < row['deadline'] <= 1301:
            continue
        common = {k:row[k] for k in ('owner','issued_round','last_round','deadline')}
        if kind == 'purchase':
            if (type(row.get('target')) is not int or type(row.get('level')) is not int
                    or type(row.get('count')) is not int or not 0 <= row['count'] <= 100
                    or not isinstance(row.get('item'),str) or len(row['item']) > 80):
                continue
            common.update({k:row[k] for k in ('target','level','count','item')})
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
        live = {w.unit_id:w for w in turn.workers()}
        for kind,row in list(self.memory.items()):
            reason = None
            if row['owner'] not in live:
                reason = 'owner_missing'
            elif not turn.is_day or turn.round_no >= row['deadline']:
                reason = 'deadline'
            elif turn.round_no-row['last_round'] not in (0,1) or (turn.round_no-1)//130 != (row['last_round']-1)//130:
                reason = 'observation_discontinuity'
            elif threat(turn):
                if kind=='construction':
                    row['phase']='return'
                    self.events.append({'kind':kind,'event':'return','reason':'visible_threat','owner':row['owner']})
                else:
                    reason = 'visible_threat'
            elif kind == 'purchase':
                target = next((u for u in turn.ours if u.unit_id==row['target'] and u.health>0),None)
                if target is None or target.level != row['level']:
                    reason = 'target_changed_observed'
                elif row['count'] and row['item'] not in live[row['owner']].backpack:
                    reason = 'item_absent_observed'
                else:
                    cost = evaluate_trip(turn,payload,row)
                    if not cost.feasible:
                        reason = cost.reason
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
            if kind == 'construction':
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

    def stage_purchase(self, report, proposal):
        if proposal is None:
            return
        owner,command = proposal
        target = next((u for u in self.turn.ours if u.unit_id==report.get('building')),None)
        worker = next((w for w in self.turn.workers() if w.unit_id==owner),None)
        if target is None or worker is None or not report.get('voucher'):
            return
        prior = self.purchase
        record = dict(owner=owner,target=target.unit_id,item=report['voucher'],level=target.level,
                      count=worker.backpack.count(report['voucher']),issued_round=(prior or {}).get('issued_round',self.turn.round_no),
                      last_round=self.turn.round_no,deadline=(prior or {}).get('deadline',self.purchase_deadline))
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
            if kind not in self.memory:
                self.events.append({'kind':kind,'event':'issued','owner':record['owner']})
            self.memory[kind] = record
        return clean_memory(self.memory)

    def protect_final(self,commands):
        """A late task move/reservation may change the earlier route proof.

        Drop only the unsafe non-courier worker action. Existing reconcile
        already forbids moving into any current occupant, so dropping an
        accepted move cannot create a same-round move into its old position.
        This does not claim protection from future opponent actions.
        """
        if self.purchase is None:
            return commands
        result = dict(commands)
        for worker in self.turn.workers():
            if worker.unit_id==self.purchase['owner']:
                continue
            command = result.get(worker.unit_id,{})
            if command.get('action') not in ('move','build'):
                continue
            guard = RouteGuard(self.turn,self.payload,self.purchase,
                {uid:c for uid,c in result.items() if uid!=worker.unit_id})
            if not guard.allows_command(worker,command):
                result.pop(worker.unit_id,None)
                self.events.append({'kind':'construction','event':'defer',
                                    'reason':'final_team_route_budget','owner':worker.unit_id})
        return result
