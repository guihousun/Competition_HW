"""Opportunity rotation is a dispatch rule, not a success or fitness claim."""
from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch

from test_maintenance_supply_v2 import scene
from test_maintenance_integration_v2 import role,unit
from agent import brain,planner,simulator,maintenance_selection,team_trip

FLAG='COMPETITION_HW_MAINTENANCE_V2'


def both_scene():
    p=scene();p['teamOur']['goldNum']=400
    p['weaponShopList'] += [dict(name='WeaponUpgradeVoucher1',price=100),dict(name='WeaponUpgradeVoucher2',price=150)]
    for uid in (510,511):role(p,uid).update(level=2,health=1500)
    p['mapInfo']['zones']=[z for z in p['mapInfo']['zones'] if z['pos']!=dict(x=13,y=11)]
    p['teamOur']['roles'].append(unit(612,'wall',13,11,hp=550))
    return p


def proposal(kind,available=True,phase='buy'):
    action=(731,dict(action='buy',name='WallFixer' if kind=='repair' else 'WeaponUpgradeVoucher1',num=1)) if available else None
    return action,dict(phase=phase,route={'remaining_actions':8 if kind=='repair' else 2})


class PureSelectionTests(unittest.TestCase):
    def test_all_availability_and_previous_dispatch_combinations(self):
        for previous in (None,'repair','upgrade'):
            memory={} if previous is None else {'operation':previous,'round':7}
            for up,repair in ((False,False),(True,False),(False,True),(True,True)):
                upgrade=proposal('upgrade',up);maintenance=proposal('repair',repair)
                original=deepcopy((upgrade,maintenance,memory))
                chosen,report=maintenance_selection.choose(upgrade,maintenance,memory,8)
                expected=('upgrade' if previous=='repair' else 'repair') if up and repair else ('upgrade' if up else 'repair' if repair else None)
                self.assertEqual(report['selected'],expected)
                self.assertEqual(chosen,maintenance if expected=='repair' else upgrade)
                self.assertEqual((upgrade,maintenance,memory),original)

    def test_funded_delivery_is_protected_and_repair_need_not_be_evaluated(self):
        for previous in ('repair','upgrade'):
            chosen,report=maintenance_selection.choose(proposal('upgrade',phase='return_with_voucher'),None,
                                                       {'operation':previous,'round':7},8)
            self.assertEqual(report['selected'],'upgrade')
            self.assertIsNone(report['available']['repair'])
            self.assertEqual(report['reason'],'carried_upgrade_delivery')

    def test_invalid_and_future_memory_never_selects_as_a_real_opportunity(self):
        for value in (None,[],{}, {'operation':'repair','round':True},{'operation':'repair','round':9},
                      {'operation':'repair','round':0},{'operation':'other','round':7}, {'operation':'repair','round':'7'}):
            chosen,report=maintenance_selection.choose(proposal('upgrade'),proposal('repair'),value,8)
            self.assertEqual(report['selected'],'repair')
            self.assertIsNone(report['previous_dispatch'])

    def test_only_a_final_new_issued_record_counts_legacy_upgrade_too(self):
        events=[dict(kind='purchase',event='issued',owner=731)]
        record={'purchase':dict(owner=731,issued_round=8)}
        self.assertEqual(maintenance_selection.new_dispatch(events,record,8),dict(operation='upgrade',round=8))
        for invalid_events,invalid_record in [([],record),(events,{}),
                ([dict(kind='purchase',event='defer',owner=731)],record),
                (events,{'purchase':dict(owner=732,issued_round=8)}),
                (events,{'purchase':dict(owner=731,issued_round=7)})]:
            self.assertIsNone(maintenance_selection.new_dispatch(invalid_events,invalid_record,8))


