"""动作 → 线上报文。**唯一懂线上动作格式的地方**（`to_wire` 写、`describe` 读）。

一条指令就是一个对象，**创建即校验**：`roleType` 传错连对象都造不出来。
这是按红线设计的 —— "指令非法"计异常、累计 5 次出局，而"角色没权限"到底算不算异常
文档没写清，口径不明就只能让它**发不出去**。

子类落地时补四样：`code`（动作码）、`roles`（可用角色，任务书 §4.4 表格**最右列**）、
参数（`RoleCommand` 里该动作用到的字段）、`to_wire`（编成扁平记录）。

已实现十个：`move` / `attack` / `sell` / `buy` / `use`（**对全部角色合法**）、
`build` / `remove` / `collect`（**仅工人**）、`acceptTask` / `submitAnswer`（**仅开拓者**）。
没实现的动作为空壳，等落地时再加。
⚠️ 权限两个方向都会错：窄了（`sell` 写成仅工人）本地测不出来 —— 报文照样合法，只有判题器会说"不"。
"""

from typing import Any, Callable, ClassVar

from ..game.grid import Pos

WORKER = frozenset({"worker"})
PIONEER = frozenset({"pioneer"})
ALL = WORKER | PIONEER


def describe(cmds: dict[str, Any], *, clip: Callable[[str], str]) -> str:
    """把 `roleCommandMap` 压成**一行**日志：`10010 move (12,22)；10020 attack controllerId=10010 (4,4)`。

    **字段是通用摊开的，不是逐个手写的**：`action` 之外的每个字段一律 `名=值` 印出来，
    `targetPos` 折成 `(x,y)`。手写一份"哪个动作有哪几个字段"的清单就是又一份会跟报文漂移的
    真相（漏打一个字段不像崩溃那么显眼）。顺带拆掉一颗老雷：原来硬读 `cmd["targetPos"]`，
    而 `acceptTask` / `submitAnswer` 压根没有这个字段 ⇒ `IndexError` ⇒ 整回合退化成空指令。

    `clip` 由**调用方注入**（唯一使用者是 `app._log`）：截断规则属于日志层。
    ⚠️ 这条边**是注入而不是 import** —— `describe` 的契约是"不认识日志"，给它塞一个日志默认值
    就等于让 `protocol` 认识日志层。字符串值一律过它，这里不必知道哪个字段是自由文本。
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
        #: `role_type` 只用于校验，**不存进对象**（没有使用者）
        if role_type not in self.roles:
            raise PermissionError(f"{role_type} 无权执行 {self.code}")

    def to_wire(self) -> dict[str, Any]:
        raise NotImplementedError(self.code)


class Move(BaseAction):
    """移动一格。每回合每个角色均可执行一次。"""

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
    """建造一座建筑。**仅工人**，且**仅白天**。

    白天那半边**不在闸门里**：它是"什么时候"而不是"谁"，由 `planner` / `Turn.is_day` 把关。
    `name` 取 `roleType` 同名（`wall` / `gatling` / `railgun` / `rocket`）。
    目标必须是**空的可建造格**：落在已有武器上的话原武器会被覆盖成 level1（25 金币白花还降级）。
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


class Remove(BaseAction):
    """拆掉一座围墙。**仅工人**，**仅白天**（与 `build` 同一条：白天那半边不进闸门，由 `planner` 把关）。

    §4.4 原文"用于拆除围墙，**需指定与自身距离一格内的围墙位置**" ⇒ 站位契约与 `build`
    完全相同（切比雪夫 ≤1）。拆完那格变**可穿越空地**（任务书 L73），
    但**不回收建造时花掉的石头**（L209）—— 所以拆墙不是免费的，`planner` 那边要按成本掂量。

    实证报文（`docs/response.txt`，全项目唯一有实证的三个动作之一）：
    `{"action":"remove","targetPos":[{"x":29,"y":7}]}` —— **没有 `name` 字段**（`build` 才有）。
    多打一个字段就是赌"指令非法"，所以用例里专门钉了 `"name" not in wire`。

    ⚠️ 昼夜限制**任务书那一格没写**（`build` 写了"仅白天"），但项目口径按"仅白天"保守执行：
    夜里不发最多是少拆一次，反过来若实际禁夜就是指令非法 —— 红线优先。
    打空处 / 目标不是己方围墙 ⇒ **执行失败，不计异常**（只回 `lastRoundRoleActionsResult[id]=false`）。
    """

    code = "remove"
    roles = WORKER

    def __init__(self, role_type: str, target: Pos) -> None:
        super().__init__(role_type)
        self.target = target

    def to_wire(self) -> dict[str, Any]:
        return {
            "action": self.code,
            "targetPos": [{"x": self.target.x, "y": self.target.y}],
        }


