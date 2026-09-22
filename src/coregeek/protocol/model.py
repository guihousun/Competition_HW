"""payload → `game.world.Turn`。唯一读线上格式的地方。

容错解析：字段缺失或类型不对一律退化成默认值 / 丢掉那一条，绝不抛异常 —— 判题器是没法
调试的黑盒。整张地图一趟扫描铺出（`_entries`）；我方角色与武器混在同一张 `teamOur.roles`
里（没有 `teamOur.weapons` 这个 key），靠 `roleType` 分流。
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from ..game.grid import Pos
from ..game.map import ENEMY_PREFIX, ROBOT_PREFIX, Map
from ..game.roles import BaseRole, make
from ..game.world import WEAPON_KINDS, Error, Robot, Turn, Wall, Weapon


def load(payload: Any) -> Turn | None:
    """解析一个回合。整条 payload 不成形就返回 None。"""
    if not isinstance(payload, dict):
        return None

    return Turn(
        round_no=_int(payload.get("roundNo")),
        map=Map(_size(payload), _entries(payload)),
        # 建筑不在 characters 里（见 roles.make）——它们在地图网格里，不在这儿
        roles=tuple(
            c
            for c in (_character(n) for n in _items(payload, "teamOur", "roles"))
            if c
        ),
        gold=_gold(payload),
        weapons=_weapons(payload),
        robots=_robots(payload),
        task_points=_tasks(payload),
        cooling_tasks=_cooling_tasks(payload),
        tasks_exhausted=_tasks_exhausted(payload),
        phase_task=_text(payload, "phaseTask"),
        llm_resp=_text(payload, "llmResp"),
        cmd_result=_text(payload, "lastCmdResult"),
        errors=_errors(payload),
        action_results=_action_results(payload),
        vendor_prices=_vendor_prices(payload),
        shop_prices=_shop_prices(payload),
        walls=_walls(payload),
        our_team=_our_team(payload),
        station_health=_station_hp(payload)[0],
        station_level=_station_hp(payload)[1],
        news=_news(payload),
    )


# ── 铺地图 ───────────────────────────────────────────────────────────
def _entries(payload: dict[str, Any]) -> dict[Pos, str]:
    """铺矩阵的原料：`{坐标: 类别}`，只装非空格。

    写入顺序即覆盖顺序：中立元素先写、单位后写（单位压在矿上时格子显示单位）。中立元素的
    字段名是 `neutralType`（不是 `zoneType`）；`roleType` / `neutralType` 一律原样写入、
    不查白名单：未知类别照旧挡路，判错方向只会多挡、不会放行。"""
    entries: dict[Pos, str] = {}

    for node in _items(payload, "mapInfo", "zones"):
        pos = _pos(node)
        kind = node.get("neutralType") if isinstance(node, dict) else None
        if pos is not None and isinstance(kind, str):
            entries[pos] = kind

    _units(entries, _items(payload, "teamOur", "roles"), "")
    _units(entries, _items(payload, "teamEnemy", "roles"), ENEMY_PREFIX)
    _units(entries, _items(payload, "robot", "roles"), ROBOT_PREFIX)
    return entries


def _units(entries: dict[Pos, str], nodes: list[Any], prefix: str) -> None:
    """单位 → 格子。`prefix` 区分来源（我方空串，敌方 / 机器人带前缀）。
    基地只写左上角那一格：2×2 的展开由 `map.Map` 铺格时统一做。"""
    for node in nodes:
        kind = node.get("roleType") if isinstance(node, dict) else None
        pos = _pos(node)
        if not isinstance(kind, str) or pos is None:
            continue
        entries[pos] = prefix + kind


# ── 解析小工具 ───────────────────────────────────────────────────────
def _items(payload: dict[str, Any], *path: str) -> list[Any]:
    """逐级 `get`；任何一级不是 dict、或末端不是 list，都返回空列表。"""
    node: Any = payload
    for key in path:
        if not isinstance(node, dict):
            return []
        node = node.get(key)
    return node if isinstance(node, list) else []


def _int(value: Any) -> int:
    """整数容错：不是 int（`bool` 不算）、或字段缺失，一律 -1。"""
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def _pos(node: Any, key: str = "pos") -> Pos | None:
    """`node[key]` → Pos。角色、中立元素、机器人、武器都叫 `pos`；任务点是唯一例外
    （`taskPosition`），所以 `key` 可换。
    """
    parent = node if isinstance(node, dict) else {}
    raw = parent.get(key)
    if not isinstance(raw, dict):
        return None
    x, y = _int(raw.get("x")), _int(raw.get("y"))
    return Pos(x, y) if x >= 0 and y >= 0 else None


def _character(node: Any) -> BaseRole | None:
    """单位 → 角色。建筑（station/gatling/railgun/rocket/wall）返回 None。`role_type` 是
    `str` 才交给 `make()` —— 那里用 `dict.get`，不可哈希的 key 会抛。
    """
    if not isinstance(node, dict) or _destroyed(node):
        return None
    pos, role_id, role_type = _pos(node), _int(node.get("id")), node.get("roleType")
    if pos is None or role_id < 0 or not isinstance(role_type, str):
        return None
    return make(role_id, pos, role_type, _bag(node))


def _weapons(payload: dict[str, Any]) -> tuple[Weapon, ...]:
    """我方武器名册 —— `attack` 唯一的目标表（key 就是这里的 `id`）。

    武器与角色混在 `teamOur.roles` 里，靠 `roleType` 认（`world.WEAPON_KINDS`），只扫我方。
    两条丢掉的规则都是"别发出非法指令"：`id < 0`（字段缺失）⇒ 丢、已毁 ⇒ 丢（`_destroyed`）。
    `attackRange` / `cooldown` 解析不出来 ⇒ `-1`，方向故意分开：射程 -1 ⇒ 不开火；冷却 -1
    ⇒ 照打。`health` 缺失同样 `-1`（未知）—— 券链"先升血少的"会把它排到最后。"""
    out = []
    for node in _items(payload, "teamOur", "roles"):
        if not isinstance(node, dict) or _destroyed(node):
            continue
        kind, pos, role_id = node.get("roleType"), _pos(node), _int(node.get("id"))
        if not isinstance(kind, str) or kind not in WEAPON_KINDS:
            continue
        if pos is None or role_id < 0:
            continue
        out.append(
            Weapon(
                id=role_id,
                kind=kind,
                pos=pos,
                attack_range=_int(node.get("attackRange")),
                cooldown=_int(node.get("cooldown")),
                # 缺失按文档的"初始等级 1"算：-1 会被升级线当成"还升得动"去买券
                level=max(1, _int(node.get("level"))),
                # 血量缺失 ⇒ -1（未知）：券链"先升血少的"，未知的排最后
                health=_int(node.get("health")),
            )
        )
    return tuple(out)


def _walls(payload: dict[str, Any]) -> tuple[Wall, ...]:
    """我方围墙实体，修墙线的判据。与武器同住 `teamOur.roles`、靠 `roleType` 认。

    已毁（health == 0）⇒ 丢 —— 那是一格缺口，`_ring` 的候选表会接住重建。health 缺失给 -1
    （判"未知"，修墙线不为一格读不出血量的墙白跑）。"""
    out = []
    for node in _items(payload, "teamOur", "roles"):
        if not isinstance(node, dict) or _destroyed(node):
            continue
        if node.get("roleType") != "wall":
            continue
        pos, wall_id = _pos(node), _int(node.get("id"))
        if pos is None or wall_id < 0:
            continue
        out.append(
            Wall(
                id=wall_id,
                pos=pos,
                health=_int(node.get("health")),
                level=max(1, _int(node.get("level"))),
            )
        )
    return tuple(out)


def _station_hp(payload: dict[str, Any]) -> tuple[int, int]:
    """基地 `(health, level)`——夜里基地升级券的判据。找不到 ⇒ (-1, 1)。"""
    for node in _items(payload, "teamOur", "roles"):
        if isinstance(node, dict) and node.get("roleType") == "station":
            return _int(node.get("health")), max(1, _int(node.get("level")))
    return -1, 1


def _news(payload: dict[str, Any]) -> str:
    """官方消息（`worldNews.officialNews`）—— 矿产事件（塌方/停工）的原文。`folkLegends`
    是宝藏线索（summonTreasure 那条线），不读。"""
    node = payload.get("worldNews")
    if not isinstance(node, dict):
        return ""
    text = node.get("officialNews")
    return text if isinstance(text, str) else ""


def _robots(payload: dict[str, Any]) -> tuple[Robot, ...]:
    """场上全部机器人（`robot.roles`）：全图可见、逐回合全量，白天为空。

    只留 `pos` + `health` + `target_team` + `kind`（`roleType`）。`target_team` 缺失给空串 ⇒
    当成打我方的（安全降级）；`kind` 缺失也给空串 ⇒ 打谁按"最低级"算（照打，不少打）。"""
    out = []
    for node in _items(payload, "robot", "roles"):
        pos = _pos(node)
        if pos is None:
            continue
        team = node.get("targetTeam") if isinstance(node, dict) else None
        kind = node.get("roleType") if isinstance(node, dict) else None
        out.append(
            Robot(
                pos=pos,
                health=_int(node.get("health")),
                target_team=team if isinstance(team, str) else "",
                kind=kind if isinstance(kind, str) else "",
            )
        )
    return tuple(out)


def _our_team(payload: dict[str, Any]) -> str:
    """我方阵营 `teamOur.type`（"challenger" / "defender"）。缺失 ⇒ 空串（不过滤机器人）。"""
    team = payload.get("teamOur") if isinstance(payload, dict) else None
    val = team.get("type") if isinstance(team, dict) else None
    return val if isinstance(val, str) else ""


def _tasks(payload: dict[str, Any]) -> tuple[Pos, ...]:
    """本回合可接取的己方任务点，坐标取自 `teamOur.playerTasks[].taskPosition`。

    `playerTasks` 是权威来源：只含我方那 2 个点、阵营已滤好。字段名是 `taskPosition` 不是
    `pos`；只认锚点格就够。降级方向故意不是"少做"：误接一个冷却中的点只是执行失败（不计
    异常），而误判成"永远接不了"会让整条任务线静默作废 ⇒ `isValid` 明确为 `False` 才排除。"""
    out = []
    for node in _items(payload, "teamOur", "playerTasks"):
        if not isinstance(node, dict) or node.get("isValid") is False:
            continue
        pos = _pos(node, "taskPosition")
        if pos is not None and _int(node.get("coldDownRounds")) <= 0:
            out.append(pos)
    return tuple(out)


def _cooling_tasks(payload: dict[str, Any]) -> tuple[tuple[Pos, int], ...]:
    """我方正在冷却的任务点（点 → 还有几回合就绪），按冷却升序、并列取坐标序。

    开拓者空闲时去**刷新最快**的那个点旁边等着（用户口径）—— `task_points` 只管能接的，
    这里管还没好的；来源同一处（`teamOur.playerTasks`）。"""
    out = []
    for node in _items(payload, "teamOur", "playerTasks"):
        if not isinstance(node, dict) or node.get("isValid") is not False:
            continue
        pos, cool = _pos(node, "taskPosition"), _int(node.get("coldDownRounds"))
        if pos is not None and cool > 0:
            out.append((pos, cool))
    return tuple(sorted(out, key=lambda item: (item[1], item[0])))


def _tasks_exhausted(payload: dict[str, Any]) -> bool:
    """任务点全都没戏了 —— `coldDownRounds == 0` **且** `isValid is False`（用户口径）。

    任务书说任务点是**有限的**：`isValid: false` + 冷却 0 = 这个点再也不会给出任务 ⇒ 两个点都
    这样 = 这一局没有任务可做了（`planner` 据此切到"无任务模式"）。⚠️ 只要有一个点还带冷却
    （`coldDownRounds > 0`）或者还 `isValid`，就不算 —— `playerTasks` 缺失/为空同样不算（未知
    ≠ 没有）。"""
    nodes = [n for n in _items(payload, "teamOur", "playerTasks") if isinstance(n, dict)]
    if not nodes:
        return False
    return all(_int(n.get("coldDownRounds")) == 0 and n.get("isValid") is False for n in nodes)


def _vendor_prices(payload: dict[str, Any]) -> Mapping[str, int]:
    """`vendorShopList` → `{矿种: 收购价}`（元素 `{name, price}`）。

    不硬编码"铜 > 铁 > 石头"：官方消息会让价格波动。只收 `price >= 0`（负的收购价不存在，
    混进来会让挑矿选出一座倒贴钱的矿）。名字不是字符串的同样丢掉。"""
    out: dict[str, int] = {}
    for node in _items(payload, "vendorShopList"):
        if not isinstance(node, dict):
            continue
        name, price = node.get("name"), _int(node.get("price"))
        if isinstance(name, str) and price >= 0:
            out[name] = price
    return MappingProxyType(out)


def _shop_prices(payload: dict[str, Any]) -> Mapping[str, int]:
    """顶层 `weaponShopList` → `{商品名: 单价}`（与 `_vendor_prices` 同一套解析）。查不到的
    商品按 0 算 ⇒ 买不起 ⇒ 不跑腿（故意的降级方向）。"""
    out: dict[str, int] = {}
    for node in _items(payload, "weaponShopList"):
        if not isinstance(node, dict):
            continue
        name, price = node.get("name"), _int(node.get("price"))
        if isinstance(name, str) and price >= 0:
            out[name] = price
    return MappingProxyType(out)


def _errors(payload: dict[str, Any]) -> tuple[Error, ...]:
    """顶层 `errors` → 判题器本轮报的错。`errorCode` 解析不出来 ⇒ 整条丢掉：一条 `-1：xxx`
    会被当成"未知错误 0"去查一个不存在的问题。`description` 缺失给空串。"""
    out = []
    for node in _items(payload, "errors"):
        if not isinstance(node, dict):
            continue
        code = _int(node.get("errorCode"))
        if code < 0:
            continue
        text = node.get("description")
        out.append(Error(code=code, description=text if isinstance(text, str) else ""))
    return tuple(out)


def _action_results(payload: dict[str, Any]) -> tuple[tuple[int, bool], ...]:
    """`lastRoundRoleActionResults` → `((实体 id, 是否合法), …)`。

    key 在 JSON 里是字符串：转不成 int 的那条丢掉。value 只认真正的布尔，不写 `bool(ok)`：
    JSON 里的 `"false"` 是非空字符串，那一步会把"不合法"读成"合法"。顺序照 payload 原样，
    排序是呈现的事（`app._log` 打印时做）。"""
    raw = payload.get("lastRoundRoleActionResults")
    if not isinstance(raw, dict):
        return ()
    out = []
    for key, ok in raw.items():
        if not isinstance(ok, bool):
            continue
        try:
            out.append((int(key), ok))
        except (TypeError, ValueError):
            continue
    return tuple(out)


def _text(payload: dict[str, Any], key: str) -> str:
    """顶层文本字段（`phaseTask` / `llmResp` / `lastCmdResult`）。非 `str` 一律退化成 `""`
    —— 空串是这三个字段天然的安全值（没任务 / 不交答案 / 没回执）。"""
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _destroyed(node: dict[str, Any]) -> bool:
    """这一条明确声明自己已毁 / 已阵亡吗？`health == 0` 才算（不是 `<= 0`：`_int` 对字段
    缺失给 -1，"声明已毁"与"没这个字段"要分开）。使用者是 `_character`（别给尸体发指令）
    与 `_weapons`（别操纵已毁的炮）；机器人不走这里。"""
    return _int(node.get("health")) == 0


def _gold(payload: dict[str, Any]) -> int:
    """我方金币 `teamOur.goldNum`。缺失给 -1 ⇒ 建不起武器 ⇒ 不建造（降级方向是"不动"）。"""
    team = payload.get("teamOur")
    team = team if isinstance(team, dict) else {}
    return _int(team.get("goldNum"))


def _bag(node: dict[str, Any]) -> Mapping[str, int]:
    """背包 → `{物品名: 件数}`（`backpack` 是物品名数组，重复即计数）。

    背包缺失或不是数组 ⇒ 空表 ⇒ 石头 0 块、一件都卖不掉（降级方向是"少做"）。非 `str` 的
    项丢弃。不读 `backPackCapability`（注意大写 P）：一天到不了那个容量上限。"""
    bag = node.get("backpack")
    if not isinstance(bag, list):
        return MappingProxyType({})
    counts: dict[str, int] = {}
    for item in bag:
        if isinstance(item, str):
            counts[item] = counts.get(item, 0) + 1
    return MappingProxyType(counts)


def _size(payload: dict[str, Any]) -> tuple[int, int]:
    """地图尺寸 `(width, height)`（取自 `mapInfo`）。缺失 ⇒ (-1, -1) ⇒ 寻路一步都走不出来、
    单位不动 —— 拿不到尺寸就别动，比走出地图边界吃一条异常划算。"""
    info = payload.get("mapInfo")
    info = info if isinstance(info, dict) else {}
    return _int(info.get("width")), _int(info.get("height"))
