"""Auditor's hand-drawn P1 case: a different first step cannot be substituted."""
from copy import deepcopy
import unittest

from test_team_trip_return import corridor
from test_coordination import unit
from agent import team_trip
from agent.protocol import Turn,Pos


def hand_drawn(mirror=False):
    p,c=corridor(20)
    closed={(5,7),(5,8),(5,13),(6,9),(6,11),(6,13),(7,6),(7,9),(7,11),
            (7,14),(8,12),(9,6),(9,11),(11,7),(12,8),(12,14),(13,6),(14,7)}
    p['mapInfo']['zones']=[dict(pos=dict(x=x,y=y),neutralType='water')
        for x in range(41) for y in range(32)
        if not (5<=x<=14 and 6<=y<=14) or (x,y) in closed]
    p['teamOur']['roles'][-1]['pos']=dict(x=6,y=10)
    builder=unit(999,'worker',7,10);builder['backpack']=['stone']
    p['teamOur']['roles'].append(builder)
    c.update(issued_round=1,last_round=19,deadline=26,last_action='move')
    if mirror:
        p['teamOur']['type']='defender'
        for u in p['teamOur']['roles']:
            u['id']+=3000
            u['pos']['x']=40-u['pos']['x']-int(u['roleType']=='station')
        for z in p['mapInfo']['zones']:z['pos']['x']=40-z['pos']['x']
        c['owner']+=3000;c['target']+=3000
    return p,c


class FixedStepTests(unittest.TestCase):
    def test_active_trip_rejects_build_that_only_fits_a_different_first_step(self):
        for mirror in (False,True):
            p,c=hand_drawn(mirror);before=deepcopy(p);t=Turn.load(p)
            flip=lambda x:40-x if mirror else x
            builder=3999 if mirror else 999
            first=dict(action='move',targetPos=[dict(x=flip(5),y=9)])
            build=dict(action='build',name='wall',targetPos=[dict(x=flip(8),y=9)])
            original=team_trip.evaluate_trip(t,p,c)
            self.assertEqual((original.actions,original.budget,original.command),(6,6,first))
            # Independent BFS witness from review: from the original cell,
            # moving NORTH instead still takes6. That is NOT our emitted step.
            reset_origin=team_trip.evaluate_trip(t,p,c,added_walls=[Pos(flip(8),9)])
            self.assertEqual((reset_origin.actions,reset_origin.budget),(6,6))
            next_without=team_trip.evaluate_after_action(t,p,c,first,{c['owner']:first})
            next_with=team_trip.evaluate_after_action(t,p,c,first,{c['owner']:first,builder:build})
            self.assertEqual((next_without.actions,next_without.budget),(5,5))
            self.assertEqual((next_with.actions,next_with.budget,next_with.feasible),(7,5,False))
            frame=team_trip.TripFrame(t,p,{'purchase':c})
            output=frame.protect_final({c['owner']:first,builder:build})
            self.assertEqual(output,{c['owner']:first},'active commitment keeps its real first step')
            self.assertEqual(frame.purchase['target'],c['target'])
            self.assertEqual(p,before)

    def test_early_constructor_guard_uses_already_selected_courier_step_too(self):
        p,c=hand_drawn();t=Turn.load(p)
        first=dict(action='move',targetPos=[dict(x=5,y=9)])
        guard=team_trip.RouteGuard(t,p,c,{731:first})
        self.assertEqual((guard.before.actions,guard.before.budget),(5,5))
        self.assertFalse(guard.check(999,Pos(7,10),(Pos(8,9),))[0])
        self.assertEqual(guard.report()['time_slice'],'after_fixed_action')

    def test_first_step_cannot_land_on_a_same_round_new_wall(self):
        p,c=corridor(20)
        p['mapInfo']['zones']=[]
        p['teamOur']['roles'][-1]['pos']=dict(x=7,y=9)
        builder=unit(999,'worker',8,8);builder['backpack']=['stone']
        p['teamOur']['roles'].append(builder)
        c.update(issued_round=1,last_round=19,deadline=26,last_action='move')
        # (8,9) is on the legal assumed radius-2 wall ring of base(10,10).
        # Each actor is adjacent; the cell is currently free. The simultaneous
        # move/build conflict must not become a fictitious shadow start.
        t=Turn.load(p)
        from agent.defense_layout import geometric_ring
        self.assertIn(Pos(8,9),geometric_ring(t.station().pos))
        self.assertNotIn(Pos(8,9),t.occupied_cells())
        first=dict(action='move',targetPos=[dict(x=8,y=9)])
        guard=team_trip.RouteGuard(t,p,c,{731:first})
        self.assertFalse(guard.check(999,Pos(8,8),(Pos(8,9),))[0])
        cost=guard.cache[(999,Pos(8,8),frozenset((Pos(8,9),)))]
        self.assertEqual(cost.reason,'first_action_conflict')


if __name__=='__main__':unittest.main()
