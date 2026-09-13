"""局面的领域表示。由 `protocol.model` 从 payload 构造，策略只读它。

字段只带**当前步骤真正用到**的：血量、等级、背包、冷却等，用到时再加。
"""

from typing import NamedTuple

from .map import Map
from .roles import BaseRole


class Turn(NamedTuple):
    round_no: int
    #: 地图信息：一张格子矩阵，每格只有一个**类别**（见 `map.Map`）。
    #: **寻路**读 `blocked` / `size`，**打印日志调试**读 `render()`
    map: Map
    #: **只含角色**（开拓者/工人），带 id。建筑不是可操控单位，见 `roles.make`。
    #: 网格里也标着角色占的格，但那只是为了挡路——**要发指令就得有 id，而 id 只在这里**
    roles: tuple[BaseRole, ...]
