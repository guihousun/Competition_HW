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
from .grid import Pos
from .path import step_onto, step_toward
from .roles import BaseRole, Pioneer, Worker
from .core import WEAPON_COST, _Ctx, _Queue, _emit
from .utils import _night_gunner, _ring, _short_handed
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


def _pioneer_errand(role: BaseRole, turn: Turn, q: _Queue, ctx: _Ctx) -> None:
    """空闲开拓者那一支（白天与**夜里清场后**共用，用户口径"提前进入白天状态"）：有任务点就
    去接，否则跑"买券 → 立刻用"的差事，连券都干不了就去最该等的地方站着。

    只发 `move` / `acceptTask` / `buy` / `use` —— 夜里全合法（`acceptTask` §4.4 没写昼夜限制）。"""
    if turn.task_points:
        task.take_task(role, turn, q)
    elif not day.voucher_errand(role, ctx):
        # 空闲又没什么可买/可升 ⇒ 去最该等的地方站着（刷新最快的任务点 / 商店）
        day.wait_errand(role, ctx)


def _day_intents(turn: Turn, q: _Queue) -> None:
    """白天那条链，逐角色跑、先命中先定夺：

    ① 开拓者被任务钉死（白天人手恒够，`_short_handed` 恒假）→ ② `day.BACK_TO_POST` 收工门
    （到点就回岗位）→ ②′ 这一夜的修墙工（`night.repairer`）走 `night.hold_the_wall`：到点先
    回正面墙后方的待命位（天黑才发现人还在盒外就晚了）→ ③ 开拓者走 `_pioneer_errand` →
    ④ 工人走 `day.DAY_CHAIN` 那几级。

    黑板装在 `_Ctx` 里逐角色顺序累计、不跨回合；环缺口按在场工人数切段也在这里。"""
    cmds = q.cmds
    # 还缺的武器名额（落点与种类）：第 1 级的判据，也是"谁建哪几座"那份分配的输入
    pending = tuple(day.slots(turn))
    ctx = _Ctx(
        turn,
        q,
        weapon_gap=bool(pending),
        can_build_all=turn.gold >= WEAPON_COST * len(pending),
        sites_pending=pending,
        build_plan=day.assign_sites(turn, pending),
    )
    # 这一夜要修墙的那个人（第 4 夜起 + 手里有包）：天黑前得先回正面墙后方的待命位
    repairer = night.repairer(turn, _night_gunner(turn))

    # 环缺口按在场工人数切段（A 领前段、B 领后段；一个工人 ⇒ 整段）
    workers_no = [r for r in turn.roles if isinstance(r, Worker)]
    gaps = _ring(turn)
    bounds = [len(gaps) * i // max(len(workers_no), 1) for i in range(len(workers_no) + 1)]
    segments = [gaps[bounds[i]: bounds[i + 1]] for i in range(len(workers_no))]
    worker_no = 0

    for role in turn.roles:
        # 服任务中的开拓者：钉死（离开任务点周围一格任务立即作废）
        if isinstance(role, Pioneer) and turn.phase_task:
            task.answer_task(role, cmds)
            continue

        # 第 0 级：到收工窗口就回岗位（判据只看还剩多少回合）
        if day.BACK_TO_POST.run(role, ctx):
            continue

        # 修墙工的先手：到点先往正面墙后方的待命位挪（与收工门同一个思路，几何共用 `core._wall_post`）
        if role.id == repairer and night.hold_the_wall(role, turn, q):
            continue

        if isinstance(role, Pioneer):
            _pioneer_errand(role, turn, q, ctx)
            continue

        if not isinstance(role, Worker):
            continue  # 不该出现的角色（`roles.make` 已挡过一道）

        # 环缺口切段：段号在这一支的最前面就吃掉（走过前面那些闸门的角色不占段）
        segment = segments[worker_no] if worker_no < len(segments) else ()
        worker_no += 1
        ctx.target = segment[0] if segment else None
        ctx.remaining = len(segment)

        # 第 1–3 级：从上往下第一个"认领了这一回合"的状态说了算
        for state in day.DAY_CHAIN:
            if state.run(role, ctx):
                break


def _night_intents(turn: Turn, q: _Queue) -> None:
    """夜里那条链（顺序就是夜里的策略），逐角色跑：

    ① 被任务钉死的开拓者 —— **人手够才钉得住**（工人阵亡 ⇒ 弃任务回炮位，生存第一）→
    ② 持基地券且基地残血 ⇒ 贴基地 `use` → ③ **手边的券先花掉**（武器券 / 墙券；修墙工有残墙时例外）
    → ③′ 炮手手里那张武器券的目标只差 `UPGRADE_WALK_MAX`(3) 步 ⇒ 先走过去升（不开火）
    → ④ **清场了 ⇒ 整夜改走白天那两条线**（`night.is_cleared`：工人 `day.sell_or_mine`、
    开拓者 `_pioneer_errand`）→ ⑤ 没清场：炮手 `night.defend`、持包的工人 `night.repair_wall`、
    其余工人 `night.mine_ore`。

    两本账是**本函数的局部变量** —— 夜里不碰白天那个 `_Ctx` 黑板：炮位认领 `taken`
    （一人一组，组内任一座被认领 = 整组被认领）与矿格认领 `ore_taken`（清场那支自己
    现造一个 `_Ctx` 把同一本账交给白天那几条线）。"""
    cmds = q.cmds
    taken: set[Pos] = set()
    ore_taken: set[Pos] = set()
    # 这一夜谁上炮位（三座火箭共用一个操作位 ⇒ 只要一个角色）：开拓者没被钉死就是它，
    # 否则名册第一个工人顶上；其余角色整夜挖矿。
    gunner = _night_gunner(turn)
    cleared = night.is_cleared(turn)
    # 这一夜谁去修墙（第 4 夜起 + 手里有包的非炮手工人）；没人持包 ⇒ `None`、工人照旧挖矿
    repairer = night.repairer(turn, gunner)
    # 清场后走白天那两条线 ⇒ 它们收的是 `_Ctx`（矿格认领那本账与挖矿共用同一份）
    ctx = _Ctx(turn, q, ore_taken=ore_taken)

    for role in turn.roles:
        # 服任务中的开拓者：钉死（离开任务点周围一格任务立即作废，夜里都不回炮位）；
        # 唯一例外是一个工人都没有（没人能顶炮位），生存第一、弃任务
        if isinstance(role, Pioneer) and turn.phase_task and not _short_handed(turn):
            task.answer_task(role, cmds)
            continue
        if night.upgrade_station(role, turn, q):
            continue
        # ③ 手边的券先花掉（武器券 / 墙券，`use` 没有昼夜限制）：目标必须贴着 ⇒ 用券那一回合
        #    不开火/不挖矿，换来永久升级（顺带回满血）。**修墙工例外**：有残墙要修时那一格优先，
        #    别把这一回合花在"升另一面墙"上（`repair_wall` 里也是先修、能打券就打券）。
        if not (role.id == repairer and night.repair_target(turn) is not None):
            if day.use_voucher_here(role, ctx):
                continue
        # ④ 清场了 ⇒ **走白天那套**（工人挖矿卖矿、开拓者接任务/买券/等刷新）：它们只发
        # `collect`/`sell`/`buy`/`use`/`acceptTask`/`move`，夜里合法；`build`/`remove` 一条都不会发
        if cleared:
            if isinstance(role, Worker):
                # 清场后的夜里也是同一本轨迹账：该往回走了就先走（回程路上顺手采在 `walk_home` 里）
                if not night.walk_home(role, turn, q, ore_taken):
                    day.sell_or_mine(role, ctx, budget=night.mine_budget(turn))
            else:
                _pioneer_errand(role, turn, q, ctx)
            continue
        if role.id == gunner:
            # ③′ 手里还有能升的武器券、目标只差几步 ⇒ **先走过去升，别急着一炮**（用户口径
            #     "优先升级武器再操作攻击"）：升级永久（+伤害/射程，还回满血），几炮就回本。
            if day.walk_to_upgrade(role, ctx):
                continue
            night.defend(role, turn, q, taken)
            continue
        if role.id == repairer:
            night.repair_wall(role, turn, q, ore_taken)
            continue
        if isinstance(role, Worker):
            night.mine_ore(role, turn, q, ore_taken)


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
