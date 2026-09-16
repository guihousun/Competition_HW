"""Explicit repair contracts, with independently hand-counted route/receipts."""
from copy import deepcopy
import json
import unittest

from test_team_trip import purchase as legacy_purchase
from test_team_trip_return import corridor
from agent import team_trip, upgrade_itinerary
from agent.protocol import Turn, Pos


def scene(round_no=63, *, held=True, inside=False):
    p,c=corridor(round_no,inside=inside)
    worker=p['teamOur']['roles'][-1]
    worker['backpack']=['WallFixer'] if held else []
    p['teamOur']['roles'][2]['health']=500
    p['teamOur']['goldNum']=10
    p['weaponShopList']=[dict(name='WallFixer',price=10),dict(name='Medicine',price=10)]
    p['mapInfo']['zones']=[z for z in p['mapInfo']['zones'] if z['pos']!=dict(x=6,y=10)]
    p['mapInfo']['zones'].append(dict(neutralType='weaponShop',pos=dict(x=6,y=10)))
    c.update(item='WallFixer',count=int(held),operation='repair',target_health=500,
             target_pos=dict(x=8,y=10),last_action='move')
    return p,c


def proposal(frame):
    action,report=upgrade_itinerary.plan(frame.turn,frame.payload,{},commitment=frame.purchase)
    report.update(operation='repair',item='WallFixer')
    report.pop('voucher',None)
    return action,report


def fresh(frame,c,command):
    frame.stage_purchase(dict(operation='repair',item='WallFixer',building=c['target']),
                         (c['owner'],command))


