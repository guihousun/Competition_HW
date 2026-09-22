"""白天：四级链 —— 0 收工回岗 → 1 建武器 → 2 建墙 → 2.5 攒修墙包 → 3 挖矿买券。

`DAY_CHAIN` 是 1–3 级那几个状态类（顺序即策略：从上往下第一个 `run` 返回 True 的说了算），
`BACK_TO_POST` 是链之前的第 0 级。级别之间靠**判据**衔接，不是靠状态：武器没建满就永远停在
第 1 级，武器满了才会问环上有没有缺口，环也补齐了才轮到攒包与挖矿买券。

`run` 的契约两条写在 `core.State` 的 docstring 里。本模块只被 `planner` 调用（单向）：
底座在 `core`（常量 / 走路账 / 黑板 / 岗位几何 / 挖矿排序），通用判定在 `utils`。
"""

from collections.abc import Iterator, Mapping

from ..protocol import actions  # 指令只能经 Action 产出
from .core import (
    POST_MARGIN,
    VOUCHER,
    VOUCHER_CHAIN,
    VOUCHER_NAMES,
    WALL,
    WALL_CHAIN,
    WALL_COST,
    WALL_FIXER,
    WALL_VOUCHER_NAMES,
    WEAPON_COST,
    State,
    _Ctx,
    _collect,
    _emit,
    _post_spots,
    _priciest_ore,
    _steps_to_post,
    _weapon_groups,
    front_wall_cells,
    needs_repair,
)
from .grid import STEPS, Pos, box_cells, weapon_sites
from .path import steps_between
from .roles import BaseRole, Worker
from .utils import _night_gunner, _passable, _ring
from .world import ROUNDS_PER_DAY, Turn, Weapon

#: 三座武器的种类，下标与 `grid.weapon_sites()` 的落点一一对应：**全是火箭**（用户口径）——
#: 三座共用一个操作位（`(back_x, by)`），一个角色站着按冷却轮换就能全操，其余角色腾出去挖矿。
WEAPONS_BY_SITE = ("rocket", "rocket", "rocket")


#: "顺路卖矿"的绕路上限（格，**切比雪夫**）：去矿的路上离小贩这么近就顺手卖掉。
DETOUR_MAX = 2

#: 背包里售价最高的那种矿攒到这么多件就先去卖（用户口径"超过 15 个"）。
BAG_SELL_AT = 15

#: 修墙包（`core.WALL_FIXER`，10 金/张）从第几天开始攒、攒到几张。
#: 第 3 天起（第 4 夜就要有人拿着它守正面墙）、3 张 —— 一夜里两格同时垮下来是常事。
REPAIR_STOCK_FROM_DAY = 3
REPAIR_PACKS = 3


#: 能卖给小贩的矿：三种（含多余的石头），挑哪种由 `Turn.vendor_prices` 现算。
SELLABLE = ("stone", "iron", "copper")


def slots(turn: Turn) -> Iterator[tuple[str, Pos]]:
    """本回合可以开建的 `(武器类别, 落点)`，按优先级排；没名额就一个都不产出。

    三道门槛：白天（`build` 仅白天）、份额 = `len(roles)`、落点是空的（建在已有武器上
    会覆盖成 level1，25 金币打水漂）。基地没了 ⇒ 不建。"""
    if not turn.is_day:
        return
    station = turn.map.station
    if station is None:
        return

    need = len(turn.roles) - len(turn.weapons)
    if need <= 0:
        return

    blocked = turn.map.blocked
    # 落点与种类绑死（`WEAPONS_BY_SITE` 与 `weapon_sites` 下标对应），只滤"落点是空的"；
    # 不按 `kind not in have` 过滤（两座同种火箭会被它跳过）
    yield from [
        (kind, cell)
        for kind, cell in zip(WEAPONS_BY_SITE, weapon_sites(station, turn.map.size[0]))
        if cell not in blocked
    ][:need]


def assign_sites(
    turn: Turn, sites: tuple[tuple[str, Pos], ...]
) -> dict[int, tuple[tuple[str, Pos], ...]]:
    """把这批落点分给两个工人：谁建哪几座 —— 让"最后一个建完的回合"最早（第 1 级的口径）。

    枚举划分（落点 ≤ 3 ⇒ 至多 8 种），每个工人的代价 = 按"最近的先去"跑完自己那几座的路程
    + 建它们的回合数（一座一回合）；取两者较大的那个作为完工回合，最小的划分胜出，并列时
    取更均衡的（两人座数之差小）。走不到的划分代价记成极大值。"""
    workers = [r for r in turn.roles if isinstance(r, Worker)]
    if not workers or not sites:
        return {}
    if len(workers) == 1:
        return {workers[0].id: sites}
    plan: dict[int, tuple[tuple[str, Pos], ...]] = {}
    best: tuple[tuple[int, int], dict[int, tuple[tuple[str, Pos], ...]]] | None = None
    for mask in range(1 << len(sites)):
        split: list[list[tuple[str, Pos]]] = [[], []]
        for i, site in enumerate(sites):
            split[(mask >> i) & 1].append(site)
        costs = [_tour_rounds(workers[i], split[i], turn) for i in (0, 1)]
        key = (max(costs), abs(len(split[0]) - len(split[1])))
        if best is None or key < best[0]:
            best = (
                key,
                {workers[0].id: tuple(split[0]), workers[1].id: tuple(split[1])},
            )
    if best is not None:
        plan = best[1]
    return plan


