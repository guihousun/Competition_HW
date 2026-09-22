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
    "maintenance": {"from_day": 3, "survival_reserve_from_day": 3,
                    "stock_target": 2, "repair_stock_max": 4,
                    "wall_upgrade_reserve": 1, "side_wall_max_level": 1,
                    "entry_fraction": 0.55, "exit_fraction": 0.85,
                    "emergency_fraction": 0.25, "base_emergency_fraction": 0.4},
    "economy": {"dynamic_return": True, "return_margin_early": 4,
                "return_margin_late": 6, "return_margin_damaged": 8,
                "return_day_index": 55, "upgrade_return_margin": 3,
                "stone_batch": 10, "metal_batch": 10, "ore_max_distance": 8},
    "nightwork": {"allow_after_clear": True, "quiet_rounds": 3},
    "navigation": {"enabled": True, "stall_rounds": 3,
                   "allow_wall_removal": True, "max_openings": 2},
    "llm": {"task_prompt_prefix": "你在协助一个自动化参赛程序回答游戏内的任务。",
            "answer_instruction": "请按题目要求作答，只输出答案本身；多个字段用 '字段=值' 并以 '; ' 分隔。",
            "include_recent_history": 4},
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
    for section in ("defense", "upgrades", "maintenance", "economy", "nightwork", "navigation", "llm"):
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
    _integer(maintenance["survival_reserve_from_day"], 1, 10, "maintenance.survival_reserve_from_day")
    _integer(maintenance["stock_target"], 0, 10, "maintenance.stock_target")
    _integer(maintenance["repair_stock_max"], 0, 10, "maintenance.repair_stock_max")
    _integer(maintenance["wall_upgrade_reserve"], 0, 2, "maintenance.wall_upgrade_reserve")
    _integer(maintenance["side_wall_max_level"], 1, 3, "maintenance.side_wall_max_level")
    for key in ("entry_fraction", "exit_fraction", "emergency_fraction", "base_emergency_fraction"):
        value = maintenance[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"maintenance.{key}: expected finite fraction in [0, 1]")
    if not maintenance["emergency_fraction"] <= maintenance["entry_fraction"] < maintenance["exit_fraction"]:
        raise ValueError("maintenance: require emergency_fraction <= entry_fraction < exit_fraction")
    for key, low, high in (("return_day_index", 0, 69), ("upgrade_return_margin", 0, 69),
                           ("stone_batch", 1, 100), ("metal_batch", 1, 100), ("ore_max_distance", 1, 40)):
        _integer(config["economy"][key], low, high, f"economy.{key}")
    if type(config["economy"]["dynamic_return"]) is not bool:
        raise ValueError("economy.dynamic_return: expected boolean")
    for key in ("return_margin_early", "return_margin_late", "return_margin_damaged"):
        _integer(config["economy"][key], 0, 20, f"economy.{key}")
    if config["economy"]["return_margin_damaged"] < max(config["economy"]["return_margin_early"], config["economy"]["return_margin_late"]):
        raise ValueError("damaged return margin must cover ordinary margins")
    if type(config["nightwork"]["allow_after_clear"]) is not bool:
        raise ValueError("nightwork.allow_after_clear: expected boolean")
    _integer(config['nightwork']['quiet_rounds'], 1, 20, 'nightwork.quiet_rounds')
    navigation = config["navigation"]
    for key in ("enabled", "allow_wall_removal"):
        if type(navigation[key]) is not bool:
            raise ValueError(f"navigation.{key}: expected boolean")
    _integer(navigation["stall_rounds"], 2, 8, "navigation.stall_rounds")
    _integer(navigation["max_openings"], 0, 6, "navigation.max_openings")
    llm = config["llm"]
    if (not isinstance(llm["task_prompt_prefix"], str) or not 1 <= len(llm["task_prompt_prefix"]) <= 1200
            or not isinstance(llm["answer_instruction"], str) or not 1 <= len(llm["answer_instruction"]) <= 800):
        raise ValueError("llm prompt text must be bounded strings")
    _integer(llm["include_recent_history"], 0, 12, "llm.include_recent_history")
    return deepcopy(config)


PUBLIC_PARAMETER_META = {
    "defense.full_defense_from_day": ("策略", "夜间全员防守的起始日；不改变白天工作。"),
    "defense.single_operator_three_rockets": ("策略", "启用一人站公共操控位轮换三座火箭。"),
    "defense.tower_loadout": ("策略", "三座武器配置；当前策略固定为三座 rocket。"),
    "upgrades.day_targets": ("策略", "第1至3天允许达到的炮台等级目标。"),
    "upgrades.late_weapon_target": ("策略", "第4天以后继续追求的炮台等级目标。"),
    "maintenance": ("策略", "维修库存、进入/退出和紧急阈值。"),
    "maintenance.survival_reserve_from_day": ("策略", "从第几天开始先为下一夜维修包和墙券预留金币。"),
    "maintenance.wall_upgrade_reserve": ("策略", "每个采购窗口预留的前六墙升级券数量；侧墙不升级。"),
    "maintenance.side_wall_max_level": ("策略", "侧面/非前六墙最高等级；默认保持一级。"),
    "economy": ("策略", "采矿批量、采购回程和入夜安全余量。"),
    "nightwork": ("策略", "确认清场后夜间恢复工作的门槛。"),
    "navigation": ("策略", "停滞恢复和非前六墙拆除通路。"),
    "llm.task_prompt_prefix": ("可调提示词", "任务 Agent 的前置背景；不能改变官方动作和答案格式。"),
    "llm.answer_instruction": ("可调提示词", "要求模型输出答案的提示；官方判题格式仍不可改。"),
    "llm.include_recent_history": ("可调提示词", "提供给模型的最近操作摘要条数。"),
}


def public_view():
    """Safe, UI-facing config view; official constants are deliberately absent."""
    config = get()
    rows = []
    for key, (kind, description) in PUBLIC_PARAMETER_META.items():
        value = config
        for part in key.split('.'):
            value = value[part]
        rows.append({'key': key, 'value': value, 'kind': kind, 'description': description})
    return {'schema': 'competition-strategy-view/1', 'identity': identity(),
            'config': config, 'parameters': rows,
            'immutable': ['官方协议/动作字段', '武器射程、伤害、冷却与价格', '昼夜长度、视野、任务机会和计分'],
            'simulator': {'editable_via': '/debug/scenario?seed=&side=&pressure=&profile=&map_layout=',
                          'note': '模拟器压力、地图档和波次档仅用于本地实验，不进入正式策略。'}}


def save(config):
    """Validate and atomically save a user strategy config for local debug UI."""
    global _cache, _identity
    validated = validate(config)
    current = identity()
    selected = Path(current['path']) if current.get('loaded') else next(
        (path for path in _default_paths() if path.parent.exists()), _default_paths()[-1])
    selected.parent.mkdir(parents=True, exist_ok=True)
    temporary = selected.with_name(selected.name + '.tmp')
    temporary.write_text(json.dumps(validated, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, selected)
    with _lock:
        _cache, _identity = load(selected)
    return identity()


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
