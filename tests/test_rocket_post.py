"""Independent public layouts for safe common-post reservation."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from test_defense_layout import board, unit, REPORTED, MIRROR
from agent import rocket_post, strategy_config, home_defense, planner, brain, team_trip
from agent.protocol import Turn, Pos, WALL_FIXER


def setup(base=REPORTED, round_no=450):
    blue = base == REPORTED
    guns = [(8,21),(8,23),(9,23)] if blue else [(32,21),(31,23),(32,23)]
    common = (8,22) if blue else (32,22)
    p = board(base=base, round_no=round_no)
    p['teamOur']['roles'] = [unit(1,'station',*base,1500),
        unit(2,'worker',8 if blue else 32,20,220),unit(4,'pioneer',*common,200)]
    p['teamOur']['roles'] += [unit(10+i,'rocket',*g) for i,g in enumerate(guns)]
    return p, Pos(*common)


class RocketPostTests(unittest.TestCase):
    def setUp(self):
        self.config=deepcopy(strategy_config.DEFAULTS)
        patcher=patch.object(strategy_config,'get',return_value=self.config)
        patcher.start();self.addCleanup(patcher.stop)

    def test_idle_common_occupant_vacates_via_day_rear_but_not_night(self):
        for base in (REPORTED,MIRROR):
            p,common=setup(base);turn=Turn.load(p);commands={}
            report=rocket_post.reserve(turn,commands)
            self.assertEqual(report[0]['reason'],'yield_common_rocket_post')
            target=Pos.load(commands[4]['targetPos'][0])
            self.assertFalse(home_defense.inside(turn,target))
            self.assertEqual(max(abs(target.x-common.x),abs(target.y-common.y)),1)
            p['roundNo']=461;commands={}
            report=rocket_post.reserve(Turn.load(p),commands)
            self.assertFalse(commands,'night cannot evict through outside wall ring')
            self.assertEqual(report[0]['reason'],'common_post_cannot_vacate_safely')

    def test_return_route_avoids_other_workers_reserved_common_post(self):
        p,common=setup();p['teamOur']['roles'][2]['pos']=dict(x=7,y=22)
        commands={4:dict(action='move',targetPos=[common.dump()])}
        rocket_post.reserve(Turn.load(p),commands)
        self.assertNotEqual(commands[4]['targetPos'][0],common.dump())

    def test_explicit_use_task_hold_and_other_movement_preserved(self):
        p,common=setup()
        for command in [dict(action='use',name='WallFixer',targetPos=[dict(x=7,y=22)]),
                        dict(action='move',targetPos=[dict(x=7,y=22)])]:
            commands={4:deepcopy(command)}
            rocket_post.reserve(Turn.load(p),commands)
            self.assertEqual(commands,{4:command})
        commands={};rocket_post.reserve(Turn.load(p),commands,protected={4})
        self.assertEqual(commands,{})

    def test_actual_planner_idle_pioneer_yields_before_night(self):
        p,common=setup()
        with patch.object(brain,'_task_step',return_value=None), \
             patch.object(brain,'_task_walk',return_value=(False,None)), \
             patch.object(brain,'_treasure_step',return_value=None):
            response=brain.plan_for_state(p,planner.PlannerState(),judge_tasks=False)
        self.assertEqual(response.commands['4']['action'],'move')
        self.assertNotEqual(response.commands['4']['targetPos'][0],common.dump())

    def test_disabled_does_not_reassign_old_strategy(self):
        self.config['enabled']=False
        p,_=setup();commands={}
        self.assertEqual(rocket_post.reserve(Turn.load(p),commands),[])
        self.assertEqual(commands,{})

    def test_stock_courier_return_never_commits_gunners_common_post(self):
        p,common=setup();p['teamOur']['roles'][2]=unit(3,'worker',7,22,220)
        p['teamOur']['roles'][2]['backpack']=[WALL_FIXER]*2
        record=dict(owner=3,target=1,item=WALL_FIXER,operation='stock',quantity=2,
                    phase='return',deadline=460)
        for round_no in range(450,460):
            p['roundNo']=round_no
            cost=team_trip.evaluate_trip(Turn.load(p),p,record)
            self.assertTrue(cost.feasible,cost.reason)
            if cost.command is None:
                self.assertEqual(cost.reason,'returned')
                break
            pos=cost.command['targetPos'][0]
            self.assertNotEqual(pos,common.dump())
            p['teamOur']['roles'][2]['pos']=deepcopy(pos)
        else:
            self.fail('stock courier must complete another safe return')

    def test_day_trip_returns_gunner_through_rear_to_exact_common_post(self):
        p,common=setup();p['teamOur']['roles'][2]['health']=0
        record=dict(owner=2,target=1,item=WALL_FIXER,operation='stock',quantity=1,
                    phase='return',deadline=460)
        for round_no in range(450,460):
            p['roundNo']=round_no
            cost=team_trip.evaluate_trip(Turn.load(p),p,record)
            self.assertTrue(cost.feasible,cost.reason)
            if cost.command is None:break
            p['teamOur']['roles'][1]['pos']=deepcopy(cost.command['targetPos'][0])
        self.assertEqual(p['teamOur']['roles'][1]['pos'],common.dump())

    def test_trip_frame_retains_gunner_until_actual_common_arrival(self):
        p,common=setup();p['teamOur']['roles'][2]['health']=0
        row=dict(owner=2,target=1,item=WALL_FIXER,operation='stock',quantity=1,
                 phase='return',deadline=460,level=1,count=0,last_action='move',
                 issued_round=440,last_round=449)
        frame=team_trip.TripFrame(Turn.load(p),p,{'purchase':row})
        self.assertIsNotNone(frame.purchase,'an arbitrary adjacent gun is not the reserved home')
        cost=team_trip.evaluate_trip(Turn.load(p),p,frame.purchase)
        self.assertGreater(cost.actions,0)
        p['roundNo']=451;p['teamOur']['roles'][1]['pos']=common.dump()
        arrived=team_trip.TripFrame(Turn.load(p),p,frame.memory)
        self.assertIsNone(arrived.purchase)
        self.assertEqual(arrived.events[-1]['reason'],'returned_observed')


if __name__=='__main__':unittest.main()
