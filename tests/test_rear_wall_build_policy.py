"""Hand-authored rear-row policy tests; not official construction-zone claims."""
import json
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'Demo/CoreGeek/src'))
from agent import brain,construction_trip,coordination,frontline,planner,strategy_config,team_trip
from agent.protocol import Pos,Turn,distance


def unit(uid,kind,x,y,*,bag=(),level=1,health=1000):
    return {'id':uid,'roleType':kind,'pos':{'x':x,'y':y},'level':level,'health':health,
            'backPackCapability':100,'backpack':list(bag)}


def board(x=9,y=22,round_no=12):
    return {'roundNo':round_no,'mapInfo':{'width':41,'height':32,'zones':[]},
        'teamOur':{'type':'challenger','goldNum':0,'roles':[unit(1,'station',x,y,health=1500),
            unit(2,'worker',x+2,y-1,bag=['stone']*12,health=220),
            unit(3,'rocket',x-1,y-1),unit(4,'rocket',x-1,y+1),unit(5,'rocket',x,y+1)],'playerTasks':[]},
        'teamEnemy':{'roles':[]},'robot':{'roles':[]},'phaseTask':'','llmResp':'','lastCmdResult':'','errors':[],
        'vendorShopList':[],'weaponShopList':[]}


def add_walls(payload,positions):
    payload['teamOur']['roles'] += [unit(100+i,'wall',p.x,p.y) for i,p in enumerate(positions)]


def contract(walls,*,phase='work',owner=2):
    return {'owner':owner,'issued_round':10,'last_round':11,'deadline':56,
            'walls':[p.dump() for p in walls],'phase':phase}