def _tour_rounds(role: Worker, sites: list[tuple[str, Pos]], turn: Turn) -> int:
    """这个工人把 `sites` 跑完要几个回合：一路"最近的先去"的 BFS 路程 + 每座一个建造回合。

    有一座走不到 ⇒ 记极大值（划分会被淘汰）。空列表 ⇒ 0（这个工人不干活）。"""
    walk, size = _passable(turn), turn.map.size
    pos, total, rest = role.pos, 0, list(sites)
    while rest:
        steps, cell = min(
            (steps_between(pos, site, walk, size), site) for _, site in rest
        )
        if steps < 0:
            return 10 ** 6
        total += steps + 1
        pos = cell
        rest = [s for s in rest if s[1] != cell]
    return total


class BackToPost:
    """第 0 级：`回岗步数 + POST_MARGIN(3) ≥ 白天剩余` ⇒ 回那一组的岗位；`True` = 到此为止。

    判据是**实时算出来的回岗步数**（BFS，与挑岗位同一个口径）：回岗要走多久就得多早动身，
    再加 `POST_MARGIN` 的容错余量。到岗后第一件事是用券（`_use_voucher_here`：贴着就能升的
    那张先用），用不上就待命。目标与夜里 `night.defend` 的岗位同一个（`core._post_spots`）：
    三座火箭共用一个操作位（要站上去）—— 天黑时人已经在岗上，夜里第一回合就能开火。
    **只发 `move`**：复用 `night.defend` 会发 `attack`，而白天发是非法指令（红线）。

    不放进 `DAY_CHAIN`：它在链之前（到点就不干活了）。只有**夜里上炮的那个角色**走它
    （`utils._night_gunner`）：开拓者操炮、工人夜里挖矿（他们不用回岗位）。"""

    def run(self, role: BaseRole, ctx: _Ctx) -> bool:
        if role.id != _night_gunner(ctx.turn):
            return False
        walk, size = _passable(ctx.turn), ctx.turn.map.size
        hops: list[tuple[int, Pos, tuple[Weapon, ...], bool]] = []
        for group in _weapon_groups(ctx.turn):
            if any(w.pos in ctx.taken for w in group):
                continue
            spots = _post_spots(group, ctx.turn, role)
            if not spots:
                continue
            onto = len(group) > 1  # 多座组的岗位是空地、要站上去（与 `night.defend` 同一个口径）
            for spot in spots:
                steps = _steps_to_post(role.pos, spot, onto, walk, size)
                if steps >= 0:
                    hops.append((steps, spot, group, onto))
        if not hops:
            return False
        steps, spot, group, onto = min(hops, key=lambda h: (h[0], h[1]))
        # 门：`回岗步数 + POST_MARGIN(3) ≥ 白天剩余`。步数实时算：回岗要走多久就得多早动身。
        if ctx.turn.day_rounds_left > steps + POST_MARGIN:
            return False
        for w in group:
            ctx.taken.add(w.pos)  # 定下这组了：认领，免得另一个角色也奔这里（一人只能操一组）
        if steps == 0:
            _use_voucher_here(role, ctx)  # 已经在岗 ⇒ 先用券（贴着就能升的那张）
            return True
        ctx.q.step(role, spot, onto=onto)  # 只发 move，绝不调 `night._fire`
        return True


class BuildWeapons(State):
    """第 1 级（钱够那一支）：在缺的落点上把武器建满，两个工人分头干。

    完备（无名额）⇒ False，让给第 2 级。钱不够建满 ⇒ False，让给 `RaiseForWeapons` 去凑。
    认领一座就预扣一份金币（`budget` 一回合内只降不升，宁可少建不可超支），登记 `sites`
    免得另一个工人也奔那一格。"""

    def run(self, role: Worker, ctx: _Ctx) -> bool:
        if not ctx.sites_pending or not ctx.can_build_all:
            return False  # 完备 ⇒ 第 2 级；钱不够建满 ⇒ 让给 `RaiseForWeapons`
        for kind, cell in ctx.build_plan.get(role.id, ()):
            if cell in ctx.sites or ctx.budget < WEAPON_COST:
                continue
            ctx.sites.add(cell)
            ctx.budget -= WEAPON_COST
            if role.pos.dist(cell) <= 1:
                if _emit(ctx.q.cmds, role, actions.Build, kind, cell):
                    return True
            elif ctx.q.step(role, cell, avoid=frozenset(ctx.sites), with_paths=True):
                return True
        return False


class RaiseForWeapons(State):
    """第 1 级（钱不够那一支）：两个工人一起挖最贵的矿，凑够建武器的钱就去卖。

    三条判据：武器有缺 且 手里的钱不够建满 且 地图上有小贩（`_can_fund`：矿挖了卖得掉）。
    团队还差的钱 ≤ 两人背包里矿的价目总值 ⇒ 背货的人去卖；否则去挖**当前收购价最高**的那种矿。
    卖完金币到账，下一回合由 `BuildWeapons` 接着建。"""

    def run(self, role: Worker, ctx: _Ctx) -> bool:
        if not ctx.weapon_gap or ctx.can_build_all or not _can_fund(ctx.turn):
            return False  # 完备 / 钱够建满 ⇒ 归 `BuildWeapons`；没小贩 ⇒ 凑不出钱
        short = WEAPON_COST * len(ctx.sites_pending) - ctx.budget
        if _cargo_value(role, ctx.turn) and _team_cargo_value(ctx) >= short:
            return _sell_cargo(role, ctx)
        _mine_ore(role, ctx)
        return True


