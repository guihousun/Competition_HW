"""Local debug/visualisation API. NOT part of the official competition entry.

Everything here is a local extension used by web/ only:
  GET  /debug/stats      constants and level tables the viewer must not invent
  GET  /debug/rules      labelled difference list (official / gap / local guess)
  GET  /debug/scenario   seeded scenario without advancing a round
  POST /debug/step       one local round, returns the same shape as before + frame
  POST /debug/series     full local match recorded frame by frame (replay source)

The competition contract stays on the root path POST and is untouched by this
module. ``_demo`` private state never reaches the policy because every payload
handed to decide() goes through scenarios.observation() first.
"""
from __future__ import annotations

import json
import hashlib
import os
import threading
from pathlib import Path
from typing import Any

from .brain import decide
from .protocol import TOWER_RANGE_BY_LEVEL, TOWER_TYPES
from .scenarios import ROBOT_STATS, observation, scenario  # noqa: F401 (re-export)
from .simulator import frame_view, step

MAX_ROUNDS = 1300
SERIES_LIMIT = 6
ROOT = Path(__file__).resolve().parents[4]
RANGE_VALUES = {k: list(v) for k, v in TOWER_RANGE_BY_LEVEL.items()}

# Health ceilings by level, taken from the task book (R03). The viewer uses
# these only to size health bars; it never feeds them back into a decision.
HEALTH_BY_LEVEL = {
    "station": [1500, 3000, 4500],
    "gatling": [1000, 1500, 2000],
    "railgun": [1000, 1500, 2000],
    "rocket": [1000, 1500, 2000],
    "wall": [1000, 1500, 2000],
}
UNIT_HEALTH = {
    "worker": 220,
    "pioneer": 200,
    "smallRobot": ROBOT_STATS["smallRobot"][0],
    "middleRobot": ROBOT_STATS["middleRobot"][0],
    "largeRobot": ROBOT_STATS["largeRobot"][0],
    "bossRobot": ROBOT_STATS["bossRobot"][0],
}
ROBOT_ATTACK = {k: v[1] for k, v in ROBOT_STATS.items()}
ROBOT_SCORE = {k: v[2] for k, v in ROBOT_STATS.items()}

