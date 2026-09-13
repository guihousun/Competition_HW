"""Intent —— 领域层的**唯一产出物**。

设计见 docs/design/code-design.md §3.1。要点：

- 这些对象描述**我们想做什么**（"走到 (9,22)"），完全不描述**线上报文长什么样**
  （`{"action":"move","targetPos":[{"x":9,"y":22}]}`）。两者的桥只有
  `protocol/commands.py` 一座。
- 因此 Intent 必须住在 `domain/`：若放在 `protocol/`，`planner` 就得反向 import
  协议层，分层立刻失效，`tools/selfcheck.py` 的 import-lint 会直接报错。

字段一律 `kw_only=True`：这些类是**每次决策都要构造几十个**的，
位置参数极易把 `pos` 和 `target` 搞反，而这类错误只会在线上表现为一次无效指令。

⚠️ **`Attack.role_id` 是操控武器的角色，而报文里的 key 是武器 id**（`weapon_id`）。
demo 的 `commands[tower.unit_id] = attack_command(role.unit_id, target)` 是唯一权威，
写反了会导致整晚攻击全部无效。
"""

from __future__ import annotations

from dataclasses import dataclass

from .grid import Pos

__all__ = [
    "Intent",
    "Idle",
    "Move",
    "Collect",
    "Build",
    "Remove",
    "Attack",
    "Sell",
    "Buy",
    "Use",
    "Drop",
    "AcceptTask",
    "SubmitAnswer",
    "SummonTreasure",
    "WALL",
    "WEAPON_TYPES",
]

#: `build` 可建造的名称：三种武器 + 围墙
WEAPON_TYPES = ("gatling", "railgun", "rocket")
WALL = "wall"
BUILDABLE = WEAPON_TYPES + (WALL,)


@dataclass(frozen=True, slots=True, kw_only=True)
class Intent:
    """所有意图的基类。

    `role_id` = **执行这条指令的角色**（工人/开拓者/操控武器的角色）。
    对 `Attack` 而言它就是 `controllerId`。
    `reason` 只进日志，不上线路——是赛后复盘"为什么这么走"的唯一线索。
    """

    role_id: int
    reason: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class Idle(Intent):
    """显式的不作为。

    与"忘了分配"区分开：日志里出现 Idle 说明策略**主动**放弃了这个角色的回合
    （例如资源不足、无可用目标），而角色凭空消失则说明分配阶段有 bug。
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class Move(Intent):
    dest: Pos


@dataclass(frozen=True, slots=True, kw_only=True)
class Collect(Intent):
    """采集矿石。`target` 是**矿格本身**，不是落脚点——角色需站在其相邻格。"""

    target: Pos


@dataclass(frozen=True, slots=True, kw_only=True)
class Build(Intent):
    target: Pos
    name: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Remove(Intent):
    """拆除围墙。策略稿 §6.3 的 Plan B（常闭 + 需要时开门）依赖它。"""

    target: Pos


@dataclass(frozen=True, slots=True, kw_only=True)
class Attack(Intent):
    """操控武器攻击。

    `role_id` = 操控者（controllerId）；`weapon_id` = 被操控的武器 = `roleCommandMap` 的 key。
    `targets` 的长度由编码器按**武器实际等级**校验：电磁狙击炮恒为 1，
    加特林/火箭 = 当前等级（接口文档 §2.2）。
    """

    weapon_id: int
    targets: tuple[Pos, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class Sell(Intent):
    name: str
    num: int = 1


@dataclass(frozen=True, slots=True, kw_only=True)
class Buy(Intent):
    name: str
    num: int = 1


@dataclass(frozen=True, slots=True, kw_only=True)
class Use(Intent):
    """使用消耗品/升级券。升级券必须带 `target`（需在目标建筑周围一格内）。"""

    name: str
    target: Pos | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Drop(Intent):
    name: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AcceptTask(Intent):
    """领取任务。**仅开拓者可用**（`protocol/commands.py` 会按 roleType 卡）。"""


@dataclass(frozen=True, slots=True, kw_only=True)
class SubmitAnswer(Intent):
    answer: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SummonTreasure(Intent):
    """献祭召唤宝藏。本轮只保留编码与校验分支，不产生（见策略稿 §12 划界）。"""

    target: Pos
    items: tuple[str, ...]
