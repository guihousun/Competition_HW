"""Hand-set geometry and official actions; no simulator oracle for expectations."""
import unittest
from types import SimpleNamespace
from itertools import combinations
from copy import deepcopy
from test_coordination import unit
from test_defense_layout import board, REPORTED, MIRROR
from test_nightwork import board as quiet_board
from agent import brain, defense_layout, home_defense, nightwork, upgrade_itinerary
from agent.protocol import Turn, Pos, Unit, distance


def battle():
    p = board(towers=[(10, 20), (10, 23)], round_no=90, workers=False)
    p['teamOur']['roles'] = [u for u in p['teamOur']['roles'] if u['roleType'] not in ('worker', 'pioneer')]
    p['teamOur']['roles'] += [unit(1, 'worker', 11, 20), unit(2, 'worker', 11, 23),
                             unit(30, 'wall', 12, 20, 600)]
    p['robot'] = {'roles': [dict(id=900, roleType='smallRobot', health=40, pos={'x':15,'y':21})]}
    return p


class FrontWallTests(unittest.TestCase):
    def test_both_forward_halves_before_rear_halves_mirrored(self):
        for base in (REPORTED, MIRROR):
            turn = Turn.load(board(base=base))
            cells = brain._wall_order(turn)
            expected = ({Pos(x,y) for x in (10,11,12) for y in (19,24)} if base == REPORTED
                        else {Pos(x,y) for x in (28,29,30) for y in (19,24)})
            self.assertEqual(set(cells[4:10]), expected)
            self.assertFalse(set(cells) & set(brain._exit_cells(turn)))

    def test_spaced_roster_has_three_distinct_transverse_positions(self):
        for base in (REPORTED, MIRROR):
            turn = Turn.load(board(base=base))
            sites = brain._tower_sites(turn)
            self.assertEqual(len({p.y for p in sites}), 3)
            self.assertTrue(all(distance(a,b) >= 2 for a,b in combinations(sites,2)))
            self.assertEqual(brain.TOWER_LOADOUT, ('rocket','railgun','rocket'))

    def test_build_type_survives_fixed_slot_reordering(self):
        p = board(workers=False)
        p['teamOur']['roles'] = [u for u in p['teamOur']['roles'] if u['roleType']=='station']
        original = brain._tower_sites(Turn.load(p))
        for index, target in enumerate(original):
            turn = Turn.load(p)
            sites = brain._tower_sites(turn)
            self.assertEqual(sites, original)
            worker = Unit.load(unit(1,'worker',target.x,target.y-1))
            commands = {}
            brain._worker_day(turn, worker, sites, list(original[index:]), [], set(), commands, p, set())
            self.assertEqual(commands[1]['name'], ('rocket','railgun','rocket')[index])
            p['teamOur']['roles'].append(unit(40+index, commands[1]['name'], target.x, target.y,1000))

    def test_outside_adjacent_controller_returns_instead_of_firing(self):
        p = battle()
        p['teamOur']['roles'][-3]['pos'] = {'x':12,'y':23} # outside, near upper gun
        turn=Turn.load(p);commands={}
        brain._night(turn,commands,p)
        brain._fill_ready_weapons(turn,commands,set())
        self.assertEqual(commands[1]['action'],'move')
        self.assertFalse(any(c.get('controllerId')=='1' for c in commands.values()))
        self.assertTrue(home_defense.inside(turn,Pos.load(commands[1]['targetPos'][0])))

    def test_outside_worker_returns_even_without_any_gun(self):
        p = battle();p['teamOur']['roles']=[u for u in p['teamOur']['roles'] if u['roleType']!='rocket']
        next(u for u in p['teamOur']['roles'] if u['id']==1)['pos']={'x':14,'y':21}
        commands={};brain._night(Turn.load(p),commands,p)
        self.assertEqual(commands[1]['action'],'move')

    def test_defending_pioneer_also_returns_but_active_task_is_not_forced_out(self):
        p = battle()
        role = next(u for u in p['teamOur']['roles'] if u['id'] == 1)
        role.update(roleType='pioneer', pos={'x':12,'y':23})
        turn = Turn.load(p); commands = {}
        brain._night(turn,commands,p)
        self.assertEqual(commands[1]['action'],'move')
        cycle = SimpleNamespace(description='active question', phase='solving', ended_round=None)
        commands = {}
        brain._night(turn,commands,p,SimpleNamespace(tasks={'cycle':cycle}))
        self.assertNotIn(1,commands, 'the task pipeline owns the active pioneer')

    def test_sealed_ring_does_not_teleport_remove_or_attack_from_outside(self):
        p=board(towers=[(11,21)],walls=[(x,y) for x in range(7,13) for y in range(19,25)
                                         if x in (7,12) or y in (19,24)],round_no=90)
        next(u for u in p['teamOur']['roles'] if u['roleType']=='worker')['pos']={'x':13,'y':21}
        turn=Turn.load(p);commands={};brain._night(turn,commands,p)
        self.assertNotIn(10010,commands)
        self.assertEqual(home_defense.status(turn,commands)[0]['state'],'return_blocked')

    def test_interior_path_cannot_detour_outside(self):
        p=battle();p['mapInfo']['zones']=[{'pos':{'x':11,'y':21},'neutralType':'water'},
                                       {'pos':{'x':11,'y':22},'neutralType':'water'}]
        # Also close the inner rear corridor; an exterior detour exists but is forbidden.
        p['mapInfo']['zones'] += [{'pos':{'x':8,'y':y},'neutralType':'water'} for y in (21,22)]
        turn=Turn.load(p)
        self.assertIsNone(home_defense.step_inside(turn,turn.workers()[0],[Pos(9,23)]))

    def test_return_routes_around_front_wall_and_does_not_collide(self):
        p=board(towers=[],walls=[(12,y) for y in range(19,25)],round_no=90)
        p['teamOur']['roles'] += [unit(2,'worker',13,23)]
        next(u for u in p['teamOur']['roles'] if u['roleType']=='worker')['pos']={'x':13,'y':21}
        turn=Turn.load(p);commands={};brain._night(turn,commands,p)
        moves=[Pos.load(c['targetPos'][0]) for c in commands.values() if c['action']=='move']
        self.assertEqual(len(moves),len(set(moves)))
        self.assertTrue(all(pos.x!=12 for pos in moves))

    def test_quiet_external_mining_preserved_and_new_robot_aborts(self):
        p=quiet_board();p['teamOur']['roles'][1]['pos']={'x':8,'y':5}
        commands={};brain._night(Turn.load(p),commands,p)
        self.assertEqual(commands[2]['action'],'collect')
        p['robot']['roles']=[dict(id=900,health=40,pos={'x':30,'y':20})]
        commands={};brain._night(Turn.load(p),commands,p)
        self.assertEqual(commands[2]['action'],'move')

    def test_front_repair_uses_one_action_then_resumes_post(self):
        p=battle()
        for role in p['teamOur']['roles']:
            if role['roleType']=='worker':role['backpack']=['WallFixer']
        turn=Turn.load(p);commands={};brain._night(turn,commands,p)
        self.assertEqual(commands[1],dict(action='use',name='WallFixer',targetPos=[dict(x=12,y=20)]))
        self.assertEqual(sum(c['action']=='use' for c in commands.values()),1)
        p['teamOur']['roles'][-1]['health']=1000
        commands={};brain._night(Turn.load(p),commands,p)
        self.assertTrue(any(c.get('controllerId')=='1' for c in commands.values()))

    def test_no_combat_repair_when_only_worker_or_outside(self):
        p=battle();p['teamOur']['roles']=[u for u in p['teamOur']['roles'] if u['id']!=2]
        next(u for u in p['teamOur']['roles'] if u['id']==1)['backpack']=['WallFixer']
        turn=Turn.load(p)
        self.assertEqual(nightwork._front_repair(turn,brain._tower_pairs(turn)),{})

    def test_weapon_level_three_outranks_healthy_base_but_not_emergency(self):
        roles=[unit(1,'station',9,22,1500),unit(2,'rocket',11,20,1500)]
        roles[1]['level']=2
        turn=Turn.load(dict(roundNo=13,mapInfo=dict(width=41,height=32),teamOur=dict(roles=roles)))
        self.assertLess(upgrade_itinerary.priority(turn.weapons()[0]),upgrade_itinerary.priority(turn.station()))
        roles[0]['health']=500;turn=Turn.load(dict(roundNo=13,mapInfo=dict(width=41,height=32),teamOur=dict(roles=roles)))
        self.assertLess(upgrade_itinerary.priority(turn.station()),upgrade_itinerary.priority(turn.weapons()[0]))
