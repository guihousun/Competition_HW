"""动作 → 线上报文。唯一懂线上动作格式的地方（`to_wire` 写、`describe` 读）。

一条指令就是一个对象，创建即校验：`roleType` 传错抛 `PermissionError`，非法动作根本造不
出来。"指令非法"计异常（5 次出局），而"角色没权限"算不算异常文档没写清，只能让它发不出
去。权限写窄了本地测不出来（报文照样合法，只有判题器会说"不"）。子类落地补四样：`code`
（动作码）、`roles`（可用角色，任务书 §4.4 表最右列）、参数、`to_wire`（编成扁平记录）。
"""

from typing import Any, Callable, ClassVar

from ..game.grid import Pos

WORKER = frozenset({"worker"})
PIONEER = frozenset({"pioneer"})
ALL = WORKER | PIONEER


def describe(cmds: dict[str, Any], *, clip: Callable[[str], str]) -> str:
    """把 `roleCommandMap` 压成一行日志：`10010 move (12,22)；10020 attack controllerId=10010 (4,4)`。

    字段是通用摊开的：`action` 之外的每个字段一律 `名=值`、`targetPos` 折成 `(x,y)`。
    不手写"哪个动作有哪几个字段"的清单 —— 那是会跟报文漂移的第二份真相，漏打一个字段不像
    崩溃那么显眼；`acceptTask` / `submitAnswer` 压根没有 `targetPos`，硬读会 `IndexError`
    ⇒ 整回合退化成空指令。`clip` 由调用方注入（唯一使用者 `app._log`）：截断规则属于日志
    层，这条边是注入而不是 import —— `describe` 的契约是"不认识日志"，只是字符串值过它。
    """
    parts = []
    for entity_id, cmd in cmds.items():
        fields = []
        for key, value in cmd.items():
            if key == "action":
                continue
            if key == "targetPos" and isinstance(value, list):
                # 坐标折成 `(x,y)`，不带字段名（全模块只有这一个坐标字段，带上是噪音）
                points = [p for p in value if isinstance(p, dict)]
                if points:
                    fields.append("、".join(f"({p.get('x')},{p.get('y')})" for p in points))
            else:
                fields.append(f"{key}={clip(value) if isinstance(value, str) else value}")
        parts.append(" ".join([str(entity_id), str(cmd.get("action", "?"))] + fields))
    return "；".join(parts) if parts else "（空指令）"


class BaseAction:
    """一条指令。创建即校验：角色无权则抛 `PermissionError`。"""

    code: ClassVar[str]
    roles: ClassVar[frozenset[str]]

    def __init__(self, role_type: str) -> None:
        # `role_type` 只用于校验，不存进对象（没有使用者）
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
    """建造一座建筑。仅工人、仅白天。

    白天那半边不在闸门里：它是"什么时候"而不是"谁"，由 `planner` / `Turn.is_day` 把关。
    `name` 取 `roleType` 同名（`wall` / `gatling` / `railgun` / `rocket`）。目标必须空且
    可建造：落在已有武器上，原武器会被覆盖成 level1（25 金币白花还降级）。
    """

    code = "build"
    roles = WORKER

    def __init__(self, role_type: str, name: str, target: Pos) -> None:
        super().__init__(role_type)
        self.name = name
        self.target = target

    def to_wire(self) -> dict[str, Any]:
        # key 是工人自己的 id（与 `move` 相同）；`targetPos` 是数组，即使只有一个点
        return {
            "action": self.code,
            "name": self.name,
            "targetPos": [{"x": self.target.x, "y": self.target.y}],
        }


