"""局面的领域表示。由 `protocol.model` 从 payload 构造，策略只读它。

字段只带**当前步骤真正用到**的：血量、等级、背包、冷却等，用到时再加。
"""

from typing import NamedTuple

from .grid import Pos

#: 会移动的角色。建筑（基地/武器/围墙）是障碍物，不是可操控单位（接口文档 §1.3.1）。
MOVERS = ("worker", "pioneer")


class Role(NamedTuple):
    id: int
    pos: Pos
    role_type: str


class Turn(NamedTuple):
    round_no: int
    roles: tuple[Role, ...]
    #: 阻挡移动的格子：双方建筑与角色 + 中立单位/任务点/矿区 + 机器人（任务书 L85）
    blocked: frozenset[Pos]
    #: 我方基地**左上角**坐标。基地 4 格都在 `blocked` 里
    station: Pos | None
