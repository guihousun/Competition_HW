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

> 目前有 `move`（对全部角色合法）与 `build`（**仅工人**）—— 按"禁止冗余设计"，
> 没实现的动作为空壳，等落地时再加。`build` 是**闸门第一次真的挡住东西**：
> 把 `build` 发给开拓者，以前只是"格式合法地做错事"，现在连对象都造不出来。
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


class Build(BaseAction):
    """建造一座建筑。**仅工人**，且**仅白天**（任务书 §4.4）。

    白天那半边**不在闸门里**：它是"什么时候"而不是"谁"，闸门只管"谁"
    （`role_type` 是 `build` 唯一能自证的权限）。昼夜判断在 `game.world.Turn.is_day`。

    `name` 取 `roleType` 同名（`wall` / `gatling` / `railgun` / `rocket`）—— 严格说
    **只有 `wall` 有实证**（`docs/response.txt` 里唯一一条 `build`），武器名是从
    `roleType` 取值表推的。见 `docs/design/code-task.md` 的已知不确定性。

    目标必须是**空的可建造格**：落在已有武器上的话，原武器会被**覆盖**成 level1
    （任务书 §4.5.1 补充说明）—— 25 金币打水漂还降级，所以 `planner` 侧要避开占用格。
    """

    code = "build"
    roles = WORKER

    def __init__(self, role_type: str, name: str, target: Pos) -> None:
        super().__init__(role_type)
        self.name = name
        self.target = target

    def to_wire(self) -> dict[str, Any]:
        # **key 是工人自己的 id**（与 `move` 相同）；`targetPos` 是数组，即使只有一个点
        return {
            "action": self.code,
            "name": self.name,
            "targetPos": [{"x": self.target.x, "y": self.target.y}],
        }
