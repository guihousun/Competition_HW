"""Real HTTP-policy boundary ownership, separate from pure module tests."""
from copy import deepcopy
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'Demo/CoreGeek/src'))
from agent import brain, planner, simulator, defense_sustain
from agent.protocol import Turn, Pos
from agent.tasks import TaskCycle
from benchmark import audit


def unit(uid,kind,x,y,*,bag=(),hp=1000,level=1,cooldown=0):
    return dict(id=uid,roleType=kind,pos=dict(x=x,y=y),health=hp,level=level,
                cooldown=cooldown,backpack=list(bag),backPackCapability=100)


def corridor():
    roles=[unit(87,'station',10,10,hp=1500),unit(412,'rocket',9,9),
           unit(731,'worker',9,10,bag=['WallFixer'],hp=220),
           unit(9081,'worker',9,8,hp=220),unit(611,'wall',13,10,hp=500)]
    free={(9,10),(10,11),(11,11),(12,11),(9,8)}
    free.update((r['pos']['x'],r['pos']['y']) for r in roles)
    zones=[dict(neutralType='obstacle',pos=dict(x=x,y=y))
           for x in range(41) for y in range(32) if (x,y) not in free]
    return dict(roundNo=80,mapInfo=dict(width=41,height=32,zones=zones),
                teamOur=dict(type='challenger',goldNum=0,totalScore=0,roles=roles),
                teamEnemy=dict(type='defender',roles=[]),robot=dict(roles=[]),
                worldNews={},weaponShopList=[],vendorShopList=[],phaseTask='')


def role(p,uid):
    return next(r for r in p['teamOur']['roles'] if r['id']==uid)


