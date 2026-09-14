import unittest
from copy import deepcopy
from test_baseline import ROOT
from agent.scenarios import scenario, observation, prepare_round
from agent.simulator import step
from agent.brain import decide, _gate_cells, _ring_is_sealed, _wall_order
from agent.protocol import Pos, Turn, station_footprint


class ScenarioTests(unittest.TestCase):
    def test_seed_and_hidden_state(self):
        a = scenario(7)
        self.assertEqual(a, scenario(7))
        self.assertNotEqual(a['mapInfo'], scenario(19)['mapInfo'])
        self.assertNotIn('_demo', observation(a))
        for seed in range(8):
            for side in ('challenger','defender'):
                p = scenario(seed,side)
                turn = Turn.load(p)
                self.assertEqual(len(turn.occupied_cells()),11)  # 2 bases + 3 roles

    def test_dusk_dawn_and_revive(self):
        p = scenario()
        p['roundNo'] = 71
        prepare_round(p, [])
        self.assertGreater(len(p['robot']['roles']),0)
        worker = p['teamOur']['roles'][1]
        worker['health'] = 0
        worker['backpack'] = ['stone']
        prepare_round(p, [])
        p['roundNo'] = 131
        prepare_round(p, [])
        self.assertEqual(p['robot']['roles'], [])
        p['roundNo'] = 151
        prepare_round(p, [])
        self.assertEqual(worker['health'],220)
        self.assertEqual(worker['backpack'],['stone'])

    def test_mine_exhaustion_refresh(self):
        p = scenario()
        turn = Turn.load(p)
        worker = p['teamOur']['roles'][1]
        mine = next(z for z in p['mapInfo']['zones'] if z['neutralType']=='stone')
        x,y = mine['pos']['x'],mine['pos']['y']
        spot = next({'x':x+dx,'y':y+dy} for dx,dy in [(1,0),(-1,0),(0,1),(0,-1)]
                    if 0<=x+dx<41 and 0<=y+dy<32)
        worker['pos'] = spot
        commands = {str(worker['id']):{'action':'collect','targetPos':[mine['pos']]}}
        for _ in range(10):
            p = step(p,commands)['state']
        self.assertEqual(next(u for u in p['teamOur']['roles'] if u['id']==worker['id'])['backpack'].count('stone'),10)
        self.assertEqual(sum(z['neutralType']=='stone' for z in p['mapInfo']['zones']),6)
        self.assertFalse(any(z['pos']==mine['pos'] for z in p['mapInfo']['zones']))

    def test_ids_order_and_terminal_idempotence(self):
        p = scenario(7,'defender')
        before = decide(observation(p))
        p['teamOur']['roles'].reverse()
        self.assertEqual(before,decide(observation(p)))
        p['roundNo']=1300
        r=step(p)
        self.assertTrue(r['done'])
        again=step(r['step'] if 'step' in r else r['state'])
        self.assertEqual(r['state'],again['state'])

    def test_wall_ring_always_leaves_a_gate(self):
        """A closed ring seals our own pioneer in and ends task work entirely.

        Regression: with a one-cell entrance, a robot standing in it trapped the
        pioneer for the whole match (measured: 0 tasks on several seeds).
        """
        for seed in (1, 3, 7, 19, 23):
            for side in ('challenger', 'defender'):
                p = scenario(seed, side)
                turn = Turn.load(p)
                station = turn.station()
                footprint = station_footprint(station.pos)
                xs = [pos.x for pos in footprint]
                ys = [pos.y for pos in footprint]
                gate = _gate_cells(min(xs), max(xs), min(ys), max(ys))
                order = set(_wall_order(turn))
                for cell in gate:
                    self.assertNotIn(cell, order,
                                     f'seed {seed}/{side}: 大门格 {cell} 不应在围墙清单中')
                self.assertGreaterEqual(len(order), 12,
                                        '围墙环带仍应围住基地（本地球形假设）')

    def test_pioneer_is_never_sealed_in_during_a_full_match(self):
        p = scenario(19, 'challenger')
        sealed_rounds = 0
        for _ in range(400):
            result = step(p)
            p = result['state']
            turn = Turn.load(p)
            if turn.pioneer() is not None and _ring_is_sealed(turn):
                sealed_rounds += 1
        self.assertEqual(sealed_rounds, 0, '开拓者不应被自己的围墙困死')
