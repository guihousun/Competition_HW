"""共用底座：两个时段都要用的常量、走路账、黑板与岗位几何。

七样东西：常量（一张共用旋钮表）、走路账与指令出口（`_Move` / `_Queue` / `_emit`）、
黑板 `_Ctx`、状态基类 `State`、矿价表（`record_news` / `_expected_price` / `_mineable`）、
挖矿排序 `_priciest_ore`、岗位几何。
**上层是 `day` / `night` / `planner`**（单向 import）—— 本模块绝不 import 它们（那就是循环导入）。

"两个模块都要问"就是进这里的门槛：只有一个消费者的判据留在各自的模块里 —— 白天那条四级链
（收工门 / 建武器 / 建墙 / 挖矿买券）连同买卖与顺路的助手全在 `day.py`，操炮与弹道全在
`night.py`。只剩四族共用：常量、走路/指令、岗位几何（白天收工门与夜里操炮共用 `_post_spots`），
外加**挖矿排序与矿价表**（白天挖矿与夜里出门采矿那一支都是"挖最值钱的矿"，区别只在夜里按
**明天**的期望价排 —— 见 `_priciest_ore` 的 `ahead`）。

两个距离口径别混：回合预算一律用 BFS 真实步数（`steps_between`，-1 = 走不到）；选点/贴着
用切比雪夫 `Pos.dist`（`dist <= 1` 是"站在建造位/采集位/炮位旁"的判据，不是步数）。
"""

import logging
from collections.abc import Callable, Iterable, Mapping, Set
from typing import Any, NamedTuple

from ..protocol import actions  # 指令只能经 Action 产出
from .grid import STEPS, Pos, wall_cells
from .path import steps_between
from .roles import BaseRole, Worker
from .utils import _passable
from .world import DAYS, Turn, Wall, Weapon

LOGGER = logging.getLogger(__name__)


#: 建一座武器的金币（三种同价）。
WEAPON_COST = 25


#: 一座矿的采集次数上限（任务书 L79："每个矿采集10次后会消失，下回合会随机刷新在地图的其他
#: 区域"）。payload 不给"剩余数"（接口文档的 `zones` 只有 `neutralType`）⇒ 只能自己数。
ORE_CHARGES = 10


#: 本地采矿账（跨回合观测状态）：`{矿格: 我们已经采过几次}`。按坐标键控、只增不减；
#: 矿采空后那格会空掉（新矿刷新在"地图的其他区域"）⇒ 旧账不会张冠李戴。**记错的代价**：
#: 把还有料的矿当成采空的（少去一座矿）或反过来（多跑一趟）—— 只影响排序，不碰红线。
_collected: dict[Pos, int] = {}


#: 围墙的 `name` 与代价：石头×1，从建造者自己的背包扣（不是全队共享），拆了不返还。
WALL = "wall"
WALL_COST = 1


#: 收工门的容错余量（回合）：`回到岗位的步数 + POST_MARGIN >= 本段剩余回合` 就动身。
#: 3 是拍的（用户口径"3 回合余量"；"固定 5 回合、不看距离"那一版没有容错，用户报"问题很大"）。
#: 白天两处共用：`day.BackToPost`（夜里上炮的那个人回岗位）与 `night.hold_the_wall`（修墙工回待命位）。
POST_MARGIN = 3


#: 围墙该处理了的血量（绝对值）。两条线共用这一个判据：白天 `day._weak_l1` 对 L1 拆了重砌，
#: 夜里 `night.repair_wall` 用修复包回满（不分等级）。
#: 为什么是绝对值、为什么是 200：机器人伤害（5/10/20/40）不吃墙的等级，而机器人的攻击距离是 3
#: ⇒ 同一格正面墙会被它身后一列里的多台同时啃 ⇒ "能不能在被打死之前修上"只由**绝对剩余血量**
#: 决定。阈值要盖过"越过阈值到修复包落地"之间挨掉的伤害：待命位到最远那格 3 步 ⇒ 其间挨 2 次
#: 结算伤害（伤害在回合末统一结算、包落地当回合的伤害在包之后）⇒ `T > 2 × 单格单回合伤害`；
#: 最坏单格 80/回合（BOSS + 两座大型）⇒ `T > 160`，取 200（余量够到 100/回合）。再往上只多
#: 撑一回合，却让"够格修"的格子同时变多 —— 一个工人一回合只修一格，会排队。
WALL_REPAIR_HP = 200


