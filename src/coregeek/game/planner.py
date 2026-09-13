"""决策入口：`Turn` → `roleCommandMap`。**策略只写在这里。**

第 2 步只做一件事：**所有角色朝我方基地走一格**。
目的是把"解析 → 决策 → 编码 → 响应"整条管线用真指令跑通，
岗位分配（谁采矿、谁建墙、夜里谁守哪座武器）留给后续步骤。

返回 `{角色ID: 指令}`，key 用**字符串** —— JSON 对象的 key 本来就是字符串。
"""

from typing import Any

from ..protocol import commands  # 唯一一条"由内往外"的依赖：指令只能经编码器产出
from .grid import Pos, step_toward
from .world import MOVERS, Turn


def plan(turn: Turn) -> dict[str, dict[str, Any]]:
    if turn.station is None:
        return {}

    cmds: dict[str, dict[str, Any]] = {}
    #: 本回合已被认领的落脚格。不设它，两个人冲同一个空格会按"目标点争夺"双双停住（任务书 §4.5.4）
    claimed: set[Pos] = set()
    for role in turn.roles:
        if role.role_type not in MOVERS:
            continue
        target = step_toward(role.pos, turn.station, turn.blocked | claimed)
        if target is None:
            continue
        claimed.add(target)
        cmds[str(role.id)] = commands.move(target)
    return cmds
