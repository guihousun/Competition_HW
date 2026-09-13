"""决策入口：`Turn` → `roleCommandMap`。**策略只写在这里。**

目前只做一件事：**所有角色朝我方基地走一格**。目的是把"解析 → 决策 → 构造 Action →
校验 → 编码 → 响应"整条管线跑通；岗位分配（谁采矿、谁建墙、夜里谁守哪座武器）留给后续步骤。

返回 `{角色ID: 指令}`，key 用**字符串** —— JSON 对象的 key 本来就是字符串。
"""

import logging
from typing import Any

from ..protocol import actions  # 唯一一条"由内往外"的依赖：指令只能经 Action 产出
from .grid import Pos, step_toward
from .world import Turn

LOGGER = logging.getLogger(__name__)


def plan(turn: Turn) -> dict[str, dict[str, Any]]:
    if turn.station is None:
        return {}

    cmds: dict[str, dict[str, Any]] = {}
    #: 本回合已被认领的落脚格。不设它，两个人冲同一个空格会按"目标点争夺"双双停住（任务书 §4.5.4）
    claimed: set[Pos] = set()

    for role in turn.roles:
        target = step_toward(role.pos, turn.station, turn.blocked | claimed)
        if target is None:
            continue  # 没有更近的落脚点：不动作，而不是撞进去
        try:
            action = actions.Move(role.type_name, target)
        except PermissionError as exc:
            # 闸门。丢**这一条**，不连坐同回合其他角色 —— 若越权是系统性的，抛出去会变成
            # "每回合空指令 → 全队冻结一整局"，现象与 main3.py 改名事故一样难排查。
            LOGGER.warning("拦下越权动作：角色 %s(%s) %s", role.id, role.type_name, exc)
            continue
        claimed.add(target)
        cmds[str(role.id)] = action.to_wire()

    return cmds
