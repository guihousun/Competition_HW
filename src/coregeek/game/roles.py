"""任务书 §4.5.2 的两种角色。

**只有开拓者与工人是"角色"**，基地/武器/围墙是建筑（§4.5.1）——它们和角色一起塞在
payload 的 `teamOur.roles` 里（接口文档 §1.3.1 的字段名叫"单位通用属性"），
`roleType` 实测有 `station`/`gatling`/`railgun`/`rocket`/`wall`/`worker`/`pioneer` 七种。
`make()` 只认后两种，建筑返回 `None`。

**刻意不带 `can()`**：权限校验在 `protocol.actions` 的 Action 构造时做。
角色侧再放一份就是**第二份真相**，迟早有一天对不上。

**也不带 §4.5.2 的 HP(200/220) 与背包容量(40格/100格)**：payload 里的 `health`/`backpack`
才是**权威的当前值**（角色会掉血、会复活），把上限写进类常量只会制造矛盾。
背包的**内容**（第 22 步起，卖矿要按矿种报件数）当然是从 payload 读的；
**容量上限**仍然不读 —— 还没有第二个使用者。
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import ClassVar

from .grid import Pos
from .map import STONE


class BaseRole:
    type_name: ClassVar[str]

    def __init__(
        self, role_id: int, pos: Pos, bag: Mapping[str, int] | None = None
    ) -> None:
        self.id = role_id
        self.pos = pos
        #: 背包：`{物品名: 件数}`（payload 的 `backpack` 是**物品名数组**，重复即计数）。
        #: 第 22 步从"只存石头的块数"改过来 —— `sell` 要按矿种报件数。
        #: 空表 ⇒ 石头 0 块（不砌墙、转去采矿）、一件都卖不掉。
        self.bag: Mapping[str, int] = bag if bag is not None else MappingProxyType({})

    @property
    def stone(self) -> int:
        """背包里**石头的块数**。围墙代价 石头×1，从建造者自己的背包扣，
        所以 `planner` 问得最多的是这一个 —— 派生属性，不是第二份真相。"""
        return self.bag.get(STONE, 0)


class Pioneer(BaseRole):
    """开拓者。背 40 格，做任务（`acceptTask` 等）只有它能发。"""

    type_name = "pioneer"


class Worker(BaseRole):
    """工人。背 100 格，`build` / `remove` / `collect` 只有它能发。"""

    type_name = "worker"


_KINDS: dict[str, type[BaseRole]] = {Pioneer.type_name: Pioneer, Worker.type_name: Worker}


def make(
    role_id: int, pos: Pos, role_type: str, bag: Mapping[str, int] | None = None
) -> BaseRole | None:
    """`roleType` → 角色。不是角色（建筑）则返回 `None`。

    调用方须先确认 `role_type` 是 `str` —— 这里用 `dict.get`，不可哈希的 key 会抛
    `TypeError`（`model._character` 已经挡在前面）。
    """
    kind = _KINDS.get(role_type)
    return None if kind is None else kind(role_id, pos, bag)
