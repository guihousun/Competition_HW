"""昼夜调度：把世界状态变成每个角色的**一条** `Intent`。

设计见 docs/design/code-design.md §3.2 第 4 条；策略稿 §8（日程模板与回防倒计时）。

分工（`_assign_jobs` 落定，跨回合粘住）：

    白天 ── 工人 A「建造手」：**先建满 3 座武器** → 攒石头 → 按 `wall_order()`
                           补围墙盒（**正面优先**）
         ── 工人 B「经济手」：挖铁/铜 → 满载后去小贩处清仓
         ── 开拓者「军需官」：去武器商店采购 → 回基地用升级券（→ 第 7 步接管任务线）
    夜晚 ── 三人各自认领一座武器，走到操控位（第 6 步在此之上加攻击目标）

⚠️ **武器的优先级高于墙，且开局窗口只有第 1 天**：三种武器初始数量都是 0
（任务书 §4.5.1），单价 25 金，开局 75 金 —— 恰好买满 3 座（§4.5.3）。
第 1 天不把武器立起来，当天入夜三人无武器可操控，基地要赤手空拳挨整晚。
`build` **仅工人、仅白天**可用（任务书 L137），所以这是建造手的活，不是采购。

⚠️ **为什么采购和应用必须同一个人**：物品只能进买家的背包，游戏里**没有**
转移物品的指令（`drop` 只把东西丢在地上，没有拾取）。所以"让经济手买、军需官用"
是行不通的 —— 券会烂在经济手的背包里。军需官一人包办买+用。

⚠️ 本模块**不做跨回合记忆之外的假设**：目标每回合从世界状态重算，
天然粘住（矿区不动、缺口不动）。真正需要粘住的只有两处 ——
角色↔武器配对（`Assignment`）与工作分工（`PlanMemory.jobs`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..infra import config
from . import calendar
from .assignment import Assignment, reconcile
from .economy import (
    mine_priority,
    next_weapon_build,
    purchase_wish,
    sell_plan,
    upgrade_targets,
)
from .entities import PIONEER, WALL
from .grid import Pos
from .intent import Build, Buy, Collect, Idle, Intent, Move, Sell, Use
from .path import INF, distance_field, next_step_toward
from .world import Turn, board_for, box_of, in_reach, wall_gaps

__all__ = ["PlanMemory", "Goal", "plan"]

JOB_FORTIFY = "fortify"
JOB_ECONOMY = "economy"
JOB_PIONEER = "pioneer"


@dataclass(frozen=True, slots=True)
class Goal:
    """一个角色的当前目标：走到 `target` 旁边，然后做 `act`。

    `why` 只进日志 —— 赛后复盘时"这回合为什么这么走"的唯一线索。
    """

    act: str  # collect | build | sell | buy | use | weapon | retreat | idle
    target: Pos | None = None
    name: str = ""
    num: int = 1
    why: str = ""


@dataclass(slots=True)
class PlanMemory:
    """跨回合的少量粘性状态。**由 `app.py` 持有并逐回合传进来。**

    刻意保持极小：能被世界状态重算出来的东西**一律不缓存**
    （缓存越多，越容易在换边/重置时留下脏状态 —— 见 code-design.md §15.2）。
    """

    jobs: dict[int, str] = field(default_factory=dict)
    assignments: Assignment = field(default_factory=Assignment)
    #: 已下单但可能还没到手的商品名，防止同一张券在两个回合各买一次
    ordered: set[str] = field(default_factory=set)
    #: 建造手的**料批状态**：role_id -> `"gather"` | `"build"`。
    #: 为什么必须是显式状态：用"手上石头少于 N 就去挖"这种无状态判据会退化成
    #: "挖一块 → 走回去建一块 → 再走回来挖" —— 每建一面墙少一块石头，立刻又
    #: 低于阈值，于是掉头回矿点。一个盒子 19 面墙就是 38 趟往返，白天 70 回合
    #: 根本不够（实测第 40 回合才建起 4 面）。见 `_builder_goal`。
    build_mode: dict[int, str] = field(default_factory=dict)

    def reset_match(self) -> None:
        """换边 / 新一场。配对和分工全部作废 —— 角色 id 会变。"""
        self.jobs.clear()
        self.assignments = Assignment()
        self.ordered.clear()
        self.build_mode.clear()


# ── 入口 ─────────────────────────────────────────────────────────────
def plan(turn: Turn, mem: PlanMemory) -> tuple[Intent, ...]:
    """产出一整回合的指令。**每个存活角色至多一条**（协议：每回合一个动作）。"""
    roles = tuple(r for r in turn.controllables if r.alive)
    if not roles:
        return ()

    box = box_of(turn)
    if calendar.is_night(turn.round_no):
        return _plan_night(turn, mem, roles, box)
    return _plan_day(turn, mem, roles, box)


# ── 白天 ─────────────────────────────────────────────────────────────
def _plan_day(turn: Turn, mem: PlanMemory, roles: tuple, box) -> tuple[Intent, ...]:
    gaps = wall_gaps(turn, box) if box is not None else ()
    jobs = _assign_jobs(turn, mem, roles, gaps, box)

    out: list[Intent] = []
    for r in roles:
        if _should_retreat(turn, r, box):
            out.append(_retreat(turn, r, box))
            continue
        job = jobs.get(r.id, JOB_ECONOMY)
        if job == JOB_PIONEER:
            out.append(_step(turn, r, _pioneer_goal(turn, mem, r, box)))
        elif job == JOB_FORTIFY:
            out.append(_step(turn, r, _builder_goal(turn, mem, r, box, gaps)))
        else:
            out.append(_step(turn, r, _economy_goal(turn, r, box)))
    return tuple(out)


def _assign_jobs(
    turn: Turn, mem: PlanMemory, roles: tuple, gaps: tuple[Pos, ...], box
) -> dict[int, str]:
    """分工。**粘住**，只在前提消失时重挑。

    为什么必须粘住：两个工人若每回合各自重新判断，很容易**同时**选"建造手"
    （或同时选"经济手"），另一条线就整天没人干。人手只有三个，分工必须显式。

    岗位是**恰好一个**建造手：多一个就没人挖钱，少一个就跟不上机器人摧毁围墙的速度。

    ⚠️ "需不需要建造手"不能只看围墙缺口 —— **开局 3 座武器也是建造手的活**
    （`build` 仅工人可用）。只看缺口的话，第 1 天没有缺口，建造手会被判成多余
    而转去挖矿，武器就没人建了。
    """
    live = {r.id for r in roles}
    need_builder = bool(gaps) or next_weapon_build(turn, box) is not None
    # 现存的、还活着的建造手（其余角色阵亡后岗位要能自动补位）
    has_fortifier = need_builder and any(
        rid in live and job == JOB_FORTIFY for rid, job in mem.jobs.items()
    )

    out: dict[int, str] = {}
    for r in roles:
        job = mem.jobs.get(r.id)
        if job is None:
            job = JOB_PIONEER if r.role_type == PIONEER else JOB_ECONOMY
        if job == JOB_FORTIFY and not need_builder:
            job = JOB_ECONOMY  # 武器满 + 盒子合拢 → 建造手转经济
        if job == JOB_ECONOMY and need_builder and not has_fortifier:
            job = JOB_FORTIFY
            has_fortifier = True
        out[r.id] = job

    # 清掉阵亡角色的岗位与料批状态，避免 id 复用或换边后残留脏状态
    mem.jobs = {rid: job for rid, job in mem.jobs.items() if rid in live}
    mem.jobs.update(out)
    mem.build_mode = {rid: m for rid, m in mem.build_mode.items() if rid in live}
    return out


def _builder_goal(turn: Turn, mem: PlanMemory, role, box, gaps: tuple[Pos, ...]) -> Goal:
    """建造手：**先建武器，再建墙，没料就去挖最近的石矿**。

    三条的先后是硬性的，不是启发式：

    ① **武器最优先**。武器是夜里唯一能造成伤害的东西，墙只是减伤。开局 75 金
       恰好买满 3 座武器（任务书 §4.5.3：初始 0 座、单价 25、开局 75 金），
       第 1 天不把它们立起来，当天入夜三个人就只能站在空地上挨打。

    ② **石头按批攒、按批用**（`mem.build_mode`）。攒够 `STONE_STOCK` 块再回盒子，
       建到一块不剩再回矿点。**必须用两段式状态**：任何"手上少于 N 就去挖"的
       无状态判据都会退化成"挖一块建一块" —— 建一面墙少一块，立刻又低于阈值。

    ③ 补缺口取 `wall_order()` 里最优先的一格：**正面 → 两侧 → 背面**，门除外。
       顺序完全由几何决定，与"哪个更近"无关 —— 正面先合拢才挡得住夜里的来路。
    """
    plan = next_weapon_build(turn, box)
    if plan is not None:
        kind, site = plan
        return Goal(
            "build", target=site, name=kind, why=f"建武器 {kind}（{site.x},{site.y}）"
        )

    if not gaps:
        return Goal("idle", why="武器已满且围墙盒已合拢，无缺口")

    # 两段式：攒满一批 → 建到空 → 再攒。切换点必须跨回合记住。
    held = role.count_item("stone")
    stock = min(config.STONE_STOCK, len(gaps))
    mode = mem.build_mode.get(role.id, "gather")
    if mode == "gather" and held >= stock:
        mode = "build"
    elif mode == "build" and held <= 0:
        mode = "gather"
    mem.build_mode[role.id] = mode

    if mode == "gather":
        mine = _best_mine(turn, role, stone_short=True)
        if mine is None:
            return Goal("idle", why="缺建墙石料，但场上没有可达石矿")
        return Goal(
            "collect",
            target=mine,
            why=f"攒建墙石料 {held}/{stock}，去挖最近的石矿 {mine.x},{mine.y}",
        )

    return Goal("build", target=gaps[0], name=WALL, why="按 wall_order 补缺口（正面优先）")


def _economy_goal(turn: Turn, role, box) -> Goal:
    """经济手：挖值钱的矿，满了去小贩处清仓。

    ⚠️ 经济手**不挖石头**（`stone_short=False`）。石头只值 1 金、只用于建墙，
    是筑墙手的料；让经济手也去挖石头，等于把唯一的现金来源也变成建材，
    金币会一直停在开局那 20 —— 升级券永远买不起。
    """
    cap = role.capacity or 100
    mine_carry = sum(role.count_item(k) for k in ("stone", "iron", "copper"))
    vendor = turn.vendor_pos

    if mine_carry >= cap * config.SELL_BACKPACK_RATIO and vendor is not None:
        return Goal("sell", target=vendor, why=f"背包装了 {mine_carry}/{cap}，回小贩处清仓")

    mine = _best_mine(turn, role, stone_short=False)
    if mine is None:
        return Goal("idle", why="场上没有可达矿区")
    kind = turn.mine_kind(mine) or "?"
    return Goal("collect", target=mine, why=f"挖 {kind}（{mine.x},{mine.y}）")


def _pioneer_goal(turn: Turn, mem: PlanMemory, role, box) -> Goal:
    """军需官：先把手上的券用掉，再去补货，最后才去挖矿。

    顺序不能反：背包里的券是**已经花掉的钱**，压着不用等于零收益。
    """
    # ① 手上有没有能立刻生效的券？
    for name in _VOUCHER_ORDER:
        if role.count_item(name) <= 0:
            continue
        targets = upgrade_targets(turn, name)
        if targets:
            return Goal("use", target=targets[0], name=name, why=f"背包里有 {name}，就近使用")

    # ② 该进货吗？（商店里不卖武器 —— 武器由工人在武器环上 `build`，见 `_builder_goal`）
    wish = purchase_wish(turn)
    shop = turn.shop_pos
    if wish is not None and shop is not None:
        if role.count_item(wish.item) <= 0:
            mem.ordered.add(wish.item)
            return Goal("buy", target=shop, name=wish.item, why=wish.reason)

    # ③ 没事干就帮着挖矿（开拓者背包 40 格，聊胜于无）
    mine = _best_mine(turn, role, stone_short=False)
    if mine is None:
        return Goal("idle", why="无可达矿区且无需采购")
    return Goal("collect", target=mine, why="军需空闲，顺路采矿")


#: 使用顺序：先花**大件**，因为它立刻改变防线强度
_VOUCHER_ORDER = (
    "WeaponUpgradeVoucher1",
    "WeaponUpgradeVoucher2",
    "StationUpgradeVoucher1",
    "StationUpgradeVoucher2",
    "WallUpgradeVoucher1",
    "WallUpgradeVoucher2",
    "WallFixer",
)


# ── 夜晚 ─────────────────────────────────────────────────────────────
def _plan_night(turn: Turn, mem: PlanMemory, roles: tuple, box) -> tuple[Intent, ...]:
    """夜战：认领武器 → 走到操控位 →（第 6 步）攻击。

    第 5 步只做**站位**：让三个人在入夜第一回合就各自贴到一座武器的操控位上。
    这不是废动作 —— 夜战第一回合是机器人最密集的一波，,
    而站位本身要跨过好几个回合的移动，提前一晚到场才算数。
    """
    assign = reconcile(turn, mem.assignments)
    mem.assignments = assign

    out: list[Intent] = []
    for r in roles:
        wid = assign.weapon_of(r.id)
        weapon = turn.our_id.get(wid) if wid is not None else None
        if weapon is None or not weapon.alive:
            out.append(Idle(role_id=r.id, reason="夜间无武器可操控，待机"))
            continue
        goal = Goal("weapon", target=weapon.pos, name=weapon.role_type, why=f"操控 {weapon.role_type}")
        out.append(_step(turn, r, goal, weapon_id=weapon.id))

    return tuple(out)


# ── 通用：把 Goal 落成一条 Intent ────────────────────────────────────
def _step(turn: Turn, role, goal: Goal, *, weapon_id: int | None = None) -> Intent:
    """把"想做什么"翻译成"这一回合具体的那个动作"。

    三种结局，与 `next_step_toward` 的三种返回值一一对应：
      · 已在交互距离内 → 直接执行动作（collect / build / sell / buy / use）；
      · 还差得远     → 朝目标走一步；
      · 走不过去     → **显式 Idle**（而不是悄悄不发指令）。

    最后一条是刻意的：日志里出现 Idle 说明策略**主动**放弃了这回合，
    而角色凭空从报文里消失则说明分配阶段有 bug —— 两者必须能区分。
    """
    if goal.target is None:
        return Idle(role_id=role.id, reason=goal.why or "无目标")

    target = goal.target
    here = role.pos

    if in_reach(here, target) and here != target:
        act = _execute(turn, role, goal, weapon_id=weapon_id)
        if act is not None:
            return act

    board = board_for(turn, role)
    nxt = next_step_toward(board, here, target)
    if nxt is None:
        return Idle(role_id=role.id, reason=f"到不了 {target.x},{target.y}（{goal.why}）")
    return Move(role_id=role.id, dest=nxt, reason=goal.why)


def _execute(turn: Turn, role, goal: Goal, *, weapon_id: int | None) -> Intent | None:
    """已经在交互距离内 —— 执行动作。返回 None 表示"这个目标其实做不了"。"""
    t = goal.target
    assert t is not None

    if goal.act == "collect":
        return Collect(role_id=role.id, target=t, reason=goal.why)

    if goal.act == "build":
        return Build(role_id=role.id, target=t, name=goal.name, reason=goal.why)

    if goal.act == "sell":
        plan_items = sell_plan(turn, _backpack_tally(role), stone_keep=_stone_keep(turn))
        if not plan_items:
            return None  # 没东西可卖 → 退回走位逻辑（会重选目标）
        name, num = plan_items[0]
        return Sell(role_id=role.id, name=name, num=num, reason=f"清仓 {name}×{num}")

    if goal.act == "buy":
        return Buy(role_id=role.id, name=goal.name, num=goal.num, reason=goal.why)

    if goal.act == "use":
        return Use(role_id=role.id, name=goal.name, target=t, reason=goal.why)

    if goal.act == "weapon":
        # 第 6 步注入攻击目标；此刻先确保站位正确。
        # ⚠️ 报文里 `attack` 的 key 是**武器 id**（weapon_id），
        #    而 controllerId 是**操控者 id** —— 写反会导致整晚攻击无效。
        if weapon_id is None:
            return None
        return None  # 保守：没有目标就不发 attack（空 attack 会被判非法指令）

    return None


def _backpack_tally(role) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in role.backpack:
        out[item] = out.get(item, 0) + 1
    return out


def _stone_keep(turn: Turn) -> int:
    """卖石头前要留下多少块 —— 让筑墙手不至于因为卖了石头而断料。"""
    box = box_of(turn)
    gaps = len(wall_gaps(turn, box)) if box is not None else 0
    return gaps + config.STONE_BUFFER


# ── 回防 ─────────────────────────────────────────────────────────────
def _should_retreat(turn: Turn, role, box) -> bool:
    """要不要现在就往回走。**用真实路长，不用切比雪夫**。

    判据：`到门的实际步数 + RETREAT_MARGIN ≥ 白天剩余回合数`。

    两个刻意的取舍：

    1. **不用策略稿 §8 的固定阈值 `within ≥ 52`**。那看起来只牺牲 18 个白天回合，
       实际代价是每天 18 回合、10 天共 180 回合（占全部白天回合的 **26%**）被浪费在
       "站在门口发呆"上 —— 因为多数角色本来就在基地附近，52 一到就全体停摆。
       固定阈值真正想防的是"从最远矿区回不来"，而那件事只有**算路**才能知道。

    2. **用 BFS 实际步数而不是 `Pos.distance`**。切比雪夫距离会**低估**：
       两点之间隔着围墙/建筑/中立单位时，绕行要的回合数远超直线距离，
       用它会算出一份"来得及"的假账，代价是整个角色被关在门外一晚上。
       一次全图 BFS 只有 41×32 格，每回合每人一次，可以忽略不计。

    ⚠️ 在门外过夜是**纯亏**：夜里角色无法攻击（只能操控武器），
    却会被机器人当障碍物打 —— 工人 220 血在 BOSS 面前撑不过几回合。
    """
    if box is None:
        return False
    left = calendar.rounds_left_in_day(turn.round_no)
    if left <= 0:
        return False  # 已是夜晚，回防交给夜战分支
    if left <= config.RETREAT_TAIL:
        return True  # 尾巴：无论多远都得往门口收
    board = board_for(turn, role)
    steps = distance_field(board, role.pos).get(box.door, INF)
    return steps + config.RETREAT_MARGIN >= left


def _retreat(turn: Turn, role, box) -> Intent:
    """往门走。门是**唯一**预留的空档，入夜后是己方进出盒子的通道。"""
    board = board_for(turn, role)
    nxt = next_step_toward(board, role.pos, box.door)
    if nxt is None:
        return Idle(role_id=role.id, reason="已就位/无法回防，原地待命")
    return Move(role_id=role.id, dest=nxt, reason="回防：入夜前进门")


# ── 找矿 ─────────────────────────────────────────────────────────────
def _best_mine(turn: Turn, role, *, stone_short: bool) -> Pos | None:
    """挑一个矿区格。`stone_short=True` 时**无条件优先石头**，且石头之间取最近的。

    评分 = `矿种优先级 − 实际步数`。用路长做减项而不是"先筛最近的"，
    是因为"最近的恰好是不值钱的石头"很常见 —— 那会让经济手整天挖 1 金的石头；
    而 `stone_short=True` 时石头拿 10000 分，等价于"只挑最近的石矿"。

    ⚠️ 用 **BFS 实际步数**而不是切比雪夫距离：矿区是**不可通行格**（任务书 L85），
    本来就必须绕到相邻格再采；两点之间还常隔着围墙与建筑。直线距离会把
    "看着最近、实际要绕一大圈"的矿选出来 —— 表现是工人往反方向跑。
    """
    mines = turn.mines()
    if not mines:
        return None

    board = board_for(turn, role)
    field = distance_field(board, role.pos)

    best: tuple[int, int, int] | None = None
    best_pos: Pos | None = None
    for m in mines:
        kind = turn.mine_kind(m)
        if kind is None:
            continue
        steps = _stand_steps(field, m)
        if steps is None:
            continue  # 这块矿这回合走不到，跳过
        score = mine_priority(kind, stone_short=stone_short) - steps
        key = (-score, steps, m.x * 1000 + m.y)
        if best is None or key < best:
            best = key
            best_pos = m
    return best_pos


def _stand_steps(field: dict[Pos, int], target: Pos) -> int | None:
    """走到 `target` **相邻一格**的步数。`target` 本身不可通行，所以不能只看它自己。

    `Pos.neighbours()` 含自己，必须排掉 —— 否则"站在矿上"会被算成 0 步。
    """
    best: int | None = None
    for p in target.neighbours():
        if p == target:
            continue
        d = field.get(p)
        if d is not None and (best is None or d < best):
            best = d
    return best
