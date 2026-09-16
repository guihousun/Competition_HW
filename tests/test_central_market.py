"""Independent central-map coordinates from user description + original sample."""
import unittest
from copy import deepcopy
from test_baseline import ROOT
from agent.scenarios import scenario, observation, _central_market
from agent.protocol import Pos, Turn, distance, station_footprint
from agent.taskworld import point_cells
from agent.twomatch import _build_world


def markets(state):
    return {z['neutralType']:(z['pos']['x'],z['pos']['y']) for z in state['mapInfo']['zones']
            if z['neutralType'] in ('vendor','weaponShop')}


class CentralMarketTests(unittest.TestCase):
    def test_absolute_coordinates_for_both_sides_seeds_and_wave_profiles(self):
        for seed in (1,7,19,90601):
            for side in ('challenger','defender'):
                for profile in ('observed-seven-days','local-pressure'):
                    p=scenario(seed,side,profile=profile)
                    self.assertEqual(markets(p),{'vendor':(20,16),'weaponShop':(25,20)})
                    self.assertEqual(p['_demo']['market_layout']['id'],'central-sample-v1')
                    self.assertFalse(p['_demo']['market_layout']['official_coordinates_confirmed'])

    def test_no_neutral_footprint_collides_with_markets_or_base_rings(self):
        for seed in range(50):
            for side in ('challenger','defender'):
                p=scenario(seed,side);turn=Turn.load(p);seen=set()
                bases=[u for u in turn.ours+turn.enemies if u.kind=='station']
                for zone in p['mapInfo']['zones']:
                    cells=set(point_cells(zone['neutralType'],Pos.load(zone['pos'])))
                    self.assertFalse(cells & seen,(seed,side,zone))
                    seen.update(cells)
                    for cell in cells:
                        self.assertTrue(0<=cell.x<41 and 0<=cell.y<32)
                        self.assertTrue(all(min(distance(cell,x) for x in station_footprint(b.pos))>2 for b in bases))

    def test_collision_with_second_task_cell_is_relocated_not_duplicated(self):
        p=scenario(1,market_layout='legacy-random-v0')
        task=next(z for z in p['mapInfo']['zones'] if z['neutralType']=='challengerTaskPoint2')
        task['pos']={'x':19,'y':16}
        _central_market(p)
        self.assertNotIn(Pos(20,16),point_cells(task['neutralType'],Pos.load(task['pos'])))
        self.assertEqual(markets(p),{'vendor':(20,16),'weaponShop':(25,20)})

    def test_legacy_generation_is_explicit_and_deterministic(self):
        a=scenario(1,market_layout='legacy-random-v0')
        self.assertEqual(a,scenario(1,market_layout='legacy-random-v0'))
        self.assertNotEqual(markets(a),markets(scenario(7,market_layout='legacy-random-v0')))
        self.assertEqual(a['_demo']['market_layout']['id'],'legacy-random-v0')
        with self.assertRaises(ValueError):scenario(market_layout='official-confirmed')

    def test_imported_observation_keeps_its_actual_market_coordinates(self):
        p=scenario(1,market_layout='legacy-random-v0');before=deepcopy(p)
        obs=observation(p)
        self.assertEqual(markets(obs),markets(before))
        self.assertNotIn('_demo',obs)
        self.assertEqual(p,before)

    def test_two_team_world_uses_one_shared_central_market(self):
        p=_build_world(1,1,'observed-seven-days')
        self.assertEqual(markets(p),{'vendor':(20,16),'weaponShop':(25,20)})
        self.assertEqual(p['_demo']['market_layout']['positions'],p['_mirror_demo']['market_layout']['positions'])

    def test_ui_rule_note_does_not_claim_coordinates_official(self):
        from agent.debug import RULE_ROWS
        row=next(r for r in RULE_ROWS if r['id']=='R02/R06/S05')
        self.assertEqual(row['status'],'local')
        self.assertIn('待实机核验',row['note'])
