"""白天工人链：状态类 + 驱动链，以及它们依赖的下层（走路账、指令出口、黑板、经济线与岗位几何）。

图见 `docs/pic/白天工人状态机.png`，图上每个盒子一个类，链上顺序即策略（`DAY_CHAIN`）。

本模块是 `planner` 的**下层**（`planner` 单向 import 本模块，反过来会循环导入）—— 所以"链和
`planner` 都要用"的那几样（`_passable` / `_ring` / `_sealed_back` / 岗位几何）留在这里、由
`planner` 取用。`run` 的契约两条写在 `State` 的 docstring 里。

只有一个调用者的控制流直接装进那个类的 `run`（`BuildWalls` / `RepairWalls` / `BackToPost` 三个
类里就是原先的 `_build_walls` / `_repair_line` / `_leave_for_the_post`）。仍留在模块级的三个
助手各有第二个调用者：`_sell_ore`（`RaiseForWeapons` + `SellCargo` + `_upgrade_line`）、
`_mine_spare_ore`（`RaiseForWeapons` + `MineSpareOre`）、`_upgrade_line`（`UpgradeWeapons` +
`planner._intents` 的开拓者空闲支）。

两个距离口径别混用：回合预算（来不来得及来回）一律用 BFS 真实步数（`steps_between`，绕障，
-1 = 走不到）；选点/贴着用切比雪夫 `Pos.dist`（`dist <= 1` 是"站在建造位/采集位/炮位旁"的
判据，不是步数）。
"""

import logging
from collections.abc import Callable, Iterator, Mapping, Set
from typing import Any, NamedTuple

from ..agent import AGENT  # 价格期望表：闲矿排序要读它（跨回合状态，退化 = 偏好偏一天）
from ..protocol import actions  # 指令只能经 Action 产出
from .grid import STEPS, Pos, box_cells, steps_between, wall_cells, weapon_sites
from .map import COPPER, IRON, STONE
from .roles import BaseRole, Pioneer, Worker
from .world import ROUNDS_PER_DAY, Turn, Wall, Weapon

LOGGER = logging.getLogger(__name__)


#: 建一座武器的金币（三种同价）。
WEAPON_COST = 25


#: 围墙的 `name` 与代价：石头×1，从建造者自己的背包扣（不是全队共享），拆了不返还。
WALL = "wall"
WALL_COST = 1


#: 一块石头的完整代价：采 1 回合 + 挪 1 回合 + 建 1 回合。
ROUNDS_PER_STONE = 3


#: 砌完墙后手里多留几块石头（用户口径）：墙夜里被打掉一格，第二天手里有货就能立刻补上。
#: 只抬高 `_stones_to_mine` 的上限，不额外开"回合够不够多"的开关 —— 回合少时那个上限本来
#: 就被回合预算压到很小。卖矿时同样按它留底（`_best_load`），两块口径同源。
STONE_RESERVE = 3


#: 筹资路上（建武器 / 买券）卖石头只留 1 块（用户口径）：那两条路缺的是几十金币，留够封口
#: 的量就行，其余全换成钱；平常的砌墙线留 `STONE_RESERVE`。
STONE_KEEP_RAISING = 1


#: 容错余量（回合）：距离全按 BFS 真实步数算，这是给收尾动作留的真余量。5 是拍的。
TIME_MARGIN = 5


#: 白天收工闸门的缓冲（回合）：离天黑只剩"回程步数 + 这个数"就动身回炮位。3 是拍的。
POST_MARGIN = 3


#: 能卖给小贩的矿：三种（含多余的石头），挑哪种由 `Turn.vendor_prices` 现算。
#: 石头只在"墙砌完了"那一支里才卖得出去 —— 调用点 `BuildWalls` 已保证。
SELLABLE = (STONE, IRON, COPPER)


#: 升级优先链：`(武器类别, 目标等级)`，先命中先用。**火箭最优先**：+1 枚导弹 = 中心 20 +
#: 周围 8 格溅射 10，多枚叠加，一炮对成簇的机器人翻倍（射程也跟着涨，L3 全图）；加特林次之 ——
#: 无冷却、每回合必开火，+1 颗子弹 = 每回合 +10（射程 +2）。同类出现两次 ⇒ 两座火箭都升得到。
UPGRADE_CHAIN = (
    ("rocket", 2),
    ("rocket", 2),
    ("gatling", 2),
    ("rocket", 3),
    ("rocket", 3),
    ("gatling", 3),
)


#: 升级券的商品名（`weaponShopList.name` 那套词；价目逐回合从载荷读，样例实证 100/150）。
VOUCHER = {2: "WeaponUpgradeVoucher1", 3: "WeaponUpgradeVoucher2"}


#: 围墙升级券：键 = **目标等级**（与 `VOUCHER` 同一口径），样例价目 20 / 30 金。升级同时把墙
#: 回满血（任务书 L297）并把上限抬高一档 ⇒ 弱墙"升级当修"，比只回血的修复包多花 10~20 金。
WALL_VOUCHER = {2: "WallUpgradeVoucher1", 3: "WallUpgradeVoucher2"}


#: 围墙修复包：10 金、目标墙回满血。L3 到顶升不动了，只剩它。
WALLFIXER = "WallFixer"


#: 建筑满血基准：修墙的 1/5 血判据与夜里基地升级的"残血"判据用。基地 1500/3000/4500 是表格
#: 实证；墙 L2/L3 的 1500/2000 按每级 +500 推断（表格被图片截断，待实盘校准）—— 推断偏小的
#: 方向是"晚修"，安全。
WALL_MAX_HP = {1: 1000, 2: 1500, 3: 2000}


#: "顺路卖矿"的绕路上限（格）：去矿的路上，绕去小贩比直走多花不超过这么多步就顺路卖掉。
DETOUR_MAX = 2


class _Move(NamedTuple):
    """第二段待解的走路意图。

    goal 型 = "朝 goal 挪一格"（BFS + 软避让，由 `_walk_out` 解）；provider 型 = 没有目标
    的走法（迈出盒子 / 挪开自己），第二段拿"当时的落子账"现算落脚格。`avoid` = 第一段已知
    的软避让；`with_paths` = 解的时候把已预留的路径也并进软避让；`reserve` = 动身成功后把
    整条路记进预留账（矿 / 卖矿 / 砌墙这些差事要 —— 后解的同事整条让开，防双双停住）；
    `onto` = 停在 goal 自己身上（默认停在贴着它的一格，见 `_Queue.step`）。
    """

    role: BaseRole
    goal: Pos | None
    avoid: frozenset[Pos] = frozenset()
    with_paths: bool = False
    reserve: bool = False
    onto: bool = False
    provider: Callable[[set[Pos]], Pos | None] | None = None


