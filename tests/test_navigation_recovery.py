"""Public synthetic regression: productive routing, front protection, no cheating."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from test_defense_layout import board, unit
from agent import brain, coordination, frontline, planner, rocket_post, traffic, strategy_config
from agent.protocol import Pos, Turn, distance

FIXTURES=Path(__file__).parent/'fixtures/navigation'


def stalled_memory(p):
    role=p['teamOur']['roles'][1]
    return {'last_round':p['roundNo']-1,'history':[
        {'round':n,'roles':{str(role['id']):{'pos':deepcopy(role['pos']),'action':'move'}}}
        for n in range(p['roundNo']-3,p['roundNo'])]}


def corridor(protected=False,bypass=False,round_no=13):
    wall=(12,9) if protected else (7,8)
    worker=(13,9) if protected else (6,8)
    target=(11,9) if protected else (9,8)
    cells={(9,10),(10,10),(9,9),(10,9),worker,wall,target,
           (11,10) if protected else (8,8)}
    if bypass:cells.update({(6,7),(7,7),(8,7)})
    p=board(round_no=round_no)
    p['teamOur']['roles']=[unit(1,'station',9,10,1500),unit(2,'worker',*worker,220),unit(7,'wall',*wall)]
    p['mapInfo']['zones']=[{'pos':{'x':x,'y':y},'neutralType':'water'}
        for x in range(41) for y in range(32) if (x,y) not in cells]
    return p,Pos(*target),Pos(*wall)


class NavigationRecoveryTests(unittest.TestCase):
    def test_real_day_one_mining_progress_is_not_reversed_by_post_reservation(self):
        p=json.loads((FIXTURES/'day1-r51-public.json').read_text(encoding='utf-8'))
        memory=planner.PlannerState.load(json.loads((FIXTURES/'day1-r51-memory.json').read_text(encoding='utf-8')))
        turn=Turn.load(p);worker=next(w for w in turn.workers() if w.unit_id==20012)
        mine=Pos(25,13);blocked=turn.blocked(worker)|rocket_post.common_cells(turn)[0]
        goals={q for q in traffic.neighbours(mine) if turn.land(q) and q not in blocked}
        before=traffic.path(turn,worker.pos,goals,blocked)
        response=brain.plan_for_state(p,memory,judge_tasks=False)
        command=response.commands['20012']
        self.assertEqual(command['action'],'move')
        target=Pos.load(command['targetPos'][0])
        self.assertNotEqual(target,Pos(34,4),'must not return to the previous cell')
        self.assertNotIn(target,blocked)
        self.assertEqual(distance(worker.pos,target),1)
        after=traffic.path(turn,target,goals,blocked)
        self.assertLess(len(after),len(before),'must progress toward the same mining goal')

    def test_demolition_requires_observed_stall_and_opens_actual_route(self):
        p,target,wall=corridor();turn=Turn.load(p);worker=turn.workers()[0]
        first=traffic.Frame(turn,{})
        first.goal(worker,target);commands={};first.recover(commands)
        self.assertFalse(commands)
        frame=traffic.Frame(turn,stalled_memory(p));frame.goal(worker,target)
        frame.recover(commands)
        self.assertEqual(commands,{2:{'action':'remove','targetPos':[wall.dump()]}})
        self.assertEqual(coordination.reconcile(turn,p,commands),commands)
        memory=frame.finish(commands)
        self.assertEqual(frame.openings(),set(),'emission is not removal success')
        duplicate=traffic.Frame(turn,memory)
        self.assertIn('pending_open',duplicate.memory)
        memory=duplicate.finish({})
        p['roundNo']+=1
        failed=traffic.Frame(Turn.load(p),memory)
        self.assertEqual(failed.openings(),set())
        p['teamOur']['roles']=[r for r in p['teamOur']['roles'] if r['id']!=7]
        confirmed=traffic.Frame(Turn.load(p),memory)
        self.assertEqual(confirmed.openings(),{wall})
        context=brain._TRAFFIC.set(confirmed)
        try:self.assertNotIn(wall,brain._wall_order(confirmed.turn))
        finally:brain._TRAFFIC.reset(context)

    def test_front_wall_is_never_demolished_even_when_only_exit(self):
        p,target,wall=corridor(protected=True);turn=Turn.load(p)
        self.assertIn(wall,frontline.protected_walls(turn))
        frame=traffic.Frame(turn,stalled_memory(p));frame.goal(turn.workers()[0],target)
        commands={};frame.recover(commands)
        self.assertFalse(commands)
        self.assertFalse(coordination.reconcile(turn,p,{2:{'action':'remove','targetPos':[wall.dump()]}}))

    def test_existing_detour_never_triggers_demolition(self):
        p,target,wall=corridor(bypass=True);turn=Turn.load(p)
        frame=traffic.Frame(turn,stalled_memory(p));frame.goal(turn.workers()[0],target)
        commands={};frame.recover(commands)
        self.assertEqual(commands[2]['action'],'move')

    def test_night_threat_active_use_and_disabled_removal_are_respected(self):
        for mode in ('night','robot','use','disabled'):
            p,target,wall=corridor(round_no=71 if mode=='night' else 13)
            if mode=='robot':p['robot']={'roles':[{'id':50,'health':40,'pos':{'x':30,'y':20}}]}
            cfg=deepcopy(strategy_config.DEFAULTS)
            if mode=='disabled':cfg['navigation']['allow_wall_removal']=False
            with patch.object(strategy_config,'get',return_value=cfg):
                turn=Turn.load(p);frame=traffic.Frame(turn,stalled_memory(p));frame.goal(turn.workers()[0],target)
                commands={2:{'action':'use','name':'WallFixer','targetPos':[wall.dump()]}} if mode=='use' else {}
                before=deepcopy(commands);frame.recover(commands)
                self.assertEqual(commands,before)

    def test_preview_duplicate_observation_and_gap_do_not_invent_stall(self):
        p=json.loads((FIXTURES/'day1-r51-public.json').read_text(encoding='utf-8'))
        state=planner.PlannerState();before=deepcopy(state.dump())
        brain.plan_for_state(p,state,commit=False,judge_tasks=False)
        self.assertEqual(state.dump(),before)
        frame=traffic.Frame(Turn.load(p),{});memory=frame.finish({})
        again=traffic.Frame(Turn.load(p),memory);memory=again.finish({})
        self.assertEqual(len(memory['history']),1)
        state.traffic_state=memory
        self.assertEqual(planner.PlannerState.load(state.dump()).traffic_state,memory)
        p['roundNo']+=4
        self.assertEqual(traffic.Frame(Turn.load(p),memory).memory['history'],[])

    def test_two_workers_keep_distinct_short_lived_wall_assignments(self):
        p=board();p['teamOur']['roles'].append(unit(99,'worker',10,23,220))
        turn=Turn.load(p);frame=traffic.Frame(turn,{})
        workers=turn.workers();sites=[Pos(12,20),Pos(12,21)]
        a=frame.wall_target(workers[0],sites);b=frame.wall_target(workers[1],sites)
        self.assertNotEqual(a,b)
        self.assertEqual(frame.wall_target(workers[0],list(reversed(sites))),a)

    def test_sheltered_night_route_cannot_exit_to_other_inner_pocket(self):
        p=board(round_no=90)
        p['teamOur']['roles']=[unit(1,'station',9,22,1500),unit(2,'worker',8,20,220),
            unit(10,'rocket',8,21),unit(11,'rocket',8,23),unit(12,'rocket',9,23)]
        turn=Turn.load(p)
        self.assertIsNone(brain._step_toward(turn,turn.workers()[0],Pos(7,22),set(),inside_only=True))
        p['roundNo']=60;turn=Turn.load(p)
        self.assertIsNotNone(brain._step_toward(turn,turn.workers()[0],Pos(7,22),set(),inside_only=True))


if __name__=='__main__':unittest.main()