class Collect(BaseAction):
    """采集矿石。**仅工人**。

    **能力列没有昼夜限制**（`build` 写"仅白天"、`attack` 写"仅黑夜"，这一格是空的）
    ⇒ 昼夜门由 `planner` 把关，不进闸门（闸门只管"谁"）。

    `targetPos` 是**矿石的坐标**，不是自己的站位；须站在矿的切比雪夫 ≤1 内
    （矿格本身挡路，所以"站在矿上"不可能发生）。一次采 1 块，一座矿采满 10 次就消失，
    但 payload 里没有"剩余次数"字段。形状与 `move` 同形，**没有实证报文**。
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
    """操作武器打**一个格子**。**仅黑夜**，且**全部角色**都能做（开拓者也在炮位上）。

    ⚠️ **报文形状与其它动作是反的，最易写错**：`roleCommandMap` 的 **key 是武器 id**，
    操控者在 `controllerId` 里 —— 所以 `planner` 那边要用 `_emit(..., key=str(weapon.id))`。
    实证报文：`{"action":"attack","controllerId":"10010","targetPos":[{"x":29,"y":7}]}`

    两条站位硬规则（不满足是"指令执行失败"，不计异常，但白打一发）：操控者须站在武器
    周围一格内，且一人同时只能操控一座；目标格须在射程内（打空处 = 执行失败）。
    多目标（`targetPos` 数量 = 武器等级数）不做：我们的武器永远是 L1。
    白天那半边不在闸门里（§4.4 是"全部"），由 `planner` 把关。
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


class Sell(BaseAction):
    """把矿石卖给小贩换金币。**须站在小贩周围一格内**。

    ⚠️ **可用角色是"全部"** —— 与 `build` / `collect` 的"仅工人"**相反**。窄成 `WORKER`
    的症状是"本地全绿、判题器说不"：报文完全合法，只有判题器知道开拓者也能卖。

    **能力列没有昼夜限制** ⇒ 昼夜门由 `planner` 把关，不进闸门（与 `collect` 同）。
    `name` 是**矿种**（英文，与 `neutralType` / `vendorShopList.name` 同一套词），
    `num` 是**批量卖的件数**（Int，不填默认 1）。形状从接口文档推的，**没有实证报文**。
    """

    code = "sell"
    roles = ALL

    def __init__(self, role_type: str, name: str, num: int) -> None:
        super().__init__(role_type)
        self.name = name
        #: 接口文档标的是 Int —— 别让 str 漏进 JSON（`Attack.controllerId` 正好相反）
        self.num = num

    def to_wire(self) -> dict[str, Any]:
        return {"action": self.code, "name": self.name, "num": self.num}


class Buy(BaseAction):
    """在武器商店买一件商品。**须站在武器商店周围一格内**（§4.4），**可用角色是"全部"**。

    `name` 是**商品名**（英文，与 `weaponShopList.name` 同一套词，如 `WeaponUpgradeVoucher1`）
    —— 与 `sell` 的矿种、`build` 的建筑名同一个 `name` 约定；`num` 是 Int（不填默认 1，
    支持批量）。**能力列没有昼夜限制** ⇒ 昼夜门由 `planner` 把关（这条线只在白天跑）。
    ⚠️ 形状从接口文档推，**没有实证报文**（与 `sell` 同一类风险）。
    """

    code = "buy"
    roles = ALL

    def __init__(self, role_type: str, name: str, num: int) -> None:
        super().__init__(role_type)
        self.name = name
        #: 接口文档标的是 Int —— 别让 str 漏进 JSON（与 `Sell.num` 同一条规则）
        self.num = num

    def to_wire(self) -> dict[str, Any]:
        return {"action": self.code, "name": self.name, "num": self.num}


class Use(BaseAction):
    """使用背包里的一件商品。**可用角色是"全部"**。本步只用于**升级券**：
    须**站在目标建筑周围一格内**并指定 `targetPos`（任务书 L292）⇒ `target` 必填。

    已 level3 再用不生效、券不消耗；非法使用也不消耗（L293-294）⇒ 发错顶多白跑一趟，
    不碰红线。⚠️ 形状从接口文档推，**没有实证报文**。
    """

    code = "use"
    roles = ALL

    def __init__(self, role_type: str, name: str, target: Pos) -> None:
        super().__init__(role_type)
        self.name = name
        self.target = target

    def to_wire(self) -> dict[str, Any]:
        return {
            "action": self.code,
            "name": self.name,
            "targetPos": [{"x": self.target.x, "y": self.target.y}],
        }


class AcceptTask(BaseAction):
    """领取任务。**仅开拓者**，**无额外参数**。

    ⚠️ 报文里**没有 `targetPos`** —— 领哪个任务点由**站位**决定（须在己方任务点周围一格内）。
    实证报文：`{"10011":{"action":"acceptTask"}}`。在**敌方**任务点执行无效，
    但 `teamOur.playerTasks` 已经把阵营滤好了。
    """

    code = "acceptTask"
    roles = PIONEER

    def to_wire(self) -> dict[str, Any]:
        return {"action": self.code}


class SubmitAnswer(BaseAction):
    """提交任务答案。**仅开拓者**。`taskAnswer` 是 **String**、同样**没有 `targetPos`**。

    实证报文：`{"10011":{"action":"submitAnswer","taskAnswer":"xxx"}}`。

    可以**反复提交**：判题器按"提交过的通过率最高的答案"算分，重复提交是预期用法。
    **空答案不发**（闸门只管"谁"，"空不空"由 `planner` 把关）。
    """

    code = "submitAnswer"
    roles = PIONEER

    def __init__(self, role_type: str, answer: str) -> None:
        super().__init__(role_type)
        self.answer = answer

    def to_wire(self) -> dict[str, Any]:
        return {"action": self.code, "taskAnswer": self.answer}
