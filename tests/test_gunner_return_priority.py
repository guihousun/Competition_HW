"""Late courier route pressure must not remove a legal dusk gunner move."""
import unittest
from unittest.mock import patch

from test_rocket_post import setup
from test_defense_layout import unit
from agent import brain, planner, team_trip
from agent.protocol import WALL_FIXER, Turn


class GunnerReturnPriorityTests(unittest.TestCase):
    def test_actual_brain_staging_survives_courier_route_veto(self):
        p,_=setup(round_no=450)
        p['teamOur']['roles'][2]=unit(3,'worker',3,20,220)
        p['teamOur']['roles'][2]['backpack']=[WALL_FIXER]*2
        state=planner.PlannerState()
        state.team_trips={'purchase':dict(owner=3,target=1,item=WALL_FIXER,
            operation='stock',quantity=2,phase='return',deadline=458,
            level=1,count=2,last_action='move',issued_round=440,last_round=449)}
        with patch.object(team_trip.RouteGuard,'allows_command',return_value=False):
            response=brain.plan_for_state(p,state,judge_tasks=False)
        self.assertEqual(response.commands['2']['action'],'move')
        self.assertEqual(state.team_trips['purchase']['owner'],3)

    def test_priority_never_exempts_build_or_changes_an_unflagged_move(self):
        from test_team_trip import observed,purchase
        p=observed(178)
        frame=team_trip.TripFrame(Turn.load(p),p,{'purchase':purchase(177)})
        move=dict(action='move',targetPos=[dict(x=36,y=7)])
        build=dict(action='build',name='wall',targetPos=[dict(x=35,y=8)])
        with patch.object(team_trip.RouteGuard,'allows_command',return_value=False):
            self.assertEqual(frame.protect_final({20010:move}),{})
            self.assertEqual(frame.protect_final({20010:move},priority_return_moves={20010}),{20010:move})
            self.assertEqual(frame.protect_final({20010:build},priority_return_moves={20010}),{})


if __name__=='__main__':unittest.main()
