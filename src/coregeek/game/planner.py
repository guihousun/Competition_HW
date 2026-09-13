"""决策入口：`Turn` → `roleCommandMap`。**策略只写在这里。**

工人每回合按 `docs/策略指导.md` 的优先级**择一**（每回合每个角色只能一个动作，任务书 L177）：

1. **建武器** —— 白天、武器数 < 角色数、金币够。位置优先放在**基地后方**那一列，
   让基地的 2×2 实体挡在武器与机器人之间（"建造的位置优先放在基地后面，让基地也能
   防守一下机器人的进攻"）。三座按 加特林 → 电磁狙击炮 → 火箭发射台。
2. 否则**朝最近的石矿走一格** —— 把管线跑通的临时线。等 `collect` 落地，这一条才真的算策略。

开拓者这一步不发指令。

返回 `{角色ID: 指令}`，key 用**字符串** —— JSON 对象的 key 本来就是字符串。
"""

import logging
from collections.abc import Iterator
from typing import Any

from ..protocol import actions  # 唯一一条"由内往外"的依赖：指令只能经 Action 产出
from .grid import Pos, back_weapon_cells, step_toward
from .roles import BaseRole, Worker
from .world import Turn

LOGGER = logging.getLogger(__name__)

#: 三座武器的建造顺序（用户选定）。数量上限 = 角色数，正好 3 —— 与策略指导
#: "武器只有建立三个才有意义，建立多了没有意义"一致。
#: 任务书 §4.5.1 表格写"每种 ≤3"（合计 9 座）、补充说明写"**全局同时最多 3 座**"，
#: **原文自相矛盾**，取保守的那一个。
WEAPON_ORDER = ("gatling", "railgun", "rocket")

#: 建一座武器的金币（任务书 §4.5.1，三种同价）。
WEAPON_COST = 25


def plan(turn: Turn) -> dict[str, dict[str, Any]]:
    cmds: dict[str, dict[str, Any]] = {}
    #: 本回合已被认领的落脚格。不设它，两个人冲同一个空格会按"目标点争夺"双双停住（任务书 §4.5.4）
    claimed: set[Pos] = set()
    #: 已被认领的**建造格**。与 `claimed` 分开：建造格是"要往里投钱的位置"，语义不同
    sites: set[Pos] = set()
    budget = turn.gold
    slots = _slots(turn)

    for role in turn.roles:
        if not isinstance(role, Worker):
            continue  # 开拓者这一步不动（见 code-task.md「不做什么」）

        slot = next(slots, None)
        if slot is not None and budget >= WEAPON_COST:
            kind, cell = slot
            budget -= WEAPON_COST  # **认领即预留**：宁可这回合少建，不可超支
            sites.add(cell)
            if role.pos.dist(cell) <= 1:
                # 「**站位即建造位**」：`build` 要求目标在自身一格内，而 `step_toward`
                # 恰好把工人停在贴着目标的那一格 —— 两条规则正好对上，不用先挪开再建。
                if _emit(cmds, role, actions.Build, kind, cell):
                    continue
            else:
                # 把 `sites` 也当障碍：免得工人径直走到建造格**上**去（那样建完自己站在武器里）
                walk = turn.map.blocked | claimed | sites
                step = step_toward(role.pos, cell, walk, turn.map.size)
                if step is not None and _emit(cmds, role, actions.Move, step):
                    claimed.add(step)
                    continue
            # 建不了 / 走不到 → 落到兜底那条，别杵着

        _mine(role, turn, cmds, claimed)

    return cmds


def _slots(turn: Turn) -> Iterator[tuple[str, Pos]]:
    """本回合可以开建的 `(武器类别, 落点)`，按优先级排；没名额就一个都不产出。

    三道门槛，缺一座都不该建：

    - **白天** —— `build` 仅白天可用（任务书 §4.4）。夜里 `build` 会被判无效。
    - **份额** = `len(roles)` —— 用户规则"武器数量 < 总角色数"（3 角色 ⇒ 3 座）。
    - **落点是空的** —— 建在已有武器上会把它**覆盖**成 level1（§4.5.1 补充说明），
      25 金币打水漂还降级；建在墙/矿上也必然失败。

    基地没了（`station is None`）就没有可建造区 ⇒ 不建。**这是故意的降级方向。**
    """
    if not turn.is_day:
        return
    station = turn.map.station
    if station is None:
        return

    have = turn.map.weapons
    need = len(turn.roles) - sum(len(positions) for positions in have.values())
    if need <= 0:
        return

    kinds = [k for k in WEAPON_ORDER if not have.get(k)]
    free = [c for c in back_weapon_cells(station, turn.map.size[0]) if c not in turn.map.blocked]
    yield from list(zip(kinds, free))[:need]


def _emit(
    cmds: dict[str, dict[str, Any]], role: BaseRole, cls: type[actions.BaseAction], *args: Any
) -> bool:
    """造一条指令放进 `cmds`，成功返回 True。

    **越权只丢这一条并告警**，不连坐同回合其他角色 —— 若越权是系统性的，抛出去会变成
    "每回合空指令 → 全队冻结一整局"，现象与 `main3.py` 改名事故一样难排查
    （`CLAUDE.md` 硬约束 3）。`role_type` 是 Action 唯一能自证的权限，所以闸门只能在这里。
    """
    try:
        cmds[str(role.id)] = cls(role.type_name, *args).to_wire()
    except PermissionError as exc:
        LOGGER.warning("拦下越权动作：角色 %s(%s) %s", role.id, role.type_name, exc)
        return False
    return True


def _mine(role: Worker, turn: Turn, cmds: dict[str, dict[str, Any]], claimed: set[Pos]) -> None:
    """兜底：朝最近的石矿走一格。

    矿格本身挡路（任务书 L85），所以工人走到**贴着矿的那一格**就会自动停下 ——
    那正是 `collect` 的位置（§4.4：矿周围一格内）。不需要单独写"停在旁边"的逻辑。
    """
    goal = _nearest_stone(role.pos, turn.map.stones)
    if goal is None:
        return  # 场上没有石矿就不动，而不是乱走
    step = step_toward(role.pos, goal, turn.map.blocked | claimed, turn.map.size)
    if step is not None and _emit(cmds, role, actions.Move, step):
        claimed.add(step)


def _nearest_stone(pos: Pos, stones: frozenset[Pos]) -> Pos | None:
    """最近的石矿。距离用切比雪夫（任务书 §4.5.4）。

    **按矿种筛选已经上移到 `map.Map`**（铺矩阵时顺手分拣出 `stones`），这里只认坐标。

    不认领矿：两个工人挤同一座矿的不同邻格**都能采**，只有"冲进同一格"才是白扔动作，
    而那件事已经由 `claimed` 挡住了。
    """
    return min(stones, key=pos.dist, default=None)
