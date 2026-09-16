"""两种可操控角色：开拓者与工人。

基地/武器/围墙是建筑，和角色一起塞在 payload 的 `teamOur.roles` 里，`make()` 只认角色、
建筑返回 `None`。刻意不带 `can()`：权限校验在 `protocol.actions` 的 Action 构造时做，
角色侧再放一份就是第二份真相。也不带 HP 与背包上限：payload 里的 `health` / `backpack`
才是权威的当前值。
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
        #: 背包：`{物品名: 件数}`（payload 的 `backpack` 是物品名数组，重复即计数）。
        #: 空表 ⇒ 石头 0 块、一件都卖不掉。
        self.bag: Mapping[str, int] = bag if bag is not None else MappingProxyType({})

    @property
    def stone(self) -> int:
        """背包里石头的块数 —— 派生属性（围墙代价石头×1，从建造者自己的背包扣）。"""
        return self.bag.get(STONE, 0)


class Pioneer(BaseRole):
    """开拓者。做任务（`acceptTask` 等）只有它能发。"""

    type_name = "pioneer"


class Worker(BaseRole):
    """工人。`build` / `remove` / `collect` 只有它能发。"""

    type_name = "worker"


_KINDS: dict[str, type[BaseRole]] = {Pioneer.type_name: Pioneer, Worker.type_name: Worker}


def make(
    role_id: int, pos: Pos, role_type: str, bag: Mapping[str, int] | None = None
) -> BaseRole | None:
    """`roleType` → 角色。不是角色（建筑）则返回 `None`。

    调用方须先确认 `role_type` 是 `str` —— 这里用 `dict.get`，不可哈希的 key 会抛 `TypeError`。
    """
    kind = _KINDS.get(role_type)
    return None if kind is None else kind(role_id, pos, bag)
