"""payload → `game.world.Turn`。**唯一读线上格式的地方。**

**容错解析**：字段缺失或类型不对一律退化成默认值 / 丢掉那一条，绝不抛异常 ——
判题器是没法调试的黑盒，宁可少认一个角色，也不能让响应管线崩掉。

字段真值以 `docs/接口文档.md` §1 为准；`docs/request.txt` 的数值是手工示意数据，不可校准。
"""

from typing import Any

from ..game.grid import Pos, base_cells
from ..game.roles import BaseRole, make
from ..game.world import Turn

#: 阻挡移动的格子来源（任务书 L85）。`teamEnemy` 只含**进入视野**的单位，
#: 但基地与围墙是全图可见的，所以敌方那一路拿到的至少是这两类。
_BLOCKING = (
    ("teamOur", "roles"),
    ("teamEnemy", "roles"),
    ("robot", "roles"),
    ("mapInfo", "zones"),
)


def load(payload: Any) -> Turn | None:
    """解析一个回合。整条 payload 不成形就返回 None。"""
    if not isinstance(payload, dict):
        return None

    units = _items(payload, "teamOur", "roles")
    characters = tuple(c for c in (_character(n) for n in units) if c)

    # 建筑不在 characters 里（见 roles.make），所以基地要单独扫一遍原始单位列表。
    # 它喂给 blocked（base_cells），漏了角色会一头撞进基地。
    station = _station(units)
    zones = _items(payload, "mapInfo", "zones")

    blocked: set[Pos] = set()
    for path in _BLOCKING:
        blocked.update(p for p in (_pos(n) for n in _items(payload, *path)) if p)
    if station is not None:
        blocked.update(base_cells(station))

    return Turn(
        round_no=_int(payload.get("roundNo")),
        size=_size(payload),
        roles=characters,
        blocked=frozenset(blocked),
        station=station,
        mines=_mines(zones),
    )


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
    return make(role_id, pos, role_type)


def _size(payload: dict[str, Any]) -> tuple[int, int]:
    """地图尺寸 `(width, height)`（接口文档 §1.2.1）。

    缺失时 `_int` 给 -1 ⇒ **没有任何格子算在地图内** ⇒ 寻路一步都走不出来、单位不动。
    **这是故意的**：拿不到尺寸就别动，比走出地图边界吃一条异常划算。
    """
    info = payload.get("mapInfo")
    info = info if isinstance(info, dict) else {}
    return _int(info.get("width")), _int(info.get("height"))


def _station(units: list[Any]) -> Pos | None:
    """我方基地的**左上角**（接口文档 §1.3.1 注）。"""
    for node in units:
        if isinstance(node, dict) and node.get("roleType") == "station":
            return _pos(node)
    return None


#: 三种矿（接口文档 §1.2.1）。同一张表里还有小贩/武器商店/任务点，这一步没有使用者。
_MINE_KINDS = ("stone", "iron", "copper")


def _mines(zones: list[Any]) -> dict[Pos, str]:
    out: dict[Pos, str] = {}
    for node in zones:
        kind = node.get("neutralType") if isinstance(node, dict) else None
        pos = _pos(node)
        if pos is not None and kind in _MINE_KINDS:
            out[pos] = kind
    return out
