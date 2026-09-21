"""User strategy integration on hand-set public observations, not a game oracle."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from test_baseline import ROOT
from test_coordination import unit
from agent import brain, planner, phase_supply, phase_maintenance, team_trip, upgrade_itinerary, strategy_config
from agent.protocol import Turn, WALL_FIXER, Pos


def observation(round_no=391):
    roles=[unit(1,'worker',11,9),unit(2,'worker',13,11),unit(3,'pioneer',8,9),
           unit(4,'station',9,10,1500),unit(5,'rocket',11,10,1500),
           unit(6,'rocket',11,8,1500),unit(7,'rocket',10,8,1500),
           unit(8,'wall',12,10,500)]
    for u in roles:
        if u['roleType']=='rocket':u.update(level=2,attackRange=15)
    return dict(roundNo=round_no,mapInfo=dict(width=41,height=32,zones=[
        dict(pos=dict(x=14,y=11),neutralType='weaponShop')]),
        teamOur=dict(type='challenger',goldNum=60,roles=roles),teamEnemy=dict(type='defender',roles=[]),
        weaponShopList=[dict(name=WALL_FIXER,price=10)])


class UserPhaseIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.config=deepcopy(strategy_config.DEFAULTS)
        self.settings=patch.object(strategy_config,'get',return_value=self.config)
        self.settings.start()
        self.constants=patch.object(brain,'_STRATEGY',self.config)
        self.constants.start()
        self.addCleanup(self.settings.stop);self.addCleanup(self.constants.stop)

    def test_stock_buy_two_return_unused_then_spare_worker_repairs_at_night(self):
        p=observation();memory={};actions=[]
        for round_no in range(391,395):
            p['roundNo']=round_no;turn=Turn.load(p)
            frame=team_trip.TripFrame(turn,p,memory,purchase_deadline=67)
            if frame.purchase:
                proposal,report=upgrade_itinerary.plan(turn,p,{},deadline=67,commitment=frame.purchase)
            else:
                proposal,report=phase_supply.plan(turn,p,{},deadline=67)
            commands={} if proposal is None else {proposal[0]:proposal[1]}
            frame.stage_purchase(report,proposal)
            memory=frame.finalize(commands)
            if not commands:continue
            self.assertEqual(set(commands),{2})
            cmd=commands[2];actions.append(cmd['action'])
            self.assertNotEqual(cmd['action'],'use')
            if cmd['action']=='buy':
                self.assertEqual(cmd['num'],2)
                p['teamOur']['roles'][1]['backpack'] += [WALL_FIXER]*2
                p['teamOur']['goldNum']-=20
            elif cmd['action']=='move':
                p['teamOur']['roles'][1]['pos']=deepcopy(cmd['targetPos'][0])
        self.assertEqual(actions,['buy','move','move'])
        self.assertNotIn('purchase',memory)
        self.assertEqual(p['teamOur']['roles'][1]['backpack'],[WALL_FIXER]*2)
        self.assertEqual(p['teamOur']['roles'][1]['pos'],dict(x=11,y=11))
        p['roundNo']=461;maintenance={}
        result=phase_maintenance.night_plan(Turn.load(p),maintenance,{},excluded_roles={1},payload=p)
        self.assertEqual(result['commands'][2],dict(action='use',name=WALL_FIXER,targetPos=[dict(x=12,y=10)]))
        phase_maintenance.finalize(maintenance,result,result['commands'])
        self.assertEqual(maintenance['lease']['owner'],2)

    def test_acquire_stage_preserves_stock_contract_across_moves(self):
        p=observation();p['teamOur']['roles'][1]['pos']=dict(x=12,y=11)
        turn=Turn.load(p);proposal,report=phase_supply.plan(turn,p,{},deadline=67)
        frame=team_trip.TripFrame(turn,p,{},purchase_deadline=67)
        frame.stage_purchase(report,proposal);memory=frame.finalize({proposal[0]:proposal[1]})
        self.assertEqual(memory['purchase']['operation'],'stock')
        self.assertEqual(memory['purchase']['quantity'],2)
        self.assertEqual(memory['purchase']['deadline'],458)
        p['roundNo']=392;p['teamOur']['roles'][1]['pos']=proposal[1]['targetPos'][0]
        turn=Turn.load(p);frame=team_trip.TripFrame(turn,p,memory,purchase_deadline=67)
        proposal,report=upgrade_itinerary.plan(turn,p,{},deadline=67,commitment=frame.purchase)
        frame.stage_purchase(report,proposal);memory=frame.finalize({proposal[0]:proposal[1]})
        self.assertEqual(memory['purchase']['operation'],'stock')
        self.assertEqual(memory['purchase']['quantity'],2)
        self.assertEqual(memory['purchase']['issued_round'],391)
        self.assertEqual(memory['purchase']['deadline'],458)

    def test_unusable_shooter_or_pioneer_stock_does_not_suppress_spare_stock(self):
        for index in (0,2):
            p=observation();p['teamOur']['roles'][index]['backpack']=[WALL_FIXER]*2
            proposal,_=phase_supply.plan(Turn.load(p),p,{},deadline=67)
            self.assertEqual(proposal,(2,dict(action='buy',name=WALL_FIXER,num=2)))

    def test_reserved_trip_owner_is_not_reassigned(self):
        p=observation()
        proposal,_=phase_supply.plan(Turn.load(p),p,{},deadline=67,reserved_workers={2})
        self.assertIsNone(proposal)
        p['roundNo']=461;p['teamOur']['roles'][1]['pos']=dict(x=11,y=11)
        p['teamOur']['roles'][1]['backpack']=[WALL_FIXER]*2
        result=phase_maintenance.night_plan(Turn.load(p),{}, {},excluded_roles={1,2},payload=p)
        self.assertFalse(result['commands'])

    def test_deadline_refuses_trip_and_invalid_stock_schema_dropped(self):
        p=observation(457);p['teamOur']['roles'][1]['pos']=dict(x=13,y=11)
        proposal,report=phase_supply.plan(Turn.load(p),p,{},deadline=67)
        self.assertIsNone(proposal)
        row=dict(owner=2,target=4,item=WALL_FIXER,operation='stock',quantity=True,
                 phase='acquire',level=1,count=0,last_action='buy',issued_round=391,last_round=391,deadline=458)
        self.assertNotIn('purchase',team_trip.clean_memory({'purchase':row}))

    def test_real_brain_night_fires_at_most_one_rocket_and_reserves_pioneer(self):
        p=observation(461);p['teamOur']['roles'][1]['pos']=dict(x=11,y=11)
        p['teamOur']['roles'][1]['backpack']=[WALL_FIXER]*2
        p['robot']=dict(roles=[dict(id=90,pos=dict(x=16,y=9),health=500,roleType='largeRobot')])
        state=planner.PlannerState()
        with patch.object(brain,'_task_step',side_effect=AssertionError('full night must reserve pioneer')):
            response=brain.plan_for_state(p,state,judge_tasks=False)
        attacks=[c for c in response.commands.values() if c.get('action')=='attack']
        self.assertEqual(len(attacks),1)
        self.assertEqual(attacks[0]['controllerId'],'1')
        self.assertEqual(response.commands.get('2',{}).get('action'),'use')
        self.assertTrue(state.tasks['supervisor']['reserve_pioneer'])

    def test_real_brain_carries_bought_stock_home_without_day_use(self):
        p=observation();state=planner.PlannerState();actions=[]
        for round_no in range(391,395):
            p['roundNo']=round_no
            response=brain.plan_for_state(p,state,judge_tasks=False)
            command=response.commands.get('2',{})
            if command.get('action')=='buy':
                self.assertEqual(command,dict(action='buy',name=WALL_FIXER,num=2))
                p['teamOur']['roles'][1]['backpack'] += [WALL_FIXER]*2
                p['teamOur']['goldNum']-=20
                actions.append('buy')
            elif command.get('action')=='move':
                p['teamOur']['roles'][1]['pos']=deepcopy(command['targetPos'][0])
                actions.append('move')
            self.assertNotEqual(command.get('action'),'use')
        self.assertEqual(actions[:3],['buy','move','move'])
        self.assertNotIn('purchase',state.team_trips)
        self.assertEqual(p['teamOur']['roles'][1]['backpack'],[WALL_FIXER]*2)

    def test_real_brain_preview_has_no_memory_side_effects_and_day_four_acts(self):
        p=observation();state=planner.PlannerState();before=deepcopy(state.dump())
        response=brain.plan_for_state(p,state,commit=False,judge_tasks=False)
        self.assertEqual(state.dump(),before)
        self.assertTrue(response.commands,'day four remains active economy/building time')
        p['roundNo']=461;p['teamOur']['roles'][1]['pos']=dict(x=11,y=11)
        p['teamOur']['roles'][1]['backpack']=[WALL_FIXER]*2
        brain.plan_for_state(p,state,commit=False,judge_tasks=False)
        self.assertEqual(state.dump(),before)


if __name__=='__main__':unittest.main()
