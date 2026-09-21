"""Public-path daylight return budget (strategy; R01/R02/R03/R07).

No predicted movements or future damage. A proposed action is affordable only
if its resulting cell can still reach a legal post on the observed board.
"""
from collections import deque
from . import frontline, home_defense, rocket_post, strategy_config
from .protocol import Pos, DAY_ROUNDS, ROUNDS_PER_DAY, distance, move_command


def enabled():
    c = strategy_config.get()
    return c['enabled'] and c['economy']['dynamic_return']


def margin(turn):
    c = strategy_config.get()
    e = c['economy']
    day = (turn.round_no - 1) // ROUNDS_PER_DAY + 1
    value = e['return_margin_early'] if day < c['defense']['full_defense_from_day'] else e['return_margin_late']
    base = turn.station()
    front = set(frontline.protected_walls(turn))
    damaged = (base is not None and base.health < {1:1500,2:3000,3:4500}.get(base.level,4500))
    damaged |= any(w.pos in front and w.health < {1:1000,2:1500,3:2000}.get(w.level,2000) for w in turn.walls())
    return max(value, e['return_margin_damaged']) if damaged else value


def deadline(turn):
    return DAY_ROUNDS - margin(turn) if enabled() else strategy_config.get()['economy']['return_day_index']


class Frame:
    def __init__(self, turn):
        self.turn = turn
        self.active = enabled() and turn.is_day and turn.station() is not None
        self.index = (turn.round_no - 1) % ROUNDS_PER_DAY
        self.margin = margin(turn) if self.active else 0
        self.deadline = DAY_ROUNDS - self.margin
        self.rows = {}
        self.returning = set()
        self.cache = {}
        self.common, _ = rocket_post.common_cells(turn)
        workers = turn.workers()
        self.gunner = workers[0].unit_id if workers else None
        cfg = strategy_config.get()
        if not cfg['defense']['single_operator_three_rockets']:
            self.common = set()
        base = turn.station()
        self.emergency = bool(base and base.health / {1:1500,2:3000,3:4500}.get(base.level,4500)
                              <= cfg['maintenance']['base_emergency_fraction'])
        front = set(frontline.protected_walls(turn)) if base else set()
        self.emergency |= any(w.pos in front and w.health / {1:1000,2:1500,3:2000}.get(w.level,2000)
                              <= cfg['maintenance']['emergency_fraction'] for w in turn.walls())

    def routes(self, role, commands=()):
        """Reverse BFS, including current occupants and proposed landing cells."""
        blocked = set(self.turn.blocked(role))
        for uid, cmd in dict(commands).items():
            if cmd.get('action') == 'build' or (uid != role.unit_id and cmd.get('action') == 'move'):
                blocked.update(Pos.load(p) for p in cmd.get('targetPos', ()))
        if self.common and role.unit_id != self.gunner:
            blocked.update(self.common - {role.pos})
        key = role.unit_id, frozenset(blocked)
        if key in self.cache:
            return self.cache[key]
        base = self.turn.station()
        goals = {Pos(x,y) for x in range(base.pos.x-1,base.pos.x+3)
                 for y in range(base.pos.y-2,base.pos.y+2)
                 if self.turn.land(Pos(x,y)) and Pos(x,y) not in blocked}
        if self.common and role.unit_id == self.gunner:
            goals &= self.common
        elif self.common:
            goals -= self.common
        elif self.turn.weapons():
            goals = {p for p in goals if any(distance(p,g.pos)==1 for g in self.turn.weapons())}
        costs = {p:0 for p in goals}
        steps = {p:None for p in goals}
        queue = deque(sorted(goals, key=lambda p:(p.x,p.y)))
        while queue:
            p = queue.popleft()
            for dx in (-1,0,1):
                for dy in (-1,0,1):
                    if not (dx or dy): continue
                    q = Pos(p.x+dx,p.y+dy)
                    if q in costs or q in blocked or not self.turn.land(q): continue
                    costs[q] = costs[p]+1
                    steps[q] = p
                    queue.append(q)
        self.cache[key] = costs,steps
        return costs,steps

    def required(self, role):
        if not self.active or not self.turn.weapons(): return False
        steps = self.routes(role)[0].get(role.pos)
        return self.emergency or (self.index >= strategy_config.get()['economy']['return_day_index']
                                  if steps is None else self.index+steps >= self.deadline)

    def apply(self, commands):
        if not self.active or not self.turn.weapons(): return dict(commands)
        result = dict(commands)
        # Urgent/far roles select their return landing first. Stationary
        # teammates remain blocked; this never relies on an unobserved vacate.
        roles = sorted(self.turn.controllable(), key=lambda r:(not self.required(r),r.unit_id))
        for role in roles:
            current = result.get(role.unit_id)
            costs,_ = self.routes(role)
            current_cost = costs.get(role.pos)
            projected = role.pos
            if current and current.get('action') == 'move':
                projected = Pos.load(current['targetPos'][0])
            after_costs,_ = self.routes(role,result)
            after_cost = after_costs.get(projected)
            fits = after_cost is not None and self.index+1+after_cost <= self.deadline
            # Once safely at the post, use/sell/build beside it remains legal
            # through the final daytime round. There is no return leg to lose.
            stays_home = current_cost == 0 and after_cost == 0
            atomic = bool(current and current.get('action') in {
                'acceptTask','submitAnswer','buy','sell','use','drop','summonTreasure','collect'})
            # A command already selected at a legal target is one atomic action;
            # finish it, then arbitrate the return on the next observation.
            keep = bool(current and (atomic or ((fits or stays_home) and not self.emergency)))
            if not keep and (self.required(role) or (current and not fits)):
                result.pop(role.unit_id,None)
                return_costs, return_steps = self.routes(role,result)
                step = return_steps.get(role.pos)
                if step is not None:
                    result[role.unit_id] = move_command(step)
                self.returning.add(role.unit_id)
                reason = ('emergency_return' if self.emergency else 'route_unreachable'
                          if return_costs.get(role.pos) is None else 'at_defence_post'
                          if step is None else 'return_budget_reached')
            else:
                reason = 'work_fits_return_budget' if keep else 'no_work_proposed'
            self.rows[str(role.unit_id)] = dict(return_steps=current_cost, margin=self.margin,
                day_index=self.index, arrival_deadline_index=self.deadline,
                remaining_work_rounds=None if current_cost is None else max(0,self.deadline-self.index-current_cost),
                proposed_action=(current or {}).get('action'), reason=reason)
        return result
