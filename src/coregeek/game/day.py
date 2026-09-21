"""白天：工人的差事链（`DAY_CHAIN`）、收工闸门（`BACK_TO_POST`）、拆墙放人与建武器名额。

`DAY_CHAIN` 就是那张状态机图（`docs/pic/白天工人状态机.png`，图上每个盒子一个类），
**顺序即策略**；`run` 的契约两条写在 `states.State` 的 docstring 里。本模块只被 `planner`
调用（单向）：底座在 `states`，通用判定在 `utils`。

只有一个调用者的控制流直接装进那个类的 `run`（`BuildWalls` / `RepairWalls` / `BackToPost`）；
仍留模块级的助手各有第二个调用者：`slots`（`planner._intents` 要在角色循环之前先算武器缺口）。

`BACK_TO_POST` 挂在 `DAY_CHAIN` **之前**、且不在链里：补墙优先于收工，开拓者也走它
（链上只跑工人）。它**只发 move** —— 到岗调 `night.defend` 会发 `attack`，白天发就是非法
指令（红线）。
"""

from collections.abc import Iterator, Set

from ..protocol import actions  # 指令只能经 Action 产出
from .grid import STEPS, Pos, box_cells, wall_cells, weapon_sites
from .path import step_outside, steps_between
from .roles import BaseRole, Pioneer, Worker
from .states import (
    ECONOMY_CHAIN,
    POST_MARGIN,
    ROUNDS_PER_STONE,
    STONE_KEEP_RAISING,
    STONE_RESERVE,
    TIME_MARGIN,
    WALL,
    WALL_COST,
    WEAPON_COST,
    State,
    _Ctx,
    _Queue,
    _emit,
    _mine_spare_ore,
    _pick_ore,
    _post_spots,
    _sell_ore,
    _steps_to_post,
    _upgrade_line,
    _wall_item,
    _weak_walls,
)
from .utils import (
    _passable,
    _pioneer_mans_guns,
    _ring,
    _sealed_back,
    _stuck_inside,
    _weapon_groups,
)
from .world import Turn, Weapon


#: 三座武器的种类，下标与 `grid.weapon_sites()` 的落点一一对应：前排相邻两格放 2 座火箭
#: （一个角色站内侧那格能同时贴两座、按冷却交替开火 ⇒ 2 人操 3 座），前排下一格放加特林。
WEAPONS_BY_SITE = ("rocket", "rocket", "gatling")


#: 拆墙放人的时间门：白天还剩这么多回合以上才允许拆 —— 拆了不回收、当天还得砌回来，
#: 太晚拆的洞等于整夜开着。拍的。
HOLE_MIN_LEFT = 30


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


def rescue(
    role: BaseRole,
    turn: Turn,
    q: _Queue,
    box: frozenset[Pos],
) -> bool:
    """有人被关在盒子里 ⇒ 工人去拆一格放人。工人自己被关住时也走这里、且先走这里。

    开哪一格：开了之后真能让某个被困的人迈出去、且离救援者最近的那一格（并列取坐标序）。
    环没砌满 ⇒ 整个不生效（`_ring` 那一句）：第 1 天那个缺口是"还没砌"、不是"刚拆的洞"，
    地图上同形，只能靠"环满不满"分开。守门：`RescueTest`。"""
    if not turn.is_day or not isinstance(role, Worker) or not box:
        return False
    station = turn.map.station
    if station is None:
        return False
    wall = wall_cells(station, turn.map.size[0], sealed=_sealed_back(turn))
    if _ring(turn) or q.claimed & set(wall):
        return False
    if role.stone < WALL_COST or turn.day_rounds_left <= HOLE_MIN_LEFT:
        return False
    stuck = _stuck_inside(turn, box)
    if not stuck:
        return False

    walk, size = turn.map.blocked | q.claimed, turn.map.size
    # 只认真有墙的格（`turn.walls` 是我方围墙实体）：`wall_cells` 是几何，没砌的那一格若被
    # 机器人踩着照样挡路（`_ring` 因此以为环齐了），照它拆就是拆一格空地
    live = {w.pos for w in turn.walls}
    free = [
        (role.pos.dist(c), c)
        for c in wall
        if c in live and any(step_outside(v.pos, box, walk - {c}, size) is not None for v in stuck)
    ]
    if not free:
        return False
    site = min(free)[1]
    if role.pos.dist(site) <= 1:
        # 登记被拆的那一格：挡住同回合的第二个工人（对寻路是空操作，它本来就在 `blocked` 里）
        q.claimed.add(site)
        return _emit(q.cmds, role, actions.Remove, site)
    # 两条路都只是"朝那一格挪一格"：贴近了下一回合自然就拆
    return q.step(role, site)


