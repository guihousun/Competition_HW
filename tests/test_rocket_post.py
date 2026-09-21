"""Independent public layouts for safe common-post reservation."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from test_defense_layout import board, unit, REPORTED, MIRROR, ring_points
from agent import rocket_post, strategy_config, home_defense, planner, brain, team_trip
from agent.protocol import Turn, Pos, WALL_FIXER, distance


def setup(base=REPORTED, round_no=450):
    blue = base == REPORTED
    guns = [(8,21),(8,23),(9,23)] if blue else [(32,21),(31,23),(32,23)]
    common = (8,22) if blue else (32,22)
    p = board(base=base, round_no=round_no)
    p['teamOur']['roles'] = [unit(1,'station',*base,1500),
        unit(2,'worker',8 if blue else 32,20,220),unit(4,'pioneer',*common,200)]
    p['teamOur']['roles'] += [unit(10+i,'rocket',*g) for i,g in enumerate(guns)]
    return p, Pos(*common)


def corridor_case(round_no=706):
    """Literal r706 building/idle corridor geometry from a62 seed1 blue trace."""
    p=board(base=(7,26),round_no=round_no)
    p['teamOur']['roles']=[unit(1,'station',7,26,1500),
        unit(2,'worker',9,24,220),unit(3,'worker',8,27,120),
        unit(4,'pioneer',7,24,200),unit(10,'rocket',6,25),
        unit(11,'rocket',6,27),unit(12,'rocket',7,27)]
    p['teamOur']['roles'] += [unit(100+i,'wall',x,y) for i,(x,y) in
        enumerate(sorted(ring_points((7,26))-{(5,25),(5,26)}))]
    return p


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

    def test_idle_corridor_blocker_moves_to_off_route_inner_cell_in_two_steps(self):
        p=corridor_case();commands={}
        report=rocket_post.reserve(Turn.load(p),commands)
        self.assertEqual(report[-1]['reason'],'yield_common_rocket_corridor')
        self.assertEqual(commands,{4:dict(action='move',targetPos=[dict(x=8,y=24)])})
        self.assertNotIn(2,commands,'a relaxed path must not send gunner through occupancy')
        p['roundNo']+=1;p['teamOur']['roles'][3]['pos']=dict(x=8,y=24);commands={}
        rocket_post.reserve(Turn.load(p),commands)
        self.assertEqual(commands[4]['targetPos'],[dict(x=9,y=25)])

    def test_corridor_yield_preserves_protected_busy_and_night_roles(self):
        for round_no,protected,commands in [
                (706,{4},{}),(721,set(),{}),(690,set(),{}),
                (706,set(),{4:dict(action='use',name='WallFixer',targetPos=[dict(x=7,y=23)])}),
                (706,set(),{4:dict(action='move',targetPos=[dict(x=8,y=24)])})]:
            p=corridor_case(round_no);before=deepcopy(commands)
            rocket_post.reserve(Turn.load(p),commands,protected=protected)
            self.assertEqual(commands,before)

    def test_corridor_yield_reserves_an_already_moving_gunners_landing(self):
        p=corridor_case();turn=Turn.load(p)
        gunner_move=dict(action='move',targetPos=[dict(x=9,y=25)])
        commands={2:deepcopy(gunner_move)}
        self.assertIsInstance(turn.blocked(turn.workers()[0]),frozenset)
        report=rocket_post.reserve(turn,commands)
        self.assertEqual(commands[2],gunner_move)
        # The only off-route bay is now reserved by the gunner: hold rather
        # than move the blocker into a corridor with no reachable free bay.
        self.assertEqual(report[-1]['reason'],'common_corridor_cannot_vacate_safely')
        self.assertNotIn(4,commands)

    def test_actual_brain_idle_corridor_yields_and_gunner_reaches_common_before_night(self):
        p=corridor_case();state=planner.PlannerState();visited=[]
        with patch.object(brain,'_STRATEGY',self.config):
            for round_no in range(706,721):
                p['roundNo']=round_no;turn=Turn.load(p)
                response=brain.plan_for_state(p,state,judge_tasks=False)
                by_id={u['id']:u for u in p['teamOur']['roles']}
                landings=[]
                for uid,cmd in response.commands.items():
                    if cmd.get('action')!='move':continue
                    role=next(r for r in turn.controllable() if r.unit_id==int(uid))
                    target=Pos.load(cmd['targetPos'][0])
                    self.assertNotIn(target,turn.blocked(role))
                    self.assertEqual(distance(role.pos,target),1)
                    landings.append(target)
                    by_id[int(uid)]['pos']=target.dump()
                    if int(uid)==2:visited.append(target)
                self.assertEqual(len(landings),len(set(landings)))
            self.assertEqual(p['teamOur']['roles'][1]['pos'],dict(x=6,y=26))
            self.assertIn(Pos(5,25),visited)
            p['roundNo']=721
            p['robot']={'roles':[dict(id=90,roleType='largeRobot',health=500,pos=dict(x=13,y=25))]}
            response=brain.plan_for_state(p,state,judge_tasks=False)
            self.assertNotIn('2',response.commands)
            attacks=[c for c in response.commands.values() if c.get('action')=='attack']
            self.assertEqual(len(attacks),1)
            self.assertEqual(attacks[0]['controllerId'],'2')


if __name__=='__main__':unittest.main()