class _Queue:
    """第一段的输出收集器：能直接干的 act 当场落 `cmds`，走路只记成意图（`moves`），路径留给
    第二段统一解。`claimed` 是第一段的决策账 —— 只记 `remove` 的落点（`_rescue` 的"本回合已
    拆过墙"判据靠它）；走路的落子账在第二段（`_walk_out`）里，两本账不混。
    """

    def __init__(self, turn: Turn) -> None:
        self.turn = turn
        self.cmds: dict[str, dict[str, Any]] = {}
        self.moves: list[_Move] = []
        self.claimed: set[Pos] = set()

    def step(
        self,
        role: BaseRole,
        goal: Pos,
        *,
        avoid: Set[Pos] = frozenset(),
        with_paths: bool = False,
        reserve: bool = False,
        onto: bool = False,
    ) -> bool:
        """记一条"role 要走到 goal 去"。False = 硬障碍就走不到（调用方接着试下一个差事）；
        True = 意图已排。

        `onto` = 停在 goal 自己身上（`step_onto`），默认停在贴着它的一格（`step_toward`）——
        差事的 goal 都挡路（矿 / 建筑 / 炮位），只有共用的操作位那种空格才要走上去。
        """
        if steps_between(role.pos, goal, self.turn.map.blocked | self.claimed, self.turn.map.size) < 0:
            return False
        self.moves.append(_Move(role, goal, frozenset(avoid), with_paths, reserve, onto))
        return True

    def beside(self, role: BaseRole, provider: Callable[[set[Pos]], Pos | None]) -> None:
        """记一条没有目标的走法（迈出盒子 / 挪开自己）—— 第二段拿落子账现算。"""
        self.moves.append(_Move(role, None, provider=provider))


def _passable(turn: Turn) -> set[Pos]:
    """估算距离用的地形：`map.blocked` 剔掉我方角色站着的格子。

    与"这一回合实际怎么走"是两套口径（`_Queue.step` / `_walk_out` 那边必须把同事当硬障碍：
    撞上就是执行失败、双双停住）。这里问的是"路有多远"：`model._entries` 把我方角色写进网格，
    而环砌满之后盒子里只剩一格宽的走廊 ⇒ 同事停在走廊上就让估算判成不可达（-1），整条经济线
    跟着静默放弃、工人一整天不动。与 `_stuck_inside` / `_post_spots` 同一个口径：自己人算路过。
    """
    return turn.map.blocked - {r.pos for r in turn.roles}


def _can_fund(turn: Turn) -> bool:
    """筹资可行：地图上有小贩、且有收购价 > 0 的矿 —— 矿挖了卖得掉，才谈得上凑
    建武器的钱。没小贩 / 没价目 ⇒ 筹不成，墙线照旧（没什么更好可干的事）。"""
    if not turn.map.vendors:
        return False
    prices = turn.vendor_prices
    return any(prices.get(kind, 0) > 0 for kind in turn.map.ores.values())


class _Ctx:
    """回合内的决策黑板：白天工人链与夜里经济线共用的那几本账。

    每本账都是同一个对象被各状态原地改（`sites.add` 而不是并集、`budget -= WEAPON_COST`
    而不是重新赋值）—— 后一个状态读到的必须是前一个刚写下的那一份（建武器的落点要立刻进
    走路避让、认领过的矿格不能再来一个人）。逐角色顺序累计，回合一过就没了。
    """

    def __init__(
        self,
        turn: Turn,
        q: _Queue,
        *,
        sites: set[Pos] | None = None,
        ore_taken: set[Pos] | None = None,
        weapon_gap: bool = False,
        leaving: frozenset[str] = frozenset(),
        slots: Iterator[tuple[str, Pos]] | None = None,
    ) -> None:
        self.turn = turn
        self.q = q
        self.sites = set() if sites is None else sites
        self.ore_taken = set() if ore_taken is None else ore_taken
        self.weapon_gap = weapon_gap  # 份额有缺的名额还在（建武器与升级线的开关）
        self.leaving = leaving  # `_trapped`：砌满墙就会被关在盒子里的人
        self.taken: set[Pos] = set()  # 炮位（夜里一人一座；白天只有收工闸门读）
        self.repair_taken: set[Pos] = set()  # 待修墙格认领
        self.budget = turn.gold  # 金币预留：认领一座武器就扣一份，宁可少建不可超支
        self.slots = iter(()) if slots is None else slots  # 待建武器名额（一次性迭代器）
        self.target: Pos | None = None  # 分给本工人的环缺口头一格
        self.remaining = 0  # 那一段还剩几格（回合预算用）


class State:
    """白天工人链上的一个状态：`run` 返回 True = 这一回合归它了（驱动方就此停在这一个）。

    契约两条，写状态的人都要守：

    一是 `False` 必须意味着"我什么都没排、什么都没发"，后一个状态接着往下跑。谁发了指令或
    排了走路意图还返回 False，这个角色就会落两条动作 —— 第二段两条都解，后解的把前一条的
    `cmds[角色]` 盖掉，报文仍然合法、本地全绿，脏账只在日志里。
    二是无状态：只读 `ctx` 与 `turn`，不往自己身上记东西。跨回合的标志位一旦卡住会静默关掉
    整条线（`_fired` 是 planner 里唯一的跨回合账，它不在这条链上）。

    链上顺序就是策略，见 `DAY_CHAIN`（与 `docs/pic/白天工人状态机.png` 的盒子一一对应）。
    """

    def run(self, role: Worker, ctx: _Ctx) -> bool:
        raise NotImplementedError


