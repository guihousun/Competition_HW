"""Self-wall rescue: public-frame mechanism, not historical planner replay."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'Demo/CoreGeek/src'))
from agent import brain, planner, simulator, team_trip
from agent.protocol import Pos, Turn
from agent.home_defense import inside


def scene(reflected=False, id_offset=0):
    data=json.loads((Path(__file__).parent/'fixtures/self_occupied_wall_r1042_public.json').read_text(encoding='utf-8'))
    p=data['observation']
    encoded=json.dumps(p,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    assert hashlib.sha256(encoded).hexdigest()==data['observation_hash']
    if reflected:p['teamOur']['type']='challenger'
    for side in ('teamOur','teamEnemy','robot'):
        for u in p[side]['roles']:
            u['id']+=id_offset
            if reflected:u['pos']['x']=40-u['pos']['x']-int(u['roleType']=='station')
    if reflected:
        for z in p['mapInfo']['zones']:
            z['pos']['x']=40-z['pos']['x']-int(z['neutralType'].endswith('TaskPoint2'))
    return p


def unit(p, uid):
    return next(u for u in p['teamOur']['roles'] if u['id']==uid)


def close_other_gap(p):
    p['teamOur']['roles'].append(dict(id=999999,roleType='wall',pos=dict(x=32,y=3),health=1000,level=1))


def purchase(owner=20010,phase='acquire'):
    return dict(owner=owner,target=20030,item='WeaponUpgradeVoucher2',level=2,count=0,
                issued_round=1041,last_round=1041,deadline=1074,phase=phase,last_action='move')


class SelfOccupiedWallTests(unittest.TestCase):
    def plan(self,p,m,commit=True):
        return brain.plan_for_state(deepcopy(p),m,commit=commit,judge_tasks=False).commands

    def test_public_frame_full_pipeline_move_build_and_observed_release(self):
        for reflected,offset in ((False,0),(True,700000)):
            p=scene(reflected,offset);m=planner.PlannerState();owner=20012+offset
            x=lambda v:40-v if reflected else v
            site=dict(x=x(32),y=4);post=dict(x=x(33),y=5)
            commands=self.plan(p,m)
            self.assertEqual(commands[str(owner)],dict(action='move',targetPos=[post]))
            self.assertEqual(m.team_trips['construction']['walls'],[site])
            self.assertEqual(m.team_trips['construction']['owner'],owner)
            self.assertNotIn(Pos(**post),Turn.load(p).occupied_cells())
            p=simulator.step(p,external_response={'roleCommandMap':commands})['state']
            self.assertTrue(p['lastRoundRoleActionResults'][str(owner)])
            self.assertEqual(unit(p,owner)['pos'],post)
            commands=self.plan(p,m)
            self.assertEqual(commands[str(owner)],dict(action='build',targetPos=[site],name='wall'))
            self.assertEqual(m.team_trips['construction']['walls'],[site])
            p=simulator.step(p,external_response={'roleCommandMap':commands})['state']
            self.assertTrue(p['lastRoundRoleActionResults'][str(owner)])
            self.assertEqual(unit(p,owner)['backpack'].count('stone'),2)
            self.assertTrue(any(u['roleType']=='wall' and u['health']>0 and u['pos']==site for u in p['teamOur']['roles']))
            self.assertTrue(inside(Turn.load(p),Pos(**post)))
            self.plan(p,m)
            self.assertNotIn('construction',m.team_trips)
            self.assertTrue(any(e.get('reason')=='returned_observed' for e in brain._UPGRADE_REPORT.get()['team_trip_events']))

    def test_another_actor_occupied_cell_is_never_used_as_our_landing(self):
        p=scene();unit(p,20010)['pos']=dict(x=33,y=5);before=deepcopy(p)
        commands=self.plan(p,planner.PlannerState())
        self.assertEqual(p,before)
        if '20012' in commands and commands['20012']['action']=='move':
            landing=Pos(**commands['20012']['targetPos'][0])
            self.assertNotIn(landing,Turn.load(p).occupied_cells())
            self.assertNotEqual(landing,Pos(33,5))

    def test_when_the_gap_is_occupied_by_someone_else_no_self_rescue_claim(self):
        p=scene();unit(p,20012)['pos']=dict(x=33,y=7);unit(p,20011)['pos']=dict(x=32,y=4)
        m=planner.PlannerState();self.plan(p,m)
        self.assertNotIn('construction',m.team_trips)

    def test_night_and_insufficient_day_budget_never_start_construction(self):
        for round_no in (1093,1094,1111):
            p=scene();p['roundNo']=round_no;m=planner.PlannerState()
            commands=self.plan(p,m)
            self.assertNotIn('construction',m.team_trips)
            command=commands.get('20012',{})
            self.assertNotIn(command.get('action'),('build','collect'))
            if command.get('action')=='move':
                self.assertTrue(inside(Turn.load(p),Pos(**command['targetPos'][0])))

    def test_exact_existing_deadline_and_margin_still_allow_two_actions(self):
        p=scene();p['roundNo']=1092;m=planner.PlannerState()
        self.assertEqual(self.plan(p,m)['20012'],dict(action='move',targetPos=[dict(x=33,y=5)]))
        self.assertEqual(m.team_trips['construction']['deadline'],1096)

    def test_purchase_route_and_final_first_action_remain_protected(self):
        p=scene();close_other_gap(p);p['teamOur']['goldNum']=150
        m=planner.PlannerState();m.team_trips={'purchase':purchase()}
        commands=self.plan(p,m)
        self.assertEqual(commands['20010'],dict(action='move',targetPos=[dict(x=37,y=6)]))
        self.assertEqual(commands['20012'],dict(action='move',targetPos=[dict(x=33,y=5)]))
        self.assertEqual(m.team_trips['purchase']['target'],20030)
        report=brain._UPGRADE_REPORT.get()['construction'][0]['team_route']
        self.assertTrue(report['active'])
        self.assertEqual((report['before']['remaining_actions'],report['before']['budget']),(31,31))
        self.assertEqual(m.team_trips['construction']['walls'],[dict(x=32,y=4)])

    def test_route_proof_exhaustion_defers_rescue_instead_of_ignoring_purchase(self):
        p=scene();close_other_gap(p);p['teamOur']['goldNum']=150
        m=planner.PlannerState();m.team_trips={'purchase':purchase()}
        with patch.object(team_trip,'MAX_ROUTE_OVERLAYS',0):commands=self.plan(p,m)
        self.assertIn('20010',commands)
        self.assertNotIn('20012',commands)
        self.assertNotIn('construction',m.team_trips)
        self.assertTrue(brain._UPGRADE_REPORT.get()['construction'][0]['team_route']['search_limit_hit'])

    def test_purchase_owner_without_a_command_is_not_adopted(self):
        p=scene();close_other_gap(p)
        # Block every possible interior post, so the existing return owner has
        # no command. This is not permission to acquire it for construction.
        t=Turn.load(p)
        occupied=t.occupied_cells()
        for x in range(33,37):
            for y in range(4,8):
                if Pos(x,y) not in occupied:
                    p['mapInfo']['zones'].append(dict(neutralType='stone',pos=dict(x=x,y=y)))
        m=planner.PlannerState();m.team_trips={'purchase':purchase(20012,'return')}
        commands=self.plan(p,m)
        self.assertNotIn('20012',commands)
        self.assertEqual(m.team_trips['purchase']['owner'],20012)
        self.assertNotIn('construction',m.team_trips)

    def test_existing_errand_with_temporarily_unreachable_vendor_keeps_ownership(self):
        p=scene();close_other_gap(p)
        unit(p,20012)['backpack']+=['copper']*3
        p['_demo']={'errands':{'20012':{'goal':'vendor'}}}
        # Vendor remains quoted, but all eight legal approach cells are blocked.
        for x in range(19,22):
            for y in range(15,18):
                if (x,y)!=(20,16):
                    p['mapInfo']['zones'].append(dict(neutralType='stone',pos=dict(x=x,y=y)))
        t=Turn.load(p);worker=next(w for w in t.workers() if w.unit_id==20012)
        commands={}
        self.assertFalse(brain._errand_mission(t,worker,commands,p,p['_demo']['errands']))
        self.assertEqual(commands,{})
        self.assertEqual(p['_demo']['errands'],{'20012':{'goal':'vendor'}})
        m=planner.PlannerState();self.plan(p,m)
        self.assertNotIn('construction',m.team_trips)

    def test_other_construction_owner_and_pending_owner_are_not_replaced(self):
        for established in (True,False):
            p=scene();m=planner.PlannerState()
            if established:
                m.team_trips={'construction':dict(owner=20010,issued_round=1041,last_round=1041,
                    deadline=1096,phase='work',walls=[dict(x=32,y=3)])}
            else:
                # Both workers stand on separate gaps. The first actual staged
                # construction is the only proposal that may acquire ownership.
                unit(p,20010)['pos']=dict(x=32,y=3)
            self.plan(p,m)
            self.assertEqual(m.team_trips['construction']['owner'],20010)
            self.assertEqual(m.team_trips['construction']['walls'],[dict(x=32,y=3)])

    def test_preview_preserves_real_planner_and_rejected_dispatch_creates_no_lease(self):
        p=scene();m=planner.PlannerState();before=deepcopy(m.dump())
        self.plan(p,m,False)
        self.assertEqual(m.dump(),before)
        real=brain.reconcile
        def drop_self(turn,payload,commands,**kwargs):
            result=real(turn,payload,commands,**kwargs);result.pop(20012,None);return result
        with patch.object(brain,'reconcile',side_effect=drop_self):commands=self.plan(p,m)
        self.assertNotIn('20012',commands)
        self.assertNotIn('construction',m.team_trips)


if __name__=='__main__':unittest.main()