class WallLine(State):
    """第 2 级：武器建满之后，把环补齐 —— 只看 L1。

    不完备有两种：**缺口**（那一格没墙）与 **L1 弱墙**（`needs_repair`：血 < `WALL_REPAIR_HP`(200)
    ⇒ 直接拆了重砌：1 块石头换满血，比 20 金的升级券便宜）。L2/L3 的损伤**不阻塞**这一级
    （升级券顺带回满血，见第 3 级；夜里还有 `night.repair_wall` 用修复包）。

    石头够 ⇒ 先拆该拆的弱墙、再砌缺口；石头不够 ⇒ 算"补齐这一摊还差几块"，挑一趟**最省回合**
    的采石（去 + 回最短的石矿），采够了再回工地砌。**不留存货**（旧口径的 `STONE_RESERVE` 删了）。"""

    def run(self, role: Worker, ctx: _Ctx) -> bool:
        turn = ctx.turn
        weak = [p for p in _weak_l1(turn) if p not in ctx.demolish_taken]
        need = ctx.remaining + (1 if weak else 0)
        if need == 0:
            return False  # 环完备 ⇒ 第 3 级（挖矿买券）
        if role.stone < WALL_COST * need and _mine_stone(role, ctx, weak):
            return True
        # 石头够（或者石矿采不到 —— 那就用手里的先干，别干等）：**先补缺口、再拆弱墙**。
        # 缺口是夜里机器人进来的门；弱墙好歹还挡着，拆完砌不回来就是白开一个洞。
        if ctx.target is not None and _build_gap(role, ctx):
            return True
        if weak and role.stone >= WALL_COST:
            return _demolish(role, ctx, weak)
        return False


def _build_gap(role: Worker, ctx: _Ctx) -> bool:
    """朝本段的头一格缺口去：贴着就 `build`，站在那格上先挪开，环上只剩最后一格就从盒外砌。

    三条边角规则都在这里（旧 `BuildWalls` 那一半原样搬过来）：① 环上只剩一格 ⇒ 站到**盒外**
    砌，站在环里砌完自己就被封在里面；② 自己正站在目标格上 ⇒ 这一回合只挪开一格（人站在墙上
    时那格在网格里只剩 "worker"，看不出砌过没有，`_ring` 又会把它复活成候选 ⇒ 每回合对同一格
    `build`、石头白花）；③ 贴着目标 ⇒ `build`。认领要发生在动身之前。"""
    turn, q, target = ctx.turn, ctx.q, ctx.target
    ctx.sites.add(target)
    outside = _outside_spots(turn, role, target) if len(_ring(turn)) == 1 else ()
    if outside:
        # 不避让 `ctx.target`：环上那个缺口正是出盒子的近路，避让它得从后方通道绕一圈
        if role.pos in outside and _emit(q.cmds, role, actions.Build, WALL, target):
            return True
        for spot in outside:
            if q.step(
                role,
                spot,
                avoid=frozenset(ctx.sites - {target}),
                onto=True,
                with_paths=True,
                reserve=True,
            ):
                return True
    elif role.pos == target:
        q.beside(
            role,
            lambda claimed, r=role, av=frozenset(ctx.sites): _aside_cell(r, turn, claimed, av),
        )
        return True
    elif role.pos.dist(target) <= 1:
        # 与建武器同一条契约：`step_toward` 停在贴着目标的一格，那正是 `build` 的站位
        if _emit(q.cmds, role, actions.Build, WALL, target):
            return True
    elif q.step(role, target, avoid=frozenset(ctx.sites), with_paths=True, reserve=True):
        return True
    return False


def _demolish(role: Worker, ctx: _Ctx, weak: list[Pos]) -> bool:
    """拆掉一格 L1 弱墙：贴着就 `remove`，否则走一步。认领在动身之前（两人不拆同一面）。

    那格拆完变空地 ⇒ 下一回合 `_ring` 把它算成缺口、照常砌回来（`remove` 不回收石头，
    所以这一格净花 1 块石头 + 2 个回合）。"""
    site = min(weak, key=lambda p: (role.pos.dist(p), p))
    ctx.demolish_taken.add(site)
    if role.pos.dist(site) <= 1:
        return _emit(ctx.q.cmds, role, actions.Remove, site)
    return ctx.q.step(role, site, avoid=frozenset(ctx.sites), with_paths=True)


class VoucherLine(State):
    """第 3 级：武器与环都完备之后，挖最贵的矿换金币，凑够一张券就卖掉去买、买到手立刻用掉。

    全部细则在 `voucher_errand`（工人与空闲的开拓者共用同一条差事）。"""

    def run(self, role: Worker, ctx: _Ctx) -> bool:
        if ctx.turn.tasks_exhausted:
            sell_or_mine(role, ctx)  # 无任务模式：工人只管"矿 → 金币"，券归开拓者
            return True
        voucher_errand(role, ctx)
        return True  # 链尾终态：全升满了也照样挖矿，不把这一回合让给谁


class RepairStock(State):
    """第 2.5 级：环补齐之后、去挖矿买券之前，先把修墙包的存量补到 `REPAIR_PACKS`。

    只由**名册里最后一个工人**跑 —— 包不能转手（谁买谁用），夜里背着包的那个工人就是修墙工，
    两个人各跑一趟等于白花一倍回合。细则在 `repair_errand`。"""

    def run(self, role: Worker, ctx: _Ctx) -> bool:
        return repair_errand(role, ctx)


