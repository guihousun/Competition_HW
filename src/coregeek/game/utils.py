"""通用判定：拿一个 `Turn` 回答一个问题（谁能走 / 环砌在哪 / 环上还差哪几格 / 人手够不够）。

`core`/`day`/`night`/`planner` 的下层（都单向 import 本模块）—— 放这儿的判据一律是两边都要
问的；只有一个消费者的判据留在各自的模块里。

⚠️ `_passable`（估算距离）与"这一回合实际怎么走"是两套口径：前者把自己人一律算路过，
`_Queue.step` / `_walk_out` 那边必须把同事当硬障碍（撞上就是执行失败）。
"""

from .grid import Pos, wall_cells
from .roles import Pioneer, Worker
from .world import ROUNDS_PER_DAY, Turn


def _passable(turn: Turn) -> set[Pos]:
    """估算距离用的地形：`map.blocked` 剔掉我方角色站着的格子。

    与"这一回合实际怎么走"是两套口径（`_Queue.step` / `_walk_out` 必须把同事当硬障碍：撞上
    就是执行失败）。这里问的是"路有多远"：环砌满之后盒子里只剩一格宽的走廊，同事停在走廊上
    就会让估算判成不可达（-1）、整条经济线静默放弃。"""
    return turn.map.blocked - {r.pos for r in turn.roles}


def _sealed_back(turn: Turn) -> bool:
    """第 3 天起把背面两个角格补上（前两天的环只有 14 格，背面整列敞开）。

    判据只能用回合号：环上"没砌"与"砌了又被拆"在地图上同形。"""
    return turn.round_no > 2 * ROUNDS_PER_DAY


def _ring(turn: Turn) -> tuple[Pos, ...]:
    """按优先级排的围墙格；基地没了 ⇒ 空。

    既不在 `wall_cells` 里、也不挡路的格才算候选 —— 但自己人站的那一格除外：工人站在待砌的
    墙格上只是路过，若把它划掉，另一个工人的 `free[0]` 会整体后移、等那人一挪窝目标又变回来
    —— 两个工人在两格之间对着改目标，一格都砌不上。这里只管"哪些格能砌"，"这一回合还砌不
    砌"是 `_trapped` 的事。"""
    station = turn.map.station
    if station is None:
        return ()
    # 我方角色当前站的格（角色能走的都在这）
    mine = {r.pos for r in turn.roles}
    cells = wall_cells(station, turn.map.size[0], sealed=_sealed_back(turn))
    return tuple(c for c in cells if c not in turn.map.blocked or c in mine)


def _night_gunner(turn: Turn) -> int | None:
    """这一夜**谁上炮位** ⇒ 角色 id；没人合适 ⇒ `None`。

    三座火箭共用一个操作位 ⇒ 只需要一个角色：开拓者上（用户口径"夜里开拓者操炮"）—— 被
    任务钉死时**名册里第一个工人顶上**（火力不断）；被钉死且一个工人都没有 ⇒ 还是它上
    （`_short_handed`：生存第一，弃任务回炮位）。昼夜同一个判据：白天用它决定收工回不回
    到炮位（`day.BackToPost`），夜里用它决定谁认领那组武器（`night.defend`）—— 两处必须
    同源，只改一处会让白天把人送进岗位、夜里又没人认领。不带跨回合状态。"""
    pioneer = next((r for r in turn.roles if isinstance(r, Pioneer)), None)
    if pioneer is not None and (not turn.phase_task or _short_handed(turn)):
        return pioneer.id
    worker = next((r for r in turn.roles if isinstance(r, Worker)), None)
    return worker.id if worker is not None else None


def _short_handed(turn: Turn) -> bool:
    """夜里操炮的人手够不够：**一个工人都没有**（开拓者被任务钉死时没人能顶）⇒ 不够，
    被钉死的开拓者也得弃任务回炮位（生存第一）。

    白天恒假：白天不能开火、回炮位没有意义。不带跨回合状态。"""
    return not turn.is_day and not any(isinstance(r, Worker) for r in turn.roles)
