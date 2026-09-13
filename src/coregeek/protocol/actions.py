"""动作 → 线上报文。**唯一懂线上动作格式的地方**（`to_wire` 写、`describe` 读）。

一条指令就是一个对象，**创建即校验**：`roleType` 传错连对象都造不出来。
这是按红线设计的 —— 判题器把"指令非法"计为一次异常，累计 5 次该队整场不再被调度，
而"角色没权限"到底算不算异常，任务书 §8 没写清（它的注解把"指令错误"收窄为
「字段缺失」或「动作码非法」，例子全是格式问题）。口径不明，就只能让它**发不出去**。

子类落地时补四样，缺一不可：

    code      任务书 §4.4 的动作码
    roles     任务书 §4.4 表格**最右列**（可用角色）
    参数      §2.2 RoleCommand 里该动作用到的字段
    to_wire   编成 §2.2 的扁平记录

> 目前有 `move` 与 `attack`（对全部角色合法）、`build` 与 `collect`（**都仅工人**）、
> `acceptTask` 与 `submitAnswer`（**都仅开拓者**）—— 按"禁止冗余设计"，
> 没实现的动作为空壳，等落地时再加。`build` 是**闸门第一次真的挡住东西**：
> 把 `build` 发给开拓者，以前只是"格式合法地做错事"，现在连对象都造不出来。
>
> ⚠️ 两个任务动作**没有 `targetPos`** —— 这件事只有 `describe` 需要留心，
> 而它第 20 步起改成**通用摊开**（有什么字段打什么），不用再记着这份差别了。
"""

from typing import Any, Callable, ClassVar

from ..game.grid import Pos

WORKER = frozenset({"worker"})
PIONEER = frozenset({"pioneer"})
ALL = WORKER | PIONEER


def describe(cmds: dict[str, Any], *, clip: Callable[[str], str]) -> str:
    """把 `roleCommandMap` 压成**一行**日志：`10010 move targetPos=(12,22)；10020 attack controllerId=10010 (4,4)`。

    **字段是通用摊开的，不是逐个手写的**（第 20 步）：`action` 之外的每个字段一律
    `名=值` 印出来，`targetPos` 折成 `(x,y)`（多格用 `、` 连 —— `attack` 的等级 >1 时才有）。
    手写一份"哪个动作有哪几个字段"的清单就是**又一份会跟报文漂移的真相**：`build` 多个
    `name`、`attack` 多个 `controllerId`、`submitAnswer` 只有 `taskAnswer`……每加一个动作
    都要回头改一次，而漏掉的那一次没人会发现（日志少打一个字段，不像崩溃那么显眼）。

    通用摊开顺手拆掉一颗老雷：原来硬读 `cmd["targetPos"]`，而 `acceptTask` /
    `submitAnswer` **压根没有这个字段** —— 这里跑在 `app.handle` 的 `try` 里，
    一个 `IndexError` 的代价是**整回合退化成空指令**（第 11 步踩过，见 `CLAUDE.md`）。

    `clip` 由**调用方传进来**（唯一使用者仍只有 `app._log`）：截断的上限与留痕格式
    （`…（共 N 字）`）是日志层的规则，全项目只有 `app._clip` 一份；而 `protocol`
    **不能 import `app`**（依赖方向是 `app → protocol`）。所以把"怎么截"当参数递进来，
    而不是在这儿复制一份截断格式。字符串值一律过它 —— 这里不必知道哪个字段是自由文本。
    """
    parts = []
    for entity_id, cmd in cmds.items():
        fields = []
        for key, value in cmd.items():
            if key == "action":
                continue
            if key == "targetPos" and isinstance(value, list):
                #: 坐标折成 `(x,y)`，**不带字段名**（全模块只有这一个坐标字段，带上是噪音）
                points = [p for p in value if isinstance(p, dict)]
                if points:
                    fields.append("、".join(f"({p.get('x')},{p.get('y')})" for p in points))
            else:
                fields.append(f"{key}={clip(value) if isinstance(value, str) else value}")
        parts.append(" ".join([str(entity_id), str(cmd.get("action", "?"))] + fields))
    return "；".join(parts) if parts else "（空指令）"


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


