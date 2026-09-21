"""Dependency-free, startup-cached strategy settings; never game-rule overrides."""
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import threading


DEFAULTS = {
    "schema_version": 1,
    "name": "user_phase_v1",
    "enabled": True,
    "defense": {"full_defense_from_day": 4,
                "single_operator_three_rockets": True,
                "tower_loadout": ["rocket", "rocket", "rocket"]},
    "upgrades": {"day_targets": [[3, 3, 3], [3, 3, 3], [3, 3, 3]],
                 "late_weapon_target": [3, 3, 3]},
    "maintenance": {"from_day": 4, "stock_target": 2,
                    "entry_fraction": 0.55, "exit_fraction": 0.85,
                    "emergency_fraction": 0.25, "base_emergency_fraction": 0.4},
    "economy": {"return_day_index": 55, "upgrade_return_margin": 3,
                "stone_batch": 10, "metal_batch": 10, "ore_max_distance": 8},
    "nightwork": {"allow_after_clear": True},
    "navigation": {"enabled": True, "stall_rounds": 3,
                   "allow_wall_removal": True, "max_openings": 2},
}
_cache = None
_identity = None
_lock = threading.Lock()


def _keys(value, expected, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label}: expected object")
    missing, extra = set(expected) - set(value), set(value) - set(expected)
    if missing or extra:
        raise ValueError(f"{label}: missing keys {sorted(missing)}, unknown keys {sorted(extra)}")


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label}: expected integer in [{low}, {high}]")


def validate(config):
    """Validate a complete config. Return an independent copy, never fill typos."""
    _keys(config, DEFAULTS, "strategy")
    _integer(config["schema_version"], 1, 1, "schema_version")
    if not isinstance(config["name"], str) or not config["name"].strip() or len(config["name"]) > 80:
        raise ValueError("name: expected nonempty string of at most 80 characters")
    if type(config["enabled"]) is not bool:
        raise ValueError("enabled: expected boolean")
    for section in ("defense", "upgrades", "maintenance", "economy", "nightwork", "navigation"):
        _keys(config[section], DEFAULTS[section], section)
    defense = config["defense"]
    _integer(defense["full_defense_from_day"], 1, 10, "defense.full_defense_from_day")
    if type(defense["single_operator_three_rockets"]) is not bool:
        raise ValueError("defense.single_operator_three_rockets: expected boolean")
    loadout = defense["tower_loadout"]
    if not isinstance(loadout, list) or len(loadout) != 3 or any(
            not isinstance(v, str) or v not in ("rocket", "railgun", "gatling") for v in loadout):
        raise ValueError("defense.tower_loadout: expected three rocket/railgun/gatling entries")
    if defense["single_operator_three_rockets"] and loadout != ["rocket"] * 3:
        raise ValueError("single_operator_three_rockets requires three rocket entries")
    upgrades = config["upgrades"]
    targets = upgrades["day_targets"]
    if not isinstance(targets, list) or len(targets) != 3:
        raise ValueError("upgrades.day_targets: expected targets for days 1, 2 and 3")
    rows = targets + [upgrades["late_weapon_target"]]
    for index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError(f"upgrades target {index}: expected three levels")
        for level in row:
            _integer(level, 1, 3, f"upgrades target {index}")
        if index and any(level < previous for level, previous in zip(row, rows[index - 1])):
            raise ValueError("upgrades: targets must not decrease across days")
    maintenance = config["maintenance"]
    _integer(maintenance["from_day"], 1, 10, "maintenance.from_day")
    _integer(maintenance["stock_target"], 0, 10, "maintenance.stock_target")
    for key in ("entry_fraction", "exit_fraction", "emergency_fraction", "base_emergency_fraction"):
        value = maintenance[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"maintenance.{key}: expected finite fraction in [0, 1]")
    if not maintenance["emergency_fraction"] <= maintenance["entry_fraction"] < maintenance["exit_fraction"]:
        raise ValueError("maintenance: require emergency_fraction <= entry_fraction < exit_fraction")
    for key, low, high in (("return_day_index", 0, 69), ("upgrade_return_margin", 0, 69),
                           ("stone_batch", 1, 100), ("metal_batch", 1, 100), ("ore_max_distance", 1, 40)):
        _integer(config["economy"][key], low, high, f"economy.{key}")
    if type(config["nightwork"]["allow_after_clear"]) is not bool:
        raise ValueError("nightwork.allow_after_clear: expected boolean")
    navigation = config["navigation"]
    for key in ("enabled", "allow_wall_removal"):
        if type(navigation[key]) is not bool:
            raise ValueError(f"navigation.{key}: expected boolean")
    _integer(navigation["stall_rounds"], 2, 8, "navigation.stall_rounds")
    _integer(navigation["max_openings"], 0, 6, "navigation.max_openings")
    return deepcopy(config)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _default_paths():
    module = Path(__file__).resolve()
    package_root = module.parents[2]
    paths = [package_root / "strategy.json"]
    if package_root.name == "CoreGeek" and package_root.parent.name == "Demo":
        paths.append(package_root.parent.parent / "strategy.json")
    return paths


def load(path=None):
    """Read without changing runtime cache. Explicit missing paths are errors."""
    explicit = path if path is not None else os.environ.get("COMPETITION_HW_STRATEGY_FILE")
    if explicit is not None:
        if not str(explicit).strip():
            raise ValueError("COMPETITION_HW_STRATEGY_FILE must not be empty")
        selected = Path(explicit).expanduser().resolve()
    else:
        selected = next((p for p in _default_paths() if p.exists()), None)
    if selected is None:
        config = validate(DEFAULTS)
        raw = json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return config, {"path": None, "sha256": hashlib.sha256(raw).hexdigest(),
                        "name": config["name"], "loaded": False, "source": "fallback/no_file",
                        "enabled": config["enabled"]}
    raw = selected.read_bytes()
    config = validate(json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object))
    return config, {"path": str(selected), "sha256": hashlib.sha256(raw).hexdigest(),
                    "name": config["name"], "loaded": True, "source": "file",
                    "enabled": config["enabled"]}


def initialize():
    """Call before binding HTTP port; changed settings require a process restart."""
    global _cache, _identity
    with _lock:
        if _cache is None:
            _cache, _identity = load()
    return identity()


def get():
    if _cache is None:
        initialize()
    return deepcopy(_cache)


def identity():
    if _cache is None:
        initialize()
    return deepcopy(_identity)
