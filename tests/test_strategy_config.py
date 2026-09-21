"""Configuration input/identity boundaries independent of game outcomes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))
from agent import strategy_config as sc


class StrategyConfigTests(unittest.TestCase):
    def setUp(self):
        self.before = (sc._cache, sc._identity)
        sc._cache = sc._identity = None
        self.env = patch.dict(os.environ)
        self.env.start()
        os.environ.pop("COMPETITION_HW_STRATEGY_FILE", None)
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "strategy.json"
        self.path.write_text(json.dumps(sc.DEFAULTS), encoding="utf-8")

    def tearDown(self):
        sc._cache, sc._identity = self.before
        self.env.stop()
        self.temp.cleanup()

    def test_checked_in_config_matches_explicit_defaults(self):
        value, receipt = sc.load(ROOT / "strategy.json")
        self.assertEqual(value, sc.DEFAULTS)
        self.assertTrue(receipt["loaded"])
        self.assertEqual(len(receipt["sha256"]), 64)

    def test_known_single_operator_loadout(self):
        value = sc.validate(sc.DEFAULTS)
        self.assertEqual(value["defense"]["tower_loadout"], ["rocket", "railgun", "rocket"])
        self.assertEqual(value["defense"]["full_defense_from_day"], 4)

    def test_unknown_and_missing_fields_are_errors(self):
        for section in (None, "defense", "upgrades", "maintenance", "economy", "nightwork", "navigation"):
            for missing in (False, True):
                value = sc.validate(sc.DEFAULTS)
                target = value if section is None else value[section]
                if missing:
                    target.pop(next(iter(target)))
                else:
                    target["typo"] = 1
                with self.subTest(section=section, missing=missing), self.assertRaises(ValueError):
                    sc.validate(value)

    def test_boolean_is_not_number(self):
        for section, key in (("defense", "full_defense_from_day"), ("maintenance", "entry_fraction"),
                             ("economy", "stone_batch")):
            value = sc.validate(sc.DEFAULTS)
            value[section][key] = True
            with self.subTest(key=key), self.assertRaises(ValueError):
                sc.validate(value)

    def test_nonfinite_fractions_and_hysteresis_rejected(self):
        for fraction in (float("nan"), float("inf"), -0.1, 1.1, 0.9):
            value = sc.validate(sc.DEFAULTS)
            value["maintenance"]["entry_fraction"] = fraction
            with self.subTest(fraction=fraction), self.assertRaises(ValueError):
                sc.validate(value)

    def test_target_downgrade_and_mixed_shared_allowed(self):
        value = sc.validate(sc.DEFAULTS)
        value["upgrades"]["late_weapon_target"] = [1, 1, 1]
        with self.assertRaises(ValueError):
            sc.validate(value)
        value = sc.validate(sc.DEFAULTS)
        value["defense"]["tower_loadout"][1] = "railgun"
        self.assertEqual(sc.validate(value), value)
        value["defense"]["single_operator_three_rockets"] = False
        self.assertEqual(sc.validate(value), value)

    def test_invalid_existing_or_explicit_missing_never_falls_back(self):
        self.path.write_text("{bad", encoding="utf-8")
        with self.assertRaises(ValueError):
            sc.load(self.path)
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            sc.load(self.path)

    def test_missing_default_has_visible_fallback(self):
        with patch.object(sc, "_default_paths", return_value=[]):
            value, receipt = sc.load()
        self.assertEqual(value, sc.DEFAULTS)
        self.assertEqual(receipt["source"], "fallback/no_file")
        self.assertFalse(receipt["loaded"])

    def test_duplicate_json_keys_rejected(self):
        self.path.write_text('{"name":"x", "name":"y"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            sc.load(self.path)

    def test_cache_restart_contract_and_defensive_copies(self):
        os.environ["COMPETITION_HW_STRATEGY_FILE"] = str(self.path)
        initial = sc.initialize()
        changed = sc.get()
        changed["name"] = "edited"
        self.path.write_text(json.dumps(changed), encoding="utf-8")
        self.assertEqual(sc.get()["name"], "user_phase_v1")
        self.assertEqual(sc.identity(), initial)
        sc._cache = sc._identity = None
        self.assertEqual(sc.get()["name"], "edited")
        self.assertNotEqual(sc.identity()["sha256"], initial["sha256"])

    def test_bom_and_empty_explicit_path(self):
        self.path.write_text(json.dumps(sc.DEFAULTS), encoding="utf-8-sig")
        self.assertEqual(sc.load(self.path)[0], sc.DEFAULTS)
        os.environ["COMPETITION_HW_STRATEGY_FILE"] = ""
        with self.assertRaises(ValueError):
            sc.load()

    def test_validator_cli_real_success_and_failure(self):
        command = [sys.executable, str(ROOT / "tools/validate_strategy.py"), str(self.path)]
        completed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["valid"])
        self.path.write_text("[]", encoding="utf-8")
        completed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("INVALID", completed.stderr)

    def _activate(self, config):
        self.path.write_text(json.dumps(config), encoding="utf-8")
        os.environ["COMPETITION_HW_STRATEGY_FILE"] = str(self.path)
        sc._cache = sc._identity = None
        sc.initialize()

    @staticmethod
    def _weapon_observation(round_no, levels=(1, 1, 1)):
        # Public list deliberately disagrees with spatial ordering and IDs.
        roles = [dict(id=uid, roleType="rocket", pos=dict(x=x, y=y),
                      health=1000, level=level, backpack=[])
                 for uid, x, y, level in ((90, 3, 9, levels[0]),
                                          (30, 3, 10, levels[1]),
                                          (10, 4, 10, levels[2]))]
        return dict(roundNo=round_no,
                    mapInfo=dict(width=41, height=32, zones=[
                        dict(pos=dict(x=20, y=20), neutralType="weaponShop")]),
                    teamOur=dict(roles=[roles[2], roles[0], roles[1]], goldNum=100),
                    teamEnemy=dict(roles=[]), robot=dict(roles=[]),
                    weaponShopList=[dict(name="WeaponUpgradeVoucher1", price=37),
                                    dict(name="WeaponUpgradeVoucher2", price=61)])

    def test_runtime_day_targets_and_actual_shop_reserve(self):
        from agent import upgrade_itinerary as ui
        from agent.protocol import Turn
        self._activate(sc.validate(sc.DEFAULTS))
        for round_no, expected in ((1, (3, 3, 3)), (130, (3, 3, 3)),
                                   (131, (3, 3, 3)), (261, (3, 3, 3)),
                                   (391, (3, 3, 3))):
            with self.subTest(round_no=round_no):
                payload = self._weapon_observation(round_no)
                turn = Turn.load(payload)
                by_id = {g.unit_id: g for g in turn.weapons()}
                actual = tuple(ui.target_level(turn, by_id[uid]) for uid in (90, 30, 10))
                self.assertEqual(actual, expected)
                reserve = ui.weapon_reserve(turn, payload)
                if max(expected) == 1:
                    self.assertIsNone(reserve)
                    self.assertFalse(ui.purchase_allowed(turn, payload, "WeaponUpgradeVoucher1", {}))
                else:
                    self.assertEqual(reserve["voucher"], "WeaponUpgradeVoucher1")
                    self.assertEqual(reserve["gold"], 37)
                    self.assertIn(reserve["building"],
                                  [uid for uid, target in zip((90, 30, 10), expected) if target > 1])
                    self.assertTrue(ui.purchase_allowed(turn, payload, "WeaponUpgradeVoucher1", {}))
                # Once observed weapon levels meet the day target, no new
                # upgrade budget may be reserved just because gold is available.
                at_target = self._weapon_observation(round_no, expected)
                capped = Turn.load(at_target)
                self.assertIsNone(ui.weapon_reserve(capped, at_target))
                self.assertFalse(ui.purchase_allowed(capped, at_target, "WeaponUpgradeVoucher2", {}))

    def test_runtime_custom_targets_and_disabled_rollback(self):
        from agent import upgrade_itinerary as ui
        from agent.protocol import Turn
        config = sc.validate(sc.DEFAULTS)
        config["upgrades"]["day_targets"][0] = [1, 1, 1]
        config["upgrades"]["day_targets"][1] = [1, 2, 1]
        config["upgrades"]["late_weapon_target"] = [3, 3, 3]
        self._activate(config)
        payload = self._weapon_observation(131)
        turn = Turn.load(payload)
        self.assertEqual(ui.weapon_reserve(turn, payload)["building"], 30)
        later = self._weapon_observation(391, (2, 2, 2))
        self.assertEqual(ui.weapon_reserve(Turn.load(later), later)["voucher"], "WeaponUpgradeVoucher2")
        config["enabled"] = False
        self._activate(config)
        first = self._weapon_observation(1)
        turn = Turn.load(first)
        self.assertEqual([ui.target_level(turn, g) for g in turn.weapons()], [3, 3, 3])
        self.assertEqual(ui.weapon_reserve(turn, first)["gold"], 37)
        self.assertFalse(sc.identity()["enabled"])

    def test_runtime_reserve_requires_observed_shop_and_usable_quote(self):
        from agent import upgrade_itinerary as ui
        from agent.protocol import Turn
        self._activate(sc.validate(sc.DEFAULTS))
        payload = self._weapon_observation(261)
        payload["mapInfo"]["zones"] = []
        self.assertIsNone(ui.weapon_reserve(Turn.load(payload), payload))
        payload = self._weapon_observation(261)
        payload["weaponShopList"] = []
        self.assertIsNone(ui.weapon_reserve(Turn.load(payload), payload))

    def test_runtime_after_clear_switch_and_day_four_rollback(self):
        from agent import nightwork
        from agent.protocol import Turn
        config = sc.validate(sc.DEFAULTS)
        payload = self._weapon_observation(462)  # Day four, second night round.
        turn = Turn.load(payload)
        self._activate(config)
        self.assertTrue(nightwork.field_clear(turn, payload))
        config["nightwork"]["allow_after_clear"] = False
        self._activate(config)
        self.assertFalse(nightwork.field_clear(turn, payload))
        config["enabled"] = False
        config["nightwork"]["allow_after_clear"] = True
        self._activate(config)
        self.assertFalse(nightwork.field_clear(turn, payload))

    def test_stock_target_bound_matches_implemented_capacity(self):
        config = sc.validate(sc.DEFAULTS)
        config["maintenance"]["stock_target"] = 10
        self.assertEqual(sc.validate(config)["maintenance"]["stock_target"], 10)
        config["maintenance"]["stock_target"] = 11
        with self.assertRaises(ValueError):
            sc.validate(config)


if __name__ == "__main__":
    unittest.main()
