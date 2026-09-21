"""Independent front-six coordinates and real upgrade-planning decisions."""
import unittest
from unittest.mock import patch
from copy import deepcopy

from test_issue27_economy import board, unit, WV, AV, SV
from agent import frontline, strategy_config, upgrade_itinerary as upgrade
from agent.protocol import Turn, Pos


class FrontlineTests(unittest.TestCase):
    def geometry(self, x, y):
        payload = board()
        payload['teamOur']['roles'] = [unit(1, 'station', x, y)]
        payload['mapInfo']['zones'] = []
        return payload

    def test_blue_and_red_front_four_then_two_corners(self):
        for x, fx in ((9,12),(30,28)):
            with self.subTest(x=x):
                turn=Turn.load(self.geometry(x,22))
                front,corners=frontline.wall_groups(turn)
                self.assertEqual(front,tuple(Pos(fx,y) for y in (20,21,22,23)))
                self.assertEqual(corners,(Pos(fx,19),Pos(fx,24)))
                self.assertEqual(frontline.protected_walls(turn),set(front+corners))
                self.assertFalse(turn.walls())  # protection does not depend on construction

    def test_vertical_approaches_rotate_four_plus_two(self):
        for y,fy in ((5,7),(25,22)):
            turn=Turn.load(self.geometry(19,y))
            front,corners=frontline.wall_groups(turn)
            self.assertEqual(front,tuple(Pos(x,fy) for x in (18,19,20,21)))
            self.assertEqual(corners,(Pos(17,fy),Pos(22,fy)))

    def test_water_and_map_edges_do_not_expand_protected_group(self):
        payload=self.geometry(9,22)
        payload['mapInfo']['zones']=[dict(pos=dict(x=12,y=20),neutralType='water')]
        protected=frontline.protected_walls(Turn.load(payload))
        self.assertEqual(len(protected),5)
        self.assertNotIn(Pos(12,20),protected)
        payload=self.geometry(9,1)
        front,corners=frontline.wall_groups(Turn.load(payload))
        self.assertEqual(front,(Pos(12,0),Pos(12,1),Pos(12,2)))
        self.assertEqual(corners,(Pos(12,3),))

    def test_missing_base_has_no_invented_front(self):
        payload=self.geometry(9,22);payload['teamOur']['roles']=[]
        self.assertEqual(frontline.wall_groups(Turn.load(payload)),((),()))


class FrontlineUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.settings=deepcopy(strategy_config.DEFAULTS)
        self.config=patch.object(strategy_config,'get',return_value=self.settings)
        self.config.start();self.addCleanup(self.config.stop)

    def payload(self, day=4, gold=300, levels=(3,3,3)):
        payload=board();payload['roundNo']=(day-1)*130+13
        payload['teamOur']['goldNum']=gold
        guns=[r for r in payload['teamOur']['roles'] if r['roleType'] in ('rocket','railgun')]
        for gun,level in zip(guns,levels):gun.update(roleType='rocket',level=level)
        payload['teamOur']['roles'][0].update(level=3,health=4500)
        payload['weaponShopList'] += [dict(name='WeaponUpgradeVoucher2',price=150),
                                      dict(name='WallUpgradeVoucher2',price=30)]
        # Base(3,4): expected front4=(6,2..5), corners=(6,1),(6,6).
        # Shift fixture guns away from these wall cells.
        for gun,x,y in zip(guns,(2,2,3),(3,5,5)):gun['pos']=dict(x=x,y=y)
        return payload

    def add_wall(self,payload,uid,x,y,level=1,health=1000):
        payload['teamOur']['roles'].append(unit(uid,'wall',x,y,level,health))

    def test_navigation_schema_rejects_silent_coercion_and_unbounded_retries(self):
        for key,value in (('enabled',1),('allow_wall_removal','false'),
                          ('stall_rounds',1),('stall_rounds',9),
                          ('max_openings',-1),('max_openings',7),('max_openings',True)):
            config=deepcopy(strategy_config.DEFAULTS);config['navigation'][key]=value
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):
                strategy_config.validate(config)

    def test_all_days_allow_funded_upgrades_toward_three(self):
        for day,expected in ((1,3),(2,3),(3,3),(4,3),(10,3)):
            p=self.payload(day=day,levels=(1,1,1));t=Turn.load(p)
            self.assertEqual([upgrade.target_level(t,g) for g in t.weapons()],[expected]*3)

    def test_day_one_funded_level_three_purchase_is_not_delayed_by_calendar(self):
        p=self.payload(day=1,gold=150,levels=(2,2,2))
        proposal,report=upgrade.plan(Turn.load(p),p,{})
        self.assertIsNotNone(proposal,report)
        self.assertEqual(report['voucher'],'WeaponUpgradeVoucher2')

    def test_level_one_guns_upgrade_before_level_two_guns(self):
        p=self.payload(day=1,gold=150,levels=(2,1,2))
        proposal,report=upgrade.plan(Turn.load(p),p,{})
        self.assertIsNotNone(proposal,report)
        self.assertEqual(report['voucher'],WV)
        self.assertEqual(report['building'],3)

    def test_level_two_guns_day_four_reserve_real_level_three_price(self):
        p=self.payload(gold=149,levels=(2,2,2));self.add_wall(p,30,6,2)
        t=Turn.load(p)
        self.assertEqual(upgrade.weapon_reserve(t,p)['gold'],150)
        self.assertFalse(upgrade.purchase_allowed(t,p,AV,{}))
        proposal,report=upgrade.plan(t,p,{})
        self.assertIsNone(proposal);self.assertEqual(report['reason'],'saving_for_weapon_upgrade')
        p['teamOur']['goldNum']=150
        proposal,report=upgrade.plan(Turn.load(p),p,{})
        self.assertIsNotNone(proposal,report)
        self.assertEqual(report['voucher'],'WeaponUpgradeVoucher2')

    def test_ordinary_wall_wear_does_not_preempt_weapon_but_critical_can(self):
        p=self.payload(levels=(2,2,2));self.add_wall(p,30,6,2,health=500)
        self.assertFalse(upgrade.purchase_allowed(Turn.load(p),p,AV,{}))
        p['teamOur']['roles'][-1]['health']=250
        self.assertTrue(upgrade.purchase_allowed(Turn.load(p),p,AV,{}))

    def test_rear_wall_does_not_take_critical_front_budget_exception(self):
        p=self.payload(levels=(2,2,2));self.add_wall(p,30,1,2,health=50)
        self.assertFalse(upgrade.purchase_allowed(Turn.load(p),p,AV,{}))

    def test_critical_base_retains_emergency_exception(self):
        p=self.payload(levels=(2,2,2));p['teamOur']['roles'][0].update(level=1,health=200)
        self.assertTrue(upgrade.purchase_allowed(Turn.load(p),p,SV,{}))

    def test_second_upgrade_front_precedes_first_corner_and_rear(self):
        p=self.payload();self.add_wall(p,99,6,2,level=2,health=1500)
        self.add_wall(p,20,6,1);self.add_wall(p,10,1,2)
        proposal,report=upgrade.plan(Turn.load(p),p,{})
        self.assertIsNotNone(proposal,report)
        self.assertEqual(report['building'],99)
        self.assertEqual(report['voucher'],'WallUpgradeVoucher2')

    def test_maxed_front_four_releases_candidate_slots_for_corner(self):
        p=self.payload()
        for uid,y in enumerate((2,3,4,5),30):self.add_wall(p,uid,6,y,level=3,health=2000)
        self.add_wall(p,99,6,6);self.add_wall(p,10,1,2)
        proposal,report=upgrade.plan(Turn.load(p),p,{})
        self.assertIsNotNone(proposal,report);self.assertEqual(report['building'],99)

    def test_six_maxed_front_walls_allow_remaining_wall_upgrades(self):
        p=self.payload()
        for uid,y in enumerate(range(1,7),30):self.add_wall(p,uid,6,y,level=3,health=2000)
        self.add_wall(p,10,1,2)
        proposal,report=upgrade.plan(Turn.load(p),p,{})
        self.assertIsNotNone(proposal,report);self.assertEqual(report['building'],10)


if __name__=='__main__':unittest.main()