def repair_errand(role: BaseRole, ctx: _Ctx) -> bool:
    """修墙差事（第 3 天起，只由名册里最后一个工人跑）：**手上的墙券先花掉 → 攒修复包 → 攒墙券**。

    墙券也归他：两条线打的是同一列正面墙、站位契约也一样（切比雪夫 ≤1）⇒ 买、用、修走同一趟路。
    反过来说，开拓者手里那张墙券在炮位上根本花不掉（`_use_voucher_here` 只管武器券），还会把他
    从岗位拽到墙边。武器还有缺 ⇒ 整条不跑（别抢武器的钱）；买不起 / 商店走不到 / 这一趟来不及
    ⇒ 返回 False，让第 3 级接着挖矿，绝不空转。"""
    turn = ctx.turn
    if ctx.weapon_gap or turn.round_no < 0:
        return False
    if (turn.round_no - 1) // ROUNDS_PER_DAY + 1 < REPAIR_STOCK_FROM_DAY:
        return False
    workers = [r for r in turn.roles if isinstance(r, Worker)]
    if not workers or role.id != workers[-1].id:
        return False
    # ① 手里那张墙券先花掉（目标就在这条差事的路上）
    held = next((v for v in WALL_VOUCHER_NAMES if role.bag.get(v, 0) > 0), None)
    if held is not None and _walk_to_use(role, ctx, held):
        return True
    # ② 修复包：手上还有 ⇒ 只在顺路时补货；一张都没有 ⇒ 肯专程跑一趟
    price = turn.shop_prices.get(WALL_FIXER, 0)
    if price > 0:
        stock = _team_holds(turn, WALL_FIXER) + ctx.bought.get(WALL_FIXER, 0)
        count = min(REPAIR_PACKS - stock, ctx.budget // price)
        if count > 0 and _buy_at_shop(role, ctx, WALL_FIXER, count, trips_fit=stock == 0):
            return True
    # ③ 墙券：按 `WALL_CHAIN` 取第一张还有东西可升的（**已经被认领要拆的格不算**：两条线都优先
    #    挑血最少的正面墙，不挡一下就会同回合一个 `remove`、一个 `use` 落在同一格上）
    target = _voucher_target(turn, WALL_CHAIN, ctx.demolish_taken)
    if target is None:
        return False
    voucher, spots = target
    price = turn.shop_prices.get(voucher, 0)
    free = len(spots) - _team_holds(turn, voucher) - ctx.bought.get(voucher, 0)
    count = min(free, ctx.budget // price) if price > 0 else 0
    return count > 0 and _buy_at_shop(role, ctx, voucher, count)


#: 白天链：从上往下第一个"认领了这一回合"的状态说了算。顺序即策略。
DAY_CHAIN: tuple[State, ...] = (
    BuildWeapons(),  # 1：名额有缺、钱够 ⇒ 两个工人分头建满
    RaiseForWeapons(),  # 1：有缺但钱不够 ⇒ 两人一起挖最贵的矿凑钱
    WallLine(),  # 2：缺口 / L1 弱墙 ⇒ 采石、拆、砌
    RepairStock(),  # 2.5：环补齐之后先把修墙包攒到目标张数（夜里那条线要用）
    VoucherLine(),  # 3：完备之后挖矿赚钱 → 买券 → 立刻用
)


#: 收工闸门（白天链之前的一道，见 `BackToPost`）
BACK_TO_POST = BackToPost()


def _wall_reserve(turn: Turn, ctx: _Ctx) -> int:
    """本回合该给"修墙那两个开销"留出多少金币（0 = 不用留）。

    名册里开拓者排在工人**前面**，而它买武器券是"尽可能多买" ⇒ 不先留这一手，第 3 天那点金币会
    被武器券一次吃光，工人一张包、一张墙券都买不到（用户口径要"提高墙体升级的优先级"）。
    留的钱 = 还缺的修墙包 + **一张**墙券（只留一张，最温和；手里已经持着一张就不再留）。

    条件与 `repair_errand` 同一套（第 3 天起、武器建满、有工人能背）；墙券那一半还要求
    `WALL_CHAIN` 上**还有东西可升** —— 前排全升满 ⇒ 不再锁钱，武器券照旧吃满。"""
    if ctx.weapon_gap or turn.round_no < 0:
        return 0
    if (turn.round_no - 1) // ROUNDS_PER_DAY + 1 < REPAIR_STOCK_FROM_DAY:
        return 0
    if not any(isinstance(r, Worker) for r in turn.roles):
        return 0
    packs = 0
    price = turn.shop_prices.get(WALL_FIXER, 0)
    if price > 0:
        stock = _team_holds(turn, WALL_FIXER) + ctx.bought.get(WALL_FIXER, 0)
        packs = max(0, REPAIR_PACKS - stock) * price
    step = _voucher_target(turn, WALL_CHAIN, ctx.demolish_taken)
    if step is None:
        return packs
    voucher = step[0]
    held = _team_holds(turn, voucher) + ctx.bought.get(voucher, 0)
    return packs if held else packs + turn.shop_prices.get(voucher, 0)


def voucher_errand(role: BaseRole, ctx: _Ctx) -> bool:
    """买券差事：挖最贵的矿攒钱 → 凑够券价就去卖 → 买 → 立刻走到目标用掉。

    第 3 级的**前提是武器建满了**（`ctx.weapon_gap` 为假）：武器还有缺就得先顾武器，拿那点
    金币去买券是本末倒置（旧口径"武器 ok 才升级"）。

    工人（第 3 级）与空闲的开拓者共用这一条。券按 `VOUCHER_CHAIN` 取**第一个还有东西可升**的
    步骤（两座非角火箭二级 → 它们三级 → 正面列墙二级 → 角上那座二级 → 正面列墙三级）——
    攒钱的目标就是它，不跳级去买更便宜的券。
    手里已经持券 ⇒ 先用掉（买完就用，不囤）。

    买入前从 `ctx.budget` **预扣券价**：另一条线（另一个工人 / 开拓者）这一回合就不会重复买
    同一张。全升满了 / 还买不起 ⇒ 工人接着挖矿（`collect` 仅工人，开拓者这时候什么都不发）。

    ⚠️ **差事走不通时一律落回挖矿，绝不干等**（券的目标暂时够不上、商店走不到、另一个角色
    正堵在炮位旁的那格……）—— 第 3 级是链尾，这里不发指令就等于这个角色整回合空转。"""
    if ctx.weapon_gap:
        return False
    turn, q = ctx.turn, ctx.q
    held = next((v for v in VOUCHER_NAMES if role.bag.get(v, 0) > 0), None)
    if held is not None:
        return _walk_to_use(role, ctx, held) or _mine_for_voucher(role, ctx)
    target = _voucher_target(turn)
    if target is None:
        return _mine_for_voucher(role, ctx)
    voucher, spots = target
    price = turn.shop_prices.get(voucher, 0)
    worth = _cargo_value(role, turn)
    if worth and _at_vendor(role, turn):
        return _sell_cargo(role, ctx)  # 顺路绕到了 ⇒ 先出手
    if _bag_is_full(role, turn):
        return _sell_cargo(role, ctx)  # 背包满了 ⇒ 先换成钱
    # 按需要多少张就买多少张（用户口径）：需要几张看"还升得动的目标有几个"，买得起几张看金币；
    # 取小的那个一次买够 —— 200 金、两张 100 金的券、两座待升的武器 ⇒ 一条 `buy num=2`。
    # ⚠️ **买之前先减掉"用不上的那些张数"**（用户口径"不能浪费金币买不能用的升级券"）：
    #   还升得动的目标数 − 全队手里已有的张数 − 这一回合已经预扣的张数
    # 只按金币限量是不够的：钱多的时候两条线会各买满一轮，同一座炮买回两张券。
    free = len(spots) - _team_holds(turn, voucher) - ctx.bought.get(voucher, 0)
    # 先扣掉修墙那份（`_wall_reserve` = 包 + 一张墙券）：券不许把工人买包 / 买墙券的钱花光
    afford = max(0, ctx.budget - _wall_reserve(turn, ctx))
    count = min(free, afford // price) if price > 0 else 0
    if count > 0 and _walk_to_shop(role, ctx, voucher, count):
        ctx.budget -= price * count  # 预扣：这一回合的另一条线不会再买
        ctx.bought[voucher] = ctx.bought.get(voucher, 0) + count
        return True
    if worth and worth + ctx.budget >= price:
        return _sell_cargo(role, ctx)  # 攒够了 ⇒ 背去卖（卖完下一回合买得上）
    return _mine_for_voucher(role, ctx)


def _voucher_target(
    turn: Turn,
    chain: tuple[tuple[str, str, int], ...] = VOUCHER_CHAIN,
    exclude: frozenset[Pos] = frozenset(),
) -> tuple[str, tuple[Pos, ...]] | None:
    """优先链上第一个**还有东西可升**的步骤 ⇒ `(券名, 它的全部目标)`；全升满 ⇒ `None`。

    返回的是一串而不是一格：买券要"按需要多少张就买多少张"（用户口径），张数就是这一串的长度。
    默认走武器链；围墙券那条链（`core.WALL_CHAIN`）由修墙工的差事自己传。
    `exclude` = 这一回合别人已经认领的格（拆墙线：`_Ctx.demolish_taken`），只对墙那几段生效。"""
    for voucher, group, level in chain:
        spots = _step_targets(turn, group, level, exclude)
        if spots:
            return voucher, spots
    return None


def _targets_for(
    voucher: str, turn: Turn, exclude: frozenset[Pos] = frozenset()
) -> tuple[Pos, ...]:
    """手里**这一张**券该打的目标（按优先链取第一个用它、且还有东西可升的步骤）；没有 ⇒ 空元组。

    ⚠️ 不能只看券名：武器二级券在链上出现两次（非角两座 / 角上一座），目标组得从步骤里取。
    两条链一起找：券名在两条链里不重名，多扫一遍不会串味。"""
    for name, group, level in VOUCHER_CHAIN + WALL_CHAIN:
        if name != voucher:
            continue
        spots = _step_targets(turn, group, level, exclude)
        if spots:
            return spots
    return ()


def _step_targets(
    turn: Turn, group: str, want: int, exclude: frozenset[Pos] = frozenset()
) -> tuple[Pos, ...]:
    """一个步骤现在能打的目标格（**血少的在前**、同血取坐标序）；该等级的一个都不剩 ⇒ 空元组。

    `group` 见 `core.VOUCHER_CHAIN` / `core.WALL_CHAIN`：`weapon-side` = 非角上那两座火箭、
    `weapon-corner` = 角上那座、`wall-middle` / `wall-edge` = 正面列（`core.front_wall_cells`）
    中间 / 边上那三格（切法见 `_wall_band`）。⚠️ 血量未知（-1）排最后：不知道就别优先动它。

    `exclude` 只作用于墙：**这一回合已经被认领要拆掉的那几格不能再被券打** —— 两条线都优先挑
    "血最少的正面墙"，不挡一下就会同回合一个 `remove`、一个 `use` 打在同一格上（`use` 先落地
    就是"券把墙升到满血、随即被拆"，反过来则是那一趟白跑）。"""
    station = turn.map.station
    if station is None:
        return ()
    if group.startswith("weapon"):
        sites = weapon_sites(station, turn.map.size[0])
        idx = (0, 1) if group == "weapon-side" else (2,)
        picks = {sites[i] for i in idx if i < len(sites)}
        guns = [w for w in turn.weapons if w.pos in picks and w.level == want - 1]
        return tuple(w.pos for w in sorted(guns, key=lambda w: (_health_rank(w), w.pos)))
    picks = _wall_band(turn, group)
    walls = [
        w
        for w in turn.walls
        if w.pos in picks and w.level == want - 1 and w.pos not in exclude
    ]
    return tuple(w.pos for w in sorted(walls, key=lambda w: (_health_rank(w), w.pos)))


def _wall_band(turn: Turn, group: str) -> set[Pos]:
    """正面列里这一段该升级的格子（`core.WALL_CHAIN` 的两个组）。

    `wall-middle` = **基地那两行 + 朝地图中心的那一行**（`y ∈ [by-2, by]`；样例基地 `(10,24)`
    ⇒ `y = 22,23,24`）—— 用户口径的"中间三个"，重点升级；`wall-edge` = 正面列剩下的三格
    （样例 ⇒ `21,25,26`），只吃多余的券。基地没了 ⇒ 空集。"""
    station = turn.map.station
    if station is None:
        return set()
    front = set(front_wall_cells(turn))
    middle = {p for p in front if station.y - 2 <= p.y <= station.y}
    return middle if group == "wall-middle" else front - middle


def _health_rank(wall) -> int:
    """排序用的血量：血少的排前面；**未知（-1）排最后**。"""
    return wall.health if wall.health >= 0 else 10 ** 9


def _walk_to_use(role: BaseRole, ctx: _Ctx, voucher: str) -> bool:
    """把手里这张券用掉：贴着目标就 `use`，否则走一步（没有可用的目标 ⇒ False）。

    手里可能攒着好几张（"尽可能多买"那一条）⇒ 每回合挑**还升得动的第一格**用掉一张。
    选目标要排掉这一回合已经认领要拆掉的墙格（`ctx.demolish_taken`，墙券才受影响）。"""
    spots = _targets_for(voucher, ctx.turn, ctx.demolish_taken)
    if not spots:
        return False
    spot = spots[0]
    if role.pos.dist(spot) <= 1:
        return _emit(ctx.q.cmds, role, actions.Use, voucher, spot)
    return ctx.q.step(role, spot, avoid=frozenset(ctx.sites))


def _walk_to_shop(role: BaseRole, ctx: _Ctx, voucher: str, count: int = 1) -> bool:
    """走到武器商店买券：贴着就 `buy num=count`，否则走一步（没有走得到的商店 ⇒ False）。

    `count` 由上一条按"还缺几张 + 买得起几张"算好 —— 一次买够，别一回合一张地磨。"""
    turn, q = ctx.turn, ctx.q
    walk, size = _passable(turn), turn.map.size
    hops = [(steps_between(role.pos, s, walk, size), s) for s in turn.map.shops]
    hops = [(d, s) for d, s in hops if d >= 0]
    if not hops:
        return False
    to_shop, shop = min(hops)
    if to_shop == 0:
        # 0 = 已经贴着商店（`steps_between` 的口径）；1 是"差一格"，那时候还不许买
        return _emit(q.cmds, role, actions.Buy, voucher, count)
    return q.step(role, shop, avoid=frozenset(ctx.sites))


def _to_shop(role: BaseRole, ctx: _Ctx) -> tuple[int, Pos] | None:
    """最近那家走得通的武器商店 ⇒ `(步数, 商店格)`；一家都走不到 ⇒ `None`（步数 0 = 已经贴着）。"""
    walk, size = _passable(ctx.turn), ctx.turn.map.size
    hops = [(steps_between(role.pos, s, walk, size), s) for s in ctx.turn.map.shops]
    hops = [(d, s) for d, s in hops if d >= 0]
    return min(hops) if hops else None


def _buy_at_shop(
    role: BaseRole, ctx: _Ctx, name: str, count: int, *, trips_fit: bool = True
) -> bool:
    """去商店买 `count` 张 `name`：贴着就当场买，否则走一步。`False` = 这一回合买不成。

    `trips_fit=False` ⇒ 只有贴着商店时才买（"顺路优先"：不为了它专程跑一趟）；专程那一趟还要
    "来回 + 买"赶得回白天结束。买入前预扣 `ctx.budget`（张数记进 `ctx.bought`），同一个回合的
    另一条线就不会重复买 —— 与 `voucher_errand` 同一条规矩。"""
    hop = _to_shop(role, ctx)
    if hop is None or count <= 0:
        return False
    to_shop, _shop = hop
    if to_shop > 0 and (not trips_fit or 2 * to_shop + 1 > ctx.turn.day_rounds_left):
        return False
    if not _walk_to_shop(role, ctx, name, count):
        return False
    price = ctx.turn.shop_prices.get(name, 0)
    ctx.budget -= price * count
    ctx.bought[name] = ctx.bought.get(name, 0) + count
    return True


def _mine_for_voucher(role: BaseRole, ctx: _Ctx) -> bool:
    """攒券钱的收尾：工人接着挖矿；开拓者**采不了矿**（`collect` 仅工人，§4.4）⇒ 什么都不发。"""
    if not isinstance(role, Worker):
        return False
    return _mine_ore(role, ctx)


def _mine_ore(role: Worker, ctx: _Ctx) -> bool:
    """白天挖矿：挑最值钱的那座矿；动身之前先看有没有顺路的买卖。"""
    mine = _priciest_ore(role, ctx.turn, ctx.ore_taken)
    if mine is None:
        return False
    ctx.ore_taken.add(mine)
    if _detour_sell(role, ctx, mine):
        return True
    if role.pos.dist(mine) <= 1:
        return _collect(ctx.q.cmds, role, mine)
    return ctx.q.step(role, mine, avoid=frozenset(ctx.sites), with_paths=True, reserve=True)


def _mine_stone(role: Worker, ctx: _Ctx, weak: list[Pos]) -> bool:
    """石头不够：挑一趟**最省回合**的采石（矿 + 采量），采够了再回工地砌。

    "最省" = `走到矿 + 从矿回工地` 的 BFS 步数最小（采量与砌墙的回合数对每座矿都一样 ⇒
    只比这一个和）；走不到的矿剔掉。采量每回合重算 = "补齐这一摊还差几块"，**够了就行**。"""
    turn, q = ctx.turn, ctx.q
    walk, size = _passable(turn), turn.map.size
    back_to = ctx.target if ctx.target is not None else weak[0]
    best: tuple[int, Pos] | None = None
    for pos, kind in turn.map.ores.items():
        if kind != "stone" or pos in ctx.ore_taken:
            continue
        to_mine = steps_between(role.pos, pos, walk, size)
        back = steps_between(pos, back_to, walk, size)
        if to_mine < 0 or back < 0:
            continue
        key = (to_mine + back, pos)
        if best is None or key < best:
            best = key
    if best is None:
        return False  # 一座能用的石矿都没有 ⇒ 这一回合不发指令
    mine = best[1]
    ctx.ore_taken.add(mine)
    if role.pos.dist(mine) <= 1:
        return _collect(q.cmds, role, mine)
    return q.step(role, mine, avoid=frozenset(ctx.sites), with_paths=True, reserve=True)


def sell_or_mine(role: Worker, ctx: _Ctx) -> bool:
    """工人"只管矿 → 金币"的那条线（无任务模式的白天 / 夜里清场后）：攒够一趟的货就背去卖，
    否则接着挖最贵的矿。

    两个去卖的触发：**够本**（货值 ≥ 2 × 到小贩的步数，少了它背一块石头也会走十几步）或
    **背包满了**（售价最高的那种超过 `BAG_SELL_AT`(15) 件）。已经贴着小贩 ⇒ 直接卖。"""
    kind, num = _best_load(role, ctx.turn.vendor_prices)
    if _worth_the_trip(role, ctx.turn, kind, num) or _bag_is_full(role, ctx.turn):
        return _sell_cargo(role, ctx)
    return _mine_ore(role, ctx)


def _bag_is_full(role: BaseRole, turn: Turn) -> bool:
    """背包里**售价最高**的那种矿超过 `BAG_SELL_AT`(15) 件 ⇒ 该去卖了（用户口径）。

    看售价最高的那一种（`_best_load` 挑的就是它）—— 一回合只发得出一条 `sell`，卖掉哪种到账哪种。"""
    return _best_load(role, turn.vendor_prices)[1] > BAG_SELL_AT


def wait_errand(role: BaseRole, ctx: _Ctx) -> bool:
    """开拓者空闲、又没什么可买时的"等着"（用户口径）：去最该等的地方站着。

    - 任务点还在冷却 ⇒ 去**刷新最快**的那个点旁边（`Turn.cooling_tasks` 的第一个）；
    - 全做完 / 一个点都没有 ⇒ 去武器商店旁边。
    到地方就什么都不发（待命）。`False` = 这两个地方都不知道在哪（没点也没商店）。"""
    turn, q = ctx.turn, ctx.q
    target = _wait_spot(turn)
    if target is None:
        return False
    if role.pos.dist(target) <= 1:
        return False  # 已经等在那儿 ⇒ 待命（照样返回 False，让链尾自己收场）
    return q.step(role, target)


def _wait_spot(turn: Turn) -> Pos | None:
    """该去哪儿等：冷却最快那个任务点（并列取坐标序）> 武器商店（并列取坐标序）。"""
    if turn.cooling_tasks:
        return turn.cooling_tasks[0][0]
    return min(turn.map.shops) if turn.map.shops else None


def _worth_the_trip(role: BaseRole, turn: Turn, kind: str, num: int) -> bool:
    """这一趟卖矿值不值：**货值 ≥ 2 × 到小贩的 BFS 步数**（≈ 每回合至少换 1 金币）。

    ⚠️ 「1 金币 ≈ 1 回合」是拍的（第 22 步的老旋钮，第 121 步删掉、第 126 步只在无任务模式的
    卖矿上回来）。已经贴着小贩 ⇒ `True`；一个走得到的小贩都没有 ⇒ `False`。"""
    if not kind:
        return False
    walk, size = _passable(turn), turn.map.size
    hops = [d for d in (steps_between(role.pos, v, walk, size) for v in turn.map.vendors) if d >= 0]
    if not hops:
        return False
    to_vendor = min(hops)
    if to_vendor == 0:
        return True
    return turn.vendor_prices.get(kind, 0) * num >= 2 * to_vendor


def _sell_cargo(role: BaseRole, ctx: _Ctx) -> bool:
    """把背包里的矿背到小贩那儿卖掉：贴着就 `sell`（一次卖光那一种），否则走一步。

    没货 / 没有小贩 / 一个都走不到 ⇒ False（调用方接着去挖矿）。卖掉哪种由 `_best_load` 挑
    （收购价最高的），一回合只发得出一条 `sell`。"""
    turn, q = ctx.turn, ctx.q
    kind, num = _best_load(role, turn.vendor_prices)
    if not kind or not turn.map.vendors:
        return False
    walk, size = _passable(turn), turn.map.size
    hops = [(steps_between(role.pos, p, walk, size), p) for p in turn.map.vendors]
    hops = [(d, p) for d, p in hops if d >= 0]
    if not hops:
        return False
    to_vendor, vendor = min(hops)
    if to_vendor == 0:
        return _emit(q.cmds, role, actions.Sell, kind, num)
    return q.step(role, vendor, avoid=frozenset(ctx.sites))


def _detour_sell(role: BaseRole, ctx: _Ctx, mine: Pos) -> bool:
    """去矿的路上**顺手卖矿**（用户口径）：离小贩切比雪夫 ≤ `DETOUR_MAX`(2) ⇒ 这一回合先卖。

    判据是**切比雪夫距离**（不是"绕路多花几步"）：路过小贩边上就卖掉，别再考虑值不值 ——
    前提当然是背包里有卖得掉的货（`_best_load` 判，口径只有一份）。已经贴着矿就别绕了（该采了）；
    **贴着小贩的那一格就是 `sell` 的站位** ⇒ 当场卖（走 `_sell_cargo` 那一支），
    不是"再朝它迈一步"：贴着时 `step_toward` 返回 `None`，迈步会退化成这一回合空指令。"""
    turn, q = ctx.turn, ctx.q
    if role.pos.dist(mine) <= 1:
        return False
    if _best_load(role, turn.vendor_prices)[1] <= 0:
        return False
    if _at_vendor(role, turn):
        return _sell_cargo(role, ctx)
    near = [v for v in turn.map.vendors if role.pos.dist(v) <= DETOUR_MAX]
    return q.step(role, min(near, key=lambda v: (role.pos.dist(v), v))) if near else False


def _best_load(role: BaseRole, prices: Mapping[str, int]) -> tuple[str, int]:
    """挑这一趟卖哪种矿：收购价最高的，同价取件数多的；挑不出来 ⇒ `("", 0)`。

    价 ≤ 0（小贩不收）或件数为 0 的矿跳过（空价目表 ⇒ 一件都不卖）。名字参与比较只是为了让
    并列可复现。"""
    loads = [
        (prices.get(kind, 0), role.bag.get(kind, 0), kind)
        for kind in SELLABLE
        if prices.get(kind, 0) > 0 and role.bag.get(kind, 0) > 0
    ]
    if not loads:
        return "", 0
    _, num, kind = max(loads)
    return kind, num


def _cargo_value(role: BaseRole, turn: Turn) -> int:
    """这个角色背包里的矿值多少金币（按当前收购价）。"""
    return sum(
        turn.vendor_prices.get(kind, 0) * num
        for kind, num in role.bag.items()
        if kind in SELLABLE
    )


def _team_cargo_value(ctx: _Ctx) -> int:
    """全队背包里的矿值多少金币 —— 第 1 级的筹资是**两个人的矿一起卖**（用户口径）。"""
    return sum(_cargo_value(r, ctx.turn) for r in ctx.turn.roles)


def _at_vendor(role: BaseRole, turn: Turn) -> bool:
    """贴着小贩了吗（`steps_between == 0` 的口径：小贩那格挡路，人只能停在它旁边）。"""
    walk, size = _passable(turn), turn.map.size
    return any(steps_between(role.pos, v, walk, size) == 0 for v in turn.map.vendors)


def _can_fund(turn: Turn) -> bool:
    """筹资可行：地图上有小贩、且有收购价 > 0 的矿 —— 矿挖了卖得掉，才谈得上凑钱。"""
    if not turn.map.vendors:
        return False
    prices = turn.vendor_prices
    return any(prices.get(kind, 0) > 0 for kind in turn.map.ores.values())


def _team_holds(turn: Turn, voucher: str) -> int:
    """全队手里这张券一共几张 —— 买之前要减掉它们：一张券对应一座待升的建筑。

    券没有转移指令（谁买谁用），但**能不能用**要看全场还剩几个目标 ⇒ 买之前按全队算，
    否则两个角色会各买一轮、同一座炮收回两张券。
    """
    return sum(r.bag.get(voucher, 0) for r in turn.roles)


def _use_voucher_here(role: BaseRole, ctx: _Ctx) -> bool:
    """站在岗位上、手里有武器券、且目标就贴着 ⇒ 先用掉它（用户口径"到了位置先用券"）。

    不满足就什么都不发（待命）—— 目标不在手边（那座炮离得远）不为了它走开，下一回合再看。"""
    held = next((v for v in VOUCHER.values() if role.bag.get(v, 0) > 0), None)
    if held is None:
        return False
    spots = _targets_for(held, ctx.turn)
    if not spots or role.pos.dist(spots[0]) > 1:
        return False
    return _emit(ctx.q.cmds, role, actions.Use, held, spots[0])


def _weak_l1(turn: Turn) -> tuple[Pos, ...]:
    """环上**该处理了的 L1 墙**（`core.needs_repair`：血 < `WALL_REPAIR_HP`(200)），按坐标序。

    ⚠️ 只认 L1（用户拍板）：L2/L3 拆了只能重砌回 L1（掉一级），它们的血量交给券链的升级券与
    夜里的修复包（升级、修复都回满血）。已毁（血 0）的墙在 `model._walls` 就丢了 ⇒ 它在那里算
    "缺口"。`health` 缺失（-1）⇒ 未知 ⇒ 不拆。"""
    return tuple(
        w.pos
        for w in sorted(turn.walls, key=lambda w: w.pos)
        if w.level == 1 and needs_repair(w)
    )


# ── 白天：采石 → 砌墙 ────────────────────────────────────────────────


def _outside_spots(turn: Turn, role: Worker, target: Pos) -> tuple[Pos, ...]:
    """`target` 的邻格里"在防御盒子外、又站得上去"的那些 —— 最后一格墙的落脚点（按坐标排）。

    要站上去（`onto`）：这些格子是盒外的空地，`step_toward` 到不了。盒外邻格全被占 / 出图
    ⇒ 空集，调用方这一回合不砌。"""
    station = turn.map.station
    if station is None:
        return ()
    box = box_cells(station)
    # `blocked` 里混着队友和自己（`model._entries`）⇒ 只把自己的那格摘出来
    taken = turn.map.blocked - {role.pos}
    width, height = turn.map.size
    return tuple(
        sorted(
            p
            for p in (Pos(target.x + d.x, target.y + d.y) for d in STEPS)
            if 0 <= p.x < width and 0 <= p.y < height and p not in box and p not in taken
        )
    )


def _aside_cell(
    role: BaseRole, turn: Turn, claimed: set[Pos], avoid: set[Pos] = frozenset()
) -> Pos | None:
    """挪开自己：从脚下这一格挑一个可走的邻格；八面都走不了 ⇒ None。

    没有"目标"的走法（唯一用途见 `WallLine.run` 站在待砌墙格上的那一支），所以不走
    `step_toward`——它的契约是"贴着 goal 即到"，对任意邻格都返回 None。挪到建造格上等于
    换个格子接着站，所以 `avoid`（建造格）照避；`claimed`（别人已落的脚格）由第二段传进来。"""
    walk = turn.map.blocked | claimed | avoid
    width, height = turn.map.size
    for d in STEPS:
        cell = Pos(role.pos.x + d.x, role.pos.y + d.y)
        if cell not in walk and 0 <= cell.x < width and 0 <= cell.y < height:
            return cell
    return None
