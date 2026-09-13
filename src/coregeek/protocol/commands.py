"""Intent → 线上指令，**并且是唯一的合法性校验点**。

设计见 docs/design/code-design.md §3.1 / §8。

为什么校验放在这里、而不是散在 planner 里：
> 判题器的"异常次数"红线只认三类（连接/响应超时、报文格式错、指令非法），
> 累计 5 次该队就不再被调度。所以**每一条可能出错的路径都必须汇到一个地方**，
> 才谈得上"这条红线可控"。这里就是那个地方。

本模块的硬约束：

1. **永不抛异常**。任何一条指令编码失败都只是被丢弃并记入 `rejected`，
   绝不冒泡到响应管线（那里只能返回合法空指令）。
2. **宁可少发一条，绝不发一条非法的**。丢一条只损失一个角色的回合；
   发一条非法指令则在赌整支队伍的调度资格。
3. `roleCommandMap` 的 key 用**字符串**（JSON 对象的 key 本来就是字符串，
   demo 也显式 `str()` 过）。用 int 做 key 在 Python 里能跑，
   但一旦别处拿 int 去查就会全 miss——历史上已经踩过一次。

⚠️ `attack` 的两处最易写反的地方：
  - `roleCommandMap` 的 **key 是武器 id**，`controllerId` 才是操控角色；
  - `targetPos` 的长度 = **武器当前等级**（电磁狙击炮恒为 1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..domain import calendar
from ..domain.grid import Pos
from ..domain.intent import (
    BUILDABLE,
    WEAPON_TYPES,
    AcceptTask,
    Attack,
    Build,
    Buy,
    Collect,
    Drop,
    Idle,
    Intent,
    Move,
    Remove,
    Sell,
    SubmitAnswer,
    SummonTreasure,
    Use,
)
from .model import Role, Turn

__all__ = [
    "ACTION_CODES",
    "EncodeResult",
    "encode_all",
    "encode_one",
    "validate",
]

#: 接口文档 §2.3 的动作码全集
ACTION_CODES = frozenset(
    {
        "move", "attack", "sell", "buy", "build", "remove",
        "acceptTask", "submitAnswer", "summonTreasure", "use", "drop", "collect",
    }
)

#: 升级券必须带 targetPos（任务书 L290：需在目标建筑周围一格内使用）
_NEEDS_TARGET = frozenset(
    {
        "WeaponUpgradeVoucher1", "WeaponUpgradeVoucher2",
        "StationUpgradeVoucher1", "StationUpgradeVoucher2",
        "WallUpgradeVoucher1", "WallUpgradeVoucher2",
        "Dizzy", "Bomb",
    }
)

_MAX_NUM = 10_000


@dataclass
class EncodeResult:
    """编码结果。`rejected` 会原样进日志——它是排查"为什么这回合没动作"的唯一线索。"""

    commands: dict[str, dict[str, Any]] = field(default_factory=dict)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, key: int, cmd: dict[str, Any], what: str) -> None:
        skey = str(key)
        if skey in self.commands:
            self.rejected.append((what, f"角色 {key} 本回合已有指令（每角色至多 1 条）"))
            return
        self.commands[skey] = cmd


def encode_all(intents: Iterable[Intent], turn: Turn) -> EncodeResult:
    """把一组 Intent 编成 `roleCommandMap`。**永不抛异常。**"""
    out = EncodeResult()
    for intent in intents:
        try:
            _encode_into(intent, turn, out)
        except Exception as exc:  # noqa: BLE001 - 响应管线绝不能因编码失败而崩
            out.rejected.append((type(intent).__name__, f"编码异常 {type(exc).__name__}: {exc}"))
    return out


def _encode_into(intent: Intent, turn: Turn, out: EncodeResult) -> None:
    if isinstance(intent, Idle):
        out.warnings.append(f"角色 {intent.role_id} 主动待机：{intent.reason}")
        return

    pair = encode_one(intent, turn)
    if pair is None:
        out.rejected.append((type(intent).__name__, f"角色 {intent.role_id}：无法编码"))
        return
    key, cmd = pair

    # 校验必须在**这里**自动发生，而不是指望调用方记得再调一次 validate()。
    # "唯一入口可控"只有在不可绕过时才成立。
    why = validate(cmd, turn, key)
    if why is not None:
        out.rejected.append((type(intent).__name__, f"{why}（key={key}）"))
        return

    out.add(key, cmd, type(intent).__name__)


def encode_one(intent: Intent, turn: Turn) -> tuple[int, dict[str, Any]] | None:
    """单个 Intent → `(roleCommandMap 的 key, 指令体)`。非法则 None。

    返回值**未经校验**，调用方必须再过一遍 `validate()`——这里只做映射。
    """
    if isinstance(intent, Move):
        return intent.role_id, _wire("move", targetPos=[intent.dest.dump()])
    if isinstance(intent, Collect):
        return intent.role_id, _wire("collect", targetPos=[intent.target.dump()])
    if isinstance(intent, Remove):
        return intent.role_id, _wire("remove", targetPos=[intent.target.dump()])
    if isinstance(intent, Build):
        return intent.role_id, _wire(
            "build", name=intent.name, targetPos=[intent.target.dump()]
        )
    if isinstance(intent, Attack):
        return intent.weapon_id, _wire(
            "attack",
            controllerId=str(intent.role_id),
            targetPos=[p.dump() for p in intent.targets],
        )
    if isinstance(intent, Sell):
        return intent.role_id, _wire("sell", name=intent.name, num=intent.num)
    if isinstance(intent, Buy):
        return intent.role_id, _wire("buy", name=intent.name, num=intent.num)
    if isinstance(intent, Use):
        cmd = _wire("use", name=intent.name)
        if intent.target is not None:
            cmd["targetPos"] = [intent.target.dump()]
        return intent.role_id, cmd
    if isinstance(intent, Drop):
        return intent.role_id, _wire("drop", name=intent.name)
    if isinstance(intent, AcceptTask):
        return intent.role_id, _wire("acceptTask")
    if isinstance(intent, SubmitAnswer):
        return intent.role_id, _wire("submitAnswer", taskAnswer=intent.answer)
    if isinstance(intent, SummonTreasure):
        return intent.role_id, _wire(
            "summonTreasure",
            targetPos=[intent.target.dump()],
            item=list(intent.items),
        )
    return None


def _wire(action: str, **kw: Any) -> dict[str, Any]:
    return {"action": action, **kw}


# ══ 校验器 ═══════════════════════════════════════════════════════════
def validate(cmd: Any, turn: Turn, key: int) -> str | None:
    """逐条校验一条即将发出的指令。返回 None = 合法，否则返回**拒绝理由**。

    这里刻意重复了 `encode_one` 已经保证过的东西。理由：`encode_one` 今天是对的，
    明天有人加一个新的 Intent 子类、或改了 `targets` 的构造方式，就可能悄悄不对。
    这一段是"最后一道闸"，它只认报文本身的形状，不认"谁生成的"。
    """
    if not isinstance(cmd, dict):
        return "指令不是对象"

    action = cmd.get("action")
    if not isinstance(action, str):
        return "action 缺失或非字符串"
    if action not in ACTION_CODES:
        return f"action {action!r} 不在动作码全集内"

    role = turn.role(key)
    if action == "attack":
        return _validate_attack(cmd, turn, key)

    if role is None:
        return f"角色 {key} 不在本方队伍中"

    if action in ("move", "collect", "remove"):
        return _check_target_pos(cmd, 1)

    if action == "build":
        name = cmd.get("name")
        if name not in BUILDABLE:
            return f"build 的 name {name!r} 不在 {BUILDABLE} 内"
        return _check_target_pos(cmd, 1)

    if action in ("sell", "buy"):
        return _check_trade(cmd, turn)

    if action == "use":
        name = cmd.get("name")
        if not isinstance(name, str) or not name:
            return "use 缺少 name"
        if name in _NEEDS_TARGET:
            return _check_target_pos(cmd, 1)
        if "targetPos" in cmd:
            return _check_target_pos(cmd, 1)
        return None

    if action == "drop":
        name = cmd.get("name")
        if not isinstance(name, str) or not name:
            return "drop 缺少 name"
        return None

    if action in ("acceptTask", "submitAnswer"):
        if role.role_type != "pioneer":
            return f"{action} 仅开拓者可用，角色 {key} 是 {role.role_type}"
        if action == "submitAnswer":
            answer = cmd.get("taskAnswer")
            if not isinstance(answer, str) or not answer:
                return "submitAnswer 缺少 taskAnswer"
        return None

    if action == "summonTreasure":
        items = cmd.get("item")
        if not isinstance(items, list) or not items:
            return "summonTreasure 的 item 必须是非空字符串数组"
        if not all(isinstance(i, str) and i for i in items):
            return "summonTreasure 的 item 含空项"
        return _check_target_pos(cmd, 1)

    return f"action {action!r} 没有对应的校验分支"  # 提前拦下：新动作码必须显式支持


def _validate_attack(cmd: dict[str, Any], turn: Turn, weapon_id: int) -> str | None:
    """攻击是唯一"白天发就一定非法"的动作，也是唯一 key 不是操控者的动作。"""
    weapon = turn.role(weapon_id)
    if weapon is None:
        return f"武器 {weapon_id} 不在本方队伍中"
    if not weapon.is_weapon:
        return f"攻击指令的 key 应是武器 id，但 {weapon_id} 是 {weapon.role_type}"

    controller_raw = cmd.get("controllerId")
    if not isinstance(controller_raw, str):
        return "attack 缺少 controllerId（必须是字符串形式的角色 ID）"
    try:
        controller_id = int(controller_raw)
    except (TypeError, ValueError):
        return f"controllerId {controller_raw!r} 不是整数"
    controller = turn.role(controller_id)
    if controller is None:
        return f"操控者 {controller_id} 不在本方队伍中"
    if not controller.is_controllable:
        return f"操控者 {controller_id} 是 {controller.role_type}，不能操控武器"

    slots = weapon.target_slots()
    err = _check_target_pos(cmd, slots)
    if err:
        return f"{err}（{weapon.role_type} 当前等级要求 {slots} 个落点）"

    if not calendar.is_night(turn.round_no):
        return f"attack 仅黑夜可用，roundNo={turn.round_no} 是白天"

    # 锥形约束：任意两个落点相对武器的方向夹角必须 ≤ 90°，否则整次攻击非法。
    # 只在能算出"方向"时检查——落点与武器重合时方向无定义，交给判题器判。
    targets = _targets(cmd)
    if len(targets) > 1:
        cone = _cone_violation(weapon.pos, targets)
        if cone:
            return cone
    return None


def _cone_violation(origin: Pos, targets: tuple[Pos, ...]) -> str | None:
    """加特林的多落点必须落在同一 90° 锥形内（任务书 §4.5 第 4 条）。

    用向量点积判夹角：`dot < 0` 即夹角 > 90°。落点与武器重合（零向量）无法定方向，
    跳过——这种落点本身多半会被判题器判为非法，不该由我们替它下结论。
    """
    dirs = [(t.x - origin.x, t.y - origin.y) for t in targets]
    dirs = [d for d in dirs if d != (0, 0)]
    for i, (ax, ay) in enumerate(dirs):
        for bx, by in dirs[i + 1:]:
            if ax * bx + ay * by < 0:
                return (
                    f"加特林落点 ({ax},{ay}) 与 ({bx},{by}) 相对武器的夹角 > 90°，"
                    "整次攻击非法"
                )
    return None


def _check_target_pos(cmd: dict[str, Any], want: int) -> str | None:
    targets = cmd.get("targetPos")
    if not isinstance(targets, list):
        return "缺少 targetPos 数组"
    if len(targets) != want:
        return f"targetPos 长度应为 {want}，实际 {len(targets)}"
    for t in targets:
        if not isinstance(t, dict):
            return "targetPos 的元素不是坐标对象"
        for axis in ("x", "y"):
            v = t.get(axis)
            if isinstance(v, bool) or not isinstance(v, int):
                return f"targetPos 的 {axis} 不是整数：{v!r}"
    return None


def _targets(cmd: dict[str, Any]) -> tuple[Pos, ...]:
    out = []
    for t in cmd.get("targetPos") or ():
        if isinstance(t, dict):
            out.append(Pos(int(t.get("x") or 0), int(t.get("y") or 0)))
    return tuple(out)


def _check_trade(cmd: dict[str, Any], turn: Turn) -> str | None:
    name = cmd.get("name")
    if not isinstance(name, str) or not name:
        return "买卖指令缺少 name"

    num = cmd.get("num", 1)
    if isinstance(num, bool) or not isinstance(num, int):
        return f"num 必须是整数，实际 {num!r}"
    if num < 1 or num > _MAX_NUM:
        return f"num={num} 越界（1..{_MAX_NUM}）"

    # 商店清单缺失时不拦——样例 payload 的商店列表未必每回合都全，
    # 拿不到清单就无从判断，宁可让判题器去否决（指令失败不计异常）。
    shop = turn.weapon_shop + turn.vendor_shop
    if shop:
        names = {item.name for item in shop}
        if name not in names:
            return f"{name!r} 不在本回合商店清单内（{sorted(names)}）"
    return None


def describe(intent: Intent) -> str:
    """一行摘要，进日志用。不要把整个 Intent 塞进日志——靠 `reason` 才读得懂。"""
    parts = [type(intent).__name__, f"role={intent.role_id}"]
    if intent.reason:
        parts.append(f"why={intent.reason}")
    return " ".join(parts)


# 供 planner 反查"这座武器现在最多能打几个落点"
def attack_slots(weapon: Role) -> int:
    return weapon.target_slots()


def is_weapon_name(name: str) -> bool:
    return name in WEAPON_TYPES
