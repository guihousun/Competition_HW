"""Hand-counted single repair purchase/use/return, not inferred future income."""
from copy import deepcopy
import os
import unittest
from unittest.mock import patch

from test_defense_sustain_v2 import board,role,unit
from agent import defense_sustain,maintenance_supply,team_trip,simulator
from agent.protocol import Turn,WALL_FIXER


def scene(*,held=False):
    p=board(bag=(WALL_FIXER,) if held else ())
    p['roundNo']=1;p['teamOur']['goldNum']=10
    p['weaponShopList']=[{'name':WALL_FIXER,'price':10}]
    additions=[unit(510,'rocket',10,8),unit(511,'railgun',12,8)]
    p['teamOur']['roles']+=additions
    clear={(8,10),(10,8),(12,8)}
    p['mapInfo']['zones']=[z for z in p['mapInfo']['zones'] if (z['pos']['x'],z['pos']['y']) not in clear]
    p['mapInfo']['zones'].append({'neutralType':'weaponShop','pos':{'x':8,'y':10}})
    return p


class MaintenanceSupplyV2Tests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{defense_sustain.ENV:'on'});self.env.start();self.addCleanup(self.env.stop)

    def plan(self,p,**kw):
        return maintenance_supply.plan(Turn.load(p),p,kw.pop('commands',{}),**kw)

    def test_current_one_kit_buy_three_moves_use_three_return(self):
        p=scene();memory={};actions=[]
        for _ in range(10):
            t=Turn.load(p);frame=team_trip.TripFrame(t,p,memory,purchase_deadline=55)
            proposal,report=self.plan(p,commitment=frame.purchase)
            frame.stage_purchase(report,proposal)
            commands={} if proposal is None else {proposal[0]:proposal[1]}
            frame.protect_final(commands);memory=frame.finalize(commands)
            if not commands:break
            self.assertEqual(set(commands),{731})
            command=commands[731];actions.append(command['action'])
            if len(actions)==1:
                self.assertEqual(command,{'action':'buy','name':WALL_FIXER,'num':1})
                self.assertEqual(report['route']['remaining_actions'],8)
                self.assertEqual(memory['purchase']['operation'],'repair')
            p=simulator.step(p,external_response={'roleCommandMap':{str(k):v for k,v in commands.items()}})['state']
            self.assertTrue(p['lastRoundRoleActionResults']['731'])
        self.assertEqual(actions,['buy']+['move']*3+['use']+['move']*3)
        self.assertEqual(p['teamOur']['goldNum'],0)
        self.assertEqual(role(p,611)['health'],1000)
        self.assertEqual(role(p,731)['pos'],{'x':9,'y':10})
        self.assertNotIn('purchase',memory)

    def test_exact_budget_includes_full_return(self):
        p=scene();p['roundNo']=48
        proposal,report=self.plan(p)
        self.assertEqual(report['route']['remaining_actions'],8);self.assertIsNotNone(proposal)
        p['roundNo']=49
        self.assertIsNone(self.plan(p)[0])  # buy+out+use fits, return does not

    def test_reflected_team_and_dynamic_owner_keep_complete_chain(self):
        p=scene();p['teamOur']['type']='defender'
        for u in p['teamOur']['roles']:
            u['pos']['x']=40-u['pos']['x']-int(u['roleType']=='station')
            if u['id']==731:u['id']=880731
        for z in p['mapInfo']['zones']:z['pos']['x']=40-z['pos']['x']
        memory={};actions=[]
        for _ in range(10):
            frame=team_trip.TripFrame(Turn.load(p),p,memory,purchase_deadline=55)
            proposal,report=self.plan(p,commitment=frame.purchase)
            frame.stage_purchase(report,proposal)
            commands={} if proposal is None else {proposal[0]:proposal[1]}
            frame.protect_final(commands);memory=frame.finalize(commands)
            if not commands:break
            self.assertEqual(set(commands),{880731});actions.append(commands[880731]['action'])
            p=simulator.step(p,external_response={'roleCommandMap':{str(k):v for k,v in commands.items()}})['state']
            self.assertTrue(p['lastRoundRoleActionResults']['880731'])
        self.assertEqual(actions,['buy']+['move']*3+['use']+['move']*3)
        self.assertEqual(role(p,880731)['pos'],{'x':31,'y':10})
        self.assertNotIn('purchase',memory)

    def test_held_kit_skips_purchase_and_does_not_require_cash(self):
        p=scene(held=True);p['teamOur']['goldNum']=0;p['weaponShopList']=[]
        proposal,report=self.plan(p)
        self.assertEqual(proposal[1]['action'],'move')
        self.assertEqual(report['route']['remaining_actions'],7)
        self.assertEqual(report['quantity'],0)

    def test_healthy_rear_or_unbuilt_guns_never_speculatively_stock(self):
        for change in ('healthy','rear','guns'):
            p=scene()
            if change=='healthy':role(p,611)['health']=701
            elif change=='rear':role(p,611)['pos']={'x':8,'y':10}
            else:p['teamOur']['roles']=[u for u in p['teamOur']['roles'] if u['id']!=510]
            self.assertIsNone(self.plan(p)[0],change)

    def test_real_price_cash_capacity_and_final_team_spend(self):
        for change in ('cash','missing_quote','full','other_spend'):
            p=scene();commands={}
            if change=='cash':p['teamOur']['goldNum']=9
            elif change=='missing_quote':p['weaponShopList']=[]
            elif change=='full':role(p,731)['backpack']=['stone']*100
            else:commands={9081:{'action':'buy','name':WALL_FIXER,'num':1}}
            self.assertIsNone(self.plan(p,commands=commands)[0],change)

    def test_owned_kit_is_not_duplicated_when_holder_reserved(self):
        p=scene(held=True)
        proposal,report=self.plan(p,reserved_workers={731})
        self.assertIsNone(proposal);self.assertEqual(report['reason'],'holders_reserved')

    def test_upgrade_owner_and_construction_reservations_are_not_stolen(self):
        p=scene()
        self.assertIsNone(self.plan(p,commitment={'owner':731,'target':412,'item':'WeaponUpgradeVoucher1'})[0])
        self.assertIsNone(self.plan(p,reserved_workers={731})[0])

    def test_default_off_and_plan_inputs_are_unchanged(self):
        p=scene();before=deepcopy(p);self.plan(p);self.assertEqual(p,before)
        with patch.dict(os.environ,{},clear=True):
            self.assertIsNone(self.plan(p)[0])
        p['roundNo']=80
        self.assertIsNone(self.plan(p)[0])

    def test_current_robot_threat_blocks_new_day_trip(self):
        p=scene();p['robot']['roles']=[unit(902,'smallRobot',4,4,hp=40)]
        self.assertIsNone(self.plan(p)[0])


if __name__=='__main__':unittest.main()