class RearWallBuildPolicyTests(unittest.TestCase):
    def setUp(self):
        self.cfg=deepcopy(strategy_config.DEFAULTS)
        self.override=patch.object(strategy_config,'get',return_value=self.cfg)
        self.override.start();self.addCleanup(self.override.stop)
        brain._TRAFFIC.set(None)
        self.addCleanup(brain._TRAFFIC.set,None)

    def test_new_layout_excludes_hand_defined_rear_rows_in_all_directions(self):
        cases=((9,22,{Pos(7,y) for y in range(19,25)},(Pos(12,21),Pos(12,22))),
               (30,22,{Pos(33,y) for y in range(19,25)},(Pos(28,21),Pos(28,22))),
               (19,5,{Pos(x,2) for x in range(17,23)},(Pos(19,7),Pos(20,7))),
               (19,25,{Pos(x,27) for x in range(17,23)},(Pos(19,22),Pos(20,22))))
        for x,y,rear,center in cases:
            with self.subTest(base=(x,y)):
                turn=Turn.load(board(x,y))
                order=brain._wall_order(turn)
                self.assertFalse(set(order)&rear)
                self.assertEqual(set(order[:2]),set(center))
                self.assertEqual(sorted(frontline.wall_tier(turn,p) for p in order),
                                 [frontline.wall_tier(turn,p) for p in order])
                self.assertFalse(turn.walls())

    def test_proven_traffic_opening_stays_open(self):
        turn=Turn.load(board());opening=Pos(11,24)
        brain._TRAFFIC.set(SimpleNamespace(turn=turn,openings=lambda:{opening}))
        self.assertNotIn(opening,brain._wall_order(turn))

    def test_disabled_uses_legacy_order_and_allows_rear_build(self):
        p=board();turn=Turn.load(p);legacy=brain._defence_layout(turn).wall_order
        self.cfg['enabled']=False
        self.assertEqual(brain._wall_order(turn),legacy)
        rear_target=next(q for q in legacy if q.x==7)
        worker=turn.workers()[0];commands={worker.unit_id:{'action':'build','name':'wall','targetPos':[rear_target.dump()]}}
        self.assertEqual(coordination.reconcile(turn,p,commands),commands)

    def test_raw_build_and_final_arbitration_reject_rear_but_keep_front(self):
        p=board();turn=Turn.load(p);worker=turn.workers()[0]
        commands={};brain._build_or_walk(turn,worker,Pos(7,20),'wall',set(),commands)
        self.assertEqual(commands,{})
        for target,wanted in ((Pos(7,20),False),(Pos(12,21),True)):
            proposed={2:{'action':'build','name':'wall','targetPos':[target.dump()]}}
            self.assertEqual(bool(coordination.reconcile(turn,p,proposed)),wanted)

    def test_existing_rear_is_preserved_and_cannot_mask_missing_front(self):
        p=board();turn=Turn.load(p);needed=set(brain._wall_order(turn));missing=Pos(12,21)
        add_walls(p,(needed-{missing})|{Pos(7,y) for y in range(19,25)})
        turn=Turn.load(p)
        self.assertGreaterEqual(len(turn.walls()),len(brain._wall_order(turn)))
        self.assertIn(missing,brain._missing_wall_sites(turn))
        before=deepcopy(p)
        brain.plan_for_state(deepcopy(p),planner.PlannerState(),commit=False,judge_tasks=False)
        self.assertEqual(p,before,'policy does not delete observed rear walls')
        self.assertEqual(sum(w.pos.x==7 for w in turn.walls()),6)

    def test_all_required_walls_complete_without_rear_row(self):
        p=board();add_walls(p,brain._wall_order(Turn.load(p)))
        self.assertFalse(brain._missing_wall_sites(Turn.load(p)))
        self.assertFalse(set(w.pos for w in Turn.load(p).walls()) & set(frontline.rear_walls(Turn.load(p))))

    def test_stone_sale_reserve_uses_missing_target_cells_not_total_count(self):
        p=board();needed=set(brain._wall_order(Turn.load(p)));missing=Pos(12,21)
        add_walls(p,(needed-{missing})|{Pos(7,y) for y in range(19,25)})
        p['teamOur']['roles'][1]['pos']={'x':5,'y':20}
        p['mapInfo']['zones']=[{'pos':{'x':4,'y':20},'neutralType':'vendor'}]
        p['vendorShopList']=[{'name':'stone','price':1}]
        turn=Turn.load(p);role=turn.workers()[0]
        self.assertFalse(brain._should_sell(turn,role,p),'12stones minus10 reserve is below surplus threshold3')
        commands={};self.assertTrue(brain._try_trade(turn,role,commands,p))
        self.assertEqual(commands[2]['num'],2)
        self.assertFalse(brain._forecast_sale_trip_fits(turn,role))

    def test_worker_day_selects_center_wall_without_building_rear(self):
        p=board();turn=Turn.load(p);role=turn.workers()[0];commands={}
        brain._worker_day(turn,role,brain._tower_sites(turn),[],list(brain._wall_order(turn)),set(),commands,p,set())
        self.assertEqual(commands[2]['action'],'build')
        self.assertIn(Pos.load(commands[2]['targetPos'][0]),{Pos(12,21),Pos(12,22)})

    def test_construction_planner_only_filters_requested_sites_never_adds_others(self):
        p=board();p['teamOur']['roles'][1]['pos']={'x':8,'y':20}
        turn=Turn.load(p);worker=turn.workers()[0]
        rear=Pos(7,20);front=Pos(12,21)
        result=construction_trip.plan(turn,worker,[rear],deadline=55)
        self.assertTrue(result is None or result[0].get('action')!='build')
        if result:self.assertNotIn('planned_walls',result[1])
        result=construction_trip.plan(turn,worker,[rear,front],deadline=55)
        self.assertIsNotNone(result)
        self.assertEqual(result[1].get('planned_walls'),[front.dump()])
        self.cfg['enabled']=False
        result=construction_trip.plan(turn,worker,[rear],deadline=55)
        self.assertIsNotNone(result)
        self.assertEqual(result[0],{'action':'build','targetPos':[rear.dump()],'name':'wall'})

    def test_old_mixed_construction_contract_filters_rear_and_roundtrips(self):
        p=board();turn=Turn.load(p);frame=team_trip.TripFrame(turn,p,{'construction':contract([Pos(7,20),Pos(12,21)])})
        self.assertEqual(frame.construction['walls'],[{'x':12,'y':21}])
        self.assertEqual(frame.construction['phase'],'work')
        restored=team_trip.clean_memory(json.loads(json.dumps(frame.finalize({}))))
        self.assertEqual(restored['construction']['walls'],[{'x':12,'y':21}])
        self.assertTrue(any(e['reason']=='rear_wall_targets_retired' for e in frame.events))

    def test_old_rear_only_contract_keeps_real_return_ownership_until_observed_home(self):
        p=board();p['teamOur']['roles'][1]['pos']={'x':4,'y':20};turn=Turn.load(p)
        frame=team_trip.TripFrame(turn,p,{'construction':contract([Pos(7,20)])})
        self.assertEqual(frame.construction['phase'],'return')
        self.assertEqual(frame.construction['walls'],[])
        memory=team_trip.clean_memory(json.loads(json.dumps(frame.finalize({}))))
        self.assertEqual(memory['construction']['phase'],'return')
        p['roundNo']=13;p['teamOur']['roles'][1]['pos']={'x':8,'y':22}
        next_frame=team_trip.TripFrame(Turn.load(p),p,memory)
        self.assertIsNone(next_frame.construction)
        self.assertTrue(any(e['reason']=='returned_observed' for e in next_frame.events))

    def test_brain_replans_legacy_rear_contract_to_safe_return(self):
        p=board();p['teamOur']['roles'][1]['pos']={'x':4,'y':20}
        memory=planner.PlannerState();memory.team_trips={'construction':contract([Pos(7,20)])}
        response=brain.plan_for_state(deepcopy(p),memory,judge_tasks=False)
        self.assertEqual(memory.team_trips['construction']['phase'],'return')
        self.assertEqual(response.commands['2']['action'],'move')
        self.assertNotEqual(response.commands['2'].get('action'),'build')
        restored=planner.PlannerState.load(json.loads(json.dumps(memory.dump())))
        self.assertEqual(restored.team_trips['construction']['walls'],[])

    def test_old_rear_upgrade_trip_returns_without_spending_or_using_voucher(self):
        p=board();p['teamOur']['roles'][1]['pos']={'x':4,'y':20}
        p['teamOur']['roles'][1]['backpack']=['WallUpgradeVoucher1']
        add_walls(p,[Pos(7,20)])
        row={'owner':2,'issued_round':10,'last_round':11,'deadline':67,'target':100,'level':1,
             'count':1,'item':'WallUpgradeVoucher1','phase':'acquire','last_action':'move'}
        frame=team_trip.TripFrame(Turn.load(p),p,{'purchase':row})
        self.assertEqual(frame.purchase['phase'],'return')
        self.assertTrue(any(e['reason']=='rear_wall_investment_retired' for e in frame.events))
        self.assertEqual(frame.purchase['count'],1)
        self.cfg['enabled']=False
        p['weaponShopList']=[{'name':'WallUpgradeVoucher1','price':20}]
        legacy=team_trip.TripFrame(Turn.load(p),p,{'purchase':row})
        self.assertEqual(legacy.purchase['phase'],'acquire')

    def test_shop_fallback_does_not_buy_for_rear_only_wall(self):
        p=board();p['teamOur']['goldNum']=500
        for u in p['teamOur']['roles']:
            if u['roleType'] in ('station','rocket'):u['level']=3
        p['teamOur']['roles'][1]['pos']={'x':6,'y':20};p['teamOur']['roles'][1]['backpack']=[]
        add_walls(p,[Pos(7,20)])
        p['weaponShopList']=[{'name':'WallUpgradeVoucher1','price':20}]
        turn=Turn.load(p)
        self.assertIsNone(brain._shop_choice(turn,turn.workers()[0],p,{}))
        self.cfg['enabled']=False
        self.assertEqual(brain._shop_choice(turn,turn.workers()[0],p,{}),'WallUpgradeVoucher1')


    def test_wall_targets_stay_on_land_and_in_bounds(self):
        for x,y in ((9,22),(9,1),(1,5),(38,25)):
            p=board(x,y)
            p['mapInfo']['zones']=[{'pos':{'x':12,'y':21},'neutralType':'water'},
                                  {'pos':{'x':11,'y':24},'neutralType':'stone'}]
            turn=Turn.load(p)
            order=brain._wall_order(turn)
            self.assertTrue(all(0<=q.x<41 and 0<=q.y<32 and turn.land(q) for q in order))
            self.assertNotIn(Pos(12,21),order)
            self.assertNotIn(Pos(11,24),order)

    def test_old_closed_rear_cannot_make_front_completion_seal_worker_inside(self):
        p=board();turn=Turn.load(p);gap=Pos(12,21)
        ring=set(brain._defence_layout(turn).ring)
        add_walls(p,ring-{gap});turn=Turn.load(p)
        self.assertIn(gap,brain._missing_wall_sites(turn))
        command={2:{'action':'build','name':'wall','targetPos':[gap.dump()]}}
        self.assertTrue(coordination.wall_build_seals_role(turn,{gap}))
        self.assertEqual(coordination.reconcile(turn,p,command),{})
        planned={};brain._build_or_walk(turn,turn.workers()[0],gap,'wall',set(),planned)
        self.assertEqual(planned,{})
        result=brain.plan_for_state(deepcopy(p),planner.PlannerState(),judge_tasks=False)
        self.assertFalse(any(c.get('action')=='build' and c.get('targetPos')==[gap.dump()]
                             for c in result.commands.values()))
        self.assertFalse(any(c.get('action')=='remove' for c in result.commands.values()))

    def test_same_round_two_wall_builds_cannot_jointly_close_last_exits(self):
        p=board();gaps={Pos(12,21),Pos(12,22)}
        add_walls(p,set(brain._defence_layout(Turn.load(p)).ring)-gaps)
        p['teamOur']['roles'].append(unit(6,'worker',11,22,bag=['stone'],health=220))
        turn=Turn.load(p)
        commands={2:{'action':'build','name':'wall','targetPos':[Pos(12,21).dump()]},
                  6:{'action':'build','name':'wall','targetPos':[Pos(12,22).dump()]}}
        self.assertFalse(coordination.wall_build_seals_role(turn,{Pos(12,21)}))
        result=coordination.reconcile(turn,p,commands)
        self.assertEqual(len(result),1,'one gap must remain structurally open')

    def test_real_brain_first_day_never_builds_or_rebuilds_rear_row(self):
        from agent import scenarios,simulator
        import os
        with patch.dict(os.environ,{brain.TASK_AGENT_ENV:'off',brain.WORLD_AGENT_ENV:'off',brain.ROUTER_ENV:'off'}):
            for side in ('challenger','defender'):
                with self.subTest(side=side):
                    planner.reset()
                    state=scenarios.scenario(1,side,1,profile='observed-seven-days')
                    base=next(u for u in state['teamOur']['roles'] if u['roleType']=='station')['pos']
                    rear_x=base['x']-2 if base['x']+1<20 else base['x']+3
                    rear={Pos(rear_x,y) for y in range(base['y']-3,base['y']+3)}
                    wall_builds=0
                    for _ in range(70):
                        obs=scenarios.observation(state)
                        self.assertNotIn('_demo',obs)
                        response=brain.respond(obs)
                        for c in response['roleCommandMap'].values():
                            if c.get('action')=='build' and c.get('name')=='wall':
                                wall_builds+=1
                                self.assertFalse({Pos.load(t) for t in c['targetPos']} & rear)
                        state=simulator.step(state,external_response=response)['state']
                    self.assertGreater(wall_builds,0,'exercise real construction, not vacuous idle output')
                    self.assertFalse({u.pos for u in Turn.load(state).walls()} & rear)
        planner.reset()


    def exterior_crew_board(self,gaps):
        p=board()
        p['teamOur']['roles'][1]['pos']={'x':13,'y':21}
        p['teamOur']['roles'] += [unit(6,'worker',13,22,bag=['stone'],health=220),
                                 unit(7,'pioneer',14,22,health=200)]
        add_walls(p,set(brain._defence_layout(Turn.load(p)).ring)-set(gaps))
        return p

    def test_all_crew_outside_cannot_close_the_only_return_gap(self):
        gap=Pos(12,21);p=self.exterior_crew_board({gap});turn=Turn.load(p)
        self.assertEqual(len(turn.controllable()),3)
        self.assertTrue(all(r.pos.x>=13 for r in turn.controllable()))
        self.assertTrue(coordination.wall_build_seals_role(turn,{gap}))
        command={2:{'action':'build','name':'wall','targetPos':[gap.dump()]}}
        self.assertEqual(coordination.reconcile(turn,p,command),{})
        direct={};brain._build_or_walk(turn,turn.workers()[0],gap,'wall',set(),direct)
        self.assertEqual(direct,{})

    def test_all_crew_outside_same_round_builds_cannot_jointly_close_return(self):
        first,second=Pos(12,21),Pos(12,22)
        p=self.exterior_crew_board({first,second});turn=Turn.load(p)
        self.assertFalse(coordination.wall_build_seals_role(turn,{first}))
        self.assertFalse(coordination.wall_build_seals_role(turn,{second}))
        self.assertTrue(coordination.wall_build_seals_role(turn,{second},existing_additions={first}))
        commands={2:{'action':'build','name':'wall','targetPos':[first.dump()]},
                  6:{'action':'build','name':'wall','targetPos':[second.dump()]}}
        self.assertEqual(len(coordination.reconcile(turn,p,commands)),1)

    def test_preexisting_inaccessible_inner_is_not_claimed_as_new_build_damage(self):
        p=self.exterior_crew_board(set());turn=Turn.load(p)
        self.assertFalse(coordination.wall_build_seals_role(turn,{Pos(15,21)}))

    def test_open_rear_keeps_return_access_for_all_exterior_roles(self):
        p=board();p['teamOur']['roles'][1]['pos']={'x':13,'y':21}
        p['teamOur']['roles'] += [unit(6,'worker',13,22,bag=['stone'],health=220),
                                 unit(7,'pioneer',14,22,health=200)]
        turn=Turn.load(p);front=Pos(12,21)
        self.assertFalse(coordination.wall_build_seals_role(turn,{front}))
        command={2:{'action':'build','name':'wall','targetPos':[front.dump()]}}
        self.assertEqual(coordination.reconcile(turn,p,command),command)


if __name__=='__main__':unittest.main()
