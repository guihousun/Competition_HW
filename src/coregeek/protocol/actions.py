"""动作 → 线上报文。**唯一写线上格式的地方。**

一条指令就是一个对象，**创建即校验**：`roleType` 传错连对象都造不出来。
这是按红线设计的 —— 判题器把"指令非法"计为一次异常，累计 5 次该队整场不再被调度，
而"角色没权限"到底算不算异常，任务书 §8 没写清（它的注解把"指令错误"收窄为
「字段缺失」或「动作码非法」，例子全是格式问题）。口径不明，就只能让它**发不出去**。

子类落地时补四样，缺一不可：

    code      任务书 §4.4 的动作码
    roles     任务书 §4.4 表格**最右列**（可用角色）
    参数      §2.2 RoleCommand 里该动作用到的字段
    to_wire   编成 §2.2 的扁平记录

> 目前只有 `move` —— 按"禁止冗余设计"，没实现的动作为空壳，等落地时再加。
> 因此**这一步的校验在真实运行中拦不到东西**（`move` 对全部角色合法），
> 它交付的是机制本身。第一次真正拦住东西，要等第一个受限动作（`build`/`collect`）。
"""

from typing import Any, ClassVar

from ..game.grid import Pos

WORKER = frozenset({"worker"})
PIONEER = frozenset({"pioneer"})
ALL = WORKER | PIONEER


class BaseAction:
    """一条指令。**创建即校验**：角色无权则抛 `PermissionError`。"""

    code: ClassVar[str]
    roles: ClassVar[frozenset[str]]

    def __init__(self, role_type: str) -> None:
        #: `role_type` 只用于校验，**不存进对象** —— `attack` 的 `controllerId`
        #: 是角色 **id** 而不是类型，存了也没有使用者。
        if role_type not in self.roles:
            raise PermissionError(f"{role_type} 无权执行 {self.code}")

    def to_wire(self) -> dict[str, Any]:
        raise NotImplementedError(self.code)


class Move(BaseAction):
    """移动一格。每回合每个角色均可执行一次（任务书 §4.4）。"""

    code = "move"
    roles = ALL

    def __init__(self, role_type: str, target: Pos) -> None:
        super().__init__(role_type)
        self.target = target

    def to_wire(self) -> dict[str, Any]:
        return {
            "action": self.code,
            "targetPos": [{"x": self.target.x, "y": self.target.y}],
        }
