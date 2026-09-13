"""决策入口：`Turn` → `roleCommandMap`。**策略只写在这里。**

目前只做一件事：**两个工人各自朝最近的石矿走一格**，开拓者不发指令。
目的是把"解析地图 → 决策 → 构造 Action → 校验 → 编码 → 响应"整条管线用真指令跑通。

返回 `{角色ID: 指令}`，key 用**字符串** —— JSON 对象的 key 本来就是字符串。
"""

import logging
from typing import Any

from ..protocol import actions  # 唯一一条"由内往外"的依赖：指令只能经 Action 产出
from .grid import Pos, step_toward
from .roles import Worker
from .world import Turn

LOGGER = logging.getLogger(__name__)

#: 石矿（接口文档 §1.2.1）。采石是第 1 天防御线的料源（任务书 §4.4 `build` 需背包有石头）。
STONE = "stone"


def plan(turn: Turn) -> dict[str, dict[str, Any]]:
    cmds: dict[str, dict[str, Any]] = {}
    #: 本回合已被认领的落脚格。不设它，两个人冲同一个空格会按"目标点争夺"双双停住（任务书 §4.5.4）
    claimed: set[Pos] = set()

    for role in turn.roles:
        if not isinstance(role, Worker):
            continue  # 开拓者这一步不动（见 code-task.md「不做什么」）

        goal = _nearest_stone(role.pos, turn.mines)
        if goal is None:
            continue  # 场上没有石矿就不动，而不是乱走
        # 矿格本身挡路（任务书 L85），所以工人走到**贴着矿的那一格**就会自动停下 ——
        # 那正是 `collect` 的位置（§4.4：矿周围一格内）。不需要单独写"停在旁边"的逻辑。
        target = step_toward(role.pos, goal, turn.blocked | claimed, turn.size)
        if target is None:
            continue  # 已经贴着矿，或压根走不到
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


def _nearest_stone(pos: Pos, mines: dict[Pos, str]) -> Pos | None:
    """最近的石矿。距离用切比雪夫（任务书 §4.5.4）。

    不认领矿：两个工人挤同一座矿的不同邻格**都能采**，只有"冲进同一格"才是白扔动作，
    而那件事已经由 `claimed` 挡住了。
    """
    return min((p for p, kind in mines.items() if kind == STONE), key=pos.dist, default=None)
