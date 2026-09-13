"""任务书 §4.5.2 的两种角色。

**只有开拓者与工人是"角色"**，基地/武器/围墙是建筑（§4.5.1）——它们和角色一起塞在
payload 的 `teamOur.roles` 里（接口文档 §1.3.1 的字段名叫"单位通用属性"），
`roleType` 实测有 `station`/`gatling`/`railgun`/`rocket`/`wall`/`worker`/`pioneer` 七种。
`make()` 只认后两种，建筑返回 `None`。

**刻意不带 `can()`**：权限校验在 `protocol.actions` 的 Action 构造时做。
角色侧再放一份就是**第二份真相**，迟早有一天对不上。

**也不带 §4.5.2 的 HP(200/220) 与背包容量(40格/100格)**：payload 里的 `health`/`backpack`
才是**权威的当前值**（角色会掉血、会复活），把上限写进类常量只会制造矛盾。
等真有决策读血量/背包时，从 payload 读。
"""

from typing import ClassVar

from .grid import Pos


class BaseRole:
    type_name: ClassVar[str]

    def __init__(self, role_id: int, pos: Pos) -> None:
        self.id = role_id
        self.pos = pos


class Pioneer(BaseRole):
    """开拓者。背 40 格，做任务（`acceptTask` 等）只有它能发。"""

    type_name = "pioneer"


class Worker(BaseRole):
    """工人。背 100 格，`build` / `remove` / `collect` 只有它能发。"""

    type_name = "worker"


_KINDS: dict[str, type[BaseRole]] = {Pioneer.type_name: Pioneer, Worker.type_name: Worker}


def make(role_id: int, pos: Pos, role_type: str) -> BaseRole | None:
    """`roleType` → 角色。不是角色（建筑）则返回 `None`。

    调用方须先确认 `role_type` 是 `str` —— 这里用 `dict.get`，不可哈希的 key 会抛
    `TypeError`（`model._character` 已经挡在前面）。
    """
    kind = _KINDS.get(role_type)
    return None if kind is None else kind(role_id, pos)