class MaintenanceIntegrationV2Tests(unittest.TestCase):
    def setUp(self):
        planner.reset()
        env=patch.dict(os.environ,{'COMPETITION_HW_MAINTENANCE_V2':'on'})
        env.start();self.addCleanup(env.stop);self.addCleanup(planner.reset)

    def step_policy(self,p):
        public={k:deepcopy(v) for k,v in p.items() if not k.startswith('_')}
        reply=brain.respond(public)
        self.assertEqual(audit(public,reply['roleCommandMap']),[])
        self.assertTrue(set(reply)<={'roleCommandMap','prompt','executeCmd'})
        return simulator.step(p,external_response=reply)['state'],reply['roleCommandMap']

    def combat(self):
        p=corridor()
        p['mapInfo']['zones']=[z for z in p['mapInfo']['zones'] if z['pos']!={'x':15,'y':10}]
        p['robot']['roles']=[unit(9000,'largeRobot',15,10,hp=500)]
        return p

    def test_full_pipeline_repairs_returns_and_resumes_fire(self):
        p=self.combat();actions=[];controllers=[]
        for _ in range(9):
            p,c=self.step_policy(p)
            if '731' in c:actions.append(c['731']['action'])
            if '412' in c:controllers.append(c['412']['controllerId'])
            self.assertFalse('731' in c and any(a.get('controllerId')=='731' for a in c.values()))
        self.assertEqual(actions,['move']*3+['use']+['move']*3)
        self.assertEqual(controllers,['9081','9081','731'])
        self.assertEqual(role(p,731)['pos'],dict(x=9,y=10))
        self.assertEqual(role(p,731)['backpack'],[])

    def test_day_full_pipeline_buys_only_one_repairs_and_returns(self):
        from test_maintenance_supply_v2 import scene
        p=scene();actions=[]
        for _ in range(10):
            p,c=self.step_policy(p)
            if '731' in c:actions.append(c['731']['action'])
        self.assertEqual(actions,['buy']+['move']*3+['use']+['move']*3)
        self.assertEqual(p['teamOur']['goldNum'],0)
        self.assertEqual(role(p,611)['health'],1000)
        self.assertEqual(role(p,731)['backpack'],[])
        self.assertEqual(role(p,731)['pos'],dict(x=9,y=10))
        self.assertNotIn('purchase',planner.state_for(p).team_trips)

    def test_day_preview_does_not_issue_purchase(self):
        from test_maintenance_supply_v2 import scene
        p=scene();m=planner.PlannerState();before=deepcopy(m.dump())
        response=brain.plan_for_state(p,m,commit=False,judge_tasks=False)
        self.assertEqual(response.commands['731']['action'],'buy')
        self.assertEqual(m.dump(),before)

    def test_carried_feasible_voucher_keeps_priority_without_memory(self):
        from test_maintenance_supply_v2 import scene
        p=scene();role(p,731)['backpack']=['WeaponUpgradeVoucher1'];m=planner.PlannerState()
        c=brain.plan_for_state(p,m,judge_tasks=False).commands
        self.assertEqual(c['731'],dict(action='use',name='WeaponUpgradeVoucher1',
                                     targetPos=[dict(x=9,y=9)]))
        self.assertEqual(m.team_trips['purchase']['item'],'WeaponUpgradeVoucher1')
        self.assertNotEqual(m.team_trips['purchase'].get('operation'),'repair')
        self.assertFalse(any(a.get('action')=='buy' and a.get('name')=='WallFixer' for a in c.values()))

    def test_night_shop_presence_never_starts_a_purchase(self):
        from test_maintenance_supply_v2 import scene
        p=scene();p['roundNo']=461;p['teamOur']['goldNum']=100
        c=brain.plan_for_state(p,planner.PlannerState(),judge_tasks=False).commands
        self.assertFalse(any(a.get('action')=='buy' for a in c.values()))

    def test_accepted_waiting_task_cannot_prove_fire_coverage(self):
        p=self.combat();role(p,9081)['roleType']='pioneer';m=planner.PlannerState()
        m.tasks['cycle']=TaskCycle(point=dict(x=9,y=8),accepted_round=79)
        c=brain.plan_for_state(p,m,judge_tasks=False).commands
        self.assertNotIn('731',c)
        self.assertEqual(c['412']['controllerId'],'731')
        self.assertFalse(any(a.get('controllerId')=='9081' for a in c.values()))
        self.assertEqual(brain.decision_report()['task']['acceptance_status']['phase'],'awaiting_observation')

    def test_evading_pioneer_does_not_prove_fire_coverage(self):
        p=corridor();p['mapInfo']['zones']=[]
        role(p,412)['pos']=dict(x=9,y=10);role(p,731)['pos']=dict(x=9,y=9)
        role(p,9081).update(roleType='pioneer',pos=dict(x=9,y=11))
        role(p,611)['pos']=dict(x=13,y=9)
        p['robot']['roles']=[unit(9000,'largeRobot',6,11,hp=500)]
        c=brain.respond(p)['roleCommandMap']
        self.assertNotIn('731',c);self.assertEqual(c['412']['controllerId'],'731')
        self.assertEqual(c['9081']['action'],'move')
        self.assertIn('fire_coverage',brain.decision_report()['defense_sustain']['rejections'])

    def test_final_reconcile_rejects_new_proposal_without_ghost_lease(self):
        p=self.combat();m=planner.PlannerState();real=brain.reconcile
        def drop(turn,payload,commands,**kwargs):
            result=real(turn,payload,commands,**kwargs);result.pop(731,None);return result
        with patch.object(brain,'reconcile',side_effect=drop):
            c=brain.plan_for_state(p,m,judge_tasks=False).commands
        self.assertNotIn('731',c);self.assertNotIn('lease',m.sustain_memory)

    def test_preview_cannot_change_new_or_existing_lease(self):
        p=self.combat();m=planner.PlannerState();before=deepcopy(m.dump())
        brain.plan_for_state(p,m,commit=False,judge_tasks=False)
        self.assertEqual(m.dump(),before)
        brain.plan_for_state(p,m,commit=True,judge_tasks=False)
        self.assertIn('lease',m.sustain_memory)
        before=deepcopy(m.dump())
        brain.plan_for_state(p,m,commit=False,judge_tasks=False)
        self.assertEqual(m.dump(),before)

    def test_disabled_does_not_call_new_modules_or_write_optional_memory(self):
        p=self.combat();m=planner.PlannerState()
        with patch.dict(os.environ,{'COMPETITION_HW_MAINTENANCE_V2':'off'}), \
             patch.object(defense_sustain,'plan',side_effect=AssertionError('disabled')):
            brain.plan_for_state(p,m,judge_tasks=False)
        self.assertNotIn('sustainMemory',m.dump())
        self.assertNotIn('defense_sustain',brain.decision_report())

    def test_memory_roundtrip_and_new_match_reset(self):
        p=self.combat();m=planner.PlannerState()
        brain.plan_for_state(p,m,judge_tasks=False)
        restored=planner.PlannerState.load(m.dump())
        self.assertEqual(restored.sustain_memory,m.sustain_memory)
        restored.last_round=80;restored.note_round(1)
        self.assertEqual(restored.sustain_memory,{})


if __name__=='__main__':unittest.main()
