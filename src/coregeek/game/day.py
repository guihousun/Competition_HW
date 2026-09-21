"""白天：四级链 —— 0 收工回岗 → 1 建武器 → 2 建墙 → 3 挖矿买券。

`DAY_CHAIN` 是 1–3 级那四个状态类（顺序即策略：从上往下第一个 `run` 返回 True 的说了算），
`BACK_TO_POST` 是链之前的第 0 级。级别之间靠**判据**衔接，不是靠状态：武器没建满就永远停在
第 1 级，武器满了才会问环上有没有缺口，环也补齐了才轮到挖矿买券。

`run` 的契约两条写在 `core.State` 的 docstring 里。本模块只被 `planner` 调用（单向）：
底座在 `core`（常量 / 走路账 / 黑板 / 岗位几何 / 挖矿排序），通用判定在 `utils`。
"""

from collections.abc import Iterator, Mapping

from ..protocol import actions  # 指令只能经 Action 产出
from .core import (
    VOUCHER,
    VOUCHER_CHAIN,
    WALL,
    WALL_COST,
    WALL_MAX_HP,
    WALL_VOUCHER,
    WEAPON_COST,
    State,
    _Ctx,
    _collect,
    _emit,
    _post_spots,
    _priciest_ore,
    _steps_to_post,
)
from .grid import STEPS, Pos, box_cells, weapon_sites
from .path import steps_between
from .roles import BaseRole, Pioneer, Worker
from .utils import _passable, _pioneer_mans_guns, _ring, _weapon_groups
from .world import Turn, Wall, Weapon

#: 三座武器的种类，下标与 `grid.weapon_sites()` 的落点一一对应：前排相邻两格放 2 座火箭
#: （一个角色站内侧那格能同时贴两座、按冷却交替开火 ⇒ 2 人操 3 座），前排下一格放加特林。
WEAPONS_BY_SITE = ("rocket", "rocket", "gatling")


#: 收工门的容错余量（回合）：`回岗步数 + POST_MARGIN + 手里的武器券数 ≥ 白天剩余` 就动身。
#: 3 是拍的（第 121 步那个"固定 5 回合、不看距离"的口径没有容错，用户报"问题很大"）。
POST_MARGIN = 3


#: "顺路卖矿"的绕路上限（格）：去矿的路上，绕去小贩比直走多花不超过这么多步就顺路卖掉。
DETOUR_MAX = 2


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
    """第 0 级：`回岗步数 + POST_MARGIN(3) + 手里的武器券数 ≥ 白天剩余` ⇒ 回那一组的岗位；
    `True` = 这一回合到此为止。

    判据是**实时算出来的回岗步数**（BFS，与挑岗位同一个口径）：回岗要走多久就得多早动身，
    再加上 `POST_MARGIN` 的容错余量、以及"到岗后一张一张用掉"的券数。到岗后第一件事是用券
    （`_use_voucher_here`：贴着就能升的那张先用），用不上就待命。目标与夜里 `night.defend` 的岗位同一个（`core._post_spots`）：
    多座组共用一个操作位（要站上去），单座组就是那座炮 —— 天黑时人已经在岗上，夜里第一回合
    就能交替开火。**只发 `move`**：复用 `night.defend` 会发 `attack`，而白天发是非法指令（红线）。

    不放进 `DAY_CHAIN`：它在链之前（到点就不干活了），且开拓者也走它，而链上只跑工人。
    开拓者是补位炮手（`_pioneer_mans_guns`）：工人够操满所有组时它不占岗位。"""

    def run(self, role: BaseRole, ctx: _Ctx) -> bool:
        if isinstance(role, Pioneer) and not _pioneer_mans_guns(ctx.turn):
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
        # 门：`回岗步数 + POST_MARGIN + 手里的武器券数 ≥ 白天剩余`。步数实时算（回岗要走多久
        # 就得多早动身），券按张数留出"到了岗一张一张用掉"的回合。
        if ctx.turn.day_rounds_left > steps + POST_MARGIN + _weapon_vouchers(role):
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

    不完备有两种：**缺口**（那一格没墙）与 **L1 弱墙**（血 < 满血 1/4 ⇒ 直接拆了重砌：1 块石头
    换满血，比 20 金的升级券便宜）。L2/L3 的损伤**不阻塞**这一级（升级券顺带回满血，见第 3 级）。

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
            _sell_or_mine(role, ctx)  # 无任务模式：工人只管"矿 → 金币"，券归开拓者
            return True
        voucher_errand(role, ctx)
        return True  # 链尾终态：全升满了也照样挖矿，不把这一回合让给谁


