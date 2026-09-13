"""局面的领域表示。由 `protocol.model` 从 payload 构造，策略只读它。

字段只带**当前步骤真正用到**的：血量、等级、冷却等，用到时再加（背包已按需收窄成 `BaseRole.stone`）。
"""

from typing import NamedTuple

from .grid import Pos
from .map import Map
from .roles import BaseRole

#: 日历（任务书 L90）：130 回合 = 1 天（**白天 70 + 夜晚 60**），共 10 天。
ROUNDS_PER_DAY = 130
DAY_ROUNDS = 70

#: 三种武器工事的 `roleType`（接口文档 §1.3.1）。**从 `map` 搬来的** ——
#: 武器原本只是网格里的一个类别串，现在 `model` 要照它把带属性的武器认出来。
#: ⚠️ 与 `planner.WEAPON_ORDER` **不是一回事**：那个是"三座炮的建造先后"（策略），
#: 这个是"哪些 roleType 算武器"（协议）。内容碰巧相同，含义不同，别合并。
WEAPON_KINDS = frozenset({"gatling", "railgun", "rocket"})


class Weapon(NamedTuple):
    """我方一座武器工事 —— 开火要用到的全部信息（接口文档 §1.3.1 的 `Role`）。

    **没有 `level`**：`targetPos` 的数量 = 武器等级数，而我们的武器永远是 L1
    （没有买升级券这条线，敌人也升不了我们的炮）⇒ 永远只传 1 个目标。
    加 `level` 等于给下一个人递一个"该按等级传多个目标"的钩子，真升到 L2 再回来加。
    **没有 `health`**：只在解析时用它筛掉已毁的炮，存下来没人读。
    """

    id: int
    """**线上那条指令的 key 就是它** —— 不是操控角色的 id（见 `actions.Attack`）。"""
    kind: str
    pos: Pos
    attack_range: int
    """以**切比雪夫距离**计的射程（任务书 L230），取自 payload 的 `attackRange`。
    任务书等级表写的是 3/6/10，样例给的是 4/7/INT_MAX —— **以 payload 为准**。"""
    cooldown: int
    """冷却**剩余回合数**；只有火箭发射台有（发射后 3 回合空窗），加特林/电磁炮恒 0。
    样例里没有这个字段 ⇒ `model._int` 给 -1 ⇒ `> 0` 为假 ⇒ 照打。"""


class Robot(NamedTuple):
    """一台机器人（接口文档 §1.5.1）。**只留这一步用得上的**：打谁只看血量。

    `id` / `roleType` / `abnormalState` / `targetTeam` 都不存 —— 目标位置用的是**坐标**，
    按类型排优先级是另一种打法（用户选定的是"补刀"），眩晕不影响"能不能打"。
    """

    pos: Pos
    health: int


class Turn(NamedTuple):
    round_no: int
    #: 地图信息：一张格子矩阵，每格只有一个**类别**（见 `map.Map`）。
    #: **寻路**读 `blocked` / `size`，**建造**读 `station`，**打印日志**读 `render()`。
    #: 武器**不在**这里 —— 它得带 id 与射程，见下面的 `weapons`。
    map: Map
    #: **只含角色**（开拓者/工人），带 id。建筑不是可操控单位，见 `roles.make`。
    #: 网格里也标着角色占的格，但那只是为了挡路——**要发指令就得有 id，而 id 只在这里**
    roles: tuple[BaseRole, ...]
    #: 我方金币（接口文档 `teamOur.goldNum`）。建一座武器 25 金，是这一步唯一的支出。
    #: 缺失时 `model._int` 给 -1 ⇒ 买不起 ⇒ 不建造，**这是故意的降级方向**。
    gold: int
    #: 我方三座武器的**名册**（不是网格里的类别串）。带默认值"空"：开局确实一座都没有，
    #: 也不关心武器的用例（寻路、昼夜）不必写它。由 `teamOur.roles` 解析而来。
    weapons: tuple[Weapon, ...] = ()
    #: 场上**全部**机器人（`robot.roles`，全图可见、逐回合全量）。白天是空的。
    robots: tuple[Robot, ...] = ()
    #: 本回合**可接取**的己方任务点（`teamOur.playerTasks` 里还接得动的那几个）。
    #: 不可接的（冷却中 / 已做完）在 `model._tasks` 就滤掉了 —— 策略侧不需要区分
    #: "没有任务点"与"任务点都在冷却"，两者都是"什么都不发"。
    task_points: tuple[Pos, ...] = ()
    #: 当前已领取任务的**原文描述**（接口文档 L28）。**非空 = 开拓者手上有任务** ——
    #: 这是"任务进行中"的**唯一判据**，白天走不走、夜里钉不钉、答不答题全靠它。
    #: 用载荷事实而不是自己记"谁领了任务"，重放/换回合都不会错。
    phase_task: str = ""
    #: 判题器 LLM 的回复（接口文档 L31）= **我们要提交的答案原文**。
    #: 上一回合的响应里发过 `prompt` 才有值。
    llm_resp: str = ""

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