#: 围墙修复包（武器商店消耗品，10 金）：站在待修复围墙一格范围内 ⇒ 目标那一格回满血。
WALL_FIXER = "WallFixer"


def needs_repair(wall: Wall) -> bool:
    """这面墙该处理了吗：血量已知（`>= 0`）且低于 `WALL_REPAIR_HP`。

    L1/L2/L3 一个口径 —— 动不动它只由绝对血量决定。血量缺失（-1）⇒ 不碰（不知道就别动）。"""
    return 0 <= wall.health < WALL_REPAIR_HP


#: 升级券的商品名（`weaponShopList.name` 那套词；价目逐回合从载荷读，样例实证 100/150）。
VOUCHER = {2: "WeaponUpgradeVoucher1", 3: "WeaponUpgradeVoucher2"}


#: 围墙升级券：键 = 目标等级（与 `VOUCHER` 同一口径）。升级同时把墙回满血并把上限抬高一档。
WALL_VOUCHER = {2: "WallUpgradeVoucher1", 3: "WallUpgradeVoucher2"}


#: 买券/升级的优先级（用户口径）：`(券名, 目标组, 目标等级)`，从上往下先命中先用。
#: 它是**目标清单**（取第一个"还有东西可升"的步骤去攒钱），不是"哪张便宜买哪张"。
#: ⚠️ 武器二级券出现两次（第 1 步管非角上两座、第 3 步管角上那座）⇒ 目标组必须一起带着走。
#: **只有武器券**：围墙券走 `WALL_CHAIN`，归修墙工那条差事（见下）。
VOUCHER_CHAIN = (
    (VOUCHER[2], "weapon-side", 2),
    (VOUCHER[3], "weapon-side", 3),
    (VOUCHER[2], "weapon-corner", 2),
)

#: 围墙券的优先级：正面列那一列墙 L1→2 → L2→3。**归修墙工**（第 2.5 级那条差事），不在券链里：
#: 券链是开拓者与工人共用的，而开拓者买到的墙券在炮位上花不掉（`day._use_voucher_here` 只管武器券）、
#: 还会把他从岗位拽去墙边；修墙工本来就守那一列，买、用、修走同一趟路。
WALL_CHAIN = (
    (WALL_VOUCHER[2], "wall-front", 2),
    (WALL_VOUCHER[3], "wall-front", 3),
)

#: 两条链上出现过的券名（按优先级去重）—— 扫"手里有没有券"用它（链是步骤表，
#: 逐条取 `step[0]` 会把整条元组当键，一张券都认不出来）。
VOUCHER_NAMES = tuple(dict.fromkeys(step[0] for step in VOUCHER_CHAIN))
WALL_VOUCHER_NAMES = tuple(dict.fromkeys(step[0] for step in WALL_CHAIN))


#: 正面列（面向机器人的那一竖排）有几格 = `wall_cells` 的前几格（正面列排第一位）。
FRONT_WALLS = 6


def front_wall_cells(turn: Turn) -> tuple[Pos, ...]:
    """正面列那 6 格 —— 券链给它们升级、夜里守着它们修。基地没了 ⇒ 空元组。"""
    station = turn.map.station
    if station is None:
        return ()
    return wall_cells(station, turn.map.size[0])[:FRONT_WALLS]


