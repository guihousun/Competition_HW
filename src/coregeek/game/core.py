"""共用底座：白天与夜里都要用的那几本账、那几条线。

四样东西：常量（一张共用旋钮表）、走路账与指令出口（`_Move` / `_Queue` / `_emit`）、
回合内黑板 `_Ctx`，以及经济线与岗位几何。**上层是 `day` / `night` / `planner`**（单向
import）—— 本模块绝不 import 它们（那就是循环导入）。

"两个模块都要问"就是进这里的门槛：只有一个消费者的判据留在各自的模块里（砌墙/修墙在
`day`，操炮与弹道在 `night`）。岗位几何是那个样板（白天收工闸门与夜里操炮共用
`_post_spots`）。⚠️ 经济线现在**只有白天问**（夜里清场后走 `night.mine_ore`，另一条线）——
按门槛它该搬去 `day.py`，本步没搬（记在 `code-task.md` 第 119 步）。

两个距离口径别混：回合预算一律用 BFS 真实步数（`steps_between`，-1 = 走不到）；选点/贴着
用切比雪夫 `Pos.dist`（`dist <= 1` 是"站在建造位/采集位/炮位旁"的判据，不是步数）。
"""

import logging
from collections.abc import Callable, Iterator, Mapping, Set
from typing import Any, NamedTuple

from ..agent import AGENT  # 价格期望表：闲矿排序要读它（跨回合状态，退化 = 偏好偏一天）
from ..protocol import actions  # 指令只能经 Action 产出
from .grid import STEPS, Pos
from .map import COPPER, IRON, STONE
from .path import steps_between
from .roles import BaseRole, Pioneer, Worker
from .utils import _passable
from .world import Turn, Wall, Weapon

LOGGER = logging.getLogger(__name__)


#: 建一座武器的金币（三种同价）。
WEAPON_COST = 25


#: 围墙的 `name` 与代价：石头×1，从建造者自己的背包扣（不是全队共享），拆了不返还。
WALL = "wall"
WALL_COST = 1


#: 一块石头的完整代价：采 1 回合 + 挪 1 回合 + 建 1 回合。
ROUNDS_PER_STONE = 3


#: 砌完墙后手里多留几块石头：墙夜里被打掉一格，第二天手里有货就能立刻补上。
#: 只抬高 `day._stones_to_mine` 的上限；卖矿时同样按它留底（`_best_load`），两块口径同源。
STONE_RESERVE = 3


#: 筹资路上（建武器 / 买券）卖石头只留 1 块：那两条路缺的是几十金币，留够封口的量就行。
STONE_KEEP_RAISING = 1


#: 容错余量（回合）：距离全按 BFS 真实步数算，这是给收尾动作留的真余量。5 是拍的。
TIME_MARGIN = 5


#: 白天收工闸门的缓冲（回合）：离天黑只剩"回程步数 + 这个数"就动身回炮位。3 是拍的。
POST_MARGIN = 3


#: 能卖给小贩的矿：三种（含多余的石头），挑哪种由 `Turn.vendor_prices` 现算。
#: 石头只在"墙砌完了"那一支里才卖得出去 —— 调用点 `day.BuildWalls` 已保证。
SELLABLE = (STONE, IRON, COPPER)


#: 升级优先链：`(武器类别, 目标等级)`，先命中先用。火箭最优先（+1 枚导弹 = 中心 20 + 溅射
#: 10 叠加，对成簇的机器人翻倍）；加特林次之（无冷却、每回合 +10）。同类出现两次 ⇒ 两座
#: 火箭都升得到。
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


#: 围墙升级券：键 = 目标等级（与 `VOUCHER` 同一口径）。升级同时把墙回满血并把上限抬高一档
#: ⇒ 弱墙"升级当修"。
WALL_VOUCHER = {2: "WallUpgradeVoucher1", 3: "WallUpgradeVoucher2"}


#: 围墙修复包：10 金、目标墙回满血。L3 到顶升不动了，只剩它。
WALLFIXER = "WallFixer"


#: 建筑满血基准。基地 1500/3000/4500 是表格实证；墙 L2/L3 的 1500/2000 按每级 +500 推断
#: （待实盘校准）—— 推断偏小的方向是"晚修"，安全。
WALL_MAX_HP = {1: 1000, 2: 1500, 3: 2000}


