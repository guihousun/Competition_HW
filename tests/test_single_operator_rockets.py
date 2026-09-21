"""R01/R04: a single worker, three rockets, public cooldowns and safe stands."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from test_defense_layout import board, unit, REPORTED, MIRROR, ring_points
from agent import brain, defense_layout, home_defense, planner, strategy_config
from agent.protocol import Turn, Pos, distance


def fixture():
    p = board(round_no=90)
    p['teamOur']['roles'] = [unit(1, 'station', 9, 22, 1500),
        unit(2, 'worker', 11, 21, 220), unit(3, 'worker', 8, 20, 220),
        unit(4, 'pioneer', 8, 23, 200), unit(10, 'rocket', 11, 20),
        unit(11, 'rocket', 10, 20), unit(12, 'rocket', 11, 22)]
    p['robot'] = {'roles': [dict(id=90, roleType='largeRobot', health=500,
                               pos=dict(x=15, y=21))]}
    return p


class SingleOperatorTests(unittest.TestCase):
    def setUp(self):
        self.config = deepcopy(strategy_config.DEFAULTS)
        self.config['enabled'] = True
        self.config['defense']['single_operator_three_rockets'] = True
        self.config['defense']['tower_loadout'] = ['rocket'] * 3
        self.config_patch = patch.object(strategy_config, 'get', return_value=self.config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    def test_all_three_common_cells_required(self):
        guns = {Pos(11,20): 'rocket', Pos(10,20): 'rocket', Pos(11,22): 'rocket'}
        self.assertEqual(brain._shared_rocket_cells(guns, {Pos(11,21),Pos(10,23)}),
                         (Pos(11,21),))
        self.assertEqual(brain._shared_rocket_cells(dict(list(guns.items())[:2]),
                                                   {Pos(11,21)}), ())

    def test_new_triples_have_common_stand_and_all_cells_keep_exit(self):
        with patch.object(brain,'TOWER_LOADOUT',('rocket',)*3):
            for base in (REPORTED,MIRROR):
                turn = Turn.load(board(base=base))
                sites = brain._tower_sites(turn)
                self.assertEqual(len(sites),3)
                _,plan,graph = brain._defence_geometry(turn)
                self.assertTrue(defense_layout.interior_reachability(
                    graph,obstacles=set(sites))[0])
                self.assertFalse(set(sites) & set(plan.exit_cells))
                free = set(graph.cells)-set(sites)
                shared = [p for p in free if all(distance(p,g)==1 for g in sites)]
                self.assertEqual(len(shared),1)
                self.assertTrue(any(distance(shared[0],e)==1 for e in plan.exit_cells))
                # User permits inner pockets if each still has a real exit.
                self.assertFalse(brain._inner_connected(graph,set(sites)))

    def test_public_cooldowns_rotate_three_rockets_one_action(self):
        p = fixture()
        for cooldowns, expected in [((0,0,0),10), ((3,0,0),11),
                                    ((2,3,0),12), ((1,2,3),None), ((0,1,2),10)]:
            for role, cooldown in zip(p['teamOur']['roles'][4:], cooldowns):
                role['cooldown'] = cooldown
            turn = Turn.load(p)
            cmd = {}
            result = brain._coordinate_rockets(turn, cmd, set())
            brain._fill_ready_weapons(turn, cmd, set())
            self.assertEqual(list(cmd), [] if expected is None else [expected])
            self.assertEqual(result['owner'], 2)
            for action in cmd.values():
                self.assertEqual(action['controllerId'], '2')

    def test_selected_mirrored_common_stations_fire_three_without_moving(self):
        for base,positions,post,robot in [
                (REPORTED,[(8,21),(8,23),(9,23)],(8,22),(15,21)),
                (MIRROR,[(32,21),(31,23),(32,23)],(32,22),(25,21))]:
            p = board(base=base,round_no=90)
            p['teamOur']['roles'] = [unit(1,'station',*base,1500),unit(2,'worker',*post,220)]
            p['teamOur']['roles'] += [unit(10+i,'rocket',*pos) for i,pos in enumerate(positions)]
            p['robot']={'roles':[dict(id=90,roleType='largeRobot',health=500,
                                    pos=dict(zip(('x','y'),robot)))]}
            for cooldowns,expected in [((0,0,0),10),((3,0,0),11),((2,3,0),12)]:
                for role,cooldown in zip(p['teamOur']['roles'][2:],cooldowns):
                    role['cooldown']=cooldown
                cmd={};result=brain._coordinate_rockets(Turn.load(p),cmd,set())
                self.assertTrue(result['common_stand_feasible'])
                self.assertEqual(result['stand'],dict(zip(('x','y'),post)))
                self.assertEqual(list(cmd),[expected])
                self.assertEqual(cmd[expected]['controllerId'],'2')

    def test_mixed_rocket_railgun_loadout_uses_same_common_post(self):
        """The common post covers the configured 2R+1 railgun loadout."""
        p = board(base=REPORTED, round_no=90)
        p['teamOur']['roles'] = [unit(1, 'station', *REPORTED, 1500),
            unit(2, 'worker', 8, 22, 220)]
        p['teamOur']['roles'] += [unit(10, 'rocket', 8, 21),
                                   unit(11, 'railgun', 8, 23),
                                   unit(12, 'rocket', 9, 23)]
        p['robot'] = {'roles': [dict(id=90, roleType='largeRobot', health=500,
                                     pos=dict(x=15, y=21))]}
        config = deepcopy(self.config)
        config['defense']['tower_loadout'] = ['rocket', 'railgun', 'rocket']
        with patch.object(strategy_config, 'get', return_value=config):
            turn = Turn.load(p)
            cmd = {}
            result = brain._coordinate_rockets(turn, cmd, set())
        self.assertEqual(result['stand'], {'x': 8, 'y': 22})
        self.assertTrue(result['common_stand_feasible'])
        self.assertEqual(list(cmd), [10])
        self.assertEqual(cmd[10]['controllerId'], '2')

    def test_higher_useful_damage_ready_gun_wins(self):
        p = fixture()
        p['teamOur']['roles'][5]['level'] = 3
        cmd = {}
        brain._coordinate_rockets(Turn.load(p), cmd, set())
        self.assertEqual(list(cmd), [11])
        self.assertEqual(len(cmd[11]['targetPos']), 3)

    def test_two_station_five_round_cycle_uses_all_three(self):
        p = fixture()
        for role,pos in zip(p['teamOur']['roles'][4:],[(10,20),(11,20),(11,23)]):
            role['pos'] = dict(zip(('x','y'),pos))
        # Hand supplied official observations, independent of simulator cooldown.
        cases=[((11,21),(0,0,0),10), ((11,21),(3,0,0),11),
               ((11,21),(2,3,0),'move'), ((11,22),(1,2,0),12),
               ((11,22),(0,1,3),'move')]
        for pos,cooldowns,expected in cases:
            p['teamOur']['roles'][1]['pos'] = dict(zip(('x','y'),pos))
            for role,cooldown in zip(p['teamOur']['roles'][4:],cooldowns):
                role['cooldown'] = cooldown
            cmd = {}
            brain._coordinate_rockets(Turn.load(p),cmd,set())
            self.assertEqual(list(cmd),[2] if expected=='move' else [expected])
            if expected=='move':
                self.assertEqual(cmd[2]['targetPos'],[dict(x=11,y=22 if pos[1]==21 else 21)])

    def test_approach_uses_actual_inner_distance_around_base(self):
        p = fixture()
        p['teamOur']['roles'] = [r for r in p['teamOur']['roles'] if r['id'] not in (3,4)]
        worker = next(r for r in p['teamOur']['roles'] if r['id']==2)
        worker['pos'] = dict(x=11,y=23)
        for role in p['teamOur']['roles']:
            if role['id']==10: role['pos']=dict(x=8,y=21)
            if role['id'] in (11,12): role['cooldown']=3
        cmd = {}
        result = brain._coordinate_rockets(Turn.load(p),cmd,set())
        # (8,20) is also Chebyshev distance 3 but its real path goes around
        # the south side. The northern (8,22) control cell is only three moves.
        self.assertEqual(result['stand'],dict(x=8,y=22))
        self.assertEqual(result['approach_steps'],3)
        self.assertEqual(cmd[2]['targetPos'],[dict(x=10,y=23)])

    def test_dead_owner_only_falls_back_to_living_worker(self):
        p = fixture()
        p['teamOur']['roles'][1]['health'] = 0
        p['teamOur']['roles'][2]['pos'] = dict(x=11,y=21)
        cmd = {}
        result = brain._coordinate_rockets(Turn.load(p), cmd, set())
        self.assertEqual(result['owner'],3)
        self.assertEqual(cmd[10]['controllerId'],'3')
        p['teamOur']['roles'][2]['health'] = 0
        cmd = {}
        result = brain._coordinate_rockets(Turn.load(p), cmd, set())
        self.assertIsNone(result['owner'])
        self.assertEqual(result['reason'],'no_living_worker')
        self.assertEqual(cmd,{})

    def test_busy_owner_is_preserved_without_second_gunner(self):
        turn = Turn.load(fixture())
        command = dict(action='use',name='WallFixer',targetPos=[dict(x=12,y=21)])
        cmd = {2: command}
        result = brain._coordinate_rockets(turn,cmd,set())
        brain._fill_ready_weapons(turn,cmd,set())
        self.assertEqual(result['owner'],2)
        self.assertEqual(result['phase'],'owner_busy')
        self.assertEqual(cmd,{2:command})

    def test_unshared_layout_routes_same_worker_inside(self):
        p = fixture()
        p['teamOur']['roles'][1]['pos'] = dict(x=11,y=23)
        p['teamOur']['roles'][4]['pos'] = dict(x=8,y=22)
        p['teamOur']['roles'][5]['pos'] = dict(x=8,y=21)
        p['teamOur']['roles'][6]['pos'] = dict(x=10,y=20)
        turn = Turn.load(p)
        cmd = {}
        result = brain._coordinate_rockets(turn,cmd,set())
        brain._fill_ready_weapons(turn,cmd,set())
        self.assertEqual(result['owner'],2)
        self.assertFalse(result['common_stand_feasible'])
        self.assertEqual(result['reason'],'no_common_inner_stand')
        self.assertEqual(list(cmd),[2])
        self.assertEqual(cmd[2]['action'],'move')
        self.assertTrue(home_defense.inside(turn,Pos.load(cmd[2]['targetPos'][0])))

    def test_unreachable_never_uses_free_adjacent_pioneer(self):
        p = fixture()
        p['teamOur']['roles'][1]['pos'] = dict(x=1,y=1)
        p['teamOur']['roles'][3]['pos'] = dict(x=11,y=21)
        p['mapInfo']['zones'] = [dict(pos=dict(x=x,y=y),neutralType='water')
            for x in range(3) for y in range(3) if (x,y)!=(1,1)]
        turn = Turn.load(p)
        cmd = {}
        result = brain._coordinate_rockets(turn,cmd,set())
        brain._fill_ready_weapons(turn,cmd,set())
        self.assertEqual(result['owner'],2)
        self.assertEqual(result['phase'],'common_stand_blocked')
        self.assertEqual(cmd,{})

    def test_blocked_common_keeps_same_worker_adjacent_safe_fire(self):
        p=fixture()
        for role,pos in zip(p['teamOur']['roles'][4:],[(8,21),(8,23),(9,23)]):
            role['pos']=dict(zip(('x','y'),pos))
        p['teamOur']['roles'][1]['pos']=dict(x=8,y=20)
        p['teamOur']['roles'][2]['pos']=dict(x=10,y=23)
        p['teamOur']['roles'][3]['pos']=dict(x=8,y=22)
        p['teamOur']['roles'][3]['backpack']=['WallFixer']
        cmd={4:dict(action='use',name='WallFixer',targetPos=[dict(x=7,y=22)])}
        turn=Turn.load(p)
        result=brain._coordinate_rockets(turn,cmd,set())
        brain._fill_ready_weapons(turn,cmd,set())
        self.assertFalse(result['common_stand_feasible'])
        self.assertEqual(result['reason'],'common_stand_unreachable')
        self.assertEqual(result['fallback'],'same_operator_adjacent_fire_common_blocked')
        self.assertEqual(cmd[10]['controllerId'],'2')
        self.assertNotIn(2,cmd)
        self.assertEqual(cmd[4]['action'],'use')
        self.assertEqual(len([c for c in cmd.values() if c['action']=='attack']),1)

    def test_common_station_staging_uses_rear_opening_only_in_day(self):
        p = fixture()
        p['roundNo'] = 55
        for role,pos in zip(p['teamOur']['roles'][4:],[(8,21),(8,23),(9,23)]):
            role['pos'] = dict(zip(('x','y'),pos))
        p['teamOur']['roles'] = [r for r in p['teamOur']['roles'] if r['id'] not in (3,4)]
        for i,(x,y) in enumerate(sorted(ring_points(REPORTED)-{(7,21),(7,22)})):
            p['teamOur']['roles'].append(unit(100+i,'wall',x,y))
        visited=[]
        for _ in range(12):
            turn = Turn.load(p);cmd={}
            result = brain._coordinate_rockets(turn,cmd,set())
            if result['active']:break
            self.assertEqual(result['phase'],'moving_to_common_stand')
            target = cmd[2]['targetPos'][0]
            self.assertNotIn(Pos.load(target),turn.blocked(turn.workers()[0]))
            visited.append((target['x'],target['y']))
            p['teamOur']['roles'][1]['pos'] = target
        self.assertTrue(result['active'])
        self.assertEqual(result['stand'],dict(x=8,y=22))
        self.assertTrue(set(visited)&{(7,21),(7,22)})
        p['roundNo'] = 90;p['teamOur']['roles'][1]['pos'] = dict(x=11,y=21)
        cmd={};result=brain._coordinate_rockets(Turn.load(p),cmd,set())
        self.assertEqual(result['phase'],'common_stand_blocked')
        self.assertEqual(cmd,{})

    def test_real_brain_dusk_stages_through_opening_then_stationary_night(self):
        for side,base,positions,post,front_x,rear_x,robot_x in [
                ('challenger',REPORTED,[(8,21),(8,23),(9,23)],(8,22),11,7,15),
                ('defender',MIRROR,[(32,21),(31,23),(32,23)],(32,22),29,33,25)]:
            with self.subTest(side=side), patch.object(brain,'_STRATEGY',self.config):
                p=board(base=base,round_no=446)
                p['teamOur']['type']=side
                p['teamOur']['roles']=[unit(1,'station',*base,1500),
                    unit(2,'worker',front_x,21,220),unit(3,'worker',front_x,20,220),
                    unit(4,'pioneer',front_x,23,200)]
                p['teamOur']['roles'] += [unit(10+i,'rocket',*pos) for i,pos in enumerate(positions)]
                openings={(rear_x,21),(rear_x,22)}
                p['teamOur']['roles'] += [unit(100+i,'wall',x,y)
                    for i,(x,y) in enumerate(sorted(ring_points(base)-openings))]
                state=planner.PlannerState();visited=[]
                for round_no in range(446,461):
                    p['roundNo']=round_no;turn=Turn.load(p)
                    response=brain.plan_for_state(p,state,judge_tasks=False)
                    self.assertFalse(any(c.get('action')=='attack' for c in response.commands.values()))
                    by_id={r['id']:r for r in p['teamOur']['roles']}
                    for uid,command in response.commands.items():
                        if command.get('action')!='move':continue
                        target=Pos.load(command['targetPos'][0])
                        role=next(r for r in turn.controllable() if r.unit_id==int(uid))
                        self.assertNotIn(target,turn.blocked(role))
                        self.assertEqual(distance(role.pos,target),1)
                        by_id[int(uid)]['pos']=target.dump()
                        if int(uid)==2:visited.append((target.x,target.y))
                self.assertTrue(set(visited)&openings,visited)
                self.assertEqual(p['teamOur']['roles'][1]['pos'],dict(zip(('x','y'),post)))
                p['robot']={'roles':[dict(id=90,roleType='largeRobot',health=500,
                                        pos=dict(x=robot_x,y=21))]}
                for round_no,cooldowns,expected in [(461,(0,0,0),10),(462,(3,0,0),11),(463,(2,3,0),12)]:
                    p['roundNo']=round_no
                    for role,cooldown in zip(p['teamOur']['roles'][4:7],cooldowns):
                        role['cooldown']=cooldown
                    response=brain.plan_for_state(p,state,judge_tasks=False)
                    self.assertNotIn('2',response.commands)
                    attacks={uid:c for uid,c in response.commands.items() if c.get('action')=='attack'}
                    self.assertEqual(set(attacks),{str(expected)})
                    self.assertEqual(attacks[str(expected)]['controllerId'],'2')

    def test_day_never_attacks(self):
        p = fixture(); p['roundNo'] = 30
        cmd = {}
        brain._coordinate_rockets(Turn.load(p),cmd,set())
        self.assertFalse(any(c['action']=='attack' for c in cmd.values()))

    def test_disabled_keeps_two_rocket_shared_contract(self):
        self.config['enabled'] = False
        p = fixture();p['teamOur']['roles'][5]['roleType'] = 'railgun'
        cmd = {}
        result = brain._coordinate_rockets(Turn.load(p),cmd,set())
        self.assertEqual(result['owner'],2)
        self.assertEqual(result['weapons'],[10,12])
        self.assertEqual(list(cmd),[10])


if __name__ == '__main__':
    unittest.main()