#: 白天链：从上往下第一个"认领了这一回合"的状态说了算。顺序即策略。
DAY_CHAIN: tuple[State, ...] = (
    BuildWeapons(),  # 1：名额有缺、钱够 ⇒ 两个工人分头建满
    RaiseForWeapons(),  # 1：有缺但钱不够 ⇒ 两人一起挖最贵的矿凑钱
    WallLine(),  # 2：缺口 / L1 弱墙 ⇒ 采石、拆、砌
    VoucherLine(),  # 3：完备之后挖矿赚钱 → 买券 → 立刻用
)


#: 收工闸门（白天链之前的一道，见 `BackToPost`）
BACK_TO_POST = BackToPost()


def voucher_errand(role: BaseRole, ctx: _Ctx) -> bool:
    """买券差事：挖最贵的矿攒钱 → 凑够券价就去卖 → 买 → 立刻走到目标用掉。

    第 3 级的**前提是武器建满了**（`ctx.weapon_gap` 为假）：武器还有缺就得先顾武器，拿那点
    金币去买券是本末倒置（旧口径"武器 ok 才升级"）。

    工人（第 3 级）与空闲的开拓者共用这一条。券按 `VOUCHER_CHAIN` 取**第一张还有东西可升**的
    （武器二级 > 武器三级 > 围墙二级 > 围墙三级）—— 攒钱的目标就是它，不跳级去买更便宜的券。
    手里已经持券 ⇒ 先用掉（买完就用，不囤）。

    买入前从 `ctx.budget` **预扣券价**：另一条线（另一个工人 / 开拓者）这一回合就不会重复买
    同一张。全升满了 / 还买不起 ⇒ 工人接着挖矿（`collect` 仅工人，开拓者这时候什么都不发）。

    ⚠️ **差事走不通时一律落回挖矿，绝不干等**（券的目标暂时够不上、商店走不到、另一个角色
    正堵在炮位旁的那格……）—— 第 3 级是链尾，这里不发指令就等于这个角色整回合空转。"""
    if ctx.weapon_gap:
        return False
    turn, q = ctx.turn, ctx.q
    held = next((v for v in VOUCHER_CHAIN if role.bag.get(v, 0) > 0), None)
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
    # 按需要多少张就买多少张（用户口径）：需要几张看"还升得动的目标有几个"，买得起几张看金币；
    # 取小的那个一次买够 —— 200 金、两张 100 金的券、两座待升的武器 ⇒ 一条 `buy num=2`。
    # ⚠️ **买之前先减掉"用不上的那些张数"**（用户口径"不能浪费金币买不能用的升级券"）：
    #   还升得动的目标数 − 全队手里已有的张数 − 这一回合已经预扣的张数
    # 只按金币限量是不够的：钱多的时候两条线会各买满一轮，同一座炮买回两张券。
    free = len(spots) - _team_holds(turn, voucher) - ctx.bought.get(voucher, 0)
    count = min(free, ctx.budget // price) if price > 0 else 0
    if count > 0 and _walk_to_shop(role, ctx, voucher, count):
        ctx.budget -= price * count  # 预扣：这一回合的另一条线不会再买
        ctx.bought[voucher] = ctx.bought.get(voucher, 0) + count
        return True
    if worth and worth + ctx.budget >= price:
        return _sell_cargo(role, ctx)  # 攒够了 ⇒ 背去卖（卖完下一回合买得上）
    return _mine_for_voucher(role, ctx)


def _voucher_target(turn: Turn) -> tuple[str, tuple[Pos, ...]] | None:
    """优先链上第一张**还有东西可升**的券 + 它这一回合能打的**全部**目标（按优先级排）。

    全升满 ⇒ `None`。返回的是一串而不是一格：买券要"按需要多少张就买多少张"（用户口径），
    张数就是这一串的长度。"""
    for voucher in VOUCHER_CHAIN:
        spots = _voucher_targets(voucher, turn)
        if spots:
            return voucher, spots
    return None


def _voucher_targets(voucher: str, turn: Turn) -> tuple[Pos, ...]:
    """这张券现在能打的所有目标（按优先级排）；一张都不剩 ⇒ 空元组。

    武器券：`level == 目标等级 − 1` 的武器，**火箭优先**（群体打击口径）、同类按 id。
    围墙券：`level == 目标等级 − 1` 的墙，先挑**残血的**（升级同时回满血，等于顺手修好），
    再按坐标序。基地券不在这条链上（夜里由 `night.upgrade_station` 管）。"""
    if voucher in VOUCHER.values():
        want = 2 if voucher == VOUCHER[2] else 3
        guns = [w for w in turn.weapons if w.level == want - 1]
        return tuple(
            w.pos for w in sorted(guns, key=lambda w: (0 if w.kind == "rocket" else 1, w.id))
        )
    want = 2 if voucher == WALL_VOUCHER[2] else 3
    walls = [w for w in turn.walls if w.level == want - 1]
    return tuple(w.pos for w in sorted(walls, key=lambda w: (not _is_weak(w), w.pos)))


def _walk_to_use(role: BaseRole, ctx: _Ctx, voucher: str) -> bool:
    """把手里这张券用掉：贴着目标就 `use`，否则走一步（没有可用的目标 ⇒ False）。

    手里可能攒着好几张（"尽可能多买"那一条）⇒ 每回合挑**还升得动的第一格**用掉一张。"""
    spots = _voucher_targets(voucher, ctx.turn)
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


def _sell_or_mine(role: Worker, ctx: _Ctx) -> bool:
    """无任务模式里工人白天唯一的事：**攒够一趟的货**就背去卖，否则接着挖最贵的矿。

    券的买与用在这个模式里全归开拓者（用户口径）⇒ 工人这边只剩"矿 → 金币"这条线。
    "够一趟"用的是老够本门（用户口径）：货值 ≥ 2 × 到小贩的步数 —— 少了它，背一块石头也会
    走十几步去卖 1 金币。已经贴着小贩 ⇒ 直接卖（这趟路早付过了）。"""
    kind, num = _best_load(role, ctx.turn.vendor_prices)
    if _worth_the_trip(role, ctx.turn, kind, num):
        return _sell_cargo(role, ctx)
    return _mine_ore(role, ctx)


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
    """去矿的路上顺路卖矿：绕去小贩比直走多花 ≤ `DETOUR_MAX` 格 ⇒ 先朝小贩迈一步。

    贴上小贩的那一回合由 `voucher_errand` 的"贴着就卖"那一支接手（`via == 0` 时这里必须
    `return False`：绕一步 = 原地不动 = 这一回合空指令）。已经贴着矿就别绕了（该采了）。
    "有没有可卖的货"用 `_best_load` 判、不在这里重抄一遍 —— 口径只有一份。"""
    turn, q = ctx.turn, ctx.q
    if role.pos.dist(mine) <= 1:
        return False
    if _best_load(role, turn.vendor_prices)[1] <= 0:
        return False
    walk, size = _passable(turn), turn.map.size
    direct = steps_between(role.pos, mine, walk, size)
    if direct < 0:
        return False
    for vendor in sorted(turn.map.vendors):
        via = steps_between(role.pos, vendor, walk, size)
        if via == 0:
            return False
        after = steps_between(vendor, mine, walk, size)
        if via < 0 or after < 0:
            continue
        if via + after - direct <= DETOUR_MAX:
            return q.step(role, vendor)
    return False


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


def _weapon_vouchers(role: BaseRole) -> int:
    """手里的**武器**升级券张数 —— 收工门按它多留几个回合（到岗第一件事就是用掉它们）。

    数张数不数"有几种"：一次买够 N 张（"尽可能多买"那条）就得留出 N 个回合。
    **围墙券不算**（用户口径"武器券数"）：它们的目标是墙、不在炮位上。

    ⚠️ 同理，买券要在收工门**之前**（第 3 级是链尾，`weapon_gap` 为真时整条不跑）——
    券在手上就一定用得上。"""
    return sum(role.bag.get(name, 0) for name in VOUCHER.values())


def _use_voucher_here(role: BaseRole, ctx: _Ctx) -> bool:
    """站在岗位上、手里有武器券、且目标就贴着 ⇒ 先用掉它（用户口径"到了位置先用券"）。

    不满足就什么都不发（待命）—— 目标不在手边（那座炮离得远）不为了它走开，下一回合再看。"""
    held = next((v for v in VOUCHER.values() if role.bag.get(v, 0) > 0), None)
    if held is None:
        return False
    spots = _voucher_targets(held, ctx.turn)
    if not spots or role.pos.dist(spots[0]) > 1:
        return False
    return _emit(ctx.q.cmds, role, actions.Use, held, spots[0])


def _is_weak(wall: Wall) -> bool:
    """血不到满血 1/4 的墙（`health` 缺失 = -1 ⇒ 未知 ⇒ 不算弱；已毁的墙不在 `turn.walls` 里）。"""
    return 0 < wall.health * 4 < WALL_MAX_HP.get(wall.level, WALL_MAX_HP[1])


def _weak_l1(turn: Turn) -> tuple[Pos, ...]:
    """环上血不到满血 1/4 的 **L1** 墙，按坐标序（可复现）。

    ⚠️ 只认 L1：L2/L3 拆了只能重砌回 L1（掉一级），不划算 —— 它们的损伤交给第 3 级的升级券
    （升级同时回满血）。已毁（血 0）的墙在 `model._walls` 就丢了 ⇒ 它在那里算"缺口"。"""
    return tuple(
        w.pos
        for w in sorted(turn.walls, key=lambda w: w.pos)
        if w.level == 1 and _is_weak(w)
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
