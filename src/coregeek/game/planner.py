"""决策入口：`Turn` → `roleCommandMap`。只做胶水 —— 两段式组装 + 按昼夜分派的两条判据链。

`plan` = `_intents`（第一段）+ `_walk_out`（第二段，按同一顺序批量解走路意图 —— BFS 落一格、
记落子账与路径预留账）。第一段**按昼夜分成两条独立的链**：`_day_intents`（任务/砌墙/经济）与
`_night_intents`（基地升级/采矿/操炮），各自一个逐角色循环、各自的账 —— 具体策略在
`task` / `day` / `night` 三个模块里，两边都要问的通用判定在 `game/utils.py`，共用底座在
`game/core.py`。

两个距离口径别混：回合预算（来不来得及来回）一律用 BFS 真实步数（`steps_between`，绕障，
-1 = 走不到）；选点/贴着用切比雪夫 `Pos.dist`。射程与溅射也是切比雪夫 —— 那是规则。

返回 `{角色ID: 指令}`（key 用字符串）；`attack` 例外 —— key 是武器 id，操控者在
`controllerId` 里。任务线不在这个返回值里：`task.task_channel` 单独产出 `(prompt, executeCmd)`。
"""

from typing import Any

from ..protocol import actions  # 指令只能经 Action 产出
from . import day, night, task
from .grid import Pos, box_cells
from .path import step_onto, step_outside, step_toward
from .roles import BaseRole, Pioneer, Worker
from .core import _Ctx, _Queue, _emit, _sell_ore, _upgrade_line
from .utils import _ring, _short_handed, _trapped
from .world import Turn


def plan(turn: Turn) -> dict[str, dict[str, Any]]:
    """两段式决策：`_intents` 按昼夜走各自那条判据链（能直接干的当场落指令、要走路的只交
    意图），`_walk_out` 按同一顺序批量解走路意图。黑板是第一段的决策账，逐角色顺序累计；
    两段都不跨回合。"""
    q = _intents(turn)
    _walk_out(turn, q)
    return q.cmds


def _intents(turn: Turn) -> _Queue:
    """第一段：**按昼夜分派** —— 白天与夜里各有一套判据链、各自的账，两条线不共用一条阶梯。"""
    q = _Queue(turn)
    if turn.is_day:
        _day_intents(turn, q)
    else:
        _night_intents(turn, q)
    return q


