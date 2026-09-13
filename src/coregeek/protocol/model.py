"""判题器 payload 的**容错**解析 —— 唯一的 JSON → 领域对象 转换点。

设计见 docs/design/code-design.md §2 / §3.1。

**本模块只做两件事**：把 payload 解析成 `domain/` 的值对象，以及收集异常清单。
实体类（`Role` / `Turn` / …）本身住在 `domain/entities.py` 与 `domain/world.py`
——分层契约是 protocol 依赖 domain，不是反过来（见那两个模块的注释）。
下面把它们 re-export 出来，`model.Turn` 这类既有写法继续可用。

容错契约（明文规则）：
  1. 可选字段缺失**绝不抛异常**，一律取文档化默认值；
  2. 每回合产出一份 `anomalies` 清单（缺失字段 / 类型不符 / 未知枚举值）；
  3. **必需字段**（`roundNo`、`teamOur.roles`）缺失时返回 `None`，
     调用方据此返回合法空响应——绝不让解析异常扩散到回合执行路径。

✅实测（样例与接口文档的缺口，见 code-design.md §2.2）：
  · `playerTasks[].timeoutRounds` 样例中缺失
  · `robot.roles[].targetTeam`  样例中缺失
  · `Role.cooldown`             样例中缺失
  · `attackRange` 样例给 4/7/2147483647，文档表给 3/6/10 → **payload 优先**
  · `challengerTaskPoint2` 在 zones 中出现**两次**（占两格）→ zones 必须是
    `dict[str, tuple[Pos, ...]]`，不能是 `dict[str, Pos]`
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from ..domain.entities import (
    BUILDING_KINDS,
    CONTROLLABLE_KINDS,
    GATLING,
    MINE_KINDS,
    PIONEER,
    RAILGUN,
    ROCKET,
    STATION,
    TASK_POINT_KINDS,
    WALL,
    WEAPON_KINDS,
    WORKER,
    NativeError,
    PlayerTask,
    Robot,
    Role,
    ShopItem,
)
from ..domain.grid import NEIGHBOUR_STEPS, Pos
from ..domain.world import Turn

__all__ = [
    "load",
    "parse_pos",
    "Turn", "Role", "Robot", "PlayerTask", "ShopItem", "NativeError", "Pos",
    "NEIGHBOUR_STEPS",
    "STATION", "GATLING", "RAILGUN", "ROCKET", "WALL", "PIONEER", "WORKER",
    "BUILDING_KINDS", "WEAPON_KINDS", "CONTROLLABLE_KINDS",
    "MINE_KINDS", "TASK_POINT_KINDS",
]


# ── 容错取值原语 ─────────────────────────────────────────────────────
def _int(v: Any, default: int = 0) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return default
    return int(v)


def _str(v: Any, default: str = "") -> str:
    return v if isinstance(v, str) else default


def _bool(v: Any, default: bool = False) -> bool:
    return v if isinstance(v, bool) else default


def _seq(v: Any) -> list[Any]:
    if isinstance(v, list):
        return v
    if isinstance(v, tuple):
        return list(v)
    return []


def _map(v: Any) -> Mapping[str, Any]:
    return v if isinstance(v, dict) else {}


def parse_pos(v: Any) -> Pos | None:
    if not isinstance(v, dict):
        return None
    if "x" not in v or "y" not in v:
        return None
    return Pos(_int(v.get("x")), _int(v.get("y")))



# ── 解析 ─────────────────────────────────────────────────────────────
def _parse_role(raw: Any) -> Role | None:
    if not isinstance(raw, dict):
        return None
    pos = parse_pos(raw.get("pos"))
    rid = raw.get("id")
    if pos is None or isinstance(rid, bool) or not isinstance(rid, (int, float)):
        return None
    level = _int(raw.get("level"), 1)
    return Role(
        id=int(rid),
        pos=pos,
        role_type=_str(raw.get("roleType")),
        health=_int(raw.get("health")),
        attack_power=_int(raw.get("attackPower")),
        attack_range=_int(raw.get("attackRange")),
        capacity=_int(raw.get("backPackCapability")),
        backpack=tuple(str(x) for x in _seq(raw.get("backpack"))),
        level=level if level > 0 else 1,
        cooldown=_int(raw.get("cooldown"), 0),
        raw=raw,
    )


def _parse_robot(raw: Any) -> Robot | None:
    if not isinstance(raw, dict):
        return None
    pos = parse_pos(raw.get("pos"))
    rid = raw.get("id")
    if pos is None or isinstance(rid, bool) or not isinstance(rid, (int, float)):
        return None
    tt = raw.get("targetTeam")
    return Robot(
        id=int(rid),
        pos=pos,
        role_type=_str(raw.get("roleType")),
        health=_int(raw.get("health")),
        abnormal_state=_str(raw.get("abnormalState")),
        target_team=tt if isinstance(tt, str) else None,
    )


def _parse_task(raw: Any) -> PlayerTask | None:
    if not isinstance(raw, dict):
        return None
    pos = parse_pos(raw.get("taskPosition"))
    if pos is None:
        return None
    tr = raw.get("timeoutRounds")
    timeout = _int(tr) if isinstance(tr, (int, float)) and not isinstance(tr, bool) else None
    return PlayerTask(
        task_type=_str(raw.get("taskType")),
        pos=pos,
        cold_down_rounds=_int(raw.get("coldDownRounds")),
        score_reward=_int(raw.get("scoreReward")),
        gold_reward=_int(raw.get("goldReward")),
        is_valid=_bool(raw.get("isValid")),
        timeout_rounds=timeout,
    )


def _parse_shop(raw: Any) -> tuple[ShopItem, ...]:
    out: list[ShopItem] = []
    for item in _seq(raw):
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            out.append(ShopItem(name=item["name"], price=_int(item.get("price"))))
    return tuple(out)


def _parse_errors(raw: Any) -> tuple[NativeError, ...]:
    out: list[NativeError] = []
    for item in _seq(raw):
        if isinstance(item, dict):
            out.append(
                NativeError(code=_int(item.get("errorCode")), description=_str(item.get("description")))
            )
    return tuple(out)


def _parse_action_results(raw: Any, anomalies: list[str], known_ids: set[int]) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for k, v in _map(raw).items():
        if isinstance(v, bool):
            out[k] = v
        else:
            anomalies.append(f"lastRoundRoleActionResults[{k}] not bool -> ignored")
    unknown = set(out) - {str(i) for i in known_ids}
    if unknown and known_ids:
        anomalies.append(f"lastRoundRoleActionResults has {len(unknown)} unknown role id(s)")
    return out


def _parse_zones(raw: Any, anomalies: list[str]) -> tuple[dict[str, tuple[Pos, ...]], dict[Pos, str]]:
    by_kind: dict[str, list[Pos]] = {}
    at: dict[Pos, str] = {}
    for item in _seq(raw):
        if not isinstance(item, dict):
            continue
        pos = parse_pos(item.get("pos"))
        kind = _str(item.get("neutralType"))
        if pos is None or not kind:
            anomalies.append("mapInfo.zones: entry missing pos or neutralType -> skipped")
            continue
        by_kind.setdefault(kind, []).append(pos)
        if pos in at and at[pos] != kind:
            anomalies.append(f"mapInfo.zones: {pos.dump()} claimed by both {at[pos]} and {kind}")
        at[pos] = kind

    # ✅实测：任务点 2 在样例中出现两次——这是正常的"占两格"，不是数据错误
    for kind in TASK_POINT_KINDS:
        cells = by_kind.get(kind, [])
        if len(cells) > 1:
            anomalies.append(f"zones: {kind} occupies {len(cells)} cells (expected for point 2)")

    return {k: tuple(v) for k, v in by_kind.items()}, at


def load(payload: Any) -> Turn | None:
    """解析一回合。**必需字段缺失时返回 None**（调用方返回合法空响应）。

    任何情况下都不抛异常。
    """
    anomalies: list[str] = []

    if not isinstance(payload, dict):
        return None

    rn = payload.get("roundNo")
    if isinstance(rn, bool) or not isinstance(rn, (int, float)):
        return None

    team_our = _map(payload.get("teamOur"))
    raw_roles = _seq(team_our.get("roles"))
    if not raw_roles:
        # 没有我方角色 = 无法产出任何有意义的调度
        return None

    map_info = _map(payload.get("mapInfo"))
    width = _int(map_info.get("width"))
    height = _int(map_info.get("height"))
    if width <= 0 or height <= 0:
        anomalies.append("mapInfo.width/height missing or non-positive")
        width = width or 0
        height = height or 0

    zones, zone_at = _parse_zones(map_info.get("zones"), anomalies)

    our_roles = tuple(r for r in (_parse_role(x) for x in raw_roles) if r is not None)
    if len(our_roles) != len(raw_roles):
        anomalies.append(f"teamOur.roles: {len(raw_roles) - len(our_roles)} entr(ies) unparsable -> dropped")

    enemy_roles = tuple(
        r for r in (_parse_role(x) for x in _seq(_map(payload.get("teamEnemy")).get("roles"))) if r is not None
    )

    robots = tuple(
        r for r in (_parse_robot(x) for x in _seq(_map(payload.get("robot")).get("roles"))) if r is not None
    )
    if robots and all(r.target_team is None for r in robots):
        anomalies.append("robot.roles[].targetTeam absent -> intel channel degraded to HP deltas")

    raw_tasks = _seq(team_our.get("playerTasks"))
    player_tasks = tuple(t for t in (_parse_task(x) for x in raw_tasks) if t is not None)
    if player_tasks and all(t.timeout_rounds is None for t in player_tasks):
        anomalies.append("playerTasks[].timeoutRounds absent -> std rounds unknown, using defaults")

    if our_roles and any(
        r.role_type in BUILDING_KINDS and "cooldown" not in r.raw for r in our_roles
    ):
        anomalies.append("Role.cooldown absent -> defaulted to 0")

    news = _map(payload.get("worldNews"))

    return Turn(
        round_no=int(rn),
        width=width,
        height=height,
        zones=zones,
        zone_at=zone_at,
        our_type=_str(team_our.get("type")),
        team_id=_str(team_our.get("teamId")),
        team_name=_str(team_our.get("teamName")),
        gold=_int(team_our.get("goldNum")),
        total_score=_int(team_our.get("totalScore")),
        player_tasks=player_tasks,
        our_roles=our_roles,
        enemy_roles=enemy_roles,
        robots=robots,
        phase_task=_str(payload.get("phaseTask")),
        action_results=_parse_action_results(
            payload.get("lastRoundRoleActionResults"), anomalies, {r.id for r in our_roles}
        ),
        treasure_result=_int(payload.get("lastSummonTreasureResult")),
        llm_resp=_str(payload.get("llmResp")),
        cmd_result=_str(payload.get("lastCmdResult")),
        official_news=_str(news.get("officialNews")),
        folk_legends=_str(news.get("folkLegends")),
        vendor_shop=_parse_shop(payload.get("vendorShopList")),
        weapon_shop=_parse_shop(payload.get("weaponShopList")),
        errors=_parse_errors(payload.get("errors")),
        anomalies=tuple(anomalies),
    )

