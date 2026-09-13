"""payload → `game.world.Turn`。**唯一读线上格式的地方。**

**容错解析**：字段缺失或类型不对一律退化成默认值 / 丢掉那一条，绝不抛异常 ——
判题器是没法调试的黑盒，宁可少认一个角色，也不能让响应管线崩掉。

字段真值以 `docs/接口文档.md` §1 为准；`docs/request.txt` 的数值是手工示意数据，不可校准
（连几何都是手画的：样例里我方那两座墙就不在基地的 6×6 环上）。

**一趟扫描铺出整张地图**（`_entries`）。以前是四趟分头扫，`blocked` 和 `mines` 各解析了一遍
`mapInfo`——两份真相迟早对不上，而对不上的症状是"以为能走、其实撞墙"。
"""

from typing import Any

from ..game.grid import Pos
from ..game.map import ENEMY_PREFIX, ROBOT_PREFIX, Map
from ..game.roles import BaseRole, make
from ..game.world import Turn


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
    )


# ── 铺地图 ───────────────────────────────────────────────────────────
def _entries(payload: dict[str, Any]) -> dict[Pos, str]:
    """铺矩阵的原料：`{坐标: 类别}`，只装**非空**格。

    **写入顺序即覆盖顺序，后写的盖前面**：中立元素先写、单位后写。单位压在矿上时
    格子显示单位——真出现那种局面说明 payload 自相矛盾，看得见比看不见强。

    `roleType` / `neutralType` **一律原样写入**，不查白名单：未知类别照旧挡路，
    判错方向只会多挡、不会放行。（`map._char` 画不出来才退化成 `?`，那只是显示。）
    """
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
    """单位 → 格子。`prefix` 区分来源（我方给空串，敌方 / 机器人给前缀）。

    **基地只写左上角那一格**：2×2 的展开是**游戏规则**（`grid.base_cells`），
    由 `map.Map` 铺格时统一做 —— 报文层只管"station 这个坐标在哪"。
    """
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
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def _pos(node: Any) -> Pos | None:
    """`node["pos"]` → Pos。角色、中立元素、机器人都是这个字段名。"""
    parent = node if isinstance(node, dict) else {}
    raw = parent.get("pos")
    if not isinstance(raw, dict):
        return None
    x, y = _int(raw.get("x")), _int(raw.get("y"))
    return Pos(x, y) if x >= 0 and y >= 0 else None


def _character(node: Any) -> BaseRole | None:
    """单位 → 角色。建筑（station/gatling/railgun/rocket/wall）返回 None。

    先确认 `role_type` 是 `str` 再交给 `make()` —— 那里用 `dict.get`，不可哈希的 key 会抛。
    """
    if not isinstance(node, dict):
        return None
    pos, role_id, role_type = _pos(node), _int(node.get("id")), node.get("roleType")
    if pos is None or role_id < 0 or not isinstance(role_type, str):
        return None
    return make(role_id, pos, role_type, _stone(node))


def _gold(payload: dict[str, Any]) -> int:
    """我方金币 `teamOur.goldNum`（接口文档 §1.3.1）。

    缺失给 -1（`_int` 的容错默认）⇒ 建不起武器 ⇒ 不建造。**降级方向是"不动"**，
    与 `_size` 同一条思路：宁可少做，不可乱花。
    """
    team = payload.get("teamOur")
    team = team if isinstance(team, dict) else {}
    return _int(team.get("goldNum"))


_STONE = "stone"


def _stone(node: dict[str, Any]) -> int:
    """背包里石头的**块数**（接口文档 §1.3.1：`backpack` 是**物品名数组**，重复即计数）。

    **只数 `stone`** —— 这一步只有围墙用它（代价 石头×1）。

    **不读 `backPackCapability`**（注意大写 P）：一座矿最多采 10 次、围墙只有 16 格，
    一天实际到不了 100 的容量上限，先不加。

    背包缺失或不是数组 ⇒ 0 块 ⇒ 不砌墙、转去采矿。**降级方向是"少做"**，
    与 `_gold` / `_size` 一致：宁可少采，不可对着空背包发 `build`。
    """
    bag = node.get("backpack")
    return sum(1 for item in bag if item == _STONE) if isinstance(bag, list) else 0


def _size(payload: dict[str, Any]) -> tuple[int, int]:
    """地图尺寸 `(width, height)`（接口文档 §1.2.1）。

    缺失时 `_int` 给 -1 ⇒ **没有任何格子算在地图内** ⇒ 寻路一步都走不出来、单位不动。
    **这是故意的**：拿不到尺寸就别动，比走出地图边界吃一条异常划算。
    """
    info = payload.get("mapInfo")
    info = info if isinstance(info, dict) else {}
    return _int(info.get("width")), _int(info.get("height"))