class BuildWeapons(State):
    """建立武器：份额有缺且钱够 ⇒ 就地建或走向落点。

    `_emit` 被拒 / `q.step` 走不到 ⇒ 返回 False，这一回合落到筹资（不是"建不了就待命"）。
    名额在钱的判据之前就吃掉：`budget` 一回合内只降不升，两种写法今天等价，别顺手调换。
    """

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
    """采矿收集（为武器）：有缺但钱不够、筹资又可行（有小贩有价可卖）⇒ 整条墙线让位（含修墙），
    先卖背包里的货、再采最值钱的矿凑钱。

    与链上别的状态不同：条件成立就无条件认领这一回合（内层卖/采成不成都不再往下走）——
    火力缺口比墙急。石头只留 `STONE_KEEP_RAISING` 块。
    """

    def run(self, role: Worker, ctx: _Ctx) -> bool:
        if not (ctx.weapon_gap and ctx.budget < WEAPON_COST and _can_fund(ctx.turn)):
            return False
        if not _sell_ore(
            role, ctx.turn, ctx.q, ctx.sites, with_paths=True, keep=STONE_KEEP_RAISING
        ):
            _mine_spare_ore(role, ctx.turn, ctx.q, ctx.sites, ctx.ore_taken)
        return True


class BuildWalls(State):
    """建墙（含采矿）：把墙砌到黑板分给本工人的那一格上 —— 缺石头先去采、手上有石头就砌，一回合
    只干其中一件；两件都干不成 ⇒ 返回 False，让驱动方接着走经济线（工人不能什么都不做）。
    `ctx.target` / `ctx.remaining` = 切段分给本工人的那一格与本段还剩几格（A 领前段、B 领后段，
    两人不挤同一段墙）。

    `ctx.target` 是 None（环砌满）⇒ False，让给修墙；有人会被砌满的墙关住（`ctx.leaving`）⇒ True
    而一条指令都不发：待命，别跑远，下回合缺口还在。

    每回合独立判定、不存跨回合状态：矿采没了、墙被别人砌了，下一回合都能自动跟着变。
    """

    def run(self, role: Worker, ctx: _Ctx) -> bool:
        if ctx.target is None:
            return False  # 没分到墙格 ⇒ 驱动方走经济线
        if ctx.leaving:
            return True
        # 只认石矿：墙只吃石头，铁/铜再多也砌不了墙。排除本回合别人认领的矿格（B 就近换一座）。
        # 认领只发生在"真要采"之后（want > 0）：石头已够的工人不该占住矿格 —— 它这回合用不上。
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
            # 还没走到矿边：排一条走路意图，动身成功后由第二段把整条路记进预留账（先预留再走
            # 会让工人绕开自己刚记下的路 —— 可绕时就地多绕三步）。
            elif ctx.q.step(role, mine, avoid=frozenset(ctx.sites), with_paths=True, reserve=True):
                return True

        if role.stone >= WALL_COST:
            # 认领要发生在动身之前：等砌完再登记的话，另一个工人会在同一回合也奔着它去。
            ctx.sites.add(ctx.target)
            # 环上只剩最后一格 ⇒ 从盒子外面砌（用户口径）：站在环里把最后一格盖上，砌墙的人自己
            # 就被封在环里了。盒外一个能站的邻格都没有（被占/出图）⇒ 照旧就近砌（环该封还得封）；
            # 有格子却走不到 ⇒ 这回合不砌，交给别的差事（下一回合它还是环上的最后一格）。
            outside = _outside_spots(ctx.turn, role, ctx.target) if len(_ring(ctx.turn)) == 1 else ()
            if outside:
                # 走到盒旁再砌（在盒内则先往外走一步）。**不避让 `ctx.target`**：环上那个缺口正是出
                # 盒子的近路，避让它就得从后方通道绕整整一圈（实测 12 步）；绕路途中停在缺口上
                # 也没关系 —— 下一回合这一支照样把它送到盒外，不会再对脚下那格砌一次。
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
                # 站在目标格上就先挪开一格、这一回合不砌：人站在墙上时那一格在网格里只剩
                # "worker"（单位铺在最后，墙被盖掉）⇒ 看不出砌过没有，而 `_ring` 的"自己人算
                # 路过"又把它复活成候选 ⇒ 每回合对同一格 `build`，石头白花。挪开一格两个方向
                # 都收敛：砌过的没人站着就现形；没砌的下一回合从邻格稳稳砌上。
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
    """围墙修复：弱墙（见 `_weak_walls`）**升级当修**（用户口径）—— 用哪件东西看墙的等级
    （`_wall_item`）：L1/L2 用围墙升级券（升级同时回满血、上限抬一档），L3 到顶只剩 10 金的修复包。
    取**等级最低**的一面（最便宜、每金币换到的血量最多），同等级取近的（认领在动身之前，两个修墙
    工人不挤同一面）。

    持券 ⇒ 走到那面墙、贴着就 `use`（目标 = 墙坐标，任务书 L292）；没券 ⇒ 商店可达、价目里有它、
    金币够就走去商店 `Buy`。环砌满了才轮得到它（排在 `BuildWalls` 后面）。
    """

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


class SellCargo(State):
    """采矿收集（变现）：背包里有值得卖的东西、这趟够本 ⇒ 卖给小贩。经济兜底的第一级。"""

    def run(self, role: Worker, ctx: _Ctx) -> bool:
        return _sell_ore(role, ctx.turn, ctx.q, ctx.sites, with_paths=True)


class UpgradeWeapons(State):
    """武器升级：买券 → 走到目标武器 → 用券。武器还有缺 ⇒ 整条不跑（用户口径"武器 ok 才升级"）。"""

    def run(self, role: Worker, ctx: _Ctx) -> bool:
        if ctx.weapon_gap:
            return False
        return _upgrade_line(role, ctx.turn, ctx.q, ctx.sites)


class MineSpareOre(State):
    """挖矿赚钱：按"买得动、回得来"筛一遍，再按性价比挑一座闲矿去采。链尾终态（永远认领）。"""

    def run(self, role: Worker, ctx: _Ctx) -> bool:
        _mine_spare_ore(role, ctx.turn, ctx.q, ctx.sites, ctx.ore_taken)
        return True