def _can_fund(turn: Turn) -> bool:
    """筹资可行：地图上有小贩、且有收购价 > 0 的矿 —— 矿挖了卖得掉，才谈得上凑钱。"""
    if not turn.map.vendors:
        return False
    prices = turn.vendor_prices
    return any(prices.get(kind, 0) > 0 for kind in turn.map.ores.values())


class BuildWeapons(State):
    """建立武器：份额有缺且钱够 ⇒ 就地建或走向落点。

    `_emit` 被拒 / `q.step` 走不到 ⇒ 返回 False，这一回合落到筹资（不是"建不了就待命"）。
    名额在钱的判据之前就吃掉：`budget` 一回合内只降不升，两种写法今天等价，别顺手调换。"""

    def run(self, role: Worker, ctx: _Ctx) -> bool:
        slot = next(ctx.slots, None)
        if slot is None or ctx.budget < WEAPON_COST:
            return False
        kind, cell = slot
        ctx.budget -= WEAPON_COST  # 认领即预留：宁可这回合少建，不可超支
        ctx.sites.add(cell)
        if role.pos.dist(cell) <= 1:
            if _emit(ctx.q.cmds, role, actions.Build, kind, cell):
                return True
        elif ctx.q.step(role, cell, avoid=frozenset(ctx.sites), with_paths=True):
            return True
        return False


class RaiseForWeapons(State):
    """采矿筹资（为武器）：有缺但钱不够、筹资又可行 ⇒ 整条墙线让位（含修墙），先卖背包
    里的货、再采最值钱的矿凑钱。

    与链上别的状态不同：条件成立就无条件认领这一回合 —— 火力缺口比墙急。石头只留
    `STONE_KEEP_RAISING` 块。"""

    def run(self, role: Worker, ctx: _Ctx) -> bool:
        if not (ctx.weapon_gap and ctx.budget < WEAPON_COST and _can_fund(ctx.turn)):
            return False
        if not _sell_ore(
            role, ctx.turn, ctx.q, ctx.sites, with_paths=True, keep=STONE_KEEP_RAISING
        ):
            _mine_spare_ore(role, ctx.turn, ctx.q, ctx.sites, ctx.ore_taken)
        return True


class BuildWalls(State):
    """建墙（含采矿）：把墙砌到黑板分给本工人的那一格上 —— 缺石头先去采、手上有石头就砌，
    一回合只干其中一件；两件都干不成 ⇒ False，让驱动方走经济线。

    `ctx.target` 是 None（环砌满）⇒ False，让给修墙；有人会被砌满的墙关住（`ctx.leaving`）
    ⇒ True 而一条指令都不发：待命，别跑远，下回合缺口还在。每回合独立判定、不存跨回合状态。"""

    def run(self, role: Worker, ctx: _Ctx) -> bool:
        if ctx.target is None:
            return False  # 没分到墙格 ⇒ 驱动方走经济线
        if ctx.leaving:
            return True
        # 只认石矿（墙只吃石头）；排除本回合别人认领的矿格
        mine = _pick_ore(
            role.pos,
            {p: k for p, k in ctx.turn.map.ores.items() if p not in ctx.ore_taken},
            ctx.turn.vendor_prices,
            want_stone=True,
        )
        want = _stones_to_mine(role, ctx.turn, ctx.target, mine, ctx.remaining)

        if want > 0 and mine is not None:
            ctx.ore_taken.add(mine)  # 挑中即登记（本回合真要采它）
            if role.pos.dist(mine) <= 1:
                if _emit(ctx.q.cmds, role, actions.Collect, mine):
                    return True
            # 还没走到矿边：动身成功后整条路记进预留账（先预留再走会绕开自己刚记下的路）
            elif ctx.q.step(role, mine, avoid=frozenset(ctx.sites), with_paths=True, reserve=True):
                return True

        if role.stone >= WALL_COST:
            # 认领要发生在动身之前：等砌完再登记，另一个工人会在同一回合也奔着它去
            ctx.sites.add(ctx.target)
            # 环上只剩最后一格 ⇒ 从盒子外面砌（站在环里砌完自己就被封在里面）；盒外一个
            # 能站的邻格都没有 ⇒ 照旧就近砌；有格子却走不到 ⇒ 这回合不砌（交给别的差事）
            outside = _outside_spots(ctx.turn, role, ctx.target) if len(_ring(ctx.turn)) == 1 else ()
            if outside:
                # 走到盒旁再砌。不避让 `ctx.target`：环上那个缺口正是出盒子的近路，避让它
                # 就得从后方通道绕整整一圈
                if role.pos in outside:
                    if _emit(ctx.q.cmds, role, actions.Build, WALL, ctx.target):
                        return True
                for spot in outside:
                    if ctx.q.step(
                        role,
                        spot,
                        avoid=frozenset(ctx.sites - {ctx.target}),
                        onto=True,
                        with_paths=True,
                        reserve=True,
                    ):
                        return True
            elif role.pos == ctx.target:
                # 站在目标格上就先挪开一格、这一回合不砌：人站在墙上时那格在网格里只剩
                # "worker"（单位铺在最后，墙被盖掉）⇒ 看不出砌过没有，`_ring` 的"自己人算
                # 路过"又把它复活成候选 ⇒ 每回合对同一格 build，石头白花
                ctx.q.beside(
                    role,
                    lambda claimed, r=role, av=frozenset(ctx.sites): _aside_cell(r, ctx.turn, claimed, av),
                )
                return True
            elif role.pos.dist(ctx.target) <= 1:
                # 与建武器同一条契约：`step_toward` 停在贴着目标的一格，那正是 `build` 的站位
                if _emit(ctx.q.cmds, role, actions.Build, WALL, ctx.target):
                    return True
            elif ctx.q.step(role, ctx.target, avoid=frozenset(ctx.sites), with_paths=True, reserve=True):
                return True
        return False  # 没石头、采不到 ⇒ 调用方走其他差事


