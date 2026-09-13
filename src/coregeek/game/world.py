"""局面的领域表示。由 `protocol.model` 从 payload 构造，策略只读它。

字段只带**当前步骤真正用到**的：血量、等级、背包、冷却等，用到时再加。
"""

from typing import NamedTuple

from .grid import Pos
from .roles import BaseRole


class Turn(NamedTuple):
    round_no: int
    #: 地图尺寸 `(width, height)`（接口文档 §1.2.1）。**寻路要它挡界外**——BFS 会往远处
    #: 探路，而任务书 L85 那张"阻挡移动"清单里没写地图边界。拿到无效值就当无格可走
    size: tuple[int, int]
    #: **只含角色**（开拓者/工人）。建筑不是可操控单位，见 `roles.make`
    roles: tuple[BaseRole, ...]
    #: 阻挡移动的格子：双方建筑与角色 + 中立单位/任务点/矿区 + 机器人（任务书 L85）
    blocked: frozenset[Pos]
    #: 我方基地**左上角**坐标。基地 4 格都在 `blocked` 里
    station: Pos | None
    #: 矿点 → 矿种（`stone` / `iron` / `copper`，接口文档 §1.2.1）。
    #: **只有矿**：小贩 / 武器商店 / 任务点这一步没有使用者，不进这里（但它们照旧挡路）。
    mines: dict[Pos, str]