class BackToPost:
    """回防守位：环砌完了、离夜里的第一波只剩回程步数 ⇒ 回那一组的岗位；这一回合到此为止 ⇒ `True`。

    白天就得动身：机器人在夜里第一个回合就全部出现，而回炮位常常要绕整面围墙、从后方通道进来、
    再横穿盒子 —— 在正面墙外干活时直线三四步、BFS 十几步。目标与夜里 `_defend` 的岗位同一个
    （`_post_spots`）：多座组站到共用的操作位、单座组站在炮旁，天黑时人已经在岗、第一回合就能
    开火。与 `_defend` 的唯一区别是它不调 `_fire`：`attack` 仅黑夜（§4.4），白天发就是非法指令。

    先看时间、再看位置：判据是"到最近那个岗位的 BFS 步数 ≥ 白天还剩的回合 − `POST_MARGIN`"
    （缓冲，拍的）；已经在岗位上时步数是 0，于是只有白天最后那几回合才轮得到"在岗待命"。少了
    时间这一道，"到岗就待命"会让早上正好站在炮边的工人整天不动。

    不放进 `DAY_CHAIN`：它在链之前（补墙优先于收工），且开拓者也要走它，而链上只跑工人。开拓者是
    **补位炮手**（`_pioneer_mans_guns`）：工人够操满所有组时它不占岗位 —— 与 `_defend` 同一个判据，
    两处必须同源：只拦夜里那一处的话，白天照样把它送进岗位（火箭对只有一格岗位），夜里它又不认领
    ⇒ 那格被占着、整个火箭组没人操。

    返回 `True` = 这一回合已由本类处理（走了、或已在岗待命）；`False` = 还来得及干活（或没有可去
    的岗位）。
    """

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
            onto = len(group) > 1  # 多座组的岗位是空地、要站上去（与 `_defend` 同一个口径）
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