def _wall_post(turn: Turn, role: BaseRole) -> Pos | None:
    """正面墙**靠基地那一列**里的待命位：夜里修墙工守着的格，也是白天回待命位的目标。

    取"到最远那格最近 → 邻接最多 → 坐标序"（左半基地 ⇒ `(12,23)`：中段一格零步够着 3 格正面墙，
    两个角格要 1–3 步）。候选全被占 / 出图 ⇒ `None`。⚠️ 判据要**把自己脚下那格除外**
    （`blocked` 里混着我方角色）：不除外就会把自己站着的格子判成"不可站"，每回合往旁边挪一格、
    永远来回晃。每回合现算，不带跨回合状态。"""
    front = front_wall_cells(turn)
    station = turn.map.station
    if not front or station is None:
        return None
    # 靠基地那一列：正面列在基地的哪一侧，就往回退一格
    back = front[0].x - (1 if front[0].x > station.x else -1)
    width, height = turn.map.size
    blocked = turn.map.blocked - {role.pos}
    cells = {
        Pos(back, y)
        for y in range(min(f.y for f in front) - 1, max(f.y for f in front) + 2)
        if 0 <= back < width and 0 <= y < height and Pos(back, y) not in blocked
    }
    if not cells:
        return None
    return min(
        cells,
        key=lambda p: (max(p.dist(f) for f in front), -sum(1 for f in front if p.dist(f) <= 1), p),
    )


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
    """这一回合的决策黑板：工人链与开拓者那两支共用的那几本账。

    每本账都是同一个对象被各状态原地改 —— 后一个状态读到的必须是前一个刚写下的那一份。
    逐角色顺序累计，回合一过就没了（夜里清场那一支现造一个，只借它的矿格认领账）。"""

    def __init__(
        self,
        turn: Turn,
        q: _Queue,
        *,
        sites: set[Pos] | None = None,
        ore_taken: set[Pos] | None = None,
        weapon_gap: bool = False,
        can_build_all: bool = False,
        sites_pending: tuple[tuple[str, Pos], ...] = (),
        build_plan: Mapping[int, tuple[tuple[str, Pos], ...]] | None = None,
    ) -> None:
        self.turn = turn
        self.q = q
        self.sites = set() if sites is None else sites
        self.ore_taken = set() if ore_taken is None else ore_taken
        self.weapon_gap = weapon_gap  # 份额有缺 ⇒ 第 1 级（建武器）没做完
        # 回合开始时的钱够不够一次把缺的武器全建了（按 `turn.gold` 判一次；`budget` 会随认领
        # 往下掉，第二个工人再去比就永远嫌少 —— 用户口径"钱不够就两人一起去挖矿"）
        self.can_build_all = can_build_all
        self.sites_pending = sites_pending  # 还缺的武器名额（落点与种类）
        self.build_plan = {} if build_plan is None else build_plan  # 谁建哪几座（`day.assign_sites`）
        self.taken: set[Pos] = set()  # 收工门认领的岗位（夜里那本账在 `planner._night_intents` 里）
        self.demolish_taken: set[Pos] = set()  # 认领了要拆的 L1 弱墙格
        self.budget = turn.gold  # 金币预留：认领一座武器 / 一张券就扣一份，宁可少买不可超支
        self.bought: dict[str, int] = {}  # 这一回合各券已预扣的张数（两条买券线共用一本账）
        self.target: Pos | None = None  # 分给本工人的环缺口头一格
        self.remaining = 0  # 那一段还剩几格


class State:
    """白天链（`day.DAY_CHAIN`）上的一个状态：`run` 返回 True = 这一回合归它了（驱动方就此停在这一个）。

    契约两条：一是 `False` 必须意味着"我什么都没排、什么都没发"—— 谁发了指令还返回 False，
    这个角色会落两条动作，后解的把前一条的 `cmds[角色]` 盖掉。二是无状态：只读 `ctx` 与
    `turn`，不往自己身上记东西（跨回合的标志位一旦卡住会静默关掉整条线）。

    链上顺序就是策略：从上往下第一个返回 True 的说了算。"""

    def run(self, role: Worker, ctx: _Ctx) -> bool:
        raise NotImplementedError


# ── 矿价：新闻钉住未来 k 天，当天价永远实测 ──────────────────────────


#: 10 天价格表：下标 = 第几天 - 1，每格 `{矿种: 期望单价}`。**只装新闻钉住的未来天**
#: （`record_news` 写）—— 当天价永远现读载荷（`_expected_price` 的那道闸门），所以这里
#: 不维护"今天"，判题器真涨价了当天就看得见。跨回合状态：记错只影响挖矿排序，不碰红线。
_prices: list[dict[str, int]] = [{} for _ in range(DAYS)]