#: "顺路卖矿"的绕路上限（格）：去矿的路上，绕去小贩比直走多花不超过这么多步就顺路卖掉。
DETOUR_MAX = 2


class _Move(NamedTuple):
    """第二段待解的走路意图。

    goal 型 = "朝 goal 挪一格"（BFS + 软避让）；provider 型 = 没有目标的走法（迈出盒子 /
    挪开自己），第二段拿"当时的落子账"现算落脚格。`avoid` = 第一段已知的软避让；
    `with_paths` = 解的时候把已预留的路径也并进软避让；`reserve` = 动身成功后把整条路记进
    预留账（防同事双双停住）；`onto` = 停在 goal 自己身上（默认停在贴着它的一格）。
    """

    role: BaseRole
    goal: Pos | None
    avoid: frozenset[Pos] = frozenset()
    with_paths: bool = False
    reserve: bool = False
    onto: bool = False
    provider: Callable[[set[Pos]], Pos | None] | None = None


class _Queue:
    """第一段的输出收集器：能直接干的 act 当场落 `cmds`，走路只记成意图（`moves`）。
    `claimed` 只记 `remove` 的落点（`day.rescue` 的"本回合已拆过墙"判据靠它）。"""

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
        """记一条"role 要走到 goal 去"。False = 硬障碍就走不到（调用方接着试下一个差事）。

        `onto` = 停在 goal 自己身上（`step_onto`），默认停在贴着它的一格（`step_toward`）——
        差事的 goal 都挡路，只有共用的操作位那种空格才要走上去。
        """
        if steps_between(role.pos, goal, self.turn.map.blocked | self.claimed, self.turn.map.size) < 0:
            return False
        self.moves.append(_Move(role, goal, frozenset(avoid), with_paths, reserve, onto))
        return True

    def beside(self, role: BaseRole, provider: Callable[[set[Pos]], Pos | None]) -> None:
        """记一条没有目标的走法（迈出盒子 / 挪开自己）—— 第二段拿落子账现算。"""
        self.moves.append(_Move(role, None, provider=provider))


class _Ctx:
    """回合内的决策黑板：白天工人链与夜里经济线共用的那几本账。

    每本账都是同一个对象被各状态原地改 —— 后一个状态读到的必须是前一个刚写下的那一份。
    逐角色顺序累计，回合一过就没了。"""

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
    """白天工人链（`day.DAY_CHAIN`）与经济链（`ECONOMY_CHAIN`）上的一个状态：`run` 返回 True = 这一回合归它了（驱动方就此停在这一个）。

    契约两条：一是 `False` 必须意味着"我什么都没排、什么都没发"—— 谁发了指令还返回 False，
    这个角色会落两条动作，后解的把前一条的 `cmds[角色]` 盖掉。二是无状态：只读 `ctx` 与
    `turn`，不往自己身上记东西（跨回合的标志位一旦卡住会静默关掉整条线）。

    链上顺序就是策略。"""

    def run(self, role: Worker, ctx: _Ctx) -> bool:
        raise NotImplementedError


# ── 经济线：白天链尾与夜里清场后共用同一份实现 ──────────────────────


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


#: 经济兜底三级：卖货 →（武器有缺则跳过升级）→ 采闲矿。**白天工人链的尾巴**
#: （`day.DAY_CHAIN` 末尾接的就是这三条）。夜里清场后走的是 `night.mine_ore`，另一条线。
ECONOMY_CHAIN: tuple[State, ...] = (SellCargo(), UpgradeWeapons(), MineSpareOre())


def _weak_walls(turn: Turn) -> tuple[Wall, ...]:
    """血量不到满血 1/5 的已砌墙，按坐标序（可复现）。health 缺失（-1）⇒ 未知 ⇒ 不算弱；
    已毁（0）的墙在 `model._walls` 就丢了 —— 那是一格缺口，归 `_ring` 管重建。"""
    return tuple(
        w
        for w in sorted(turn.walls, key=lambda w: w.pos)
        if 0 < w.health and w.health * 5 < WALL_MAX_HP.get(w.level, WALL_MAX_HP[1])
    )