def _day_intents(turn: Turn, q: _Queue) -> None:
    """白天那条链，逐角色跑、先命中先定夺：

    ① 开拓者被任务钉死（白天人手恒够，`_short_handed` 恒假）→ ② 砌满墙会被关住的人先迈出
    盒子 → ③ `day.rescue` 拆墙放人 → ④ `day.BACK_TO_POST` 收工闸门 → ⑤ 开拓者：去接任务 /
    任务点全空则跑腿买券、卖矿兜底 → ⑥ 工人走 `day.DAY_CHAIN`。

    黑板装在 `_Ctx` 里逐角色顺序累计、不跨回合；环缺口按在场工人数切段也在这里。"""
    cmds = q.cmds
    # 武器缺口（份额有缺且落点为空的名额）：建武器最优先的判据，也是筹资线的开关
    pending = list(day.slots(turn))
    weapon_gap = bool(pending)
    # 防御盒子的 36 格（空集 = 没基地 ⇒ 没有"里面"）
    box = box_cells(turn.map.station) if turn.map.station else frozenset()
    # 砌满墙就会被关在盒子里的人：闸门按人放行，建墙状态随 `leaving` 待命
    leaving = _trapped(turn, box)
    ctx = _Ctx(turn, q, weapon_gap=weapon_gap, leaving=leaving, slots=iter(pending))

    # 环缺口按在场工人数切段（A 领前段、B 领后段；一个工人 ⇒ 整段）
    workers_no = [r for r in turn.roles if isinstance(r, Worker)]
    gaps = _ring(turn)
    bounds = [len(gaps) * i // max(len(workers_no), 1) for i in range(len(workers_no) + 1)]
    segments = [gaps[bounds[i]: bounds[i + 1]] for i in range(len(workers_no))]
    worker_no = 0

    for role in turn.roles:
        # 服任务中的开拓者：钉死（离开任务点周围一格任务立即作废）
        if isinstance(role, Pioneer) and turn.phase_task:
            task.answer_task(role, turn, cmds)
            continue

        # 要被关住的人先出来：出得去 ⇒ 交 provider 意图；出不去（门被机器人堵死）⇒ 落到 `day.rescue`
        if role.id in leaving:
            if step_outside(role.pos, box, turn.map.blocked, turn.map.size) is not None:
                q.beside(
                    role,
                    lambda claimed, r=role, b=box: step_outside(
                        r.pos, b, turn.map.blocked | claimed, turn.map.size
                    ),
                )
                continue

        # 有人被关在盒子里 ⇒ 工人去拆一格放人（上面那道闸门是预防，这里是补救）
        if day.rescue(role, turn, q, box):
            continue

        # 收工：环砌完了、离夜里第一波只剩回程步数才回家（判据在 `day.BackToPost`）
        if day.BACK_TO_POST.run(role, ctx):
            continue

        if isinstance(role, Pioneer):
            if turn.task_points:
                task.take_task(role, turn, q)
            else:
                # 任务点全空 ⇒ 真空闲：领"买券 → 用券"差事（武器齐了才跑）；卖矿兜底
                if not weapon_gap and not _upgrade_line(role, turn, q, ctx.sites):
                    _sell_ore(role, turn, q, ctx.sites)
            continue

        if not isinstance(role, Worker):
            continue  # 不该出现的角色（`roles.make` 已挡过一道）

        # 环缺口切段：段号在这一支的最前面就吃掉（走过前面那些闸门的角色不占段）
        segment = segments[worker_no] if worker_no < len(segments) else ()
        worker_no += 1
        ctx.target = segment[0] if segment else None
        ctx.remaining = len(segment)

        # 白天工人链：从上往下第一个"认领了这一回合"的状态说了算
        for state in day.DAY_CHAIN:
            if state.run(role, ctx):
                break


def _night_intents(turn: Turn, q: _Queue) -> None:
    """夜里那条链（顺序就是夜里的策略），逐角色跑：

    ① 被任务钉死的开拓者 —— **人手够才钉得住**（工人阵亡 ⇒ 弃任务回炮位，生存第一）→
    ② 持基地券且基地残血 ⇒ 贴基地 `use` → ③ 清场后工人出门采最值钱的矿
    （`night.is_cleared` + `night.mine_ore`）→ ④ 其余角色回炮位开火。

    两本账是**本函数的局部变量** —— 夜里不碰白天那个 `_Ctx` 黑板：炮位认领 `taken`
    （一人一组，组内任一座被认领 = 整组被认领）与伤害记账 `assigned`（先开火的把估计伤害
    记上，后开的按剩余血挑目标）。"""
    cmds = q.cmds
    taken: set[Pos] = set()
    ore_taken: set[Pos] = set()
    assigned: dict[Pos, int] = {}

    for role in turn.roles:
        # 服任务中的开拓者：钉死（离开任务点周围一格任务立即作废，夜里都不回炮位）；
        # 唯一例外是夜里人手不够（工人阵亡 ⇒ 有炮没人操），生存第一、弃任务
        if isinstance(role, Pioneer) and turn.phase_task and not _short_handed(turn):
            task.answer_task(role, turn, cmds)
            continue
        if night.upgrade_station(role, turn, q):
            continue
        # 判据是"没有打我方的活机器人"、不是"场上全空"：打对方那波也在表里、我们从不打它
        if night.is_cleared(turn) and isinstance(role, Worker):
            night.mine_ore(role, turn, q, ore_taken)
            continue
        night.defend(role, turn, q, taken, assigned)


def _walk_out(turn: Turn, q: _Queue) -> None:
    """第二段：按第一段的顺序解走路意图（批量算路、逐个落子）。

    落子账 `claimed` 与路径预留账 `paths` 按意图顺序累计：后解的把已落的子当硬障碍、
    把已预留的路当软避让（绕不开退回硬障碍照走 —— 让路的代价不能是原地卡死）。
    provider 型意图拿"此刻的落子账"现算落脚格，算不出 ⇒ 这一回合不动。"""
    claimed: set[Pos] = set()
    paths: set[Pos] = set()
    blocked, size = turn.map.blocked, turn.map.size
    for m in q.moves:
        if m.provider is not None:
            cell = m.provider(claimed)
            if cell is not None and _emit(q.cmds, m.role, actions.Move, cell):
                claimed.add(cell)
            continue
        avoid = m.avoid | (paths if m.with_paths else frozenset())
        walk = blocked | claimed | avoid
        # `onto` 的意图要走 goal 自己（`step_onto`），其余停在贴着它的一格
        walker = step_onto if m.onto else step_toward
        step = walker(m.role.pos, m.goal, walk, size)
        if step is None and avoid:
            step = walker(m.role.pos, m.goal, blocked | claimed, size)
        if step is None or not _emit(q.cmds, m.role, actions.Move, step):
            continue
        claimed.add(step)
        if m.reserve:
            _reserve_path(m.role, m.goal, turn, claimed, paths)


def _reserve_path(
    role: BaseRole,
    goal: Pos,
    turn: Turn,
    claimed: set[Pos],
    paths: set[Pos],
) -> None:
    """把"从 `role.pos` 走到 `goal` 的 BFS 路径"的中间格记进 `paths`（路径预留）。

    后解的走路意图把 `paths` 当软避让 ⇒ 整条让开，而不是撞上前一个工人的本回合这一格才让
    （盒子里走廊就那几条，双双停住一回合是纯亏）。算路只用 `blocked | claimed`、不含
    `paths` 自己 —— 否则第二个工人的预留会绕开第一个的、越绕越远。"""
    pos, walk, size = role.pos, turn.map.blocked | claimed, turn.map.size
    for _ in range(64):
        if pos.dist(goal) <= 1:
            return
        step = step_toward(pos, goal, walk, size)
        if step is None:
            return
        paths.add(step)
        pos = step