class TeamTripRepairTests(unittest.TestCase):
    def test_legacy_upgrade_json_bytes_remain_unchanged(self):
        expected='{"purchase":{"owner":20012,"issued_round":164,"last_round":178,"deadline":198,"target":20041,"level":1,"count":0,"item":"WeaponUpgradeVoucher1","phase":"acquire","last_action":"move"}}'
        result=team_trip.clean_memory({'purchase':legacy_purchase()})
        self.assertEqual(json.dumps(result,separators=(',',':')),expected)
        self.assertEqual(team_trip.clean_memory({}),{})
        self.assertNotIn('operation',result['purchase'])

    def test_repair_is_explicit_and_only_a_damaged_live_wall_is_eligible(self):
        p,c=scene()
        self.assertTrue(team_trip.evaluate_trip(Turn.load(p),p,c).feasible)
        for change in ({'operation':'unknown'},{'item':'WallUpgradeVoucher1'}, {'operation':'upgrade'}):
            self.assertFalse(team_trip.evaluate_trip(Turn.load(p),p,dict(c,**change)).feasible)
        old=dict(c);old.pop('operation')
        self.assertFalse(team_trip.evaluate_trip(Turn.load(p),p,old).feasible)
        for kind,hp in (('rocket',500),('station',500),('wall',1000),('wall',0)):
            q=deepcopy(p);q['teamOur']['roles'][2].update(roleType=kind,health=hp)
            self.assertFalse(team_trip.evaluate_trip(Turn.load(q),q,c).feasible)

    def test_complete_cost_includes_purchase_use_and_real_four_step_return(self):
        p,c=scene(62,held=False)
        cost=team_trip.evaluate_trip(Turn.load(p),p,c)
        self.assertEqual((cost.actions,cost.budget,cost.command),(6,6,dict(action='buy',name='WallFixer',num=1)))
        p['roundNo']=63
        self.assertFalse(team_trip.evaluate_trip(Turn.load(p),p,c).feasible)
        p,c=scene(63)
        self.assertEqual(team_trip.evaluate_trip(Turn.load(p),p,c).actions,5)
        p,c=scene(67,inside=True)
        cost=team_trip.evaluate_trip(Turn.load(p),p,c)
        self.assertEqual((cost.actions,cost.budget),(1,1))

    def test_final_purchase_and_use_do_not_persist_shadow_success(self):
        p,c=scene(62,held=False);before=deepcopy(p);f=team_trip.TripFrame(Turn.load(p),p,{})
        command=dict(action='buy',name='WallFixer',num=1)
        fresh(f,c,command)
        self.assertEqual(f.finalize({}),{})
        memory=f.finalize({731:command})
        self.assertEqual((memory['purchase']['count'],memory['purchase']['phase']),(0,'acquire'))
        self.assertEqual(memory['purchase']['target_health'],500)
        self.assertNotIn('use_round',memory['purchase'])
        self.assertEqual(p,before)
        p['roundNo']=63;p['teamOur']['roles'][-1]['backpack']=['WallFixer']
        f=team_trip.TripFrame(Turn.load(p),p,memory)
        action,report=proposal(f);self.assertEqual(action[1]['action'],'use')
        f.stage_purchase(report,action);memory=f.finalize(dict([action]))
        self.assertEqual(memory['purchase']['count'],1)
        self.assertEqual(memory['purchase']['phase'],'acquire')
        self.assertEqual(memory['purchase']['use_round'],63)

    def test_insufficient_shared_gold_capacity_missing_quote_and_wrong_buy_rejected(self):
        for mode in ('gold','other_spending','capacity','quote','wrong_item','two_kits','already_held','not_adjacent'):
            p,c=scene(62,held=False);commands={}
            command=dict(action='buy',name='WallFixer',num=1)
            if mode=='gold':p['teamOur']['goldNum']=9
            if mode=='other_spending':commands[999]=dict(action='buy',name='Medicine',num=1)
            if mode=='capacity':p['teamOur']['roles'][-1]['backpack']=['stone']*100
            if mode=='quote':p['weaponShopList']=[]
            if mode=='wrong_item':command['name']='Medicine'
            if mode=='two_kits':command['num']=2
            if mode=='already_held':p['teamOur']['roles'][-1]['backpack']=['WallFixer']
            if mode=='not_adjacent':p['teamOur']['roles'][-1]['pos']=dict(x=8,y=12)
            f=team_trip.TripFrame(Turn.load(p),p,{})
            fresh(f,c,command);commands[731]=command
            with self.subTest(mode=mode):self.assertEqual(f.finalize(commands),{})

    def dispatched_use(self):
        p,c=scene(63);f=team_trip.TripFrame(Turn.load(p),p,{'purchase':c})
        action,report=proposal(f);f.stage_purchase(report,action)
        return p,f.finalize(dict([action]))

    def test_consumed_confirmed_use_returns_even_if_simultaneous_damage_masks_heal(self):
        p,memory=self.dispatched_use()
        p['roundNo']=64;p['teamOur']['roles'][-1]['backpack']=[]
        p['teamOur']['roles'][2]['health']=450
        p['lastRoundRoleActionResults']={'731':True}
        f=team_trip.TripFrame(Turn.load(p),p,memory)
        self.assertEqual(f.purchase['phase'],'return')
        self.assertEqual(f.events[0]['reason'],'repair_confirmed_observed')
        action,report=proposal(f)
        self.assertEqual(action[1],dict(action='move',targetPos=[dict(x=7,y=11)]))
        self.assertNotIn(action[1]['action'],('buy','use'))

    def test_consumption_without_reliable_receipt_or_wrong_round_never_confirms(self):
        for receipt in (False,None,'true'):
            p,memory=self.dispatched_use();p['roundNo']=64
            p['teamOur']['roles'][-1]['backpack']=[];p['teamOur']['roles'][2]['health']=1000
            p['lastRoundRoleActionResults']={'731':receipt}
            f=team_trip.TripFrame(Turn.load(p),p,memory)
            self.assertEqual(f.purchase['phase'],'return')
            self.assertEqual(f.events[0]['reason'],'repair_consumed_unconfirmed')
        p,memory=self.dispatched_use();p['roundNo']=64
        memory['purchase']['use_round']=61
        p['teamOur']['roles'][-1]['backpack']=[];p['lastRoundRoleActionResults']={'731':True}
        f=team_trip.TripFrame(Turn.load(p),p,memory)
        self.assertEqual(f.events[0]['reason'],'repair_consumed_unconfirmed')

    def test_contradictory_success_with_unchanged_inventory_returns_unknown(self):
        p,memory=self.dispatched_use();p['roundNo']=64
        p['lastRoundRoleActionResults']={'731':True};p['teamOur']['roles'][2]['health']=900
        f=team_trip.TripFrame(Turn.load(p),p,memory)
        self.assertEqual(f.events[0]['reason'],'repair_use_unconfirmed')
        self.assertEqual(f.purchase['phase'],'return')
        self.assertEqual(p['teamOur']['roles'][-1]['backpack'],['WallFixer'])

    def test_failed_use_does_not_consume_or_confirm_and_repeat_frame_is_not_success(self):
        p,memory=self.dispatched_use()
        same=team_trip.TripFrame(Turn.load(p),p,memory)
        self.assertEqual(same.purchase['phase'],'acquire')
        self.assertFalse(any('confirmed' in e.get('reason','') for e in same.events))
        p['roundNo']=64;p['lastRoundRoleActionResults']={'731':False}
        # Failure consumed a turn, so the original four-step return plus use
        # no longer fits. This is a budget return, not a successful repair.
        f=team_trip.TripFrame(Turn.load(p),p,memory)
        self.assertEqual(f.purchase['phase'],'return')
        self.assertEqual(f.events[0]['event'],'repair_use_failed_observed')
        self.assertEqual(p['teamOur']['roles'][-1]['backpack'],['WallFixer'])

    def test_explicit_failed_use_retries_only_while_whole_return_still_fits(self):
        p,c=scene(62);f=team_trip.TripFrame(Turn.load(p),p,{'purchase':c})
        action,report=proposal(f);f.stage_purchase(report,action);memory=f.finalize(dict([action]))
        p['roundNo']=63;p['lastRoundRoleActionResults']={'731':False}
        f=team_trip.TripFrame(Turn.load(p),p,memory)
        self.assertEqual(f.purchase['phase'],'acquire')
        self.assertEqual(proposal(f)[0][1]['action'],'use')
        self.assertEqual(p['teamOur']['roles'][-1]['backpack'],['WallFixer'])

    def test_task_point_second_cell_blocks_repair_return_for_both_owners(self):
        for kind in ('challengerTaskPoint2','defenderTaskPoint2'):
            p,c=scene()
            p['mapInfo']['zones']=[z for z in p['mapInfo']['zones'] if z['pos']!=dict(x=6,y=11)]
            p['mapInfo']['zones'].append(dict(neutralType=kind,pos=dict(x=6,y=11)))
            self.assertFalse(team_trip.evaluate_trip(Turn.load(p),p,c).feasible)

    def test_active_repair_uses_same_construction_route_guard_after_fixed_use(self):
        p,c=scene();t=Turn.load(p)
        command=dict(action='use',name='WallFixer',targetPos=[dict(x=8,y=10)])
        guard=team_trip.RouteGuard(t,p,c,{731:command})
        self.assertEqual((guard.before.actions,guard.before.budget),(4,4))
        # Hypothetical added obstacle closes the sole return gateway; this is a
        # route-layer test, not a claim the base's build ring permits this wall.
        self.assertFalse(guard.check(999,Pos(8,12),(Pos(7,11),))[0])
        self.assertEqual(guard.report()['time_slice'],'after_fixed_action')

    def test_return_ownership_until_real_post_with_serialization_and_mirror(self):
        for reflected in (False,True):
            p,memory=self.dispatched_use();p['roundNo']=64
            p['teamOur']['roles'][-1]['backpack']=[];p['lastRoundRoleActionResults']={'731':True}
            if reflected:
                p['teamOur']['type']='defender'
                for u in p['teamOur']['roles']:
                    u['id']+=3000;u['pos']['x']=40-u['pos']['x']-int(u['roleType']=='station')
                for z in p['mapInfo']['zones']:z['pos']['x']=40-z['pos']['x']
                memory['purchase']['owner']+=3000;memory['purchase']['target']+=3000
                memory['purchase']['target_pos']['x']=32
                p['lastRoundRoleActionResults']={'3731':True}
            owner=3731 if reflected else 731
            expected=[(7,11),(8,12),(9,11),(9,10)]
            for x,y in expected:
                f=team_trip.TripFrame(Turn.load(p),p,json.loads(json.dumps(memory)))
                self.assertEqual(f.purchase['owner'],owner)
                action,report=proposal(f)
                target=dict(x=40-x if reflected else x,y=y)
                self.assertEqual(action[1],dict(action='move',targetPos=[target]))
                f.stage_purchase(report,action);memory=f.finalize(dict([action]))
                p['teamOur']['roles'][-1]['pos']=target;p['roundNo']+=1
            f=team_trip.TripFrame(Turn.load(p),p,memory)
            self.assertIsNone(f.purchase)
            self.assertEqual(f.events[-1]['reason'],'returned_observed')

    def test_changed_target_destroyed_owner_gap_night_and_blocked_return(self):
        for mode in ('target','owner','gap','night','blocked'):
            p,memory=self.dispatched_use();p['roundNo']=64
            if mode=='target':p['teamOur']['roles'][2]['health']=0
            if mode=='owner':p['teamOur']['roles'][-1]['health']=0
            if mode=='gap':p['roundNo']=66
            if mode=='night':p['roundNo']=71
            if mode=='blocked':
                p['teamOur']['roles'][-1]['backpack']=[]
                p['mapInfo']['zones'].append(dict(neutralType='stone',pos=dict(x=7,y=11)))
            f=team_trip.TripFrame(Turn.load(p),p,memory)
            if mode in ('owner','gap','night'):self.assertIsNone(f.purchase)
            else:
                self.assertEqual(f.purchase['phase'],'return')
                action,_=proposal(f)
                if mode=='blocked':self.assertIsNone(action)

    def test_repair_proposal_does_not_replace_existing_upgrade_or_constructor(self):
        p,c=scene();command=dict(action='use',name='WallFixer',targetPos=[dict(x=8,y=10)])
        f=team_trip.TripFrame(Turn.load(p),p,{})
        old=dict(c,item='WallUpgradeVoucher1');old.pop('operation')
        f.memory['purchase']=old
        fresh(f,c,command);self.assertNotIn('purchase',f.pending)
        f.memory={'construction':dict(owner=731)}
        fresh(f,c,command);self.assertNotIn('purchase',f.pending)

    def test_missing_operation_and_wrong_item_target_report_never_guess_repair(self):
        p,c=scene();command=dict(action='use',name='WallFixer',targetPos=[dict(x=8,y=10)])
        reports=[dict(item='WallFixer',building=800),dict(voucher='WallFixer',building=800),
                 dict(operation='repair',item='Medicine',building=800),
                 dict(operation='repair',item='WallFixer',voucher='Medicine',building=800)]
        for report in reports:
            f=team_trip.TripFrame(Turn.load(p),p,{})
            f.stage_purchase(report,(731,command));self.assertNotIn('purchase',f.pending)
        f=team_trip.TripFrame(Turn.load(p),p,{})
        fresh(f,c,dict(command,targetPos=[dict(x=9,y=10)]))
        self.assertNotIn('purchase',f.pending)

    def test_repair_metadata_sanitized_and_legacy_upgrade_not_extended(self):
        p,c=scene();clean=team_trip.clean_memory({'purchase':c})
        self.assertEqual(clean['purchase']['operation'],'repair')
        self.assertEqual(clean['purchase']['target_pos'],dict(x=8,y=10))
        for change in ({'operation':'unknown'},{'item':'Medicine'},{'target_health':True},
                       {'target_pos':dict(x=41,y=10)},{'use_round':True},{'use_round':100}):
            self.assertEqual(team_trip.clean_memory({'purchase':dict(c,**change)}),{})

    def test_final_other_landing_and_wait_consume_exact_one_round(self):
        p,c=scene(63);before=deepcopy(p)
        command=dict(action='use',name='WallFixer',targetPos=[dict(x=8,y=10)])
        cost=team_trip.evaluate_after_action(Turn.load(p),p,c,command,{731:command})
        self.assertEqual((cost.actions,cost.budget),(4,4))
        wait=team_trip.evaluate_after_action(Turn.load(p),p,c,None,{})
        self.assertEqual((wait.actions,wait.budget,wait.feasible),(5,4,False))
        conflict=team_trip.evaluate_after_action(Turn.load(p),p,c,command,
            {731:command,999:dict(action='move',targetPos=[dict(x=7,y=10)])})
        self.assertEqual(conflict.reason,'first_action_conflict')
        self.assertEqual(p,before)


if __name__=='__main__':unittest.main()