def _wall_item(level: int) -> str:
    """修这面墙该用哪件东西：升级当修 —— L1/L2 用对应等级的围墙升级券，L3 到顶只剩修复包。"""
    return WALL_VOUCHER.get(level + 1, WALLFIXER)


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

    卖的可用角色是全部（§4.4）。四条门，任一条不成立就 `False`：① 有货（`_best_load` 挑
    收购价最高的一种；`keep` = 石头留底块数 —— 判"卖得起"的调用方必须传同一个值）；
    ② 有小贩且走得到；③ 够本：货值 ≥ 2 × 往返回合数（"1 金币 ≈ 1 回合"是拍的，唯一的调参
    旋钮；`urgent=True` 跳过 —— 那趟路的回报是券不是矿价）；④ 回得来：`走到小贩 + 回基地
    ≤ 本段还剩的回合 − TIME_MARGIN`（`Turn.rounds_left`，夜里也成立 —— 夜里必须站回炮位）。

    距离一律 BFS 真实步数；-1 一律当"这趟不去"。一回合只能发一条指令 ⇒ 一次只卖一种矿，
    `num` = 手上那种的全部件数（卖光）。"""
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

    价 ≤ 0 或件数为 0 的矿跳过（空价目表 ⇒ 一件都不卖）。石头留底 `keep` 块不卖、另外两种
    照卖（默认 `STONE_RESERVE`，筹资那两条路传 `STONE_KEEP_RAISING`）；手里不到 `keep` 块
    ⇒ 这一趟不卖石头。名字参与比较只是为了让并列可复现。"""
    loads = [
        (prices.get(kind, 0), role.bag.get(kind, 0) - (keep if kind == STONE else 0), kind)
        for kind in SELLABLE
        if prices.get(kind, 0) > 0 and role.bag.get(kind, 0) > (keep if kind == STONE else 0)
    ]
    if not loads:
        return "", 0
    _, num, kind = max(loads)
    return kind, num


def _mine_spare_ore(
    role: Worker,
    turn: Turn,
    q: _Queue,
    sites: set[Pos],
    ore_taken: set[Pos] | None = None,
) -> None:
    """墙砌完了 ⇒ 不再闲着：就近采买得动、回得来的矿。

    先把"走得动、回得来"的矿筛出来、再按性价比挑：`价 × 新闻修正 ÷ (到矿 + 采一块 + 回炮位)`。
    回程参照 = 最近的武器位（没有武器才用基地）。新闻修正是 `AGENT.price_hint`（跨回合状态，
    退化 = 偏好偏一天）。先看 `_detour_buy`，再 `_detour_sell`。距离一律 BFS；-1 的矿作废。"""
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

    只在买得起时才列。优先级：修墙要的东西（与 `RepairWalls` 同一条挑墙口径，买错件就白绕
    一趟）> 升级链下一张券（别人包里已有一张就不再买 —— 没有转移指令，囤两张是白花金币）。
    围墙券不查别人拿没拿：它会消耗掉，墙还会再坏。"""
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
    """去差事的路上顺路买：绕 `DETOUR_MAX` 格内有商店、且购物单上有东西 ⇒ 先朝商店迈一步。

    绕路账与 `_detour_sell` 同一套（`via + after - direct ≤ DETOUR_MAX`）；-1 的绕法放弃。
    已经贴着差事目标就别绕了（这一回合该干活）。"""
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

    贴上小贩的那一回合同一条链里更早的 `_sell_ore` 自然把货出手（贴着跳过够本门），卖完
    没货、绕路条件消失，下一回合继续去矿。已经贴着矿就别绕了（这一回合该采）。

    "有没有可卖的货"用 `_best_load` 判、不在这里重抄一遍：那边有"石头留底"的口径，抄一遍
    就会出现"绕去卖那几块石头、到了却不肯卖"—— 白绕一趟。"""
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
    持券的那个角色（券在谁包里谁用）> 真空闲的开拓者（无可接任务）> 名册上第一个工人。
    距离一律 BFS；-1 一律这一轮不去。

    钱不够买券时，若"现钱 + 背包里最好那一堆货"够 ⇒ 先去卖矿（`_sell_ore(urgent=True)`）。
    用最好那一堆而不是背包总值：一回合只能发一条 `sell`，卖掉哪一种就到账哪一种 —— 拿总值
    凑够、到了却发现只够卖出一半，等于把路白走一遍。判"够不够"与真卖必须同一个 `keep`。"""
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
    payload 顺序）。券只认"从几级升"，不绑定类别。"""
    by_id = sorted(turn.weapons, key=lambda w: w.id)
    for kind, want in UPGRADE_CHAIN:
        for weapon in by_id:
            if weapon.kind == kind and weapon.level == want - 1:
                return weapon, VOUCHER[want]
    return None


