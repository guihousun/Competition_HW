"""Independent tiny-board repair expectations, without simulator settlement."""
from copy import deepcopy
import unittest

from test_baseline import ROOT
from test_coordination import unit
from agent import phase_maintenance as pm, home_defense
from agent.protocol import Turn, WALL_FIXER, Pos, move_command
from legacy_strategy import LegacyStrategyCase


def observation():
    p = dict(roundNo=461, mapInfo=dict(width=41, height=32, zones=[]),
             teamOur=dict(goldNum=40, roles=[unit(1, 'worker', 8, 10),
                 unit(2, 'worker', 11, 9), unit(3, 'pioneer', 8, 9),
                 unit(4, 'station', 9, 10, 1500), unit(5, 'rocket', 11, 10, 1000),
                 unit(6, 'rocket', 11, 8, 1000), unit(7, 'rocket', 10, 8, 1000),
                 unit(8, 'wall', 7, 10, 500)]), teamEnemy=dict(roles=[]),
             weaponShopList=[dict(name=WALL_FIXER, price=10)])
    p['teamOur']['roles'][0]['backpack'] = [WALL_FIXER] * 2
    return p


# The historical fixture deliberately repairs x=7, the rear of the x=9 base.
# Keep lease/feedback rollback coverage; new default front geometry has its own tests.
class PhaseMaintenanceTests(LegacyStrategyCase):
    def plan(self, p, memory=None, **kwargs):
        memory = {} if memory is None else memory
        return pm.night_plan(Turn.load(p), memory, kwargs.pop('commands', {}),
                             excluded_roles=kwargs.pop('excluded_roles', (2,)), payload=p, **kwargs)

    def test_day_and_early_night_do_not_start(self):
        for round_no in (1, 71, 201, 331, 391):
            p = observation(); p['roundNo'] = round_no
            self.assertFalse(self.plan(p)['owned_roles'])

    def test_default_spare_worker_repairs_while_operator_is_excluded(self):
        p = observation(); result = self.plan(p)
        self.assertEqual(result['owned_roles'], [1])
        self.assertEqual(result['commands'][1], dict(action='use', name=WALL_FIXER, targetPos=[dict(x=7,y=10)]))
        self.assertNotIn(2, result['commands'])

    def test_no_item_no_repair_and_pioneer_never_selected(self):
        p = observation(); p['teamOur']['roles'][0]['backpack'] = []
        p['teamOur']['roles'][2]['backpack'] = [WALL_FIXER]
        self.assertFalse(self.plan(p)['owned_roles'])

    def test_actual_attack_controller_and_task_worker_not_stolen(self):
        p = observation()
        for commands in ({5:dict(action='attack', controllerId='1', targetPos=[])},
                         {1:dict(action='acceptTask')}):
            self.assertFalse(self.plan(p, commands=commands)['owned_roles'])

    def test_cooldown_only_round_does_not_require_ready_weapon(self):
        p = observation()
        for u in p['teamOur']['roles']:
            if u['roleType']=='rocket': u['cooldown']=2
        self.assertEqual(self.plan(p, ready_weapons=())['owned_roles'], [1])

    def test_hysteresis_continues_at_seventy_percent_after_confirmed_use(self):
        p = observation(); memory={}; r=self.plan(p,memory)
        pm.finalize(memory,r,r['commands'])
        p['roundNo']+=1; p['teamOur']['roles'][0]['backpack'].pop()
        p['teamOur']['roles'][-1]['health']=700
        p['lastRoundRoleActionResults']={'1':True}
        r=self.plan(p,memory)
        self.assertEqual(r['report']['use_feedback'],'confirmed')
        self.assertEqual(r['commands'][1]['action'],'use')
        fresh=self.plan(p,{})
        self.assertFalse(fresh['commands'], 'seventy percent does not enter a new repair lease')

    def test_successful_use_not_inferred_from_health_increase_or_dispatch(self):
        p=observation(); memory={}; r=self.plan(p,memory); pm.finalize(memory,r,r['commands'])
        p['roundNo']+=1; p['teamOur']['roles'][-1]['health']=700
        p['lastRoundRoleActionResults']={'1':True}
        r=self.plan(p,memory)
        self.assertEqual(r['report']['use_feedback'],'unconfirmed')
        self.assertFalse(r['commands'])

    def test_same_round_does_not_submit_second_use(self):
        p=observation();memory={};r=self.plan(p,memory);pm.finalize(memory,r,r['commands'])
        r=self.plan(p,memory)
        self.assertFalse(r['commands']); self.assertEqual(r['owned_roles'],[1])

    def test_final_rejected_first_proposal_has_no_lease(self):
        p=observation();memory={};r=self.plan(p,memory)
        pm.finalize(memory,r,{})
        self.assertNotIn('lease',memory)

    def test_interior_move_and_observed_return(self):
        p=observation(); p['teamOur']['roles'][0]['pos']=dict(x=8,y=8)
        p['teamOur']['roles'][2]['health']=0
        memory={};r=self.plan(p,memory)
        step=Pos.load(r['commands'][1]['targetPos'][0])
        self.assertTrue(home_defense.inside(Turn.load(p),step))
        self.assertEqual(r['commands'][1]['action'],'move')
        pm.finalize(memory,r,r['commands'])
        p['roundNo']+=1;p['teamOur']['roles'][0]['pos']=step.dump()
        p['teamOur']['roles'][-1]['health']=900
        r=self.plan(p,memory)
        self.assertEqual(r['report']['phase'],'return')
        self.assertEqual(r['owned_roles'],[1])
        p['roundNo']+=1;p['teamOur']['roles'][0]['pos']=dict(x=8,y=8)
        r=self.plan(p,memory)
        self.assertEqual(r['report']['reason'],'returned_observed')
        self.assertFalse(r['owned_roles'])

    def test_outside_worker_cannot_begin_route(self):
        p=observation();p['teamOur']['roles'][0]['pos']=dict(x=6,y=10)
        self.assertFalse(self.plan(p)['commands'])

    def test_mirrored_red_geometry_has_same_worker_and_use(self):
        p=observation()
        for u in p['teamOur']['roles']:
            u['pos']['x']=40-u['pos']['x']-(1 if u['roleType']=='station' else 0)
        p['teamOur']['type']='defender'
        result=self.plan(p)
        self.assertEqual(result['owned_roles'],[1])
        self.assertEqual(result['commands'][1]['targetPos'],[dict(x=33,y=10)])

    def test_blocked_return_keeps_owner_and_does_not_move_outside(self):
        p=observation();p['teamOur']['roles'][0]['pos']=dict(x=8,y=8)
        p['teamOur']['roles'][2]['health']=0
        memory={};r=self.plan(p,memory);pm.finalize(memory,r,r['commands'])
        p['roundNo']+=1;p['teamOur']['roles'][0]['pos']=r['commands'][1]['targetPos'][0]
        p['teamOur']['roles'][-1]['health']=900
        p['teamOur']['roles'].append(unit(90,'worker',8,8))
        r=self.plan(p,memory)
        self.assertEqual(r['report']['reason'],'return_blocked')
        self.assertEqual(r['owned_roles'],[1]);self.assertFalse(r['commands'])

    def test_invalid_and_future_memory_not_revived(self):
        p=observation(); p['teamOur']['roles'][0]['backpack']=[]
        memory={'last_round':999,'lease':{'bad':'value'}}
        self.plan(p,memory)
        self.assertEqual(memory,{'last_round':461})
        self.assertEqual(pm.sanitize_memory({'lease':['invalid']}),{})

    def test_gap_and_next_night_drop_old_home_without_invented_use_receipt(self):
        for next_round in (464,591):
            p=observation();memory={};r=self.plan(p,memory);pm.finalize(memory,r,r['commands'])
            p['roundNo']=next_round
            r=self.plan(p,memory)
            self.assertNotIn('lease',memory)
            self.assertEqual(r['report']['reason'],'observation_gap_or_expired_night')
            self.assertNotIn('use_feedback',r['report'])

    def test_sanitizer_rejects_boolean_role_and_future_use_round(self):
        p=observation();memory={};self.plan(p,memory)
        invalid=deepcopy(memory);invalid['lease']['owner']=True
        self.assertNotIn('lease',pm.sanitize_memory(invalid))
        self.assertNotIn('lease',pm.sanitize_memory(memory,last_round=400))
        memory['lease']['use_round']=999
        p['teamOur']['roles'][0]['backpack']=[]
        self.plan(p,memory)
        self.assertNotIn('lease',memory)

    def test_restock_respects_observed_price_stock_and_team_reservation(self):
        p=observation();p['roundNo']=391;p['teamOur']['roles'][0]['backpack']=[]
        p['mapInfo']['zones']=[dict(pos=dict(x=7,y=10),neutralType='weaponShop')]
        turn=Turn.load(p)
        proposal,_=pm.restock_plan(turn,p,{},(2,))
        self.assertEqual(proposal,(1,dict(action='buy',name=WALL_FIXER,num=2)))
        proposal,_=pm.restock_plan(turn,p,{2:dict(action='buy',name=WALL_FIXER,num=3)},(2,))
        self.assertEqual(proposal,(1,dict(action='buy',name=WALL_FIXER,num=1)))
        proposal,_=pm.restock_plan(turn,p,{},(2,),dict(reserve_gold=35))
        self.assertIsNone(proposal)

    def test_restock_no_assumed_price_or_night_shopping(self):
        p=observation();self.assertIsNone(pm.restock_plan(Turn.load(p),p,{})[0])
        p['roundNo']=391;p['weaponShopList']=[]
        self.assertIsNone(pm.restock_plan(Turn.load(p),p,{})[0])


if __name__=='__main__': unittest.main()