class RepairWalls(State):
    """围墙修复：弱墙（见 `_weak_walls`）升级当修 —— 用哪件东西看墙的等级（`_wall_item`）。
    取等级最低的一面（最便宜），同等级取近的（认领在动身之前，两个修墙工人不挤同一面）。

    持券 ⇒ 走到那面墙、贴着就 `use`；没券 ⇒ 商店可达、价目里有它、金币够就走去商店 `Buy`。
    环砌满了才轮得到它（排在 `BuildWalls` 后面）。"""

    def run(self, role: Worker, ctx: _Ctx) -> bool:
        budget = ctx.turn.day_rounds_left - TIME_MARGIN
        walk, size = _passable(ctx.turn), ctx.turn.map.size
        weak = [w for w in _weak_walls(ctx.turn) if w.pos not in ctx.repair_taken]
        if not weak:
            return False
        target = min(weak, key=lambda w: (w.level, role.pos.dist(w.pos), w.pos))
        item = _wall_item(target.level)

        if item not in role.bag:
            price = ctx.turn.shop_prices.get(item, 0)
            hops = [(steps_between(role.pos, s, walk, size), s) for s in ctx.turn.map.shops]
            hops = [(d, s) for d, s in hops if d >= 0]
            to_shop, shop = min(hops) if hops else (-1, None)
            if shop is None or price <= 0 or ctx.turn.gold < price or to_shop + 1 > budget:
                return False  # 这一面这一轮修不了 ⇒ 待命（明天再说），不换另一面
            if to_shop == 0:
                return _emit(ctx.q.cmds, role, actions.Buy, item, 1)
            return ctx.q.step(role, shop, avoid=frozenset(ctx.sites))

        ctx.repair_taken.add(target.pos)
        if role.pos.dist(target.pos) <= 1:
            return _emit(ctx.q.cmds, role, actions.Use, item, target.pos)
        to_wall = steps_between(role.pos, target.pos, walk, size)
        if to_wall < 0 or to_wall + 1 > budget:
            return False  # 来不及 ⇒ 待命，明天接着走
        return ctx.q.step(role, target.pos, avoid=frozenset(ctx.sites))