class Remove(BaseAction):
    """拆掉一座围墙。仅工人、仅白天（白天那半边不进闸门，由 `planner` 把关）。

    §4.4："需指定与自身距离一格内的围墙位置" ⇒ 站位契约与 `build` 相同（切比雪夫 ≤1）。
    拆完那格变可穿越空地，但不回收石头 —— 拆墙不是免费的。实测报文（`docs/response.txt`）
    里没有 `name` 字段（`build` 才有），多打一个字段就是赌"指令非法"。昼夜限制任务书没写，
    按"仅白天"保守执行：夜里不发最多少拆一次，反之若实际禁夜就是指令非法。打空处 / 目标
    不是己方围墙 ⇒ 执行失败、不计异常。
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
    """采集矿石。仅工人。

    能力列没有昼夜限制（`build` 写"仅白天"、`attack` 写"仅黑夜"，它那格是空的）⇒ 昼夜门
    由 `planner` 把关、不进闸门（闸门只管"谁"）。`targetPos` 是矿石的坐标而不是自己的站位；
    须站在矿的切比雪夫 ≤1 内（矿格挡路，"站在矿上"不可能）。一次采 1 块，一座矿采满 10 次
    就消失，payload 没有"剩余次数"字段。形状与 `move` 同形，没有实证报文。
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
    """操作武器打目标格。仅黑夜（昼夜门由 `planner` 把关，不进闸门），全部角色都能做
    （开拓者也在炮位上）。

    报文形状与其它动作是反的，最易写错：`roleCommandMap` 的 key 是武器 id，操控者在
    `controllerId` 里 ⇒ `planner` 那边要 `_emit(..., key=str(weapon.id))`。两条站位硬规则
    （不满足是执行失败、不计异常，但白打一发）：操控者须站在武器周围一格内、一人只能操
    一座；目标格须在射程内（打空处 = 执行失败）。

    `targets` 的**个数必须等于武器当前等级**（接口文档 L218）：加特林/火箭 L2 发 2 个、
    L3 发 3 个，电磁狙击炮恒 1 个 ⇒ 个数由 `planner` 按 `Weapon.level` 决定，这里照单编。
    """

    code = "attack"
    roles = ALL

    def __init__(self, role_type: str, controller_id: str, targets: tuple[Pos, ...]) -> None:
        super().__init__(role_type)
        # 接口文档标的是 String —— 在这里定死，别让 int 漏进 JSON
        self.controller_id = str(controller_id)
        self.targets = targets

    def to_wire(self) -> dict[str, Any]:
        return {
            "action": self.code,
            "controllerId": self.controller_id,
            "targetPos": [{"x": t.x, "y": t.y} for t in self.targets],
        }


class Sell(BaseAction):
    """把矿石卖给小贩换金币。须站在小贩周围一格内。

    可用角色是"全部"——与 `build` / `collect` 的"仅工人"相反。窄成 `WORKER` 的症状是
    "本地全绿、判题器说不"。能力列没有昼夜限制 ⇒ 昼夜门由 `planner` 把关、不进闸门。
    `name` 是矿种（英文，与 `neutralType` / `vendorShopList.name` 同一套词），`num` 是批量
    卖的件数（Int，不填默认 1）。形状从接口文档推的，没有实证报文。
    """

    code = "sell"
    roles = ALL

    def __init__(self, role_type: str, name: str, num: int) -> None:
        super().__init__(role_type)
        self.name = name
        # 接口文档标的是 Int —— 别让 str 漏进 JSON（`Attack.controllerId` 正好相反）
        self.num = num

    def to_wire(self) -> dict[str, Any]:
        return {"action": self.code, "name": self.name, "num": self.num}


class Buy(BaseAction):
    """在武器商店买一件商品。须站在武器商店周围一格内（§4.4），可用角色是"全部"。

    `name` 是商品名（英文，与 `weaponShopList.name` 同一套词，如 `WeaponUpgradeVoucher1`）；
    `num` 是 Int（不填默认 1，支持批量）。能力列没有昼夜限制 ⇒ 昼夜门由 `planner` 把关
    （这条线只在白天跑）。形状从接口文档推，没有实证报文（与 `sell` 同一类风险）。
    """

    code = "buy"
    roles = ALL

    def __init__(self, role_type: str, name: str, num: int) -> None:
        super().__init__(role_type)
        self.name = name
        # 接口文档标的是 Int —— 别让 str 漏进 JSON（与 `Sell.num` 同一条规则）
        self.num = num

    def to_wire(self) -> dict[str, Any]:
        return {"action": self.code, "name": self.name, "num": self.num}


class Use(BaseAction):
    """使用背包里的一件商品。可用角色是"全部"。

    目前只用于升级券：须站在目标建筑周围一格内并指定 `targetPos`（任务书 L292）⇒ `target`
    必填。已 level3 再用不生效、券不消耗，非法使用也不消耗（L293-294）⇒ 发错顶多白跑一趟、
    不碰红线。形状从接口文档推，没有实证报文。
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
    """领取任务。仅开拓者，无额外参数。

    报文里没有 `targetPos` —— 领哪个任务点由站位决定（须在己方任务点周围一格内）。实测报文：
    `{"10011":{"action":"acceptTask"}}`。在敌方任务点执行无效，但 `teamOur.playerTasks` 已经
    把阵营滤好了。
    """

    code = "acceptTask"
    roles = PIONEER

    def to_wire(self) -> dict[str, Any]:
        return {"action": self.code}


class SubmitAnswer(BaseAction):
    """提交任务答案。仅开拓者。`taskAnswer` 是 String，同样没有 `targetPos`。

    实测报文：`{"10011":{"action":"submitAnswer","taskAnswer":"xxx"}}`。可以反复提交：判题器
    按"提交过的通过率最高的答案"算分，重复提交是预期用法。空答案不发（闸门只管"谁"，"空不
    空"由 `planner` 把关）。
    """

    code = "submitAnswer"
    roles = PIONEER

    def __init__(self, role_type: str, answer: str) -> None:
        super().__init__(role_type)
        self.answer = answer

    def to_wire(self) -> dict[str, Any]:
        return {"action": self.code, "taskAnswer": self.answer}
