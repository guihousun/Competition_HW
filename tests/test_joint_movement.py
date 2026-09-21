"""Hand-worked R02 joint collisions; AI choice and collision are separate layers."""
from copy import deepcopy
import unittest

from test_robots import board
from test_simulator_static_occupancy import unit, move
from agent.protocol import Pos
from agent.simulator import _settle_joint_moves, step


def robot(uid, x, y, **fields):
    result = unit(uid, 'smallRobot', x, y, 40)
    result.update(targetTeam='challenger', abnormalState='')
    result.update(fields)
    return result


def advance(state, commands=None):
    return step(state, external_response={'roleCommandMap': commands or {}})


def positions(state, side='robot'):
    return {u['id']: (u['pos']['x'], u['pos']['y']) for u in state[side]['roles']}


class JointIntentStageTests(unittest.TestCase):
    """No mock: exercise the same complete movement stage called by step.

    Current robot AI prefers attacking any role within 3, so cross-species
    following cannot naturally be selected by that AI. These tests supply legal
    intentions independently and do not redefine its target-selection policy.
    """
    def state(self, worker=(5, 5), bot=(6, 5)):
        state = board(robot_pos=bot)
        state['teamOur']['roles'].append(unit(601, 'worker', *worker))
        # Robot and role IDs deliberately overlap: identities must not alias.
        state['robot']['roles'] = [robot(601, *bot)]
        return state

    def test_role_follows_vacating_robot_and_reverse(self):
        for worker, bot, role_goal, robot_goal in [
                ((5,5), (6,5), (6,5), (7,5)),
                ((6,5), (5,5), (7,5), (6,5))]:
            for side in ('challenger', 'defender'):
                with self.subTest(worker=worker, side=side):
                    state = self.state(worker, bot)
                    state['teamOur']['type'] = side
                    roles, bots, rejected = _settle_joint_moves(
                        state, {'601':Pos(*role_goal)}, {'601':Pos(*robot_goal)})
                    self.assertEqual(positions(state, 'teamOur')[601], role_goal)
                    self.assertEqual(positions(state)[601], robot_goal)
                    self.assertEqual((len(roles), len(bots), rejected), (1,1,[]))

    def test_role_and_robot_contest_both_stop_without_second_choice(self):
        state = self.state(worker=(5,5), bot=(7,5))
        roles, bots, rejected = _settle_joint_moves(
            state, {'601':Pos(6,5)}, {'601':Pos(6,5)})
        self.assertEqual((roles,bots), ([],[]))
        self.assertEqual({uid for uid,_,_ in rejected}, {'601','robot:601'})
        self.assertEqual(positions(state, 'teamOur')[601], (5,5))
        self.assertEqual(positions(state)[601], (7,5))

    def test_role_robot_swap_both_stop(self):
        state = self.state()
        roles, bots, rejected = _settle_joint_moves(
            state, {'601':Pos(6,5)}, {'601':Pos(5,5)})
        self.assertEqual((roles,bots), ([],[]))
        self.assertEqual(len(rejected), 2)

    def test_stationary_robot_and_stationary_role_block_followers(self):
        for role_moves, robot_moves in [({'601':Pos(6,5)}, {}),
                                        ({}, {'601':Pos(5,5)})]:
            with self.subTest(role_moves=role_moves):
                state = self.state()
                roles, bots, rejected = _settle_joint_moves(state, role_moves, robot_moves)
                self.assertEqual((roles,bots), ([],[]))
                self.assertEqual(len(rejected), 1)

    def test_contested_robot_departure_blocks_role_follower(self):
        state = self.state()
        state['robot']['roles'].append(robot(602, 8,5))
        roles,bots,rejected = _settle_joint_moves(
            state, {'601':Pos(6,5)}, {'601':Pos(7,5), '602':Pos(7,5)})
        self.assertEqual((roles,bots), ([],[]))
        self.assertEqual(len(rejected), 3)
        self.assertEqual(positions(state), {601:(6,5),602:(8,5)})

    def test_robot_swap_and_mixed_triangle(self):
        # Unlike role-role swaps, robot-related swaps are a conservative local
        # extension: the official swap clause explicitly says "two roles".
        state = self.state()
        state['robot']['roles'].append(robot(602,7,5))
        roles,bots,rejected = _settle_joint_moves(
            state, {}, {'601':Pos(7,5), '602':Pos(6,5)})
        self.assertEqual((roles,bots,len(rejected)), ([],[],2))
        state = self.state(worker=(5,5),bot=(6,5))
        state['robot']['roles'].append(robot(602,6,6))
        roles,bots,rejected = _settle_joint_moves(
            state, {'601':Pos(6,5)}, {'601':Pos(6,6), '602':Pos(5,5)})
        self.assertEqual((len(roles),len(bots),rejected), (1,2,[]))


