"""Hand-priced spending boundaries; priority must survive affordability filtering."""
import unittest
from test_issue27_economy import board, unit, WV, AV, SV
from agent import brain, upgrade_itinerary as upgrade, nightwork
from agent.protocol import Turn


class WeaponBudgetTests(unittest.TestCase):
    def wall_board(self, gold=99):
        p=board();p['teamOur']['goldNum']=gold
        p['teamOur']['roles'].append(unit(30,'wall',3,6))
        return p

    def test_save_instead_of_buying_cheap_wall_and_begin_weapon_trip_at_100(self):
        p=self.wall_board()
        proposal,report=upgrade.plan(Turn.load(p),p,{})
        self.assertIsNone(proposal)
        self.assertEqual(report['reason'],'saving_for_weapon_upgrade')
        self.assertEqual(report['weapon_reserve']['gold'],100)
        self.assertFalse(brain._should_buy(Turn.load(p),p))
        p['teamOur']['goldNum']=100
        proposal,report=upgrade.plan(Turn.load(p),p,{})
        self.assertIsNotNone(proposal);self.assertEqual(report['voucher'],WV)

    def test_actual_price_and_previous_purchase_reduce_spare_budget(self):
        p=self.wall_board(92);p['weaponShopList'][0]['price']=73
        self.assertFalse(upgrade.purchase_allowed(Turn.load(p),p,AV,{}))
        p['teamOur']['goldNum']=93
        self.assertTrue(upgrade.purchase_allowed(Turn.load(p),p,AV,{}))
        self.assertFalse(upgrade.purchase_allowed(Turn.load(p),p,AV,{13:{'action':'buy','name':AV}}))
        # Already paid for this weapon voucher: another 73 is not reserved again.
        self.assertTrue(upgrade.purchase_allowed(Turn.load(p),p,AV,{13:{'action':'buy','name':WV}}))

    def test_adjacent_shop_cannot_bypass_reserve_for_wall(self):
        p=self.wall_board();p['mapInfo']['zones']=[{'neutralType':'weaponShop','pos':{'x':4,'y':7}}]
        t=Turn.load(p);worker=next(w for w in t.workers() if w.unit_id==12)
        self.assertIsNone(brain._shop_choice(t,worker,p,{}))

    def test_night_shopping_cannot_bypass_reserve(self):
        p=self.wall_board();p['roundNo']=90
        p['mapInfo']['zones']=[{'neutralType':'weaponShop','pos':{'x':4,'y':7}}]
        t=Turn.load(p);commands=nightwork.plan(t,p,brain._tower_pairs(t))
        self.assertFalse(any(c.get('name')==AV and c['action']=='buy' for c in commands.values()))

    def test_healthy_base_waits_emergency_base_may_spend(self):
        p=self.wall_board(100);t=Turn.load(p)
        self.assertFalse(upgrade.purchase_allowed(t,p,SV,{}))
        p['teamOur']['roles'][0]['health']=500
        self.assertTrue(upgrade.purchase_allowed(Turn.load(p),p,SV,{}))
        proposal,report=upgrade.plan(Turn.load(p),p,{})
        self.assertEqual(report['voucher'],SV)

    def test_fully_upgraded_guns_release_reserve(self):
        p=self.wall_board(20)
        for r in p['teamOur']['roles']:
            if r['roleType'] in ('rocket','railgun','station'):r['level']=3
        self.assertIsNone(upgrade.weapon_reserve(Turn.load(p),p))
        self.assertTrue(upgrade.purchase_allowed(Turn.load(p),p,AV,{}))

    def test_missing_quote_does_not_invent_weapon_price(self):
        p=self.wall_board(20);p['weaponShopList']=[{'name':AV,'price':20}]
        self.assertIsNone(upgrade.weapon_reserve(Turn.load(p),p))

    def test_carried_weapon_voucher_precedes_carried_wall_voucher(self):
        p=self.wall_board()
        for r in p['teamOur']['roles']:
            if r['roleType']=='worker':r['backpack']=[AV,WV]
        proposal,report=upgrade.plan(Turn.load(p),p,{})
        self.assertEqual(report['voucher'],WV)

    def test_level_two_reserves_the_published_second_voucher(self):
        p=self.wall_board(149)
        for r in p['teamOur']['roles']:
            if r['roleType'] in ('rocket','railgun'):r['level']=2
        p['weaponShopList'].append({'name':'WeaponUpgradeVoucher2','price':150})
        self.assertEqual(upgrade.weapon_reserve(Turn.load(p),p)['gold'],150)
        self.assertFalse(upgrade.purchase_allowed(Turn.load(p),p,AV,{}))


if __name__=='__main__':unittest.main()