class SelectionIntegrationTests(unittest.TestCase):
    def setUp(self):
        env=patch.dict(os.environ,{FLAG:'on'});env.start();self.addCleanup(env.stop)
        planner.reset();self.addCleanup(planner.reset)

    def test_real_buy_use_return_then_upgrade_then_repair_with_both_feasible(self):
        p=both_scene();dispatches=[];memory_by_round={};snapshots={}
        for _ in range(12):
            n=p['roundNo'];reply=brain.respond(deepcopy(p));state=planner.state_for(p)
            self.assertTrue(set(reply)<={'roleCommandMap','prompt','executeCmd'})
            report=deepcopy(brain.decision_report()['upgrade_itinerary'])
            memory_by_round[n]=deepcopy(state.purchase_selection)
            if any(e.get('kind')=='purchase' and e.get('event')=='issued' for e in report['team_trip_events']):
                dispatches.append((n,reply['roleCommandMap']['731']['name']))
                snapshots[n]=report
            p=simulator.step(p,external_response=reply)['state']
            self.assertTrue(all(value is True for value in p['lastRoundRoleActionResults'].values()))
        self.assertEqual(dispatches,[(1,'WallFixer'),(9,'WeaponUpgradeVoucher1'),(11,'WallFixer')])
        for n in (1,9,11):self.assertEqual(snapshots[n]['purchase_selection']['available'],dict(upgrade=True,repair=True))
        self.assertEqual(snapshots[9]['purchase_selection']['selected'],'upgrade')
        self.assertEqual(snapshots[11]['purchase_selection']['selected'],'repair')
        self.assertEqual([e['event'] for e in snapshots[9]['team_trip_events']],['cancel','issued'])
        self.assertTrue(all(memory_by_round[n]==dict(operation='repair',round=1) for n in range(1,9)))
        self.assertEqual(memory_by_round[10],dict(operation='upgrade',round=9))
        self.assertEqual(role(p,611)['health'],1000)
        self.assertEqual(role(p,412)['level'],2)

    def test_failed_first_action_records_opportunity_once_not_success(self):
        p=both_scene();reply=brain.respond(deepcopy(p));state=planner.state_for(p)
        self.assertEqual(reply['roleCommandMap']['731']['name'],'WallFixer')
        self.assertEqual(state.purchase_selection,dict(operation='repair',round=1))
        # Public failure fixture: no inventory/gold is fabricated as success.
        p['roundNo']=2;p['lastRoundRoleActionResults']={'731':False}
        brain.respond(deepcopy(p));state=planner.state_for(p)
        self.assertEqual(state.purchase_selection,dict(operation='repair',round=1))
        self.assertEqual(state.team_trips['purchase']['issued_round'],1)
        self.assertNotIn('purchase_selection',brain.decision_report()['upgrade_itinerary'])

    def test_existing_purchase_not_overridden_and_funded_voucher_still_first(self):
        p=both_scene();m=planner.PlannerState();m.note_round(1)
        m.purchase_selection=dict(operation='upgrade',round=1)
        role(p,731)['backpack']=['WeaponUpgradeVoucher1']
        with patch.object(brain.maintenance_supply,'plan',side_effect=AssertionError('do not evaluate a rival')):
            response=brain.plan_for_state(p,m,judge_tasks=False)
        self.assertEqual(response.commands['731']['action'],'use')
        self.assertEqual(response.commands['731']['name'],'WeaponUpgradeVoucher1')
        self.assertEqual(m.purchase_selection,dict(operation='upgrade',round=1))
        p=both_scene();planner.reset();brain.respond(deepcopy(p))
        state=planner.state_for(p);state.purchase_selection=dict(operation='repair',round=1)
        p['roundNo']=2;role(p,731)['backpack']=['WallFixer'];p['teamOur']['goldNum']=390
        with patch.object(maintenance_selection,'choose',side_effect=AssertionError('existing trip is not a new opportunity')):
            reply=brain.respond(deepcopy(p))
        self.assertEqual(reply['roleCommandMap']['731']['action'],'move')
        self.assertEqual(planner.state_for(p).team_trips['purchase']['operation'],'repair')

    def test_final_reconcile_and_route_rejection_do_not_advance_history(self):
        for failure in ('reconcile','proof'):
            p=both_scene();p['roundNo']=20;m=planner.PlannerState();m.note_round(20)
            m.purchase_selection=dict(operation='upgrade',round=7);before=deepcopy(m.purchase_selection)
            if failure=='reconcile':
                original=brain.reconcile
                def drop(*args,**kwargs):
                    result=original(*args,**kwargs);result.pop(731,None);return result
                test_patch=patch.object(brain,'reconcile',side_effect=drop)
            else:
                test_patch=patch.object(team_trip.TripFrame,'prospective_new',return_value=team_trip.TripCost(False,9,8,'test_final_route_changed'))
            with test_patch:reply=brain.plan_for_state(p,m,judge_tasks=False)
            self.assertNotIn('731',reply.commands)
            self.assertNotIn('purchase',m.team_trips)
            self.assertEqual(m.purchase_selection,before)
            report=brain.decision_report()['upgrade_itinerary']['purchase_selection']
            self.assertEqual(report['scope'],'proposal_choice_not_dispatch_or_success')

    def test_cross_day_roundtrip_new_match_and_bad_transport(self):
        m=planner.PlannerState();m.note_round(69);m.purchase_selection=dict(operation='repair',round=20)
        m.note_round(71);m.note_round(131)
        restored=planner.PlannerState.load(json.loads(json.dumps(m.dump())))
        self.assertEqual(restored.purchase_selection,dict(operation='repair',round=20))
        restored.note_round(1);self.assertEqual(restored.purchase_selection,{})
        for value in ({'operation':'repair','round':132},{'operation':'repair','round':True},
                      {'operation':'unknown','round':20}):
            dump=m.dump();dump['purchaseSelection']=value
            restored=planner.PlannerState.load(dump);self.assertEqual(restored.purchase_selection,{})
            restored.note_round(200);self.assertEqual(restored.purchase_selection,{})

    def test_future_in_memory_is_dropped_not_reactivated_later(self):
        p=both_scene();p['roundNo']=20;p['teamOur']['goldNum']=0
        m=planner.PlannerState();m.note_round(20);m.purchase_selection=dict(operation='repair',round=22)
        brain.plan_for_state(p,m,judge_tasks=False)
        self.assertEqual(m.purchase_selection,{})
        p['roundNo']=22;m.note_round(22)
        brain.plan_for_state(p,m,judge_tasks=False);self.assertEqual(m.purchase_selection,{})

    def test_future_night_memory_is_discarded_before_later_day_can_reach_it(self):
        p=both_scene();p['roundNo']=80;m=planner.PlannerState();m.note_round(80)
        m.purchase_selection=dict(operation='repair',round=100)
        brain.plan_for_state(p,m,judge_tasks=False)
        self.assertEqual(m.purchase_selection,{})
        # Both proposals are still feasible next dawn; stale round100 must not
        # masquerade as a repair dispatch and select upgrade at round131.
        p['roundNo']=131;m.note_round(131)
        reply=brain.plan_for_state(p,m,judge_tasks=False)
        self.assertEqual(reply.commands['731']['name'],'WallFixer')
        selection=brain.decision_report()['upgrade_itinerary']['purchase_selection']
        self.assertIsNone(selection['previous_dispatch'])
        self.assertEqual(selection['reason'],'initial_repair_opportunity')
        self.assertEqual(m.purchase_selection,dict(operation='repair',round=131))

    def test_night_preview_sanitizes_only_detached_future_memory(self):
        p=both_scene();p['roundNo']=80;m=planner.PlannerState();m.note_round(80)
        m.purchase_selection=dict(operation='repair',round=100)
        before=deepcopy(m.dump())
        brain.plan_for_state(p,m,commit=False,judge_tasks=False)
        self.assertEqual(m.dump(),before)
        self.assertEqual(m.purchase_selection,dict(operation='repair',round=100))

    def test_preview_in_both_modes_and_explicit_off_commit(self):
        for enabled in ('on','off'):
            p=both_scene();p['roundNo']=20;m=planner.PlannerState();m.note_round(20)
            m.purchase_selection=dict(operation='repair',round=7)
            with patch.dict(os.environ,{FLAG:enabled}):
                before=deepcopy(m.dump())
                brain.plan_for_state(p,m,commit=False,judge_tasks=False)
                self.assertEqual(m.dump(),before)
        with patch.dict(os.environ,{FLAG:'off'}):
            brain.plan_for_state(p,m,judge_tasks=False)
            self.assertEqual(m.purchase_selection,{})
            self.assertNotIn('purchaseSelection',m.dump())
            self.assertNotIn('purchase_selection',brain.decision_report()['upgrade_itinerary'])

    def test_default_off_never_calls_selector_or_adds_empty_dump_field(self):
        p=both_scene();m=planner.PlannerState()
        with patch.dict(os.environ,{FLAG:'off'}),patch.object(maintenance_selection,'choose',side_effect=AssertionError('disabled')):
            brain.plan_for_state(p,m,judge_tasks=False)
        self.assertNotIn('purchaseSelection',m.dump())
        self.assertNotIn('purchase_selection',brain.decision_report()['upgrade_itinerary'])


if __name__=='__main__':unittest.main()
