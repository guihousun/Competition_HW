"""Public replay fixtures plus hand-counted route and receipt expectations."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from test_baseline import ROOT
from agent import brain, construction_trip, planner, team_trip, upgrade_itinerary
from agent.protocol import Turn, Pos, distance
from test_coordination import unit

FIXTURES = Path(__file__).parent/'fixtures/issue32-trip'


def observed(round_no=179):
    return json.loads((FIXTURES/f'candidate-observed-r{round_no}.json').read_text(encoding='utf-8'))


def purchase(round_no=178):
    # Synthetic internal memory for replaying the target that the historical
    # planner selected; NOT a claim the old program had this new memory field.
    return dict(owner=20012,target=20041,item='WeaponUpgradeVoucher1',level=1,count=0,
                issued_round=164,last_round=round_no,deadline=198,phase='acquire',last_action='move')


def construction(round_no=177):
    return dict(owner=20010,walls=[dict(x=35,y=8)],phase='work',
                issued_round=140,last_round=round_no,deadline=186)


def remove_new_wall(payload):
    payload['teamOur']['roles']=[u for u in payload['teamOur']['roles']
        if u['roleType']!='wall' or u['pos']!=dict(x=35,y=8)]


def move_actor(payload,uid,pos):
    next(u for u in payload['teamOur']['roles'] if u['id']==uid)['pos']=pos


class TeamTripTests(unittest.TestCase):
    def test_costs_are_geometry_based_under_mirror_translation_and_id_changes(self):
        for mirror,shift in ((True,0),(False,-2)):
            p=observed();c=purchase()
            def position(pos,wide=False):
                return dict(x=(40-pos['x']-int(wide) if mirror else pos['x']+shift),y=pos['y'])
            for u in p['teamOur']['roles']:
                u['pos']=position(u['pos'],u['roleType']=='station');u['id']+=777
            for z in p['mapInfo']['zones']:
                z['pos']=position(z['pos'],z['neutralType'].endswith('TaskPoint2'))
            c['owner']+=777;c['target']+=777
            if mirror:p['teamOur']['type']='challenger'
            t=Turn.load(p)
            actual=team_trip.evaluate_trip(t,p,c)
            moved=team_trip.evaluate_trip(t,p,c,actor_positions={20010+777:Pos.load(position(dict(x=36,y=7)))})
            self.assertEqual((actual.actions,actual.budget,moved.actions),(20,19,16))

    def test_r179_independent_costs_same_time_slice(self):
        p=observed(); original=deepcopy(p); c=purchase()
        actual=team_trip.evaluate_trip(Turn.load(p),p,c)
        self.assertEqual((actual.actions,actual.budget,actual.feasible),(20,19,False))
        moved=team_trip.evaluate_trip(Turn.load(p),p,c,actor_positions={20010:Pos(36,7)})
        self.assertEqual((moved.actions,moved.budget,moved.feasible),(16,19,True))
        self.assertEqual(p,original,'overlay must not mutate a real observation')
        remove_new_wall(p)
        without=team_trip.evaluate_trip(Turn.load(p),p,c)
        self.assertEqual((without.actions,without.budget,without.feasible),(15,19,True))

    def test_r178_build_rejected_but_legal_stand_keeps_both_trips_feasible(self):
        p=observed(178); t=Turn.load(p); worker=next(w for w in t.workers() if w.unit_id==20010)
        guard=team_trip.RouteGuard(t,p,purchase(177))
        # At r178 all three costs are one action larger, and the budget is20;
        # never compare r178 cost to the r179 budget19.
        self.assertEqual((guard.before.actions,guard.before.budget),(16,20))
        self.assertFalse(guard.check(20010,Pos(35,7),(Pos(35,8),))[0])
        self.assertTrue(guard.check(20010,Pos(36,7),(Pos(35,8),))[0])
        old=construction_trip.plan(t,worker,[Pos(35,8)])
        new=construction_trip.plan(t,worker,[Pos(35,8)],route_guard=guard)
        self.assertEqual(old[0]['action'],'build')
        self.assertEqual(new[0],dict(action='move',targetPos=[dict(x=36,y=7)]))
        self.assertGreaterEqual(new[1]['slack'],2)

    def test_locked_target_never_substitutes_closer_other_weapon(self):
        p=observed(); c=purchase()
        proposal,report=upgrade_itinerary.plan(Turn.load(p),p,{},deadline=67,commitment=c)
        self.assertIsNone(proposal)
        self.assertEqual(report['building'],20041)
        self.assertEqual(report['reason'],'route_exceeds_budget')
        # The historical stateless planner may choose the railgun instead.
        proposal,report=upgrade_itinerary.plan(Turn.load(p),p,{},deadline=67)
        self.assertEqual(report['building'],20030)
        frame=team_trip.TripFrame(Turn.load(p),p,{'purchase':c})
        self.assertEqual(frame.purchase['phase'],'return')
        self.assertEqual(frame.events[0]['reason'],'route_exceeds_budget',
                         'an independently changed board must explicitly cancel, not pin forever')

    def test_no_commitment_means_no_new_weapon_veto(self):
        p=observed(178); guard=team_trip.RouteGuard(Turn.load(p),p,None)
        self.assertEqual(guard.check(20010,Pos(35,7),(Pos(35,8),)),(True,0))
        self.assertFalse(guard.report()['active'])

    def test_r164_no_all_day_construction_owner_exclusion(self):
        p=observed(164); t=Turn.load(p)
        proposal,_=upgrade_itinerary.plan(t,p,{},start=0,deadline=67)
        self.assertEqual(proposal[0],20010)
        memory={'construction':construction(163)}
        # A completed finite assignment at a real safe post releases ownership
        # before new dispatch; the date alone cannot keep excluding its owner.
        standing=next(w for w in t.walls())
        memory['construction']['walls']=[standing.pos.dump()]
        move_actor(p,20010,dict(x=34,y=7))
        self.assertIsNone(team_trip.TripFrame(Turn.load(p),p,memory).construction)

    def test_only_final_emitted_purchase_establishes_commitment(self):
        p=observed(164); t=Turn.load(p)
        proposal,report=upgrade_itinerary.plan(t,p,{},start=0,deadline=67)
        frame=team_trip.TripFrame(t,p,{})
        frame.stage_purchase(report,proposal)
        self.assertIsNone(frame.purchase,'a new proposal is not an active commitment')
        self.assertEqual(frame.finalize({}),{})
        owner,command=proposal
        memory=frame.finalize({owner:command})
        self.assertEqual(memory['purchase']['owner'],owner)
        self.assertEqual(memory['purchase']['count'],0,'dispatch does not fabricate a bought voucher')

    def test_unemitted_constructor_is_not_reserved(self):
        p=observed(178);f=team_trip.TripFrame(Turn.load(p),p,{})
        proposed=dict(action='move',targetPos=[dict(x=36,y=7)])
        f.stage_construction(20010,proposed,dict(phase='to_wall',planned_walls=[dict(x=35,y=8)]))
        self.assertEqual(f.finalize({}),{})
        self.assertEqual(f.finalize({20010:proposed})['construction']['walls'],[dict(x=35,y=8)])

    def test_failed_buy_and_failed_use_do_not_advance_from_dispatch(self):
        p=observed(179); remove_new_wall(p)
        move_actor(p,20012,dict(x=25,y=19))
        c=purchase(); t=Turn.load(p)
        proposal,report=upgrade_itinerary.plan(t,p,{},deadline=67,commitment=c)
        self.assertEqual(proposal[1]['action'],'buy')
        f=team_trip.TripFrame(t,p,{'purchase':c});f.stage_purchase(report,proposal)
        mem=f.finalize(dict([proposal]));p['roundNo']+=1
        f=team_trip.TripFrame(Turn.load(p),p,mem)
        retry,_=upgrade_itinerary.plan(Turn.load(p),p,{},commitment=f.purchase)
        self.assertEqual(retry[1]['action'],'buy')
        owner=next(u for u in p['teamOur']['roles'] if u['id']==20012)
        owner['backpack'].append(c['item']);owner['pos']=dict(x=34,y=7)
        proposal,report=upgrade_itinerary.plan(Turn.load(p),p,{},commitment=f.purchase)
        self.assertEqual(proposal[1]['action'],'use')
        f.stage_purchase(report,proposal);mem=f.finalize(dict([proposal]))
        p['roundNo']+=1
        f=team_trip.TripFrame(Turn.load(p),p,mem)
        self.assertIsNotNone(f.purchase)
        retry,_=upgrade_itinerary.plan(Turn.load(p),p,{},commitment=f.purchase)
        self.assertEqual(retry[1]['action'],'use')

    def test_target_change_outside_retains_return_without_claiming_our_use(self):
        p=observed(179); c=purchase();c['count']=1
        next(u for u in p['teamOur']['roles'] if u['id']==20041)['level']=2
        before=deepcopy(p)
        frame=team_trip.TripFrame(Turn.load(p),p,{'purchase':c})
        self.assertEqual(frame.purchase['phase'],'return')
        self.assertEqual(frame.events[0]['reason'],'target_changed_observed')
        self.assertEqual(p,before)

    def test_observed_acquisition_is_remembered_even_if_next_command_dropped(self):
        p=observed(179);remove_new_wall(p)
        owner=next(u for u in p['teamOur']['roles'] if u['id']==20012)
        owner['backpack'].append('WeaponUpgradeVoucher1')
        frame=team_trip.TripFrame(Turn.load(p),p,{'purchase':purchase()})
        mem=frame.finalize({})
        self.assertEqual(mem['purchase']['count'],1)
        owner['backpack'].remove('WeaponUpgradeVoucher1');p['roundNo']+=1
        frame=team_trip.TripFrame(Turn.load(p),p,mem)
        self.assertEqual(frame.purchase['phase'],'return')
        self.assertEqual(frame.events[0]['reason'],'item_absent_observed')

    def test_future_income_capacity_and_current_quote_cannot_be_assumed(self):
        for mode in ('gold','capacity','quote'):
            p=observed(179);remove_new_wall(p)
            if mode=='gold':p['teamOur']['goldNum']=0
            if mode=='capacity':next(u for u in p['teamOur']['roles'] if u['id']==20012)['backpack']=['stone']*100
            if mode=='quote':p['weaponShopList']=[]
            self.assertEqual(team_trip.evaluate_trip(Turn.load(p),p,purchase()).reason,'purchase_unavailable')

    def test_json_preview_gap_death_and_night(self):
        p=observed(178); memory=planner.PlannerState()
        memory.team_trips={'purchase':purchase(177),'construction':construction()}
        restored=planner.PlannerState.load(json.loads(json.dumps(memory.dump())))
        self.assertEqual(restored.team_trips,memory.team_trips)
        before=deepcopy(memory.team_trips)
        brain.plan_for_state(deepcopy(p),memory,commit=False,judge_tasks=False)
        self.assertEqual(memory.team_trips,before)
        for mode in ('gap','death','night'):
            q=deepcopy(p)
            if mode=='gap':q['roundNo']+=3
            if mode=='death':
                for u in q['teamOur']['roles']:
                    if u['roleType']=='worker':u['health']=0
            if mode=='night':q['roundNo']=201
            self.assertEqual(team_trip.TripFrame(Turn.load(q),q,memory.team_trips).memory,{},mode)

    def test_malformed_and_same_owner_double_contract_rejected(self):
        self.assertEqual(team_trip.clean_memory({'purchase':dict(purchase(),owner=True)}),{})
        self.assertEqual(team_trip.clean_memory({'construction':dict(construction(),walls=[dict(x=99,y=8)])}),{})
        both=team_trip.clean_memory({'purchase':purchase(),'construction':dict(construction(),owner=20012)})
        self.assertNotIn('construction',both)

    def test_overlay_search_cap_defers_instead_of_ignoring_guard(self):
        p=observed(178);g=team_trip.RouteGuard(Turn.load(p),p,purchase(177))
        with patch.object(team_trip,'MAX_ROUTE_OVERLAYS',0):
            self.assertFalse(g.check(20010,Pos(36,7),(Pos(35,8),))[0])
        self.assertTrue(g.limit_hit)

    def test_real_dispatch_moves_builder_before_building_and_keeps_target(self):
        p=observed(178); memory=planner.PlannerState()
        memory.team_trips={'purchase':purchase(177),'construction':construction()}
        response=brain.plan_for_state(deepcopy(p),memory,judge_tasks=False).commands
        self.assertEqual(response['20010'],dict(action='move',targetPos=[dict(x=36,y=7)]))
        self.assertEqual(memory.team_trips['purchase']['target'],20041)
        before=Turn.load(p)
        for uid,cmd in response.items():
            if cmd['action']=='move':
                role=next(r for r in before.controllable() if r.unit_id==int(uid))
                dest=Pos.load(cmd['targetPos'][0])
                self.assertEqual(distance(role.pos,dest),1)
                self.assertNotIn(dest,before.blocked(role))
                move_actor(p,int(uid),dest.dump())
        p['roundNo']+=1
        response=brain.plan_for_state(deepcopy(p),memory,judge_tasks=False).commands
        self.assertEqual(response['20010'],dict(action='build',name='wall',targetPos=[dict(x=35,y=8)]))
        self.assertEqual(memory.team_trips['purchase']['target'],20041)
        # The queued build is not yet observed: construction still owns the
        # target and cannot be marked finished merely because it was emitted.
        self.assertEqual(memory.team_trips['construction']['phase'],'work')

    def test_normal_nonleased_builder_also_checks_active_teammate_route(self):
        p=observed(178);memory=planner.PlannerState()
        memory.team_trips={'purchase':purchase(177)}
        response=brain.plan_for_state(deepcopy(p),memory,judge_tasks=False).commands
        self.assertEqual(response['20010'],dict(action='move',targetPos=[dict(x=36,y=7)]))
        self.assertEqual(memory.team_trips['purchase']['target'],20041)

    def test_final_route_veto_does_not_establish_a_dropped_constructor(self):
        p=observed(178);f=team_trip.TripFrame(Turn.load(p),p,{'purchase':purchase(177)})
        dangerous=dict(action='build',name='wall',targetPos=[dict(x=35,y=8)])
        f.stage_construction(20010,dangerous,dict(phase='build',planned_walls=[dict(x=35,y=8)]))
        accepted=f.protect_final({20010:dangerous})
        self.assertEqual(accepted,{})
        self.assertNotIn('construction',f.finalize(accepted))

    def test_finite_targets_enter_return_and_release_only_at_observed_post(self):
        p=observed(179);memory={'construction':construction(178)}
        # The one leased wall is now OBSERVED, so the finite assignment must not
        # silently adopt other missing walls and reserve the worker all day.
        f=team_trip.TripFrame(Turn.load(p),p,memory)
        self.assertEqual(f.construction['phase'],'return')
        self.assertEqual(f.construction['walls'],[dict(x=35,y=8)])
        move_actor(p,20010,dict(x=34,y=7));p['roundNo']+=1
        f=team_trip.TripFrame(Turn.load(p),p,f.memory)
        self.assertIsNone(f.construction)

    def test_new_threat_aborts_work_but_keeps_return_assignment(self):
        p=observed(178);p['robot']['roles']=[dict(id=800,health=40,pos=dict(x=10,y=10))]
        f=team_trip.TripFrame(Turn.load(p),p,{'purchase':purchase(177),'construction':construction()})
        self.assertEqual(f.purchase['phase'],'return')
        self.assertEqual(f.construction['phase'],'return')

    def test_real_quarry_wall_return_revalidates_every_move(self):
        from agent import simulator
        p=dict(roundNo=391,mapInfo=dict(width=41,height=32,zones=[
            dict(pos=dict(x=4,y=29),neutralType='stone'),dict(pos=dict(x=18,y=22),neutralType='weaponShop')]),
            teamOur=dict(type='challenger',goldNum=100,roles=[unit(1,'station',9,22,1500),
                unit(2,'rocket',11,20,1000),unit(3,'railgun',10,23,1000),unit(4,'rocket',11,22,1000),
                unit(11,'worker',17,21),unit(12,'worker',6,27),unit(13,'pioneer',8,22)],playerTasks=[]),
            teamEnemy=dict(roles=[]),robot=dict(roles=[]),weaponShopList=[dict(name='WeaponUpgradeVoucher1',price=100)],
            phaseTask='',llmResp='',lastCmdResult='',errors=[])
        memory=planner.PlannerState();actions=[];started=False;finished=False
        for _ in range(55):
            before=Turn.load(p)
            commands=brain.plan_for_state(deepcopy(p),memory,judge_tasks=False).commands
            started=started or 'construction' in memory.team_trips
            if started and 'construction' not in memory.team_trips:
                # Completion is observed BEFORE this round's next ordinary job.
                # A returned worker may immediately depart with remaining stone.
                events=(brain.decision_report().get('upgrade_itinerary') or {}).get('team_trip_events',[])
                if any(e.get('kind')=='construction' and e.get('reason')=='returned_observed' and e.get('owner')==12 for e in events):
                    worker=next(w for w in before.workers() if w.unit_id==12)
                    finished=team_trip.inside(before,worker.pos) and any(distance(worker.pos,g.pos)==1 for g in before.weapons())
                    self.assertTrue(finished,'contract must only release after observed safe arrival')
            for uid,cmd in commands.items():
                if cmd['action']=='move':
                    role=next(r for r in before.controllable() if str(r.unit_id)==uid)
                    dest=Pos.load(cmd['targetPos'][0])
                    self.assertEqual(distance(role.pos,dest),1)
                    self.assertNotIn(dest,before.blocked(role))
            if '12' in commands:actions.append(commands['12']['action'])
            p=simulator.step(p,external_response={'roleCommandMap':commands})['state']
            self.assertFalse(any(v is False for v in p['lastRoundRoleActionResults'].values()))
            memory=planner.PlannerState.load(json.loads(json.dumps(memory.dump())))
            if finished:break
        self.assertTrue(started)
        self.assertIn('collect',actions)
        self.assertIn('build',actions)
        self.assertTrue(finished,'finite work must reach a real post, not reserve its actor until day end')


if __name__=='__main__':unittest.main()