#: 经济兜底三级：卖货 →（武器有缺则跳过升级）→ 采闲矿。白天链尾与夜里清场后走同一条
#: （`_economy` 就是这条链的驱动器）—— 一份实现，两个时段不会漂成两套。
ECONOMY_CHAIN: tuple[State, ...] = (SellCargo(), UpgradeWeapons(), MineSpareOre())


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

    唯一用途见 `BuildWalls.run`：站在环里砌最后一格会把自己封在环里。**要站上去**（`onto`）——
    这些格子是盒外的空地，`step_toward` 到不了；`role.pos` 自己那一格保留（已经站在盒外的
    贴格上时直接就能砌）。盒外的邻格全被占 / 出图 ⇒ 空集，调用方这一回合不砌。
    """
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


def _sealed_back(turn: Turn) -> bool:
    """第 3 天起把背面两个角格补上（前两天的环只有 14 格，背面整列敞开）。

    判据只能用回合号：环上"没砌"与"砌了又被拆"在地图上同形（第 1 天的缺口是真的没砌）。
    """
    return turn.round_no > 2 * ROUNDS_PER_DAY


def _ring(turn: Turn) -> tuple[Pos, ...]:
    """按优先级排的围墙格；基地没了 ⇒ 空。

    既不在 `wall_cells` 里、也不挡路的格才算候选 —— 但自己人站的那一格除外。工人站在待砌的
    墙格上只是路过，若把它划掉，另一个工人的 `free[0]` 会整体后移、等那人一挪窝目标又变回来
    —— 两个工人在两格之间对着改目标，一格都砌不上。

    这里只管"哪些格能砌"（几何 + 占用），"这一回合还砌不砌"是 `_trapped` 的事，两者正交。
    """
    station = turn.map.station
    if station is None:
        return ()
    # 我方角色当前站的格（角色能走的都在这）
    mine = {r.pos for r in turn.roles}
    cells = wall_cells(station, turn.map.size[0], sealed=_sealed_back(turn))
    return tuple(c for c in cells if c not in turn.map.blocked or c in mine)


def _weak_walls(turn: Turn) -> tuple[Wall, ...]:
    """血量不到满血 **1/5** 的已砌墙 —— "墙不完备"的判据，按坐标序（可复现）。

    满血基准按等级查 `WALL_MAX_HP`（升级后回满血）。health 缺失（-1）⇒ 未知 ⇒ 不算弱；已毁（0）
    的墙在 `model._walls` 就丢了 —— 那是一格缺口，归 `_ring` 管重建。
    """
    return tuple(
        w
        for w in sorted(turn.walls, key=lambda w: w.pos)
        if 0 < w.health and w.health * 5 < WALL_MAX_HP.get(w.level, WALL_MAX_HP[1])
    )


def _wall_item(level: int) -> str:
    """修这面墙该用哪件东西：**升级当修**（用户口径）—— L1/L2 用对应等级的围墙升级券，
    L3 到顶升不动了，只剩 10 金的修复包。
    """
    return WALL_VOUCHER.get(level + 1, WALLFIXER)


def _stones_to_mine(role: Worker, turn: Turn, target: Pos, mine: Pos | None, free: int) -> int:
    """这一趟还该采几块石头 —— 按回合预算每回合现算。

    记 `s` = 手里的石头、`k` = 还要采的块数：

        走到矿 + 采 k 块 + 从矿走回工地 + 砌 s+k 座的"挪一格 + 建造"
        = d_mine + d_wall + 2s − 1 + 3k        # 到达工地那一步已贴着首格
                                            # ⇒ 每多采一块净花 3 回合

    令它 ≤ `白天还剩的回合 − TIME_MARGIN` 解出 k，再与"还差几格墙 **+ `STONE_RESERVE`**"取小
    —— 多出来的几块是砌完墙后的存货（没有转移物品的指令，多采的石头给不了别人，只能自己拿着，
    墙被打掉一格时立刻补得上）。距离一律用 BFS 真实步数：回工地常要绕整面围墙、从后方通道
    进来，切比雪夫会把 10+ 步说成 3 步。-1（走不到）⇒ 一块都别采（宁可这回合不动）。
    """
    if mine is None:
        return 0
    walk, size = _passable(turn), turn.map.size
    to_mine = steps_between(role.pos, mine, walk, size)
    to_wall = steps_between(mine, target, walk, size)
    if to_mine < 0 or to_wall < 0:
        return 0
    budget = turn.day_rounds_left - TIME_MARGIN - to_mine - to_wall - 2 * role.stone + 1
    return max(0, min(budget // ROUNDS_PER_STONE, free + STONE_RESERVE - role.stone))


def _sell_ore(
    role: BaseRole,
    turn: Turn,
    q: _Queue,
    sites: set[Pos],
    *,
    with_paths: bool = False,
    urgent: bool = False,
    keep: int = STONE_RESERVE,
) -> bool:
    """把小贩肯收的矿背过去换金币。这一回合没去卖就返回 `False`（调用方接着去采）。

    卖的可用角色是全部（§4.4）—— 工人（墙砌完后）与开拓者（任务点全空时）都走这里。四条门，
    任一条不成立就 `False`：

    ① 有货：`_best_load` 从 `SELLABLE` 里挑收购价最高的一种；`keep` = 石头留底块数（那两条
       筹资路传 `STONE_KEEP_RAISING`，其余走默认）—— 判"卖得起"的调用方必须传同一个值；
    ② 有小贩且走得到（`Map.vendors` 空、或一个都走不到就无处可卖）；
    ③ 够本：已经不贴着才算 —— 货值 < 往返回合数（`2 × 步数`）就留在矿边接着采（贴着时这趟路
       早付过了）；"1 金币 ≈ 1 回合"是拍的，唯一的调参旋钮。`urgent=True` 跳过这一条：那趟路
       的回报不是矿价（是买券的钱，见 `_upgrade_line`）；
    ④ 回得来：`走到小贩 + 从小贩回基地 ≤ 本回合起这一天还剩的回合 − TIME_MARGIN`
       （`Turn.rounds_left`，夜里同样成立 —— 夜里必须站回炮位）—— 这条 `urgent` 也不跳：
       赶不回来就是白丢货。

    距离一律 BFS 真实步数（小贩常在盒子外，回基地要绕后方通道）；-1 一律当"这趟不去"。站位
    是 `sell` 要求的"小贩周围一格内"，与小贩格本身挡路正好对上。一回合只能发一条指令 ⇒ 一次
    只卖一种矿，`num` = 手上那种的全部件数（卖光）。
    """
    station = turn.map.station
    kind, num = _best_load(role, turn.vendor_prices, keep=keep)
    if not kind or station is None or not turn.map.vendors:
        return False
    walk, size = _passable(turn), turn.map.size
    # 并列按坐标排：先后不能取决于 payload 里的顺序。走不到的小贩直接剔掉（BFS -1）。
    hops = [(steps_between(role.pos, p, walk, size), p) for p in turn.map.vendors]
    hops = [(steps, pos) for steps, pos in hops if steps >= 0]
    if not hops:
        return False
    to_vendor, vendor = min(hops)
    if to_vendor == 0:
        # 0 = 已经贴着小贩（`steps_between` 的口径）。1 是"差一格"，那时候还不许卖
        return _emit(q.cmds, role, actions.Sell, kind, num)
    value = turn.vendor_prices.get(kind, 0) * num
    if not urgent and value < 2 * to_vendor:
        return False  # ③ 为这一堆货走这么远不划算，接着采
    back = steps_between(vendor, station, walk, size)
    if back < 0 or to_vendor + back > turn.rounds_left - TIME_MARGIN:
        return False  # ④ 去了就赶不回来
    # 差事差事之间要互相让路（with_paths：动身成功后整条路进预留账）
    return q.step(role, vendor, avoid=frozenset(sites), with_paths=with_paths, reserve=with_paths)


def _best_load(
    role: Worker, prices: Mapping[str, int], *, keep: int = STONE_RESERVE
) -> tuple[str, int]:
    """挑这一趟卖哪种矿：收购价最高的，同价取件数多的；挑不出来 ⇒ `("", 0)`。

    价 ≤ 0 或件数为 0 的矿跳过（小贩不收的矿换不来金币）；价目表为空 ⇒ 一件都不卖。名字参与
    比较只是为了让并列可复现。

    石头留底 `keep` 块不卖，另外两种矿照卖：默认 `STONE_RESERVE` —— 与 `_stones_to_mine` 的
    存货上限同源，墙夜里被打掉一格、第二天手里有货就能立刻补上；筹资那两条路传
    `STONE_KEEP_RAISING`。手里不到 `keep` 块 ⇒ 这一趟不卖石头（别的照卖）；一块都没有 ⇒
    挑不出来。墙只吃石头，留下的这几块正是它要的。
    """
    loads = [
        (prices.get(kind, 0), role.bag.get(kind, 0) - (keep if kind == STONE else 0), kind)
        for kind in SELLABLE
        if prices.get(kind, 0) > 0 and role.bag.get(kind, 0) > (keep if kind == STONE else 0)
    ]
    if not loads:
        return "", 0
    _, num, kind = max(loads)
    return kind, num


def _economy(
    role: Worker,
    turn: Turn,
    q: _Queue,
    sites: set[Pos],
    ore_taken: set[Pos],
    *,
    weapon_gap: bool,
) -> None:
    """经济线兜底：卖矿 →（武器有缺则跳过升级）→ 采闲矿。白天最后一级与夜里清场后共用一套
    （就是 `ECONOMY_CHAIN` 那三个状态）。

    不返回布尔：三级都是"能发就发"，一个都不成立时自然不落指令（合法空指令）。夜里也走这里
    —— 时间预算由 `Turn.rounds_left` 兜着，走远了回不了炮位的活四道门自己会拦。
    """
    ctx = _Ctx(turn, q, sites=sites, ore_taken=ore_taken, weapon_gap=weapon_gap)
    for state in ECONOMY_CHAIN:
        if state.run(role, ctx):
            break


def _mine_spare_ore(
    role: Worker,
    turn: Turn,
    q: _Queue,
    sites: set[Pos],
    ore_taken: set[Pos] | None = None,
) -> None:
    """墙砌完了 ⇒ 不再闲着：就近采买得动、回得来的矿。

    先把"走得动、回得来"的矿筛出来、再按性价比挑：`价 × 新闻修正 ÷ (到矿 + 采一块 + 回炮位)`，
    单位回合价值最高 —— 近的贱矿可能跑赢远的贵矿。回程参照 = 最近的武器位（没有武器才用基地）。
    新闻修正是 `AGENT.price_hint`（跨回合状态，退化 = 偏好偏一天）。先看 `_detour_buy`（修墙
    缺包 / 升级缺券），再 `_detour_sell`。距离一律 BFS 真实步数；-1（走不到）的矿直接作废；
    白天与夜里共用"这一趟赶不赶得回来"这一条（`Turn.rounds_left`），夜里没有额外的紧半径。
    """
    ore_taken = set() if ore_taken is None else ore_taken
    station = turn.map.station
    posts = [w.pos for w in turn.weapons] or ([station] if station else [])
    budget = turn.rounds_left - TIME_MARGIN
    walk, size = _passable(turn), turn.map.size
    # 先筛可行（走过去 + 回得来），再按性价比挑。BFS 是"命中目标即停"的，开销跟距离相关、
    # 不是整张图 —— 最坏 12 矿 × (1 + 3 炮) 次，实测每回合几十毫秒。
    feasible: dict[Pos, tuple[str, int, int]] = {}
    for p, kind in turn.map.ores.items():
        if p in ore_taken:
            continue  # 本回合已被人认领
        out = steps_between(role.pos, p, walk, size)
        return_home = [steps_between(post, p, walk, size) for post in posts]
        back = min((d for d in return_home if d >= 0), default=-1)
        if out < 0 or back < 0:
            continue
        if out + back > budget:
            continue  # 这一趟赶不回来
        feasible[p] = (kind, out, back)
    best: tuple[float, int, Pos] | None = None
    for p, (kind, out, back) in feasible.items():
        value = turn.vendor_prices.get(kind, 0) * AGENT.price_hint(kind)
        if value <= 0:
            continue  # 小贩不收的矿不为它多走一步
        key = (-(value / (out + back + 1)), out, p)
        if best is None or key < best:
            best = key
    mine = best[2] if best else None
    if station is None or mine is None:
        return
    ore_taken.add(mine)
    if _detour_buy(role, mine, turn, q):
        return
    if _detour_sell(role, turn, q, mine):
        return
    if role.pos.dist(mine) <= 1:
        _emit(q.cmds, role, actions.Collect, mine)
        return
    if q.step(role, mine, avoid=frozenset(sites), with_paths=True, reserve=True):
        return  # 动身成功后整条路进预留账（第二段干）


def _shopping_list(role: Worker, turn: Turn) -> str | None:
    """这一趟该顺路买什么 ⇒ 商品名；什么都不缺 ⇒ `None`。

    只在买得起时才列：钱不够绕过去也白绕。优先级：修墙要的东西（有弱墙且包里没有它 —— 与
    `RepairWalls` 同一条挑墙口径，买错件就白绕一趟）> 升级链下一张券（别人包里已有一张就不再买
    —— 没有转移指令，囤两张是白花金币）。围墙券**不查别人拿没拿**：它会消耗掉，墙还会再坏。
    """
    prices = turn.shop_prices
    weak = _weak_walls(turn)
    if weak:
        item = _wall_item(min(weak, key=lambda w: (w.level, w.pos)).level)
        if item not in role.bag and 0 < prices.get(item, 0) <= turn.gold:
            return item
    target = _upgrade_target(turn)
    if target is not None:
        voucher = target[1]
        held = any(voucher in r.bag for r in turn.roles)
        if not held and voucher not in role.bag and 0 < prices.get(voucher, 0) <= turn.gold:
            return voucher
    return None


def _detour_buy(
    role: Worker, goal: Pos, turn: Turn, q: _Queue
) -> bool:
    """去差事的路上顺路买：绕 2 格内有商店、且购物单上有东西 ⇒ 先朝商店迈一步。

    贴上商店的那回合 `Buy`（下一回合走 `use` / 升级线），之后再继续去差事。绕路账与
    `_detour_sell` 同一套（`via + after - direct ≤ DETOUR_MAX`）；-1（走不到）的绕法直接放弃。
    已经贴着差事目标就别绕了（这一回合该干活）。
    """
    want = _shopping_list(role, turn)
    if want is None or role.pos.dist(goal) <= 1:
        return False
    walk, size = _passable(turn), turn.map.size
    direct = steps_between(role.pos, goal, walk, size)
    if direct < 0:
        return False
    for shop in sorted(turn.map.shops):
        via = steps_between(role.pos, shop, walk, size)
        if via == 0:
            return False  # 已经贴着商店：绕一步 = 原地不动 = 这一回合空指令
        after = steps_between(shop, goal, walk, size)
        if via < 0 or after < 0:
            continue
        if via + after - direct <= DETOUR_MAX:
            return q.step(role, shop)
    return False


def _detour_sell(
    role: BaseRole, turn: Turn, q: _Queue, mine: Pos
) -> bool:
    """去矿的路上顺路卖矿：绕去小贩比直走多花 ≤ `DETOUR_MAX` 格 ⇒ 先朝小贩迈一步。

    贴上小贩的那一回合同一条链里更早的 `_sell_ore` 自然把货出手（贴着跳过够本门），卖完没货、
    绕路条件消失，下一回合继续去矿。已经贴着矿就别绕了（这一回合该采）。

    "有没有可卖的货"用 `_best_load` 判、不在这里重抄一遍：那边有"石头留底 `STONE_RESERVE`
    块"的口径，抄一遍就会出现"绕去卖那几块石头、到了却不肯卖"—— 白绕一趟。距离一律 BFS
    真实步数；-1 的绕法直接放弃。
    """
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
            return False  # 已经贴着小贩：绕一步 = 原地不动 = 这一回合空指令
        after = steps_between(vendor, mine, walk, size)
        if via < 0 or after < 0:
            continue
        if via + after - direct <= DETOUR_MAX:
            return q.step(role, vendor)
    return False


def _upgrade_line(
    role: BaseRole, turn: Turn, q: _Queue, sites: set[Pos]
) -> bool:
    """墙砌完后的第二优先：买券 → 走到目标武器 → 用券。这一回合没发指令就返回 `False`。

    无状态：拿没拿券看背包（买完金变少、包里多一张，两个阶段天然可分，跨夜不丢）。跑腿者 =
    持券的那个角色（券在谁包里谁用 —— 没有转移物品的指令；开拓者也能持券）> 真空闲的开拓者
    > 名册上第一个工人。白天跑；夜里只有"清场后"那一支会走到这里（`_economy`），预算按
    `Turn.rounds_left` 算 —— 一轮跑不完就待命，天亮接着走。
    距离一律 BFS 真实步数：商店/炮位在盒子内外两侧，整趟要绕门；-1 一律这一轮不去。

    钱不够买券时，若"现钱 + 背包里最好那一堆货"够 ⇒ 先去卖矿（`_sell_ore(urgent=True)`）。
    用最好那一堆而不是背包总值：一回合只能发一条 `sell`，卖掉哪一种就到账哪一种 —— 拿总值
    凑够、到了却发现这一趟只够卖出一半，等于把路白走一遍。
    """
    workers = [r for r in turn.roles if isinstance(r, Worker)]
    holder = next(
        (r for r in turn.roles if any(name in r.bag for name in VOUCHER.values())), None
    )
    # 真空闲 = 无可接任务 —— 有任务在的话开拓者马上会被钉死，跑腿该让给工人
    idle = (
        None if turn.task_points else next((r for r in turn.roles if isinstance(r, Pioneer)), None)
    )
    runner = holder or idle or (workers[0] if workers else None)
    if runner is None or runner is not role:
        return False  # 跑腿的是别人；持券者不在场（比如夜里阵亡）⇒ 券先躺在包里
    target = _upgrade_target(turn)
    if target is None:
        return False
    weapon, voucher = target
    budget = turn.rounds_left - TIME_MARGIN
    walk, size = _passable(turn), turn.map.size
    if voucher in role.bag:
        # 持券阶段：终点就是炮位，用完正好站岗 —— 不用留回程
        if role.pos.dist(weapon.pos) <= 1:
            return _emit(q.cmds, role, actions.Use, voucher, weapon.pos)
        to_weapon = steps_between(role.pos, weapon.pos, walk, size)
        if to_weapon < 0 or to_weapon + 1 > budget:
            return False  # 走不到 / 今天来不及 ⇒ 待命，明天接着走
        return q.step(role, weapon.pos)
    # 买券阶段：整趟 = 走到商店 + 买到武器 + 买/用两个动作回合
    price = turn.shop_prices.get(voucher, 0)
    if price <= 0:
        return False
    if turn.gold < price:
        # 钱不够但卖掉背包里最好那一堆就够 ⇒ 先去卖（那趟路的回报是券，不是矿价 ⇒ `urgent`）。
        # 石头留 `STONE_KEEP_RAISING` 块 —— 判"够不够"与真卖必须同一个 `keep`，否则这边按
        # 留 3 块算出"不够"、那边却肯卖到只剩 1 块，白跑一趟。
        kind, num = _best_load(role, turn.vendor_prices, keep=STONE_KEEP_RAISING)
        if not kind or turn.gold + turn.vendor_prices.get(kind, 0) * num < price:
            return False
        return _sell_ore(role, turn, q, sites, with_paths=True, urgent=True, keep=STONE_KEEP_RAISING)
    # 并列按坐标排：先后不能取决于 payload 里的顺序。走不到的商店直接剔掉（BFS -1）。
    hops = [(steps_between(role.pos, s, walk, size), s) for s in turn.map.shops]
    hops = [(steps, pos) for steps, pos in hops if steps >= 0]
    if not hops:
        return False  # 没有商店、或者一个都走不到
    to_shop, shop = min(hops)
    if to_shop == 0:
        # 0 = 已经贴着商店（`steps_between` 的口径）。1 是"差一格"，那时候还不许买
        return _emit(q.cmds, role, actions.Buy, voucher, 1)
    to_weapon = steps_between(shop, weapon.pos, walk, size)
    if to_weapon < 0 or to_shop + to_weapon + 2 > budget:
        return False
    return q.step(role, shop)


def _upgrade_target(turn: Turn) -> tuple[Weapon, str] | None:
    """优先链上第一座还升得动的武器 + 该买的那张券名。全升满 ⇒ `None`。

    按 `(类别, 目标等级)` 沿 `UPGRADE_CHAIN` 找；同类多座按 id 排（并列不能取决于
    payload 顺序）。券只认"从几级升"（V1 = 任何 L1 武器），不绑定类别。
    """
    by_id = sorted(turn.weapons, key=lambda w: w.id)
    for kind, want in UPGRADE_CHAIN:
        for weapon in by_id:
            if weapon.kind == kind and weapon.level == want - 1:
                return weapon, VOUCHER[want]
    return None


def _pick_ore(
    pos: Pos, ores: Mapping[Pos, str], prices: Mapping[str, int], *, want_stone: bool
) -> Pos | None:
    """挑一座矿。两种口径：

    - `want_stone`（石头还不够砌完剩下的墙）⇒ 只认石矿、取最近的（铁/铜砌不了墙）；
    - 否则 ⇒ 三种矿按小贩收购价从高到低，同价再取近的。价目表里没有的矿种按 0 算 ⇒ 小贩不收
      的矿不值得为它多走一步（空价目表也就自然落成"谁也不采"）。

    价格逐回合从载荷读、不写死"铜 > 铁 > 石头"。并列按坐标排：先后不能取决于 payload 顺序。
    不认领矿：两个工人挤同一座矿的不同邻格都能采，只有"冲进同一格"才是白扔动作（`claimed` 挡着）。
    """
    if want_stone:
        return min(
            (p for p, kind in ores.items() if kind == STONE),
            key=lambda p: (pos.dist(p), p),
            default=None,
        )
    # `(取负的价, 距离, 坐标)` —— 价排第一，`min` 出来就是"价最高、同价取近的、再同取坐标最小"
    best = min(
        ((-prices.get(kind, 0), pos.dist(p), p) for p, kind in ores.items()),
        default=None,
    )
    # 价 0 ⇒ 小贩不收，不为它多走一步；一张空价目表也就自然落成"谁也不采"
    return best[2] if best is not None and best[0] < 0 else None


def _pioneer_mans_guns(turn: Turn) -> bool:
    """开拓者这一轮该不该上炮位：**只有工人不够覆盖全部武器组时才补位**（用户口径）。

    工人夜里除了操炮没别的活（经济线只在场上没有活机器人时才跑），而开拓者是任务线的主力 ——
    两个工人活着就能操满两组，让开拓者占一组等于把工人挤成闲置。工人阵亡（只可能在夜里）后
    人手不够了，它才补位。昼夜同一个判据：白天用它决定收工回不回到炮位（`BackToPost`），
    夜里用它决定认不认领武器（`_defend`）—— 两处必须同源，只改一处的话白天把人送进岗位、
    夜里又不认领，那格被占着、整组没人操（火箭对只有一格岗位）。

    名册里只有工人与开拓者两种角色 ⇒ "工人数"就是"没被钉住的角色数"，`_short_handed` 取用它。
    """
    workers = sum(1 for r in turn.roles if isinstance(r, Worker))
    return workers < len(_weapon_groups(turn))


def _weapon_groups(turn: Turn) -> tuple[tuple[Weapon, ...], ...]:
    """把武器分成操作组：同一组的武器由同一个角色操作。

    当前阵形 = 2 火箭（相邻）+ 1 加特林 ⇒ 两组：`(rocket1, rocket2)` 和 `(gatling,)`。分组依据
    是 `weapon_sites` 的下标（0,1 = 火箭对；2 = 加特林），不按场上已有武器的种类猜（种类重复
    时猜不准）。某座还没建出来 ⇒ 那一组就只含已建的。
    """
    station = turn.map.station
    sites = weapon_sites(station, turn.map.size[0]) if station is not None else ()
    by_pos = {w.pos: w for w in turn.weapons}
    grouped: set[Pos] = set()
    groups: list[tuple[Weapon, ...]] = []
    for indices in ((0, 1), (2,)):
        group = tuple(by_pos[sites[i]] for i in indices if i < len(sites) and sites[i] in by_pos)
        if group:
            groups.append(group)
            grouped.update(w.pos for w in group)
    # 不在 `weapon_sites` 里的武器（测试手搭的位置 / 摧毁后重建的偏移）⇒ 单独成组，降级为
    # "一人操一座"，避免测试里手搭的炮没人认领。
    for w in turn.weapons:
        if w.pos not in grouped:
            groups.append((w,))
    return tuple(groups)


def _operator_spots(
    group: tuple[Weapon, ...], blocked: set[Pos], size: tuple[int, int]
) -> list[Pos]:
    """一组武器的操作站位：贴着组内所有武器的格子（去障碍）。

    单座组 ⇒ 返回那座本身（`step_toward` 会停在邻格）；多座组 ⇒ 所有武器 8 邻域的
    交集里走得通的格子（**要站上去** —— `step_onto` 就是为它加的）。调用者一律是
    `_post_spots`：`blocked` 要预先剔掉自己人，还得再排除别人占着的岗位。
    """
    if len(group) == 1:
        return [group[0].pos]
    common: set[Pos] | None = None
    for w in group:
        nbrs = {Pos(w.pos.x + d.x, w.pos.y + d.y) for d in STEPS}
        common = nbrs if common is None else common & nbrs
    if common is None:
        return []
    width, height = size
    return sorted(c for c in common if c not in blocked and 0 <= c.x < width and 0 <= c.y < height)


def _near_spots(
    group: tuple[Weapon, ...], blocked: set[Pos], size: tuple[int, int]
) -> list[Pos]:
    """贴着组内**任意一座**的格子（去障碍、去武器自己那几格）：共用操作位没了时的退路。

    那一格只打得了其中一座，但总比整组无人可打强 —— 共用操作位被机器人踩死、或者进不去
    那条一格宽的走廊时，整组会被跳过 ⇒ 两个火箭一发不打的死锁。
    """
    width, height = size
    cells = {Pos(w.pos.x + d.x, w.pos.y + d.y) for w in group for d in STEPS}
    cells -= {w.pos for w in group}
    return sorted(c for c in cells if c not in blocked and 0 <= c.x < width and 0 <= c.y < height)


def _post_spots(group: tuple[Weapon, ...], turn: Turn, role: BaseRole) -> list[Pos]:
    """这一组这一回合能用的岗位：贴着组内每一座、又没被别人占着的格子。

    自己人一律从障碍里剔掉（`model._entries` 把我方角色写进网格 ⇒ 整份 `blocked` 里混着
    队友和**自己**，不放行的话站在岗位上的操作者看不见自己的岗位）；但别人**站着**的岗位
    要排除 —— 那格被占了就得换一组去（否则一个走不进去、一个干等着，两人一起卡住）。
    自己那格保留：站在岗位上的人得认得出自己的岗位。
    """
    cells = {r.pos for r in turn.roles}
    spots = _operator_spots(group, _passable(turn), turn.map.size)
    others = cells - {role.pos}
    return [s for s in spots if s not in others]


def _steps_to_post(pos: Pos, spot: Pos, onto: bool, blocked: Set[Pos], size: tuple[int, int]) -> int:
    """到岗位的 BFS 步数（走不到 -1）。口径与 `_Queue.step` 一致。

    多座组的岗位是空地、要站上去 ⇒ 比"贴着它"多一步；单座组的岗位就是那座炮本身 ⇒
    `steps_between` 的口径就是答案。已经在岗位上 ⇒ 0。
    """
    if pos == spot:
        return 0
    steps = steps_between(pos, spot, blocked, size)
    return steps + 1 if steps >= 0 and onto else steps


# ── 发指令 ──────────────────────────────────────────────────────────


def _aside_cell(
    role: BaseRole, turn: Turn, claimed: set[Pos], avoid: Set[Pos] = frozenset()
) -> Pos | None:
    """挪开自己：从脚下这一格挑一个可走的邻格；八面都走不了 ⇒ None。

    没有"目标"的走法（唯一用途见 `BuildWalls.run` 站在待砌墙格上的那一支），所以不走
    `step_toward`——它的契约是"贴着 goal 即到"，对任意邻格都返回 None。挪到建造格上等于换个
    格子接着站，所以 `avoid`（建造格）照避；`claimed`（别人已落的脚格）由第二段传进来。
    方向顺序无所谓：任何一个可走的邻格都等价（挪开一步就够了）。
    """
    walk = turn.map.blocked | claimed | avoid
    width, height = turn.map.size
    for d in STEPS:
        cell = Pos(role.pos.x + d.x, role.pos.y + d.y)
        if cell not in walk and 0 <= cell.x < width and 0 <= cell.y < height:
            return cell
    return None


def _emit(
    cmds: dict[str, dict[str, Any]],
    role: BaseRole,
    cls: type[actions.BaseAction],
    *args: Any,
    key: str | None = None,
) -> bool:
    """造一条指令放进 `cmds`，成功返回 True。

    默认挂在 `str(role.id)` 下，只有 `attack` 例外（key 是武器 id，操控者在 `controllerId` 里）
    ⇒ 开一个 keyword-only 的口子，只有 `_fire` 那一处传 `key`。越权（`PermissionError`）只丢
    这一条并告警，不连坐同回合其他角色 —— 抛出去会变成"每回合空指令 ⇒ 全队冻结一整局"。
    """
    try:
        cmds[key if key is not None else str(role.id)] = cls(role.type_name, *args).to_wire()
    except PermissionError as exc:
        LOGGER.warning("拦下越权动作：角色 %s(%s) %s", role.id, role.type_name, exc)
        return False
    return True