class RealRobotStepTests(unittest.TestCase):
    def test_two_robots_reserve_different_cells_before_settlement(self):
        for reverse in (False,True):
            state = board()
            state['robot']['roles'] = [robot(901,6,4), robot(902,6,3)]
            # Without local intent coordination both would choose (7,3).
            # Round80 rotates the equal-distance rank: 902 reserves it first,
            # 901 chooses (7,4). This is AI choice, not collision forgiveness.
            state['mapInfo']['zones'].append({'neutralType':'stone','pos':{'x':7,'y':2}})
            if reverse:
                state['robot']['roles'].reverse()
            before = deepcopy(state)
            result = advance(state)
            self.assertEqual(positions(result['state']), {901:(7,4),902:(7,3)})
            self.assertEqual(len(result['frame']['robotMoves']), 2)
            self.assertEqual(result['frame']['robotAttacks'], [])
            self.assertEqual(state, before)

    def test_explicit_two_robot_same_destination_still_stops_both(self):
        state=board()
        state['robot']['roles']=[robot(901,6,4),robot(902,6,3)]
        roles,bots,rejected=_settle_joint_moves(state,{}, {'901':Pos(7,3),'902':Pos(7,3)})
        self.assertEqual((roles,bots),([],[]))
        self.assertEqual({uid for uid,_,_ in rejected},{'robot:901','robot:902'})
        self.assertEqual(positions(state),{901:(6,4),902:(6,3)})

    def test_robot_chain_follows_vacated_cells_independent_of_list_order(self):
        for reverse in (False,True):
            state = board()
            state['robot']['roles'] = [robot(901,5,5),robot(902,6,4),robot(903,7,3)]
            if reverse:
                state['robot']['roles'].reverse()
            result = advance(state)
            self.assertEqual(positions(result['state']), {901:(6,4),902:(7,3),903:(8,2)})
            self.assertEqual(len(result['frame']['robotMoves']), 3)

    def test_known_dizzy_robot_is_routed_around_before_intent(self):
        state = board()
        state['robot']['roles'] = [robot(901,5,5),robot(902,6,4),
                                  robot(903,7,3,abnormalState='dizzy',dizzyRounds=2)]
        result = advance(state)
        self.assertEqual(positions(result['state']), {901:(6,4),902:(7,4),903:(7,3)})
        self.assertEqual(len(result['frame']['robotMoves']), 2)

    def test_dizzy_obstacle_three_step_public_detour(self):
        for reverse in (False,True):
            state = board()
            state['robot']['roles'] = [robot(901,6,5),
                                      robot(902,7,4,abnormalState='dizzy',dizzyRounds=5)]
            if reverse:
                state['robot']['roles'].reverse()
            # Empty (7,5) is one step closer to base. Then (8,4),(9,3)
            # follow the unchanged (distance,x,y) greedy tie-break.
            for expected, remaining in [((7,5),4),((8,4),3),((9,3),2)]:
                state = advance(state)['state']
                self.assertEqual(positions(state)[901], expected)
                self.assertEqual(positions(state)[902], (7,4))
                stunned = next(r for r in state['robot']['roles'] if r['id']==902)
                self.assertEqual(stunned['dizzyRounds'], remaining)

    def test_attacking_robot_is_known_stationary_when_other_robot_routes(self):
        for reverse in (False,True):
            state = board()
            state['teamOur']['roles'].append(unit(601,'worker',8,1))
            # Worker distance: 4 from901 (stays base-directed), 3 from902 (attacks).
            state['robot']['roles'] = [robot(901,6,5),robot(902,7,4)]
            if reverse:
                state['robot']['roles'].reverse()
            result = advance(state)
            # At distance four robot 901 keeps its base-directed route. Robot
            # 902 is stationary because it attacks the worker at distance 3.
            self.assertEqual(positions(result['state']), {901:(7,5),902:(7,4)})
            self.assertEqual([a['robot'] for a in result['frame']['robotAttacks']], [902])
            worker = next(r for r in result['state']['teamOur']['roles'] if r['id']==601)
            self.assertEqual(worker['health'], 215)

    def test_other_team_inactive_robot_does_not_hide_legal_detour(self):
        state = board()
        state['robot']['roles'] = [robot(901,6,5),robot(902,7,4,targetTeam='defender')]
        result = advance(state)
        self.assertEqual(positions(result['state']), {901:(7,5),902:(7,4)})

    def test_shot_uses_original_robot_position_before_joint_move(self):
        state = board(robot_pos=(4,5))
        state['teamOur']['roles'] += [unit(601,'worker',8,6),unit(801,'railgun',8,5,1000)]
        result = advance(state, {'801':{'action':'attack','controllerId':'601',
                                        'targetPos':[{'x':4,'y':5}]}})
        self.assertTrue(result['state']['lastRoundRoleActionResults']['801'])
        self.assertEqual(result['state']['robot']['roles'][0]['health'], 30)
        self.assertEqual(positions(result['state'])[307100], (5,4))

    def test_lethal_shot_still_allows_existing_end_round_move(self):
        state = board(robot_pos=(4,5))
        state['robot']['roles'][0]['health'] = 10
        state['teamOur']['roles'] += [unit(601,'worker',8,6),unit(801,'railgun',8,5,1000)]
        result = advance(state, {'801':{'action':'attack','controllerId':'601',
                                        'targetPos':[{'x':4,'y':5}]}})
        self.assertEqual(result['state']['robot']['roles'][0]['health'], 0)
        self.assertEqual(positions(result['state'])[307100], (5,4))

    def test_bomb_and_dizzy_take_effect_before_robot_intention(self):
        for item in ('Bomb','DizzyWeapon'):
            with self.subTest(item=item):
                state = board()
                caster = unit(601,'worker',20,20)
                caster['backpack'] = [item]
                state['teamOur']['roles'].append(caster)
                result = advance(state, {'601':{'action':'use','name':item,
                                                'targetPos':[{'x':6,'y':5}]}})
                self.assertTrue(result['state']['lastRoundRoleActionResults']['601'])
                self.assertEqual(result['frame']['robotMoves'], [])
                self.assertEqual(result['frame']['robotAttacks'], [])
                self.assertEqual(positions(result['state'])[307100], (6,5))

    def test_cross_attack_snapshot_is_explicit_local_timing_assumption(self):
        state = board(robot_pos=(6,5))
        state['teamOur']['roles'].append(unit(601,'worker',6,8))
        result = advance(state, {'601':move(6,9)})
        worker = next(u for u in result['state']['teamOur']['roles'] if u['id']==601)
        self.assertEqual(worker['pos'], {'x':6,'y':9})
        self.assertEqual(worker['health'], 215)
        self.assertEqual(result['frame']['robotAttacks'][0]['to'], {'x':6,'y':8})