#: 新闻钉住的"这几天采不了"（同形，值是矿种集合）。判据 `_mineable`。
_blocked: list[set[str]] = [set() for _ in range(DAYS)]

#: 新闻方向的倍数（**拍的**）：黑盒 LLM 给的"涨多少"没法校准，方向才是它判得准的那一维。
_NEWS_FACTOR = {"up": 2.0, "down": 0.5}


def _expected_price(turn: Turn, kind: str, day: int) -> int:
    """第 `day` 天这种矿的期望单价（`day` = 今天几号 + `ahead`）。

    只有**未来天**以新闻钉的为准（`record_news` 写进去的）；没钉过 / 出表 / 就是今天 ⇒
    一律照当回合的实测价 —— 没有新闻时与"只读载荷"逐字等价，当天价永远最真。"""
    if day > turn.day_no and 1 <= day <= DAYS:
        pinned = _prices[day - 1].get(kind)
        if pinned is not None:
            return pinned
    return turn.vendor_prices.get(kind, 0)


def _mineable(turn: Turn, kind: str) -> bool:
    """这种矿**今天**采不采得了（新闻说"停工 k 天"就采不了）—— 只看今天那一格。

    采不了的这一趟不去：`_priciest_ore` 挑矿、`day._mine_stone` 采石、`day._can_fund` 筹资
    三处共用这一条。"""
    day = turn.day_no
    return not (1 <= day <= DAYS and kind in _blocked[day - 1])


def record_news(events: Iterable[tuple[str, str, int, int]], turn: Turn) -> None:
    """新闻查价的回复进表 —— `[(矿种, 方向, 起始天, 天数), …]`（`agent.chat.is_prices_reply` 的产物）。

    窗口 = `[今天 + 起始天, + 天数 - 1]`，裁到 `1..DAYS`，整段落在表外 ⇒ 丢。
    `stop` ⇒ 进 `_blocked`（那几天采不了）；`up` / `down` ⇒ 以**当天实测价**为基准钉一个绝对价
    （拿倍数去乘最新实测价会跨天双重计数）；`flat` 与"查不到价"什么都不写（等于照旧）。
    有实质改动才打一条日志 —— 那个方向本来就是 LLM 猜的，复盘要看得到它猜了什么。"""
    today = turn.day_no
    parts: list[str] = []
    for kind, direction, first, days in events:
        start = today + first
        last = min(start + days - 1, DAYS)
        if start < 1 or start > last:
            continue
        span = f"第{start}天" if start == last else f"第{start}-{last}天"
        if direction == "stop":
            added = [day for day in range(start, last + 1) if kind not in _blocked[day - 1]]
            for day in added:
                _blocked[day - 1].add(kind)
            if added:
                parts.append(f"{kind} 停工 {span}")
            continue
        factor = _NEWS_FACTOR.get(direction)
        base = turn.vendor_prices.get(kind, 0)
        if factor is None or base <= 0:
            continue
        pinned = round(base * factor)
        if any(_prices[day - 1].get(kind) != pinned for day in range(start, last + 1)):
            for day in range(start, last + 1):
                _prices[day - 1][kind] = pinned
            parts.append(f"{kind} {direction} {span}→{pinned}（今 {base}）")
    if parts:
        LOGGER.info("【价格表】新闻进表：%s", " ｜ ".join(parts))


# ── 挖矿：白天与夜里同一个排序 ──────────────────────────────────────