# One row per behaviour a user could otherwise mistake for an official rule.
# Sourced from docs/DEMO.md and docs/DEVELOPMENT_RULES.md; keep both in sync.
RULE_ROWS: list[dict[str, str]] = [
    {"id": "R01", "item": "HTTP POST / roleCommandMap / controllerId",
     "status": "official", "note": "与接口文档 v1.0 字段一致；调试端点与 _demo 不进入策略"},
    {"id": "R01", "item": "12 个官方动作码与字段契约",
     "status": "official", "note": "统一构造与校验；非官方字段/非法数量在发送前被拒，不制造判题器异常"},
    {"id": "R02", "item": "41×32、左下原点、切比雪夫距离、八方向移动",
     "status": "official", "note": "基地 2×2，pos 为左上角"},
    {"id": "R02", "item": "70 白天 + 60 夜晚 = 130 回合/天，共 1300 回合",
     "status": "official", "note": "第 71 回合入夜，第 131 回合黎明"},
    {"id": "R02", "item": "目标占用、同目标争抢、互换位置",
     "status": "approx", "note": "本地为顺序近似，未实现官方同时结算"},
    {"id": "R03", "item": "75 金币、1 基地 + 1 开拓者 + 2 工人、无初始武器",
     "status": "official", "note": "建造扣 25 金币，武器全局上限 3"},
    {"id": "R03", "item": "拆墙不退还石头；围墙只由工人白天建造",
     "status": "official", "note": "本地拆除已实现，refund 固定为 0"},
    {"id": "R04", "item": "火箭 10/15/全图，伤害 20+周围10，冷却 3 回合",
     "status": "official", "note": "仅夜晚、需角色在武器旁一格操控"},
    {"id": "R04", "item": "加特林 90° 锥形约束与逐发弹道",
     "status": "gap", "note": "本地只做落点判定，未实现锥形合法性与沿弹道命中最近机器人"},
    {"id": "R04", "item": "电磁炮能量穿透与按伤害扣减",
     "status": "gap", "note": "本地按落点直接结算 10/20/30，未实现穿透顺序"},
    {"id": "R04", "item": "射击先于机器人移动、伤害回合末结算",
     "status": "official", "note": "本地已按此顺序执行"},
    {"id": "R05", "item": "机器人 HP/攻击/射程/击杀分",
     "status": "official", "note": "40/60/500/800 与 5/10/20/40 按任务书表值"},
    {"id": "R04/S01", "item": "己方围墙不阻挡己方子弹",
     "status": "local", "note": "用户补充（原文“围棋”，暂按围墙理解，待确认）；当前弹道已不受己方墙阻挡"},
    {"id": "R05/S02", "item": "机器人基础数量随夜数增加",
     "status": "official", "note": "任务书 §4.7.3 与用户补充一致；具体数量公式仍是本地假设"},
    {"id": "R05/S03", "item": "固定机器人刷新点",
     "status": "approx", "note": "用户补充要求固定点；当前环带随机出生与之冲突。固定坐标/占用处理待确认，旧录像不是合规证据"},
    {"id": "R05", "item": "基础波次公式、机器人选敌与移动顺序",
     "status": "local", "note": "具体算法未给出；压力档不代表官方难度"},
    {"id": "R06", "item": "小贩卖出 / 商店购买（价格读观测）",
     "status": "official", "note": "需周围一格；金币或背包不足则失败；价格随新闻变动，本地不写死"},
    {"id": "R06", "item": "升级券 Lv1→2 / Lv2→3（武器/围墙/基地）",
     "status": "official", "note": "需站在目标建筑一格内并类型与等级匹配；失败不消耗券，成功后回满血"},
    {"id": "R06", "item": "修墙包 / 生命药剂 / 眩晕法宝 / 范围炸弹",
     "status": "official", "note": "法宝与炸弹仅对双方机器人有效，结算先于机器人移动；眩晕 5 回合"},
    {"id": "R06", "item": "机器人召唤令叠加到对方下一夜",
     "status": "official", "note": "每天最多 10 张；本地已实现转移与上限"},
    {"id": "R02", "item": "建造区域（蓝/黄）几何",
     "status": "local", "note": "请求无区域字段；一圈武器两圈墙为本地假设（D01）"},
    {"id": "R03", "item": "复活落点、矿点刷新位置",
     "status": "local", "note": "复活点在基地附近找空格；刷新点随机（D07）"},
    {"id": "R07", "item": "任务与 LLM 请求/回执通道",
     "status": "local", "note": "官方 prompt/executeCmd 保留；本地显式启用 DeepSeek 可跑通任务。任务内容及判题仍是本地夹具"},
    {"id": "R07", "item": "官方隔离沙盒与真实任务判题",
     "status": "gap", "note": "宿主机不执行 executeCmd；本地返回明确的未接入错误；公司平台尚未测试"},
    {"id": "R07", "item": "召唤宝藏（献祭物品与祭坛条件）",
     "status": "gap", "note": "未实现；不猜测宝藏地点与开启条件"},
    {"id": "R08", "item": "官方总分与胜负（双队、换边、5 次异常终局）",
     "status": "gap", "note": "本地只统计单队生存分+击杀分，不代表官方成绩"},
    {"id": "R08", "item": "生存分 10×天数×基地存活系数",
     "status": "approx", "note": "本地按 130 回合整点累计 10×天数"},
]

_LOCK = threading.Lock()
_SERIES_CACHE: dict[str, dict[str, Any]] = {}


