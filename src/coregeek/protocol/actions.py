"""动作 → 线上报文。唯一懂线上动作格式的地方（`to_wire` 写、`describe` 读）。

一条指令就是一个对象，创建即校验：`roleType` 传错抛 `PermissionError`，非法动作根本造不
出来 —— "指令非法"计异常（5 次出局），只能让它发不出去。权限写窄了本地测不出来（报文
合法，只有判题器会说"不"）。子类落地补四样：`code`（动作码）、`roles`（可用角色，§4.4 表
最右列）、参数、`to_wire`。
"""

from typing import Any, Callable, ClassVar

from ..game.grid import Pos

WORKER = frozenset({"worker"})
PIONEER = frozenset({"pioneer"})
ALL = WORKER | PIONEER


def describe(cmds: dict[str, Any], *, clip: Callable[[str], str]) -> str:
    """把 `roleCommandMap` 压成一行日志：`10010 move (12,22)；10020 attack controllerId=10010 (4,4)`。

    字段通用摊开：`action` 之外的每个字段一律 `名=值`、`targetPos` 折成 `(x,y)`。不手写
    "哪个动作有哪几个字段"的清单 —— 那是会跟报文漂移的第二份真相；`acceptTask` /
    `submitAnswer` 压根没有 `targetPos`，硬读会 `IndexError` ⇒ 整回合退化成空指令。`clip`
    由调用方注入（`describe` 的契约是"不认识日志"，这条边是注入而不是 import）。"""
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
    """建造一座建筑。仅工人、仅白天（白天那半边由 `planner` / `Turn.is_day` 把关）。

    `name` 取 `roleType` 同名。目标必须空且可建造：落在已有武器上，原武器会被覆盖成
    level1（25 金币白花还降级）。"""
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
    """拆掉一座围墙。仅工人、仅白天（任务书没写昼夜，按"仅白天"保守执行；报文实证
    `docs/response.txt`：没有 `name` 字段）。

    站位契约与 `build` 相同（切比雪夫 ≤1）。拆完那格变可穿越空地，但不回收石头。打空处 /
    目标不是己方围墙 ⇒ 执行失败、不计异常。"""
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
    """采集矿石。仅工人；能力列没有昼夜限制 ⇒ 昼夜门由 `planner` 把关、不进闸门。

    `targetPos` 是矿石的坐标（不是自己的站位），与 `move` 同形，没有实证报文。须站在矿的
    切比雪夫 ≤1 内（矿格挡路，"站在矿上"不可能）。一次采 1 块，一座矿采满 10 次就消失。"""
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
    """操作武器打目标格。仅黑夜（昼夜门由 `planner` 把关），全部角色都能做。

    报文形状与其它动作是反的，最易写错：`roleCommandMap` 的 key 是武器 id，操控者在
    `controllerId` 里。站位硬规则（不满足是执行失败、不计异常）：操控者须站在武器周围一格
    内、一人只能操一座；目标格须在射程内。`targets` 的个数必须等于武器当前等级（接口文档
    L218）：电磁恒 1、加特林/火箭 = 等级数，由 `planner` 按 `Weapon.level` 决定，这里照单编。"""
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
    """把矿石卖给小贩换金币。须站在小贩周围一格内；可用角色是"全部"（与 `build` /
    `collect` 相反 —— 窄成工人是"本地全绿、判题器说不"）。昼夜门由 `planner` 把关。

    `name` 是矿种（英文，与 `neutralType` / `vendorShopList.name` 同一套词），`num` 是批量
    件数（Int）。形状从接口文档推的，没有实证报文。"""
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

    `name` 是商品名（英文，与 `weaponShopList.name` 同一套词）；`num` 是 Int（不填默认 1）。
    昼夜门由 `planner` 把关（这条线只在白天跑）。形状从接口文档推，没有实证报文。"""
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
    必填。已 level3 再用不生效、券不消耗（L293-294）⇒ 发错顶多白跑一趟、不碰红线。形状从
    接口文档推，没有实证报文。"""
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
    """领取任务。仅开拓者，无额外参数 —— 领哪个任务点由站位决定（须在己方任务点周围
    一格内）。实测报文：`{"10011":{"action":"acceptTask"}}`。"""
    code = "acceptTask"
    roles = PIONEER

    def to_wire(self) -> dict[str, Any]:
        return {"action": self.code}


class SubmitAnswer(BaseAction):
    """提交任务答案。仅开拓者。`taskAnswer` 是 String，没有 `targetPos`。实测报文：
    `{"10011":{"action":"submitAnswer","taskAnswer":"xxx"}}`。可以反复提交：判题器按
    "通过率最高的答案"算分。空答案不发（由 `planner` 把关）。"""
    code = "submitAnswer"
    roles = PIONEER

    def __init__(self, role_type: str, answer: str) -> None:
        super().__init__(role_type)
        self.answer = answer

    def to_wire(self) -> dict[str, Any]:
        return {"action": self.code, "taskAnswer": self.answer}
