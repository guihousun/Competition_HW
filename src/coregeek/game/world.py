"""局面的领域表示。由 `protocol.model` 从 payload 构造，策略只读它。

字段只带**当前步骤真正用到**的：血量、等级、冷却等，用到时再加（背包已按需收窄成 `BaseRole.stone`）。
"""

from typing import NamedTuple

from .map import Map
from .roles import BaseRole

#: 日历（任务书 L90）：130 回合 = 1 天（**白天 70 + 夜晚 60**），共 10 天。
ROUNDS_PER_DAY = 130
DAY_ROUNDS = 70


class Turn(NamedTuple):
    round_no: int
    #: 地图信息：一张格子矩阵，每格只有一个**类别**（见 `map.Map`）。
    #: **寻路**读 `blocked` / `size`，**建造**读 `station` / `weapons`，**打印日志**读 `render()`
    map: Map
    #: **只含角色**（开拓者/工人），带 id。建筑不是可操控单位，见 `roles.make`。
    #: 网格里也标着角色占的格，但那只是为了挡路——**要发指令就得有 id，而 id 只在这里**
    roles: tuple[BaseRole, ...]
    #: 我方金币（接口文档 `teamOur.goldNum`）。建一座武器 25 金，是这一步唯一的支出。
    #: 缺失时 `model._int` 给 -1 ⇒ 买不起 ⇒ 不建造，**这是故意的降级方向**。
    gold: int

    @property
    def within(self) -> int:
        """本回合在"一天"里的序号（1..130）。`round_no` 缺失时是 -1 ⇒ 129。

        两个使用者（`is_day` 与 `day_rounds_left`），所以从 `is_day` 里提出来。
        """
        return (self.round_no - 1) % ROUNDS_PER_DAY + 1

    @property
    def is_day(self) -> bool:
        """白天吗？`build` / `remove` **仅白天**可用（任务书 §4.4）。

        `within <= 70` 为白天（任务书 L90）。`round_no` 缺失 ⇒ 129 ⇒ 判成夜晚 ⇒ 不建造。
        """
        return self.within <= DAY_ROUNDS

    @property
    def day_rounds_left(self) -> int:
        """白天还剩几回合，**含本回合**；夜里为 0。

        `planner` 拿它算"这一趟还该采几块石头" —— 策略指导唯一的时间硬约束是
        「必须在晚上到来前将墙建好，注意计算回合数」。按回合计而**不记任何跨回合状态**，
        所以 `handle` 仍然是纯函数。
        """
        return DAY_ROUNDS - self.within + 1 if self.is_day else 0