def stats_payload() -> dict[str, Any]:
    return {
        "mapWidth": 41,
        "mapHeight": 32,
        "dayRounds": 70,
        "nightRounds": 60,
        "roundsPerDay": 130,
        "maxRounds": MAX_ROUNDS,
        "weaponCost": 25,
        "towerLimit": 3,
        "towerTypes": list(TOWER_TYPES),
        "towerRangeByLevel": RANGE_VALUES,
        "healthByLevel": HEALTH_BY_LEVEL,
        "unitHealth": UNIT_HEALTH,
        "robotAttack": ROBOT_ATTACK,
        "robotScore": ROBOT_SCORE,
        "side": ["challenger", "defender"],
        "pressure": [1, 2, 3],
    }


def scenario_payload(seed: Any, side: Any, pressure: Any) -> dict[str, Any]:
    state = scenario(seed, side, pressure)
    return {"state": _viewer_state(state), "view": frame_view(state),
            "metadata": recording_metadata()}


def step_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """One local round. ``_commands`` (local debug only) injects a command map.

    Without ``_commands`` the strategy decides, exactly like the competition
    path. With it, a debugger can verify how a specific official action resolves
    locally without writing a throwaway strategy.
    """
    from . import local_llm
    state, pending = local_llm.before_step(payload)
    if pending:
        return {'pending': True, 'state': state, 'done': False, 'roleCommandMap': {}}
    commands = None
    if isinstance(payload.get('_commands'), dict):
        state = {key: value for key, value in state.items() if key != '_commands'}
        commands = {str(key): dict(value)
                    for key, value in payload['_commands'].items()}
    # Any within-round signal from the previous call must not be treated as input:
    # the browser feeds this state straight back, and a stale claim would suppress
    # planning for the rest of the match (see `_viewer_state`).
    state.pop('_treasureRound', None)
    result = local_llm.after_step(step(state, commands))
    # The response carries the state twice (directly and inside the frame); make
    # both JSON-safe so a live planner object can never break the endpoint.
    safe = dict(result)
    safe['state'] = _viewer_state(result['state'])
    if isinstance(safe.get('frame'), dict):
        frame = dict(safe['frame'])
        if isinstance(frame.get('view'), dict):
            frame['view'] = _json_safe(frame['view'])
        safe['frame'] = frame
    return safe


def llm_scenario_payload(seed=1, side='challenger'):
    """One explicit local LLM fixture; never used by ordinary benchmarks."""
    import uuid
    from .protocol import Pos, distance
    state = scenario(seed, side, 1)
    meta = state['_demo']
    meta.update(llm_enabled=True, llm_run_id=uuid.uuid4().hex)
    meta['task_world']['llm_demo_once'] = True
    point = next(z for z in state['mapInfo']['zones'] if z['neutralType'] == side + 'TaskPoint1')
    from .scenarios import free_cells
    pioneer = next(u for u in state['teamOur']['roles'] if u['roleType'] == 'pioneer')
    spot = min(free_cells(state), key=lambda p: distance(p, Pos.load(point['pos'])))
    pioneer['pos'] = spot.dump()
    return {'state': _viewer_state(state), 'view': frame_view(state), 'metadata': recording_metadata()}


def _viewer_state(state: dict[str, Any]) -> dict[str, Any]:
    """Copy a state for transport: JSON-safe, private bookkeeping summarised.

    The simulator keeps live objects (the planner, the render ledger) under
    ``_demo``. They must never reach the wire as objects, because the whole state
    is serialised into every debug response — but the viewer does want to *see*
    the planner's verdicts, so the live object is replaced by a small summary
    instead of being dropped.
    """
    trimmed = _json_safe(state)
    meta = trimmed.get('_demo')
    if isinstance(meta, dict):
        planner = meta.get('planner')
        if planner is not None and not isinstance(planner, dict):
            # Hand back the *whole* memory, not a summary: the debug page posts this
            # state back on the next step, and a lossy summary made every round start
            # from a fresh planner — so the viewer ran a memoryless strategy while
            # the judge path kept its task cycles, cooldowns and LLM quota.
            meta['planner'] = planner.dump() if hasattr(planner, 'dump') else None
        meta.pop('vis_prev', None)
        meta['vis'] = {'seq': (meta.get('vis') or {}).get('seq', 0)}
        # Lifecycle state the local simulator needs (mine depletion, pending
        # mine refreshes, revive schedule) is kept: the browser sends this state
        # straight back to /debug/step, so dropping it would change the match.
        # Only the render identity ledger is trimmed, and frame records already
        # carry the identities the viewer animates with.
    engine = trimmed.get('_engine')
    if isinstance(engine, dict):
        trimmed['_engine'] = {key: value for key, value in engine.items()
                              if key != 'lastCommands'}
    # `_treasureRound` is a within-round signal ("the treasure itinerary owns the
    # pioneer this round"), not durable state. Leaving it in the response meant the
    # browser sent it straight back every round, so every later round believed the
    # treasure still owned the pioneer and the task walk was suppressed for the rest
    # of the match — the treasure stopped opening in the viewer while it opened fine
    # in the in-process run.
    trimmed.pop('_treasureRound', None)
    return trimmed