class Collect(BaseAction):
    """采集矿石。**仅工人**（任务书 §4.4）。

    **§4.4 的能力列没有昼夜限制** —— `build` / `remove` 写"仅白天"、`attack` 写"仅黑夜"，
    `collect` 那一格什么都没写。所以夜里采也大概率合法；但**这一步夜里不采**
    （用户选定：夜里回基地操炮），昼夜门由 `planner` 把关，不进闸门 —— 闸门只管"谁"。

    `targetPos` 是**矿石的坐标**（接口文档 §2.3），不是自己的站位。需要站在矿的
    **切比雪夫 ≤1** 内：矿格本身挡路（任务书 L85），所以"站在矿上"不可能发生，
    `<= 1` 就够了，不用再排掉 0。一次采 1 块；**一座矿采满 10 次就消失**（任务书 L79），
    但 payload 里**没有"剩余次数"字段**，采没了只是下一回合换最近的一座。

    ⚠️ **没有实证报文**：`docs/response.txt` 里只有 `move`/`build`/`remove`。
    形状是从接口文档 §2.2 推的 —— 与 `move` 同形（只有 `action` + `targetPos`）。
    """

    code = "collect"
    roles = WORKER

    def __init__(self, role_type: str, target: Pos) -> None:
        super().__init__(role_type)
        self.target = target

    def to_wire(self) -> dict[str, Any]:
        return {
            "action": self.code,
            "targetPos": [{"x": self.target.x, "y": self.target.y}],
        }


class Attack(BaseAction):
    """操作武器打**一个格子**。**仅黑夜**，且**全部角色**都能做（任务书 §4.4 最右列）——
    开拓者也在炮位上，别把 `roles` 误窄成工人。

    ⚠️ **报文形状与其它动作是反的，最易写错**：`roleCommandMap` 的 **key 是武器 id**，
    操控者在 `controllerId` 里 —— 所以 `planner` 那边发射要用 `_emit(..., key=str(weapon.id))`。
    实证报文（`docs/response.txt`，那条 key `10020` 就是一座加特林）：
    `{"action":"attack","controllerId":"10010","targetPos":[{"x":29,"y":7}]}`

    判题器侧的两条站位硬规则（不满足是"指令执行失败"，**不计**异常，但白打一发）：
    操控者须站在武器**周围一格内**（**切比雪夫 ≤1**），且**一人同时只能操控一座**武器。
    目标格须在射程内 —— **打空处 = 执行失败**（任务书 L508）。多目标
    （`targetPos` 数量 = 武器等级数）这一步不做：我们的武器永远是 L1。

    白天那半边**不在闸门里**（闸门只管"谁"，而 §4.4 是"全部"）—— 由 `planner` 的
    `turn.is_day` 把关，与 `build` 的白天门同一条做法。但要留意「**缺失回合号被判成夜里**」
    这个降级方向对 `attack` 是**危险**的（白天开火非法），所以 `_defend` 另有 `round_no < 0` 的硬门。
    """

    code = "attack"
    roles = ALL

    def __init__(self, role_type: str, controller_id: str, target: Pos) -> None:
        super().__init__(role_type)
        #: 接口文档标的是 String —— 在这里定死，别让 int 漏进 JSON
        self.controller_id = str(controller_id)
        self.target = target

    def to_wire(self) -> dict[str, Any]:
        return {
            "action": self.code,
            "controllerId": self.controller_id,
            "targetPos": [{"x": self.target.x, "y": self.target.y}],
        }


class AcceptTask(BaseAction):
    """领取任务。**仅开拓者**（任务书 §4.4 §4.6.2），**无额外参数**（接口文档 L234）。

    ⚠️ 报文里**没有 `targetPos`** —— 领哪个任务点由**站位**决定（须在己方任务点
    周围一格内），不由参数决定。实证报文（`docs/response.txt` L51）：
    `{"10011":{"action":"acceptTask"}}`。这是第一个真正走"无坐标"分支的动作。

    在**敌方**任务点执行无效（任务书 L272），但 `teamOur.playerTasks` 已经把阵营滤好了，
    策略侧走到的那几个点必然是我方的。

    `PIONEER` 是**唯一的角色闸门**；"任务在不在冷却""领了没有"都由 `planner` 把关 ——
    闸门只管"谁"，与 `build` 的昼夜门同一条做法。
    """

    code = "acceptTask"
    roles = PIONEER

    def to_wire(self) -> dict[str, Any]:
        return {"action": self.code}


class SubmitAnswer(BaseAction):
    """提交任务答案。**仅开拓者**（任务书 §4.4）。`taskAnswer` 是 **String**（接口文档 L221）。

    实证报文（`docs/response.txt` L54）：
    `{"10011":{"action":"submitAnswer","taskAnswer":"xxx"}}` —— 同样**没有 `targetPos`**。

    **空答案不发**：闸门只管"谁"（`role_type`），"答案空不空"由 `planner` 把关 ——
    答案为空时判题器算不算"字段缺失"（= 指令非法 = 一次异常）文档没写，所以干脆不发。

    可以**反复提交**：接口文档 L140「以之前提交过的**通过率最高的**答案计算积分与金币」
    —— 判题器专门为"反复交、取最好"设计了这个字段，重复提交是预期用法。
    """

    code = "submitAnswer"
    roles = PIONEER

    def __init__(self, role_type: str, answer: str) -> None:
        super().__init__(role_type)
        self.answer = answer

    def to_wire(self) -> dict[str, Any]:
        return {"action": self.code, "taskAnswer": self.answer}
