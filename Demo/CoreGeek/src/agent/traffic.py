"""Bounded public-state recovery; no future occupancy or assumed removals."""
from collections import deque
from copy import deepcopy

from . import frontline, strategy_config
from .home_defense import inside as home_inside
from .protocol import Pos, distance, move_command


def neighbours(p):
    return (Pos(p.x+x,p.y+y) for x in (-1,0,1) for y in (-1,0,1) if x or y)


def path(turn, start, goals, blocked):
    from .grid import policy_obstacles
    blocked = set(blocked) | policy_obstacles(turn)
    blocked = set(blocked) | set(turn.neutral_cells())
    parents, pending = {start:None}, deque([start])
    while pending:
        at = pending.popleft()
        if at in goals:
            result=[]
            while at is not None:
                result.append(at);at=parents[at]
            return result[::-1]
        for nxt in neighbours(at):
            if nxt in parents or nxt in blocked or not turn.land(nxt):continue
            parents[nxt]=at;pending.append(nxt)
    return None


def clean_memory(value):
    if not isinstance(value,dict):return {}
    def number(n):return type(n) is int and 0<=n<2**63
    def pos(p):return isinstance(p,dict) and all(type(p.get(k)) is int and 0<=p[k]<256 for k in ('x','y'))
    out={'last_round':value.get('last_round',0),'openings':[], 'history':[], 'walls':{}}
    if not number(out['last_round']):return {}
    board=value.get('board')
    if isinstance(board,list) and len(board)==4 and all(type(n) is int and 0<=n<256 for n in board):out['board']=list(board)
    out['openings']=[{'x':p['x'],'y':p['y']} for p in (value.get('openings') or [])[:6] if pos(p)] if isinstance(value.get('openings'),list) else []
    for row in (value.get('history') or [])[-8:] if isinstance(value.get('history'),list) else []:
        if not isinstance(row,dict) or not number(row.get('round')):continue
        roles={}
        for uid,data in list(row.get('roles',{}).items())[:3] if isinstance(row.get('roles'),dict) else []:
            if str(uid).isdigit() and isinstance(data,dict) and pos(data.get('pos')):
                roles[str(uid)]={'pos':data['pos'], 'goal':data['goal'] if pos(data.get('goal')) else None,
                                'action':data.get('action') if data.get('action') in ('move','build','collect','buy','sell','use','remove') else None}
        out['history'].append({'round':row['round'],'roles':roles})
    for uid,row in list(value.get('walls',{}).items())[:2] if isinstance(value.get('walls'),dict) else []:
        if str(uid).isdigit() and isinstance(row,dict) and pos(row.get('pos')) and number(row.get('round')):
            out['walls'][str(uid)]={'pos':row['pos'],'round':row['round']}
    pending=value.get('pending_open')
    if isinstance(pending,dict) and pos(pending.get('pos')) and number(pending.get('round')) and pending['round']<=out['last_round']:
        out['pending_open']={'pos':pending['pos'],'round':pending['round']}
    out['history']=sorted({r['round']:r for r in out['history'] if r['round']<=out['last_round']}.values(),key=lambda r:r['round'])[-8:]
    return out