def _json_safe(value: Any, depth: int = 0) -> Any:
    """Recursively convert a state into JSON-safe values.

    Live objects that know how to serialise themselves (the planner's memory) are
    asked for their ``dump()`` instead of being turned into a placeholder string:
    the debug page posts this state straight back, so a stringified planner silently
    became an empty one on the next round and the viewer ran a different strategy
    from the judge path. Anything else becomes a readable placeholder rather than
    killing the whole response — a debug endpoint must explain a failure, not fail.
    """
    if depth > 14:
        return '<deep>'
    if isinstance(value, dict):
        return {str(key): _json_safe(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth + 1) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    dump = getattr(value, 'dump', None)
    if callable(dump):
        try:
            return _json_safe(dump(), depth + 1)
        except Exception:  # a broken dumper must not break the debug path
            pass
    return f'<{type(value).__name__}>'


def series_payload(seed: Any, side: Any, pressure: Any, limit: Any = None,
                   *, progress=None, cancelled=None) -> dict[str, Any]:
    """Record a full local match: initial state, per-round frames, final state.

    This is a replay source, not a second simulator: every frame comes from the
    same step() the live path uses, and the initial state is the seeded scenario
    the live path starts from.
    """
    initial = scenario(seed, side, pressure)
    key = json.dumps(
        [initial['_demo']['seed'], side, str(pressure), limit], sort_keys=True,
        ensure_ascii=False,
    )
    with _LOCK:
        cached = _SERIES_CACHE.get(key)
    if cached is not None and progress is None and cancelled is None:
        return cached
    stop = MAX_ROUNDS if limit in (None, '', 0) else max(1, min(int(limit), MAX_ROUNDS))
    state = initial
    frames: list[dict[str, Any]] = []
    states: list[dict[str, Any]] = [_viewer_state(initial)]
    done = False
    metadata = recording_metadata()
    while True:
        if cancelled is not None and cancelled():
            break
        round_no = int(state['roundNo'])
        result = step(state)
        frame = result['frame']
        frame['commands'] = result['roleCommandMap']
        frame['executed'] = result['executed']
        frames.append(frame)
        state = result['state']
        # states[i] is the committed state after frames[i]; states[0] is the
        # initial layout. The viewer needs both to animate honestly.
        states.append(_viewer_state(state))
        done = bool(result['done'])
        if progress is not None:
            progress(len(frames), stop)
        if result['done'] or round_no >= stop:
            break
    payload = {
        "seed": initial['_demo']['seed'],
        "side": initial['teamOur']['type'],
        "pressure": initial['_demo']['pressure'],
        "initial": states[0],
        "frames": frames,
        "states": states,
        "final": states[-1],
        "done": done,
        "rounds": len(frames),
        "metadata": metadata,
        "note": "本地可复现记录：同一 step() 逐回合生成，重放不等于官方回放。",
    }
    if progress is None and cancelled is None:
        with _LOCK:
            _SERIES_CACHE[key] = payload
            while len(_SERIES_CACHE) > SERIES_LIMIT:
                _SERIES_CACHE.pop(next(iter(_SERIES_CACHE)))
    return payload


def recording_metadata() -> dict[str, Any]:
    """Source evidence for local recordings; no claims of official certification."""
    root = Path(__file__).resolve().parents[4]
    files = sorted(Path(__file__).parent.glob('*.py'))
    files += [root / 'docs/任务书.md', root / 'docs/接口文档.md']
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(root)).replace('\\', '/').encode('utf-8'))
        digest.update(b'\0')
        digest.update(path.read_bytes())
    return {'sourceSha256': digest.hexdigest(), 'rulesBaseline': 'v1.0 / 2026-09-09',
            'local': True, 'scope': 'single-team-local',
            'limitations': ['单队本地结算，不是官方双队胜负', '建造区域、波次、任务夹具与弹道细节含本地假设']}


