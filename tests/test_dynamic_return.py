"""Hand-counted daylight budgets, independent of policy-generated answers."""
import sys
from pathlib import Path
import unittest
from copy import deepcopy
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'Demo/CoreGeek/src'))
from agent import dusk_return, strategy_config, brain, planner
from agent.protocol import Turn


def board(index=58, side='challenger', worker_x=14):
    return dict(roundNo=index+1,mapInfo=dict(width=41,height=32,zones=[
        dict(neutralType='copper',pos=dict(x=worker_x+1,y=10))]),
        teamOur=dict(type=side,goldNum=0,totalScore=0,playerTasks=[],roles=[
            dict(id=1,roleType='station',pos=dict(x=10,y=10),health=1500,level=1),
            dict(id=2,roleType='worker',pos=dict(x=worker_x,y=10),health=220,backpack=[],backPackCapability=100),
        ]),teamEnemy=dict(roles=[]),robot=dict(roles=[]),phaseTask='',worldNews={},errors=[])


class DynamicReturnTests(unittest.TestCase):
    def frame(self, data):
        turn=Turn.load(data)
        return dusk_return.Frame(turn),turn.workers()[0]

    def test_near_worker_keeps_collecting_after_old_55_boundary(self):
        for side in ('challenger','defender'):
            f,w=self.frame(board(side=side))
            self.assertEqual(f.routes(w)[0][w.pos],2) # (14,10)->(13,10)->(12,10)
            cmd={2:dict(action='collect',targetPos=[dict(x=15,y=10)])}
            self.assertEqual(f.apply(cmd),cmd)
            self.assertEqual(f.rows['2']['remaining_work_rounds'],6)

    def test_far_worker_returns_before_old_55_boundary(self):
        f,w=self.frame(board(45,worker_x=33))
        self.assertEqual(f.routes(w)[0][w.pos],21)
        self.assertTrue(f.required(w))
        self.assertEqual(f.apply({})[2]['action'],'move')

    def test_atomic_last_action_fits_but_no_extra_round_after_deadline(self):
        cmd={2:dict(action='collect',targetPos=[dict(x=15,y=10)])}
        f,w=self.frame(board(63)) # 63+1+2=66; four rounds spare
        self.assertEqual(f.apply(cmd),cmd)
        f,w=self.frame(board(64))
        self.assertEqual(f.apply(cmd)[2]['action'],'move')

    def test_fourth_day_and_damage_increase_margin(self):
        s=board(58);s['roundNo']+=390
        f,w=self.frame(s);self.assertEqual(f.deadline,64)
        s['teamOur']['roles'][0]['health']=1499
        f,w=self.frame(s);self.assertEqual(f.deadline,62)
        s['teamOur']['roles'][0]['health']=500
        f,w=self.frame(s);self.assertEqual(f.apply({})[2]['action'],'move')
        self.assertEqual(f.rows['2']['reason'],'emergency_return')

    def test_obstacle_detour_uses_path_not_distance(self):
        s=board(56)
        s['mapInfo']['zones'] += [dict(neutralType='water',pos=dict(x=13,y=y)) for y in range(7,15)]
        f,w=self.frame(s)
        self.assertGreater(f.routes(w)[0][w.pos],2)

    def test_no_path_and_night_do_not_invent_return(self):
        s=board();s['mapInfo']['zones'] += [dict(neutralType='water',pos=dict(x=13,y=y)) for y in range(32)]
        f,w=self.frame(s);self.assertIsNone(f.routes(w)[0].get(w.pos))
        self.assertEqual(f.apply({}),{})
        self.assertEqual(f.rows['2']['reason'],'route_unreachable')
        s=board(70);f,w=self.frame(s)
        cmd={2:dict(action='collect',targetPos=[dict(x=15,y=10)])}
        self.assertEqual(f.apply(cmd),cmd)

    def test_movement_is_costed_from_proposed_next_cell(self):
        f,w=self.frame(board(63))
        cmd={2:dict(action='move',targetPos=[dict(x=15,y=11)])}
        self.assertNotEqual(f.apply(cmd),cmd)

    def test_config_and_input_immutable(self):
        s=board();before=deepcopy(s);f,w=self.frame(s)
        f.apply({});self.assertEqual(s,before)
        c=strategy_config.get();c['economy']['return_margin_early']=-1
        with self.assertRaises(ValueError):strategy_config.validate(c)

    def test_planner_reports_dynamic_return_for_both_sides(self):
        for side in ('challenger','defender'):
            s=board(side=side);state=planner.PlannerState()
            brain.plan_for_state(s,state,judge_tasks=False)
            self.assertIn('dusk_return',brain.decision_report())
            self.assertEqual(brain.decision_report()['dusk_return']['2']['margin'],4)

if __name__=='__main__':unittest.main()
