"""本方的游戏实体。**纯领域值对象，不含任何线上 JSON 形态。**

设计见 docs/design/code-design.md §3.1。为什么这些类在 domain 而不在 protocol：

分层契约是 protocol 依赖 domain。策略层（planner / defense / economy）要读
`Role.health`、`Role.backpack`、`Turn.weapons`……如果这些定义在 protocol 里，
整个 domain 就得反向 import protocol，分层当场失效（import-lint 会直接报错）。
更根本地说：**它们本来就不是"线上格式"**——`Turn` 里没有一个字段是 wire 形状的
（wire 形状是 `{"x":1,"y":2}`，而我们这里是 `Pos`）。

`protocol/model.py` 只保留一件事：把 payload **解析成**这些对象，外加异常清单。

⚠️ `Role.raw` 是唯一一处刻意留下的原始字典（保留未知字段供赛后复盘）。
不要基于它做决策——它的形状没有任何保证。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..infra import config
from .grid import Pos

__all__ = [
    "STATION", "GATLING", "RAILGUN", "ROCKET", "WALL", "PIONEER", "WORKER",
    "BUILDING_KINDS", "WEAPON_KINDS", "CONTROLLABLE_KINDS",
    "MINE_KINDS", "TASK_POINT_KINDS",
    "Role", "Robot", "PlayerTask", "ShopItem", "NativeError",
]

# ── 角色类型 ─────────────────────────────────────────────────────────
STATION = "station"
GATLING = "gatling"
RAILGUN = "railgun"
ROCKET = "rocket"
WALL = "wall"
PIONEER = "pioneer"
WORKER = "worker"

BUILDING_KINDS = (STATION, GATLING, RAILGUN, ROCKET, WALL)
WEAPON_KINDS = (GATLING, RAILGUN, ROCKET)
CONTROLLABLE_KINDS = (WORKER, PIONEER)

MINE_KINDS = ("stone", "iron", "copper")

# 任务点：挑战者/防守者各两个，其中"点 2"占两格
TASK_POINT_KINDS = (
    "challengerTaskPoint1",
    "challengerTaskPoint2",
    "defenderTaskPoint1",
    "defenderTaskPoint2",
)


@dataclass(frozen=True, slots=True)
class Role:
    id: int
    pos: Pos
    role_type: str
    health: int = 0
    attack_power: int = 0
    attack_range: int = 0
    capacity: int = 0
    backpack: tuple[str, ...] = ()
    level: int = 1
    cooldown: int = 0
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    # ── 派生 ────────────────────────────────────────────────────────
    @property
    def alive(self) -> bool:
        return self.health > 0

    @property
    def is_weapon(self) -> bool:
        return self.role_type in WEAPON_KINDS

    @property
    def is_controllable(self) -> bool:
        return self.role_type in CONTROLLABLE_KINDS

    @property
    def backpack_full(self) -> bool:
        return self.capacity > 0 and len(self.backpack) >= self.capacity

    def count_item(self, name: str) -> int:
        return sum(1 for it in self.backpack if it == name)

    def range_of_attack(self) -> int:
        """⚠️ **payload 优先，等级表兜底**（✅样例与文档矛盾，见模块 docstring）。

        demo 的 `Unit.range_of_attack()` 就是这个优先级，必须保留。
        """
        if self.attack_range > 0:
            return self.attack_range
        table = config.TOWER_RANGE_BY_LEVEL.get(self.role_type)
        if not table:
            return 0
        level = min(max(self.level, 1), len(table))
        return table[level - 1]

    def target_slots(self) -> int:
        """`attack` 的 `targetPos` 个数。

        接口文档：**电磁狙击炮只能攻击一个目标**；加特林/火箭 = 当前等级。
        """
        if self.role_type not in config.MULTI_TARGET_WEAPONS:
            return 1
        return min(max(self.level, 1), config.MAX_TARGETS)


@dataclass(frozen=True, slots=True)
class Robot:
    id: int
    pos: Pos
    role_type: str
    health: int = 0
    abnormal_state: str = ""
    # ⚠️ 接口文档有、样例没有：缺失时 None，调用方不得依赖它
    target_team: str | None = None

    @property
    def dizzy(self) -> bool:
        return self.abnormal_state == "dizzy"

    @property
    def alive(self) -> bool:
        return self.health > 0


@dataclass(frozen=True, slots=True)
class PlayerTask:
    task_type: str
    pos: Pos
    cold_down_rounds: int = 0
    score_reward: int = 0
    gold_reward: int = 0
    is_valid: bool = False
    # ⚠️ 样例缺失；它决定 score₁ 的标准回合数与保底提交时点
    timeout_rounds: int | None = None

    @property
    def ready(self) -> bool:
        return self.is_valid and self.cold_down_rounds <= 0


@dataclass(frozen=True, slots=True)
class ShopItem:
    name: str
    price: int = 0


@dataclass(frozen=True, slots=True)
class NativeError:
    """判题器 errors[] 里的一条。"""

    code: int
    description: str = ""

    @property
    def is_cmd_error(self) -> bool:
        return self.code == 4  # 指令错误 = 异常信号

    @property
    def is_task_timeout(self) -> bool:
        return self.code == 1

    @property
    def is_answer_wrong(self) -> bool:
        return self.code == 2

    @property
    def is_llm_over_quota(self) -> bool:
        return self.code == 5

