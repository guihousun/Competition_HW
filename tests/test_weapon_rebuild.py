"""R03 / task book 4.5.1: paid level-1 replacement, still max three weapons."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'Demo/CoreGeek/src')]
from agent.scenarios import scenario
from agent.simulator import step
from benchmark import audit


KINDS=('rocket','railgun','gatling')


def board(side='challenger',gold=25):
    state=scenario(1,side)
    roles=state['teamOur']['roles'];base=roles[0];x,y=base['pos']['x'],base['pos']['y']
    prefix=10000 if side=='challenger' else 20000
    roles[1]['pos']={'x':x-1,'y':y+1}
    roles[2]['pos']={'x':x-1,'y':y-2}
    roles[3]['pos']={'x':x+2,'y':y+1}
    target={'x':x-1,'y':y}
    for uid,kind,pos,level,hp in ((prefix+40,'rocket',target,3,2000),
        (prefix+20,'gatling',{'x':x+1,'y':y+1},1,1000),
        (prefix+30,'railgun',{'x':x+2,'y':y-1},1,1000)):
        roles.append({'id':uid,'roleType':kind,'pos':dict(pos),'health':hp,'level':level,'cooldown':0})
    state['teamOur']['goldNum']=gold
    return state,str(prefix+10),target


class WeaponRebuildTests(unittest.TestCase):
    def run_build(self,state,uid,target,kind='railgun'):
        commands={uid:{'action':'build','name':kind,'targetPos':[target]}}
        return step(state,external_response={'roleCommandMap':commands}),commands

    def test_replacement_at_three_weapon_cap_on_both_sides(self):
        for side in ('challenger','defender'):
            for kind in KINDS:
                with self.subTest(side=side,kind=kind):
                    state,uid,target=board(side);original=deepcopy(state)
                    out,commands=self.run_build(state,uid,target,kind)
                    self.assertEqual([],audit(state,commands))
                    after=out['state'];self.assertTrue(after['lastRoundRoleActionResults'][uid])
                    self.assertEqual(0,after['teamOur']['goldNum'])
                    towers=[u for u in after['teamOur']['roles'] if u['roleType'] in KINDS and u['health']>0]
                    self.assertEqual(3,len(towers));self.assertEqual(3,len({u['id'] for u in towers}))
                    replacement=next(u for u in towers if u['pos']==target)
                    self.assertEqual((kind,1,1000,0),(replacement['roleType'],replacement['level'],replacement['health'],replacement['cooldown']))
                    self.assertEqual([int(uid)-10+40],out['frame']['actions'][0]['replaced'])
                    self.assertEqual(original,state,'input is immutable')

    def test_insufficient_gold_and_night_do_not_destroy_old_weapon(self):
        for gold,round_no in ((24,1),(25,71)):
            state,uid,target=board(gold=gold);state['roundNo']=round_no
            old=deepcopy([u for u in state['teamOur']['roles'] if u['roleType'] in KINDS])
            out,_=self.run_build(state,uid,target)
            self.assertFalse(out['state']['lastRoundRoleActionResults'][uid])
            self.assertEqual(gold,out['state']['teamOur']['goldNum'])
            self.assertEqual(old,[u for u in out['state']['teamOur']['roles'] if u['roleType'] in KINDS])

    def test_replacement_does_not_allow_a_fourth_weapon(self):
        state,uid,target=board(gold=75)
        target=dict(target,y=target['y']-1)
        state['teamOur']['roles'][1]['pos']={'x':target['x']-1,'y':target['y']}
        out,commands=self.run_build(state,uid,target)
        self.assertFalse(out['state']['lastRoundRoleActionResults'][uid])
        self.assertEqual(75,out['state']['teamOur']['goldNum'])
        self.assertIn('tower limit',audit(state,commands))

    def test_worker_occupancy_cannot_be_overwritten(self):
        state,uid,target=board()
        state['teamOur']['roles']=[u for u in state['teamOur']['roles'] if not(u['roleType']=='rocket' and u['pos']==target)]
        worker=next(u for u in state['teamOur']['roles'] if u['id']==10012);worker['pos']=dict(target)
        out,_=self.run_build(state,uid,target)
        self.assertFalse(out['state']['lastRoundRoleActionResults'][uid])
        self.assertEqual(25,out['state']['teamOur']['goldNum'])
        self.assertEqual(220,next(u for u in out['state']['teamOur']['roles'] if u['id']==10012)['health'])

    def test_pioneer_cannot_replace_weapon(self):
        state,uid,target=board()
        pioneer=next(u for u in state['teamOur']['roles'] if u['roleType']=='pioneer')
        pioneer['pos']={'x':target['x']-1,'y':target['y']}
        out,_=self.run_build(state,str(pioneer['id']),target)
        self.assertFalse(out['state']['lastRoundRoleActionResults'][str(pioneer['id'])])
        self.assertEqual(25,out['state']['teamOur']['goldNum'])


if __name__=='__main__':unittest.main()