def load_asset(web_root: str, path: str) -> tuple[bytes, str] | None:
    """Serve a file from web/ only, refusing traversal outside it."""
    root = os.path.realpath(web_root)
    target = os.path.realpath(root if path in ('', '/') else os.path.join(root, path.lstrip('/')))
    if not os.path.isdir(root):
        return None
    if target != root and not target.startswith(root + os.sep):
        return None
    if not os.path.isfile(target):
        return None
    return _read(target)


def _read(target: str) -> tuple[bytes, str]:
    with open(target, 'rb') as handle:
        body = handle.read()
    return body, _content_type(target)


ASSET_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.webp': 'image/webp',
    '.ico': 'image/x-icon',
    '.woff2': 'font/woff2',
}


def _content_type(target: str) -> str:
    # Prevent a stale cached bundle from hiding a freshly served change.
    return ASSET_TYPES.get(os.path.splitext(target)[1].lower(), 'application/octet-stream')


def mismatch_notes() -> list[str]:
    """Short, human-readable warnings rendered in the viewer."""
    return [
        "本地模拟：不是官方判题器，分数/压力档不是官方成绩或难度。",
        "未覆盖：官方隔离沙盒、真实任务判题与完整官方双队胜负。真实 DeepSeek 仅在显式启用的本地场景调用。",
        "本地假设：建造区域几何、具体波次数量公式、机器人选敌与移动顺序。",
        "非合规近似：当前环带随机出生与用户补充的固定刷新点要求冲突；具体固定坐标待确认（S03）。",
        "预览指令与已执行结果分开显示；箭头预览不代表一定执行成功。",
    ]


SHOT_DIR = "reports/screenshots"


def save_screenshot(name: Any, data_url: Any) -> dict[str, Any]:
    """Local acceptance aid: store a browser capture under reports/screenshots.

    Accepts a PNG data URL from the page. Refuses anything that is not a PNG
    data URL and never leaves the screenshot directory, so this cannot be used
    as a general file writer.
    """
    if not isinstance(name, str) or not isinstance(data_url, str):
        raise ValueError("need {name, dataUrl}")
    safe = "".join(c for c in name if c.isalnum() or c in "-_.")[:80]
    if not safe.lower().endswith(".png"):
        safe += ".png"
    head = "data:image/png;base64,"
    if not data_url.startswith(head):
        raise ValueError("only PNG data URLs are accepted")
    import base64

    raw = base64.b64decode(data_url[len(head):], validate=True)
    if len(raw) > 12 * 1024 * 1024:
        raise ValueError("screenshot too large")
    root = os.path.realpath(os.path.join(str(ROOT), SHOT_DIR))
    os.makedirs(root, exist_ok=True)
    target = os.path.join(root, safe)
    with open(target, "wb") as handle:
        handle.write(raw)
    return {"saved": os.path.relpath(target, str(ROOT)).replace(os.sep, "/"),
            "bytes": len(raw)}


__all__ = [
    "decide", "frame_view", "step", "observation", "scenario",
    "stats_payload", "scenario_payload", "step_payload", "series_payload",
    "load_asset", "mismatch_notes", "RULE_ROWS", "save_screenshot",
]
