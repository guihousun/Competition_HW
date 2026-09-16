"""Independent hand-drawn corridor expectations; no policy-produced answers."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from test_coordination import observation, unit
from agent.coordination import reconcile as _reconcile
from agent.protocol import Pos, Turn, move_command
from agent import brain, planner
from agent.home_defense import inside


def reconcile(turn,payload,commands):
    return _reconcile(turn,payload,commands,day_yield_deadline=55)


def corridor(mirror=False, round_no=391, dead_end_only=False):
    payload = observation()
    flip = lambda x: 40-x if mirror else x
    # Pioneer can move north into a dead end but cannot exit west until W1
    # moves aside. Diagonal movement is allowed; all omitted cells are solid.
    free = {(5,5),(5,6),(4,5),(3,4),(3,5),(2,4),(2,5),(1,5)}
    if dead_end_only:
        free = {(5,5),(5,6),(4,5),(3,5)}
    payload['roundNo'] = round_no
    payload['teamOur']['type'] = 'defender' if mirror else 'challenger'
    payload['teamOur']['roles'] = [unit(71,'worker',flip(4),5),unit(92,'pioneer',flip(5),5)]
    payload['mapInfo']['zones'] = [dict(pos=dict(x=flip(x),y=y),neutralType='wall')
        for x in range(41) for y in range(32) if (x,y) not in free]
    return payload, {92:move_command(Pos(flip(5),4))}, flip


class ComponentYieldTests(unittest.TestCase):
    def setUp(self):
        planner.reset()

    def tearDown(self):
        planner.reset()

    def test_real_entry_opens_three_role_chain_without_assumed_settlement(self):
        for side in ('challenger','defender'):
            with self.subTest(side=side):
                planner.reset()
                p=json.loads((Path(__file__).parent/'fixtures/issue32'/f'{side}.json').read_text(encoding='utf-8'))
                workers={r['id'] for r in p['teamOur']['roles'] if r['roleType']=='worker'}
                hero=next(r for r in p['teamOur']['roles'] if r['roleType']=='pioneer')
                seen_workers=[]
                for step in range(8):
                    before=Turn.load(p)
                    response=brain.respond(deepcopy(p))['roleCommandMap']
                    self.assertTrue(response)
                    self.assertEqual(len(response),1)
                    uid,command=next(iter(response.items()));uid=int(uid)
                    self.assertEqual(command['action'],'move')
                    role=next(r for r in before.controllable() if r.unit_id==uid)
                    destination=Pos.load(command['targetPos'][0])
                    self.assertEqual(max(abs(destination.x-role.pos.x),abs(destination.y-role.pos.y)),1)
                    self.assertTrue(before.land(destination))
                    self.assertNotIn(destination,before.blocked(role))
                    if step<3:
                        self.assertIn(uid,workers);seen_workers.append(uid)
                    else:self.assertEqual(uid,hero['id'])
                    # Independent movement settlement: change only a verified
                    # legal one-step position, then obtain a NEW observation.
                    next(r for r in p['teamOur']['roles'] if r['id']==uid)['pos']=destination.dump()
                    p['roundNo']+=1
                self.assertEqual(len(set(seen_workers)),2)
                self.assertFalse(inside(Turn.load(p),Pos.load(hero['pos'])))

    def test_failed_first_yield_is_replanned_from_real_positions(self):
        p=json.loads((Path(__file__).parent/'fixtures/issue32/challenger.json').read_text(encoding='utf-8'))
        first=brain.respond(deepcopy(p))['roleCommandMap']
        # Simulate a rejected/failed move by leaving ALL positions unchanged.
        p['roundNo']+=1
        second=brain.respond(deepcopy(p))['roleCommandMap']
        self.assertEqual(second,first)

    def test_free_dead_end_does_not_prevent_daytime_opening_both_sides(self):
        for mirror in (False,True):
            with self.subTest(mirror=mirror):
                p, commands, flip = corridor(mirror)
                before = deepcopy((p,commands))
                result=reconcile(Turn.load(p),p,commands)
                self.assertEqual(set(result),{71})
                # Either the north pocket or west alcove is a valid opening;
                # do not turn a coordinate tie-break into a rule requirement.
                target=Pos.load(result[71]['targetPos'][0])
                self.assertIn(target,{Pos(flip(3),4),Pos(flip(5),6)})
                self.assertEqual((p,commands),before)
                # Only the yielding worker moves now; pioneer uses the vacated
                # cell next round. Never enter a currently occupied cell.
                p['teamOur']['roles'][0]['pos']=target.dump()
                p['roundNo']+=1
                step={92:move_command(Pos(flip(4),5))}
                self.assertEqual(reconcile(Turn.load(p),p,step),step)

    def test_night_and_visible_threat_do_not_expand_yield(self):
        for mode in ('night','dusk','robot','enemy'):
            p,commands,_=corridor(round_no={'night':461,'dusk':460}.get(mode,391))
            if mode=='robot':p['robot']={'roles':[dict(id=800,pos=dict(x=20,y=20),health=40)]}
            if mode=='enemy':p['teamEnemy']['roles']=[unit(800,'worker',20,20)]
            with self.subTest(mode=mode):
                self.assertEqual(reconcile(Turn.load(p),p,commands),{})

    def test_no_yield_if_it_only_moves_blockage_deeper(self):
        p,commands,_=corridor(dead_end_only=True)
        # On otherwise open land the idle worker is not a cut vertex: moving
        # it merely trades one occupied cell for another, opening no region.
        p['mapInfo']['zones']=[dict(pos=dict(x=5,y=4),neutralType='wall')]
        self.assertEqual(reconcile(Turn.load(p),p,commands),{})

    def test_second_task_point_cell_is_not_a_yield_destination(self):
        p,commands,_=corridor()
        p['mapInfo']['zones'].append(dict(pos=dict(x=2,y=4),neutralType='challengerTaskPoint2'))
        response=reconcile(Turn.load(p),p,commands)
        self.assertEqual(response,{71:move_command(Pos(5,6))})

    def test_does_not_displace_assigned_worker_or_controller(self):
        for action in ({71:dict(action='collect',targetPos=[dict(x=3,y=5)])},
                       {700:dict(action='attack',controllerId='71',targetPos=[dict(x=2,y=5)])}):
            p,commands,_=corridor()
            self.assertEqual(reconcile(Turn.load(p),p,{**commands,**action}),action)


if __name__=='__main__':unittest.main()
