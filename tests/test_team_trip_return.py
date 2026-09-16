"""Independent corridor: outside use costs1 and a legal return costs4."""
from copy import deepcopy
import unittest

from test_team_trip import observed
from test_coordination import unit
from agent import brain, planner, team_trip, upgrade_itinerary
from agent.protocol import Turn, Pos

ITEM='WallUpgradeVoucher1'


def corridor(round_no=63,inside=False):
    free={(7,10),(7,11),(8,12),(9,11),(9,10),
          (10,10),(11,10),(10,9),(11,9),(9,9),(8,10)}
    p=dict(roundNo=round_no,mapInfo=dict(width=41,height=32,zones=[
        dict(pos=dict(x=x,y=y),neutralType='water') for x in range(41) for y in range(32)
        if (x,y) not in free]),teamOur=dict(type='challenger',goldNum=0,roles=[
            unit(87,'station',10,10,1500),unit(412,'rocket',9,9,1000),
            unit(800,'wall',8,10,1000),unit(731,'worker',9 if inside else 7,10)],playerTasks=[]),
        teamEnemy=dict(roles=[]),robot=dict(roles=[]),weaponShopList=[],phaseTask='',llmResp='',lastCmdResult='',errors=[])
    p['teamOur']['roles'][-1]['backpack']=[ITEM]
    c=dict(owner=731,target=800,item=ITEM,level=1,count=1,issued_round=60,
           last_round=round_no-1,deadline=68,phase='acquire',last_action='use')
    return p,c