class BackToPost:
    """回防守位：环砌完了、离夜里的第一波只剩回程步数 ⇒ 回那一组的岗位；`True` = 这一回合
    到此为止。

    白天就得动身：回炮位常要绕整面围墙、从后方通道进来，直线三四步、BFS 十几步。目标与夜里
    `night.defend` 的岗位同一个（`_post_spots`），天黑时人已在岗、第一回合就能开火。与它的
    唯一区别是不调 `night._fire`：`attack` 仅黑夜，白天发就是非法指令。

    先看时间、再看位置：判据是"到最近那个岗位的 BFS 步数 ≥ 白天还剩的回合 − `POST_MARGIN`"
    （拍的）；已在岗时步数 0，只有白天最后那几回合才轮得到"在岗待命"。

    不放进 `DAY_CHAIN`：它在链之前（补墙优先于收工），且开拓者也走它，而链上只跑工人。
    开拓者是补位炮手（`_pioneer_mans_guns`）：工人够操满所有组时它不占岗位 —— 与 `night.defend`
    同一个判据，两处必须同源，只拦一处会让白天把它送进岗位、夜里又不认领，整组没人操。"""

    def run(self, role: BaseRole, ctx: _Ctx) -> bool:
        if _ring(ctx.turn):
            return False  # 环上还有缺口 ⇒ 这一支不生效（补墙优先于收工）
        if isinstance(role, Pioneer) and not _pioneer_mans_guns(ctx.turn):
            return False
        walk, size = _passable(ctx.turn), ctx.turn.map.size
        # 最近、还没人认领、而且真走得到的岗位 —— 一趟判定就够：最近的都赶不上，更远的更赶不上。
        # BFS -1（不可达）剔掉，切比雪夫给不出这个值；已经在岗位上 ⇒ 步数 0，与"还差 3 步"同一刻度。
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
        if steps < ctx.turn.day_rounds_left - POST_MARGIN:
            return False  # 还剩富裕回合 ⇒ 照常干活
        for w in group:
            ctx.taken.add(w.pos)  # 定下这组了：认领，免得另一个角色也奔这里（一人只能操一座）
        if steps == 0:
            return True  # 已经在岗 ⇒ 这一回合待命（什么都不发 = 合法空指令）
        ctx.q.step(role, spot, onto=onto)  # 只发 move，绝不调 `_fire`（白天发 attack = 非法指令）
        return True


#: 白天工人链：从上往下第一个"认领了这一回合"的状态说了算。顺序即策略。
DAY_CHAIN: tuple[State, ...] = (
    BuildWeapons(),  # 建立武器：名额有缺、钱够 ⇒ 建 / 走向落点
    RaiseForWeapons(),  # 采矿收集（为武器）：有缺但钱不够、筹资可行 ⇒ 整条墙线让位
    BuildWalls(),  # 建墙（含采矿）：补缺口 / 采石 / 待命
    RepairWalls(),  # 围墙修复：弱墙"升级当修"，环砌满才轮得到
) + ECONOMY_CHAIN


#: 收工闸门（白天工人链之前的一道，见 `BackToPost`）
BACK_TO_POST = BackToPost()


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


def _stones_to_mine(role: Worker, turn: Turn, target: Pos, mine: Pos | None, free: int) -> int:
    """这一趟还该采几块石头 —— 按回合预算每回合现算。

    记 `s` = 手里的石头、`k` = 还要采的块数：

        走到矿 + 采 k 块 + 从矿走回工地 + 砌 s+k 座的"挪一格 + 建造"
        = d_mine + d_wall + 2s − 1 + 3k        # 到达工地那一步已贴着首格
                                            # ⇒ 每多采一块净花 3 回合

    令它 ≤ `白天还剩的回合 − TIME_MARGIN` 解出 k，再与"还差几格墙 + `STONE_RESERVE`"取小
    （存货：没有转移物品的指令，多采的石头只能自己拿着，墙被打掉时立刻补得上）。距离一律
    BFS 真实步数（回工地常要绕整面围墙，切比雪夫会把 10+ 步说成 3 步）。-1 ⇒ 一块都别采。"""
    if mine is None:
        return 0
    walk, size = _passable(turn), turn.map.size
    to_mine = steps_between(role.pos, mine, walk, size)
    to_wall = steps_between(mine, target, walk, size)
    if to_mine < 0 or to_wall < 0:
        return 0
    budget = turn.day_rounds_left - TIME_MARGIN - to_mine - to_wall - 2 * role.stone + 1
    return max(0, min(budget // ROUNDS_PER_STONE, free + STONE_RESERVE - role.stone))


def _aside_cell(
    role: BaseRole, turn: Turn, claimed: set[Pos], avoid: Set[Pos] = frozenset()
) -> Pos | None:
    """挪开自己：从脚下这一格挑一个可走的邻格；八面都走不了 ⇒ None。

    没有"目标"的走法（唯一用途见 `BuildWalls.run` 站在待砌墙格上的那一支），所以不走
    `step_toward`——它的契约是"贴着 goal 即到"，对任意邻格都返回 None。挪到建造格上等于
    换个格子接着站，所以 `avoid`（建造格）照避；`claimed`（别人已落的脚格）由第二段传进来。"""
    walk = turn.map.blocked | claimed | avoid
    width, height = turn.map.size
    for d in STEPS:
        cell = Pos(role.pos.x + d.x, role.pos.y + d.y)
        if cell not in walk and 0 <= cell.x < width and 0 <= cell.y < height:
            return cell
    return None
