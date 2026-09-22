"""Local AI liveness expectations; not official time-to-base guarantees."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

from test_robots import board
from test_joint_movement import robot
from agent.protocol import Pos, Turn
from agent.simulator import _allocate_robot_moves, _plan_robot_actions, _settle_joint_moves, step


def mirror(state):
    state=deepcopy(state)
    side='defender' if state['teamOur']['type']=='challenger' else 'challenger'
    state['teamOur']['type']=side
    for team in ('teamOur','teamEnemy','robot'):
        for unit in state.get(team,{}).get('roles',[]):
            unit['pos']['x']=40-unit['pos']['x']-int(unit['roleType']=='station')
            if team=='robot':unit['targetTeam']=side
    for zone in state['mapInfo']['zones']:
        zone['pos']['x']=40-zone['pos']['x']-int(zone['neutralType'].endswith('TaskPoint2'))
    return state


def native_frame():
    record=json.loads((Path(__file__).parent/'fixtures/native_solo_rockets_r100_public.json').read_text(encoding='utf-8'))
    encoded=json.dumps(record['observation'],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    assert hashlib.sha256(encoded).hexdigest()==record['observation_hash']
    return record['observation']


class RobotIntentProgressTests(unittest.TestCase):
    def assert_legal_step(self,before,result):
        # Independent geometry on actual receipts, not resolve_moves as oracle.
        distance=lambda a,b:max(abs(a['x']-b['x']),abs(a['y']-b['y']))
        blocked=set()
        for side in ('teamOur','teamEnemy'):
            for unit in before.get(side,{}).get('roles',[]):
                if unit['health']<=0 or unit['roleType'] in ('worker','pioneer'):continue
                x,y=unit['pos']['x'],unit['pos']['y'];blocked.add((x,y))
                if unit['roleType']=='station':blocked.update(((x+1,y),(x,y-1),(x+1,y-1)))
        for zone in before['mapInfo']['zones']:
            if zone['neutralType']=='land':continue
            x,y=zone['pos']['x'],zone['pos']['y'];blocked.add((x,y))
            if zone['neutralType'].endswith('TaskPoint2'):blocked.add((x+1,y))
        moves=result['frame']['robotMoves'];attacks=result['frame']['robotAttacks']
        self.assertEqual(len(moves),len({m['robot'] for m in moves}))
        self.assertEqual(len(attacks),len({a['robot'] for a in attacks}))
        self.assertFalse({m['robot'] for m in moves}&{a['robot'] for a in attacks})
        for move in moves:
            self.assertEqual(distance(move['from'],move['to']),1)
            self.assertNotIn((move['to']['x'],move['to']['y']),blocked)
        # Official robots are melee units and may stack on one grid cell; a
        # dense wave is not forced into a queue.  They still cannot enter a
        # living player role's cell.
        destinations=[(r['pos']['x'],r['pos']['y']) for r in result['state']['robot']['roles'] if r['health']>0]
        for role in result['state']['teamOur']['roles']:
            if role['health']>0:self.assertNotIn((role['pos']['x'],role['pos']['y']),destinations)

    def test_real_r100_twenty_rounds_and_mirror_make_progress_and_threat(self):
        for reflected in (False,True):
            state=mirror(native_frame()) if reflected else native_frame()
            start=deepcopy(state);moves=[];attacks=[]
            for _ in range(20):
                before=deepcopy(state)
                result=step(state,external_response={'roleCommandMap':{}})
                self.assert_legal_step(before,result)
                state=result['state'];moves.append(len(result['frame']['robotMoves']))
                attacks.append(len(result['frame']['robotAttacks']))
            self.assertTrue(all(n>0 for n in moves[:7]),moves)
            self.assertGreater(sum(attacks),0,attacks)
            self.assertEqual(sum(r['health']>0 for r in state['robot']['roles']),26)
            self.assertEqual([r['health'] for r in state['robot']['roles']], [r['health'] for r in start['robot']['roles']])
            old=sum(r['health'] for r in start['teamOur']['roles'] if r['roleType']=='wall')
            new=sum(r['health'] for r in state['teamOur']['roles'] if r['roleType']=='wall')
            self.assertLess(new,old)

    def test_dense_35_and_91_empty_defence_reach_and_attack_base_both_sides(self):
        # Counts are specified local pressure probes, not new official waves.
        for count in (35,91):
            for reflected in (False,True):
                state=board();state['mapInfo']['zones']=[]
                base=state['teamOur']['roles'][0]
                base['pos']={'x':7,'y':26};state['teamOur']['roles']=[base]
                state['robot']['roles']=[robot(5000+i,27+i//13,12+i%13) for i in range(count)]
                if reflected:state=mirror(state)
                moved=attacked=0;initial=deepcopy(state['robot']['roles'])
                for _ in range(40):
                    before=deepcopy(state);result=step(state,external_response={'roleCommandMap':{}})
                    self.assert_legal_step(before,result)
                    moved+=len(result['frame']['robotMoves'])
                    attacked+=sum(a.get('buildingKind')=='station' for a in result['frame']['robotAttacks'])
                    state=result['state']
                    if result['done']:break
                self.assertGreater(moved,count)
                self.assertGreater(attacked,0)
                self.assertLess(state['teamOur']['roles'][0]['health'],1500)
                self.assertEqual([(r['id'],r['health']) for r in state['robot']['roles']],[(r['id'],r['health']) for r in initial])

    def test_intent_generation_is_immutable_and_list_order_independent(self):
        state=native_frame();before=deepcopy(state)
        expected=_plan_robot_actions(state)
        self.assertEqual(state,before)
        state['robot']['roles'].reverse()
        self.assertEqual(_plan_robot_actions(state),expected)
        # Multiple robots may intentionally choose the same next cell.
        self.assertEqual(len(expected[0].values()), len(expected[0]))

    def test_equal_rank_bottleneck_priority_rotates_with_public_round(self):
        state=board();state['robot']['roles']=[robot(901,6,4),robot(902,6,3)]
        walkers=[(r,Pos(r['pos']['x'],r['pos']['y']),Pos(10,5)) for r in state['robot']['roles']]
        free={Pos(6,4),Pos(6,3),Pos(7,3)}
        obstacles={Pos(x,y) for x in range(41) for y in range(32)}-free
        winners=[]
        for n in (71,72):
            state['roundNo']=n
            moves=_allocate_robot_moves(Turn.load(state),walkers,obstacles)
            self.assertEqual(len(moves),2)
            self.assertEqual(set(moves.values()),{Pos(7,3)})
            winners.extend(moves)
        self.assertEqual(set(winners),{'901','902'})

    def test_actual_player_conflict_still_rejects_without_ai_retry(self):
        state=native_frame();moves,_=_plan_robot_actions(state)
        uid,target=next(iter(moves.items()))
        # Supply a separate legal role intent into that planned robot cell.
        # The complete joint stage must reject, not ask AI for another step.
        occupied={(r['pos']['x'],r['pos']['y']) for r in state['robot']['roles']}
        origin=next(Pos(target.x+dx,target.y+dy) for dx in (-1,0,1) for dy in (-1,0,1)
                    if (dx or dy) and (target.x+dx,target.y+dy) not in occupied
                    and 0<=target.x+dx<41 and 0<=target.y+dy<32)
        worker=next(r for r in state['teamOur']['roles'] if r['roleType']=='worker')
        worker['pos']=origin.dump();robot_origin=next(r['pos'].copy() for r in state['robot']['roles'] if str(r['id'])==uid)
        _,records,rejected=_settle_joint_moves(state,{str(worker['id']):target},moves)
        self.assertNotIn(int(uid),{r['robot'] for r in records})
        self.assertIn('robot:'+uid,{r[0] for r in rejected})
        self.assertEqual(next(r['pos'] for r in state['robot']['roles'] if str(r['id'])==uid),robot_origin)


if __name__=='__main__':unittest.main()