class ReturnContractTests(unittest.TestCase):
    def test_new_purchase_conflict_defers_purchase_not_construction(self):
        p=observed(178);t=Turn.load(p)
        proposal,report=upgrade_itinerary.plan(t,p,{},start=0,deadline=67)
        self.assertEqual((proposal[0],report['building']),(20012,20041))
        f=team_trip.TripFrame(t,p,{});f.stage_purchase(report,proposal)
        building=dict(action='build',name='wall',targetPos=[dict(x=35,y=8)])
        commands={proposal[0]:proposal[1],20010:building};before=deepcopy(commands)
        accepted=f.protect_final(commands)
        self.assertEqual(accepted,{20010:building})
        self.assertEqual(commands,before)
        self.assertEqual(f.finalize(accepted),{})
        self.assertEqual([e['event'] for e in f.events],['defer'])
        self.assertEqual(f.events[0]['route']['remaining_actions'],20)
        self.assertEqual(f.events[0]['route']['budget'],19)
        # Direct finalize also refuses phantom registration; repeated checks do
        # not produce an issued/cancelled/issued event loop in this frame.
        self.assertEqual(f.finalize(commands),{})
        self.assertEqual([e['event'] for e in f.events],['defer'])

    def test_new_trip_accounts_for_exact_emitted_first_step(self):
        p=observed(178);t=Turn.load(p)
        proposal,report=upgrade_itinerary.plan(t,p,{},start=0,deadline=67)
        f=team_trip.TripFrame(t,p,{});f.stage_purchase(report,proposal)
        record=f.pending['purchase'][0]
        # Without the construction, the conditional next state consumes one
        # round and starts at (26,18); it is not the original round's cost.
        cost=f.prospective_new(record,proposal[1],dict([proposal]))
        self.assertEqual((cost.actions,cost.budget),(15,19))

    def test_final_worker_move_can_also_invalidate_new_trip(self):
        p=observed(179)
        next(u for u in p['teamOur']['roles'] if u['id']==20010)['pos']=dict(x=36,y=7)
        t=Turn.load(p);proposal,report=upgrade_itinerary.plan(t,p,{},start=0,deadline=67)
        self.assertEqual(report['building'],20041)
        f=team_trip.TripFrame(t,p,{});f.stage_purchase(report,proposal)
        moving=dict(action='move',targetPos=[dict(x=35,y=7)])
        commands={proposal[0]:proposal[1],20010:moving}
        accepted=f.protect_final(commands)
        self.assertEqual(accepted,{20010:moving})
        self.assertEqual(f.finalize(accepted),{})

    def test_valid_concurrent_move_registers_only_actual_current_facts(self):
        p=observed(178);before=deepcopy(p);t=Turn.load(p)
        proposal,report=upgrade_itinerary.plan(t,p,{},start=0,deadline=67)
        f=team_trip.TripFrame(t,p,{});f.stage_purchase(report,proposal)
        commands={proposal[0]:proposal[1],20010:dict(action='move',targetPos=[dict(x=36,y=7)])}
        accepted=f.protect_final(commands)
        self.assertEqual(accepted,commands)
        memory=f.finalize(accepted)
        self.assertEqual(memory['purchase']['phase'],'acquire')
        self.assertEqual(memory['purchase']['count'],0)
        self.assertEqual(memory['purchase']['level'],1)
        self.assertEqual(memory['purchase']['last_round'],178)
        self.assertEqual(p,before,'conditional next state must never replace the public observation')

    def test_base_use_must_also_budget_its_nonzero_inside_return(self):
        p,c=corridor()
        free={(9,11),(9,10),(9,9),(10,8),(11,8),
              (10,10),(11,10),(10,9),(11,9),(12,8)}
        p['mapInfo']['zones']=[dict(pos=dict(x=x,y=y),neutralType='water')
            for x in range(41) for y in range(32) if (x,y) not in free]
        p['teamOur']['roles']=[u for u in p['teamOur']['roles'] if u['id']!=800]
        next(u for u in p['teamOur']['roles'] if u['id']==412)['pos']=dict(x=12,y=8)
        owner=next(u for u in p['teamOur']['roles'] if u['id']==731)
        owner['pos']=dict(x=9,y=11);owner['backpack']=['StationUpgradeVoucher1']
        c.update(target=87,item='StationUpgradeVoucher1')
        cost=team_trip.evaluate_trip(Turn.load(p),p,c)
        self.assertEqual((cost.actions,cost.command['action']),(5,'use'))
        p['roundNo']=64
        self.assertFalse(team_trip.evaluate_trip(Turn.load(p),p,c).feasible)

    def test_outside_use_includes_real_four_step_return(self):
        p,c=corridor();cost=team_trip.evaluate_trip(Turn.load(p),p,c)
        self.assertEqual((cost.actions,cost.budget,cost.command['action']),(5,5,'use'))
        p['roundNo']=64
        cost=team_trip.evaluate_trip(Turn.load(p),p,c)
        self.assertEqual((cost.actions,cost.budget,cost.feasible),(5,4,False))
        proposal,_=upgrade_itinerary.plan(Turn.load(p),p,{},start=0,deadline=67)
        self.assertIsNone(proposal,'a fresh proposal must not repeat a known impossible use+return')

    def test_inside_post_use_keeps_zero_return_cost(self):
        p,c=corridor(67,inside=True)
        cost=team_trip.evaluate_trip(Turn.load(p),p,c)
        self.assertEqual((cost.actions,cost.budget,cost.command['action']),(1,1,'use'))

    def test_confirmed_use_owns_four_return_moves_until_real_post(self):
        p,c=corridor(64);c['last_round']=63
        p['teamOur']['roles'][2]['level']=2
        p['teamOur']['roles'][-1]['backpack']=[]
        memory={'purchase':c}
        expected=[(7,11),(8,12),(9,11),(9,10)]
        for index,(x,y) in enumerate(expected):
            f=team_trip.TripFrame(Turn.load(p),p,memory)
            self.assertEqual(f.purchase['phase'],'return')
            if index==0:self.assertEqual(f.events[0]['reason'],'upgrade_confirmed_observed')
            proposal,report=upgrade_itinerary.plan(Turn.load(p),p,{},commitment=f.purchase)
            self.assertEqual(proposal[1],dict(action='move',targetPos=[dict(x=x,y=y)]))
            self.assertNotIn(Pos(x,y),Turn.load(p).blocked(Turn.load(p).workers()[0]))
            f.stage_purchase(report,proposal);memory=f.finalize(dict([proposal]))
            p['teamOur']['roles'][-1]['pos']=dict(x=x,y=y);p['roundNo']+=1
        f=team_trip.TripFrame(Turn.load(p),p,memory)
        self.assertIsNone(f.purchase)
        self.assertEqual(f.events[-1]['reason'],'returned_observed')

    def test_inside_confirmed_use_can_release_immediately(self):
        p,c=corridor(67,inside=True);p['teamOur']['roles'][2]['level']=2
        p['teamOur']['roles'][-1]['backpack']=[]
        f=team_trip.TripFrame(Turn.load(p),p,{'purchase':c})
        self.assertIsNone(f.purchase)
        self.assertEqual([e['reason'] for e in f.events],['upgrade_confirmed_observed','returned_observed'])

    def test_failed_use_retains_item_and_only_retries_if_whole_trip_fits(self):
        p,c=corridor(63)
        f=team_trip.TripFrame(Turn.load(p),p,{'purchase':c})
        proposal,_=upgrade_itinerary.plan(Turn.load(p),p,{},commitment=f.purchase)
        self.assertEqual(proposal[1]['action'],'use')
        # One unsuccessful use later, both the level and bag are unchanged.
        p['roundNo']=64;c['last_round']=63
        f=team_trip.TripFrame(Turn.load(p),p,{'purchase':c})
        self.assertEqual(f.purchase['phase'],'return')
        self.assertEqual(f.events[0]['reason'],'route_exceeds_budget')
        self.assertEqual(p['teamOur']['roles'][-1]['backpack'],[ITEM])
        self.assertFalse(any(e.get('reason')=='upgrade_confirmed_observed' for e in f.events))

    def test_level_without_consumption_is_not_our_success(self):
        p,c=corridor(64);p['teamOur']['roles'][2]['level']=2
        f=team_trip.TripFrame(Turn.load(p),p,{'purchase':c})
        self.assertEqual(f.events[0]['reason'],'target_changed_observed')
        self.assertEqual(p['teamOur']['roles'][-1]['backpack'],[ITEM])

    def test_blocked_return_keeps_ownership_without_new_buy_or_use(self):
        p,c=corridor(64);c['phase']='return'
        p['mapInfo']['zones'].append(dict(pos=dict(x=7,y=11),neutralType='water'))
        f=team_trip.TripFrame(Turn.load(p),p,{'purchase':c})
        proposal,report=upgrade_itinerary.plan(Turn.load(p),p,{},commitment=f.purchase)
        self.assertIsNone(proposal);self.assertEqual(report['reason'],'return_blocked')
        mem=f.finalize({});self.assertEqual(mem['purchase']['phase'],'return')
        m=planner.PlannerState();m.team_trips=mem
        response=brain.plan_for_state(deepcopy(p),m,judge_tasks=False).commands
        self.assertNotIn(response.get('731',{}).get('action'),('use','buy','collect'))
        self.assertEqual(m.team_trips['purchase']['phase'],'return')

    def test_threat_late_day_and_night_handoff(self):
        p,c=corridor(63);p['robot']['roles']=[dict(id=90,health=40,pos=dict(x=30,y=20))]
        f=team_trip.TripFrame(Turn.load(p),p,{'purchase':c})
        self.assertEqual(f.purchase['phase'],'return')
        self.assertEqual(f.events[0]['reason'],'visible_threat')
        # Unexpected blockage/failed movement may miss the planning deadline.
        # Return safely anyway; never label this as on-time completion.
        p,c=corridor(69);c['phase']='return'
        f=team_trip.TripFrame(Turn.load(p),p,{'purchase':c})
        proposal,report=upgrade_itinerary.plan(Turn.load(p),p,{},commitment=f.purchase)
        self.assertEqual(proposal[1]['action'],'move')
        self.assertEqual(report['reason'],'late_return')
        p['roundNo']=71
        self.assertEqual(team_trip.TripFrame(Turn.load(p),p,f.memory).memory,{})

    def test_new_danger_does_not_restart_an_outbound_held_voucher_trip(self):
        p,c=corridor(63)
        p['robot']['roles']=[dict(id=90,health=40,pos=dict(x=30,y=20))]
        proposal,report=upgrade_itinerary.plan(Turn.load(p),p,{},start=0,deadline=67)
        self.assertIsNone(proposal)
        self.assertEqual(report['reason'],'held_voucher_deferred_for_threat')
        # A one-action use at a safe actual gun post remains useful and legal.
        p,c=corridor(63,inside=True)
        p['robot']['roles']=[dict(id=90,health=40,pos=dict(x=30,y=20))]
        proposal,_=upgrade_itinerary.plan(Turn.load(p),p,{},start=0,deadline=67)
        self.assertEqual(proposal[1]['action'],'use')


if __name__=='__main__':unittest.main()