class Frame:
    def __init__(self,turn,memory):
        cfg=strategy_config.get()
        self.cfg=cfg.get('navigation',{})
        self.single=cfg['defense']['single_operator_three_rockets']
        self.deadline=cfg['economy']['return_day_index']
        self.enabled=cfg['enabled'] and self.cfg.get('enabled',False)
        self.turn=turn;self.memory=clean_memory(memory);self.goals={};self.events=[];self.pending_remove=None
        base=turn.station()
        board=[turn.width,turn.height,base.pos.x if base else 0,base.pos.y if base else 0]
        if self.memory.get('board') not in (None,board):self.memory={}
        old=self.memory.get('last_round',0)
        if turn.round_no<old or turn.round_no>old+1:
            openings=self.memory.get('openings',[]) if turn.round_no>old else []
            self.memory={'openings':openings}
        self.memory.setdefault('history',[]);self.memory.setdefault('walls',{});self.memory.setdefault('openings',[])
        self.memory['board']=board
        pending=self.memory.get('pending_open')
        if pending and turn.round_no>pending['round']:self.memory.pop('pending_open',None)
        if pending and turn.round_no==pending['round']+1:
            at=Pos.load(pending['pos'])
            if not any(w.pos==at for w in turn.walls()) and pending['pos'] not in self.memory['openings']:
                self.memory['openings'].append(pending['pos'])
        if not turn.is_day:self.memory['walls']={}

    def goal(self,role,target,inside_only=False):
        if self.enabled:self.goals[role.unit_id]=(target,inside_only)

    def wall_target(self,role,candidates):
        if not candidates:return None
        if not self.enabled:return candidates[0]
        leases=self.memory['walls']
        valid={str(w.unit_id) for w in self.turn.workers()}
        standing={w.pos for w in self.turn.walls()}
        leases={uid:row for uid,row in leases.items() if uid in valid and 0<=self.turn.round_no-row['round']<=2
                and Pos.load(row['pos']) not in standing and self.turn.land(Pos.load(row['pos']))}
        self.memory['walls']=leases
        used={Pos.load(row['pos']) for uid,row in leases.items() if uid!=str(role.unit_id)}
        available=[p for p in candidates if p not in used]
        prior=leases.get(str(role.unit_id))
        if prior and Pos.load(prior['pos']) not in candidates:prior=None
        target=Pos.load(prior['pos']) if prior else (available[0] if available else None)
        if target is not None:leases[str(role.unit_id)]={'pos':target.dump(),'round':self.turn.round_no}
        return target

    def openings(self):
        return {Pos.load(p) for p in self.memory['openings']} if self.enabled else set()

    def stalled(self,role):
        rows=self.memory['history']
        uid=str(role.unit_id);need=self.cfg.get('stall_rounds',3)
        if [r['round'] for r in rows[-need:]]!=list(range(self.turn.round_no-need,self.turn.round_no)):return False
        recent=[r['roles'].get(uid) for r in rows[-need:]]
        if len(recent)<need or any(r is None for r in recent):return False
        goal=self.goals.get(role.unit_id)
        if goal is None or any(r.get('goal')!=goal[0].dump() for r in recent):return False
        current_night = (self.turn.round_no - 1) % 130 >= 70
        if any((r['round'] - 1) // 130 != (self.turn.round_no - 1) // 130
               or (((r['round'] - 1) % 130 >= 70) != current_night)
               for r in rows[-need:]):
            return False
        if any(r['action'] not in (None,'move') for r in recent):return False
        positions=[Pos.load(r['pos']) for r in recent]+[role.pos]
        return len(set(positions))==1 or (len(positions)>=4 and len(set(positions))==2 and positions[-1]==positions[-3])

    def recover(self,commands,protected=()):
        turn=self.turn
        if (not self.enabled or turn.station() is None
                or (turn.is_day and (turn.round_no-1)%130+5>=self.deadline)
                or (turn.is_day and any(r.health>0 for r in turn.robots))
                or (turn.is_day and any(u.health>0 and u.kind in ('worker','pioneer','rocket','railgun','gatling') for u in turn.enemies))):
            return
        for role in turn.workers():
            request=self.goals.get(role.unit_id)
            if (not request or role.unit_id in protected or not self.stalled(role)
                    or commands.get(role.unit_id,{}).get('action') not in (None,'move')):continue
            target,inside_only=request
            if inside_only:continue
            # Night recovery is only for a worker that is outside and has
            # repeatedly failed to advance toward the base. It may open a
            # removable flank wall; front-six protection below still applies.
            if not turn.is_day and home_inside(turn, role.pos):
                continue
            blocked=turn.blocked(role)
            from . import rocket_post
            reserved_post=rocket_post.common_cells(turn)[0] if self.single and role.unit_id!=turn.workers()[0].unit_id else set()
            blocked |= reserved_post-{role.pos}
            claimed={Pos.load(p) for uid,c in commands.items() if uid!=role.unit_id
                     and c.get('action') in ('move','build') for p in c.get('targetPos',())}
            goals={p for p in neighbours(target) if turn.land(p) and p not in blocked and p not in claimed}
            direct=path(turn,role.pos,goals,blocked|claimed)
            if direct and len(direct)==1:continue
            if direct and len(direct)>1:
                commands[role.unit_id]=move_command(direct[1])
                self.events.append({'owner':role.unit_id,'reason':'replan_complete_route'})
                continue
            # Move an idle or similarly stalled teammate into a genuine bay.
            others=[w for w in turn.workers() if w.unit_id!=role.unit_id and w.unit_id not in protected
                    and (w.unit_id not in commands or (commands[w.unit_id].get('action')=='move' and self.stalled(w)))]
            # Even a busy worker may be the obstruction; wait for its real work
            # rather than demolishing a wall to compensate for current occupancy.
            relaxed_blocked=(blocked-{w.pos for w in turn.workers() if w.unit_id!=role.unit_id})|claimed|(reserved_post-{role.pos})
            relaxed_goals={p for p in neighbours(target) if turn.land(p) and p not in relaxed_blocked}
            relaxed=path(turn,role.pos,relaxed_goals,relaxed_blocked)
            if relaxed:
                for other in others:
                    if other.pos not in relaxed:continue
                    other_blocked=turn.blocked(other)|claimed
                    if self.single and other.unit_id!=turn.workers()[0].unit_id:
                        other_blocked |= rocket_post.common_cells(turn)[0]-{other.pos}
                    free={Pos(x,y) for x in range(turn.width) for y in range(turn.height)
                          if turn.land(Pos(x,y)) and Pos(x,y) not in set(relaxed)|other_blocked}
                    aside=path(turn,other.pos,free,other_blocked)
                    if aside and len(aside)>1:
                        commands.pop(role.unit_id,None);commands[other.unit_id]=move_command(aside[1])
                        self.events.append({'owner':other.unit_id,'reason':'yield_stalled_route','for':role.unit_id})
                        return
                self.events.append({'owner':role.unit_id,'reason':'friendly_route_blocked'})
                continue  # A teammate blockage is not a reason to demolish.
            if not self.cfg.get('allow_wall_removal',True) or len(self.openings())>=self.cfg.get('max_openings',2):continue
            protected_walls=frontline.protected_walls(turn)
            options=[]
            for wall in turn.walls():
                if wall.pos in protected_walls or distance(role.pos,wall.pos)!=1:continue
                opened=blocked-{wall.pos}
                after_goals={p for p in neighbours(target) if turn.land(p) and p not in opened and p not in claimed}
                route=path(turn,role.pos,after_goals,opened|claimed)
                # A night escape is an emergency action: the worker is already
                # outside the ring, so the daytime return deadline must not
                # suppress opening a safe flank. Daytime recovery keeps the
                # configured deadline guard.
                budget = 10 ** 6 if not turn.is_day else self.deadline - (turn.round_no - 1) % 130
                if route and len(route)>1 and len(route)+1<=budget:
                    options.append((wall.level,wall.health,len(route),wall.unit_id,wall.pos))
            if options:
                at=min(options)[-1]
                cmd={'action':'remove','targetPos':[at.dump()]}
                commands[role.unit_id]=cmd;self.pending_remove=(role.unit_id,at,deepcopy(cmd))
                self.events.append({'owner':role.unit_id,'reason':'open_nonfront_route','wall':at.dump()})
                return
            self.events.append({'owner':role.unit_id,'reason':'no_safe_recovery'})

    def finish(self,commands):
        if not self.enabled:return {}
        n=self.turn.round_no
        self.memory['walls']={uid:r for uid,r in self.memory['walls'].items()
            if r['round']!=n or commands.get(int(uid),{}).get('action') in ('move','build')}
        if self.pending_remove:
            uid,at,cmd=self.pending_remove
            if commands.get(uid)==cmd and at not in self.openings():
                self.memory['pending_open']={'pos':at.dump(),'round':n}
        rows=[r for r in self.memory['history'] if r['round']<n]
        rows.append({'round':n,'roles':{str(w.unit_id):{'pos':w.pos.dump(),
            'goal':self.goals[w.unit_id][0].dump() if w.unit_id in self.goals else None,
            'action':commands.get(w.unit_id,{}).get('action')} for w in self.turn.workers()}})
        self.memory['history']=rows[-8:];self.memory['last_round']=n
        return clean_memory(self.memory)