def _pick_ore(
    pos: Pos, ores: Mapping[Pos, str], prices: Mapping[str, int], *, want_stone: bool
) -> Pos | None:
    """挑一座矿。`want_stone`（石头还不够砌完剩下的墙）⇒ 只认石矿、取最近的；否则三种矿
    按收购价从高到低，同价再取近的。价目表里没有的矿种按 0 算 ⇒ 不为它多走一步（空价目表
    也就自然落成"谁也不采"）。价格逐回合从载荷读、不写死。并列按坐标排。

    不认领矿：两个工人挤同一座矿的不同邻格都能采，只有"冲进同一格"才是白扔动作。"""
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


# ── 岗位几何：白天收工闸门与夜里操炮同一个口径 ──────────────────────


def _operator_spots(
    group: tuple[Weapon, ...], blocked: set[Pos], size: tuple[int, int]
) -> list[Pos]:
    """一组武器的操作站位：贴着组内所有武器的格子（去障碍）。

    单座组 ⇒ 返回那座本身（`step_toward` 会停在邻格）；多座组 ⇒ 所有武器 8 邻域的交集里
    走得通的格子（要站上去 —— `step_onto` 就是为它加的）。调用者是 `_post_spots`：
    `blocked` 要预先剔掉自己人。"""
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
    """贴着组内任意一座的格子（去障碍、去武器自己那几格）：共用操作位没了时的退路。

    那一格只打得了其中一座，但总比整组无人可打强 —— 共用操作位被机器人踩死、或者进不去
    那条一格宽的走廊时，整组会被跳过。"""
    width, height = size
    cells = {Pos(w.pos.x + d.x, w.pos.y + d.y) for w in group for d in STEPS}
    cells -= {w.pos for w in group}
    return sorted(c for c in cells if c not in blocked and 0 <= c.x < width and 0 <= c.y < height)


def _post_spots(group: tuple[Weapon, ...], turn: Turn, role: BaseRole) -> list[Pos]:
    """这一组这一回合能用的岗位：贴着组内每一座、又没被别人占着的格子。

    自己人一律从障碍里剔掉（`model._entries` 把我方角色写进网格，不放行的话站在岗位上的
    操作者看不见自己的岗位）；但别人站着的岗位要排除 —— 那格被占了就得换一组，否则一个
    走不进去、一个干等着，两人一起卡住。自己那格保留。"""
    cells = {r.pos for r in turn.roles}
    spots = _operator_spots(group, _passable(turn), turn.map.size)
    others = cells - {role.pos}
    return [s for s in spots if s not in others]


def _steps_to_post(pos: Pos, spot: Pos, onto: bool, blocked: Set[Pos], size: tuple[int, int]) -> int:
    """到岗位的 BFS 步数（走不到 -1），口径与 `_Queue.step` 一致。多座组的岗位是空地、
    要站上去 ⇒ 比"贴着它"多一步；已经在岗位上 ⇒ 0。"""
    if pos == spot:
        return 0
    steps = steps_between(pos, spot, blocked, size)
    return steps + 1 if steps >= 0 and onto else steps


# ── 发指令 ──────────────────────────────────────────────────────────


def _emit(
    cmds: dict[str, dict[str, Any]],
    role: BaseRole,
    cls: type[actions.BaseAction],
    *args: Any,
    key: str | None = None,
) -> bool:
    """造一条指令放进 `cmds`，成功返回 True。

    默认挂在 `str(role.id)` 下，只有 `attack` 例外（key 是武器 id，操控者在 `controllerId`
    里）⇒ 开一个 keyword-only 的口子，只有 `_fire` 那一处传 `key`。越权（`PermissionError`）
    只丢这一条并告警，不连坐同回合其他角色 —— 抛出去会变成"每回合空指令 ⇒ 全队冻结一整局"。"""
    try:
        cmds[key if key is not None else str(role.id)] = cls(role.type_name, *args).to_wire()
    except PermissionError as exc:
        LOGGER.warning("拦下越权动作：角色 %s(%s) %s", role.id, role.type_name, exc)
        return False
    return True