def _priciest_ore(
    role: Worker,
    turn: Turn,
    ore_taken: set[Pos],
    *,
    walk: set[Pos] | None = None,
    ahead: int = 0,
) -> Pos | None:
    """**实际单价**最高的那座矿（并列取近的、再取坐标序）；一座都不值钱 / 都走不到 ⇒ `None`。

    实际单价（用户口径）= `单价 × 剩余 / (剩余 + 去 + 回)` —— 一次把这座矿采空的平均收益，
    回合都算进去：

    - **单价** = `_expected_price(turn, kind, 今天 + ahead)`：`ahead=0` 是当回合的实测价，
      `ahead=1` 是**明天**的期望价（新闻钉过就用新闻的）—— 夜里采的货第二天才卖；
    - **剩余** = 本地账 `_ore_left(pos)`（payload 不给这个数，任务书 L79 说每座矿采 10 次消失）；
    - **去** = `role.pos → 矿`、**回** = `矿 → 最近的武器位`（没有武器就用基地）的 BFS 步数。

    ⇒ 采空了的矿（剩余 0）、小贩不收的（价 ≤ 0）、**今天采不了的**（`_mineable`：新闻钉了
    停工）、走不到的（BFS -1）一律剔掉。
    `ore_taken` 是本回合已被别人认领的矿格（两个工人才不会都奔同一座）。

    `walk` 换一份地形（默认 `_passable`）—— 夜里那条挖矿线用它把"机器人周围"也当成走不通
    （安全半径见 `night._safe`）。"""
    walk = _passable(turn) if walk is None else walk
    size = turn.map.size
    station = turn.map.station
    posts = [w.pos for w in turn.weapons] or ([station] if station else [])

    def pick(use_ledger: bool) -> Pos | None:
        best: tuple[float, int, Pos] | None = None
        for pos, kind in turn.map.ores.items():
            if pos in ore_taken or not _mineable(turn, kind):
                continue
            price = _expected_price(turn, kind, turn.day_no + ahead)
            left = _ore_left(pos) if use_ledger else ORE_CHARGES
            if price <= 0 or left <= 0:
                continue
            out = steps_between(role.pos, pos, walk, size)
            back = min((steps_between(post, pos, walk, size) for post in posts), default=-1)
            if out < 0 or back < 0:
                continue
            key = (-(price * left) / (left + out + back), out, pos)
            if best is None or key < best:
                best = key
        return best[2] if best else None

    # 账本把候选全清空了（记错 / 与判题器的口径不一致）⇒ **当没账本再挑一遍**。
    # 一本本地账绝不能把整条挖矿线静默关掉 —— 那是"跨回合状态卡住"的老病，
    # 退化成"偶尔白跑一趟"要好得多。
    return pick(True) or pick(False)


def _ore_left(pos: Pos) -> int:
    """这座矿还剩几块（本地账：初值 `ORE_CHARGES`，我们每发一条 `collect` 减一块，最少 0）。"""
    return max(0, ORE_CHARGES - _collected.get(pos, 0))


def _collect(cmds: dict[str, dict[str, Any]], role: Worker, mine: Pos) -> bool:
    """发一条采集指令，**并记一笔本地采矿账** —— 那本账是"这座矿还剩几块"的唯一来源。

    采集动作在别处都不记（`_emit` 是通用的），所以只有这一个出口：谁要采谁走它。"""
    if not _emit(cmds, role, actions.Collect, mine):
        return False
    _collected[mine] = _collected.get(mine, 0) + 1
    return True


# ── 岗位几何：白天收工闸门与夜里操炮同一个口径 ──────────────────────


def _weapon_groups(turn: Turn) -> tuple[tuple[Weapon, ...], ...]:
    """把武器分成操作组：**同一组有一个共同的操作位**（贴着组内每一座的格子）。

    当前阵形 = 三座火箭共用一个操作位（`grid.weapon_sites` 那三格）⇒ 它们是一组，一个角色
    按冷却轮换就能全操。分组不按种类猜、也不按固定下标切：逐座试"并进这一组之后还有没有共同
    操作位"，并得进去就并（`_operator_spots` 一处口径）。两座离得远的武器自然各成一组
    （降级为"一人操一座"）。"""
    # `_operator_spots` 的契约：blocked 要预先剔掉自己人 —— 炮手就站在操作位上，
    # 不剔的话那一格"消失"、整组被拆散（站在岗位上的操作者看不见自己的岗位）。
    blocked = _passable(turn)
    groups: list[list[Weapon]] = []
    for weapon in turn.weapons:
        for group in groups:
            if _operator_spots(tuple(group) + (weapon,), blocked, turn.map.size):
                group.append(weapon)
                break
        else:
            groups.append([weapon])
    return tuple(tuple(group) for group in groups)


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
