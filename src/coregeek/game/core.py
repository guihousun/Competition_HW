"""共用底座：两个时段都要用的常量、走路账、黑板与岗位几何。

五样东西：常量（一张共用旋钮表）、走路账与指令出口（`_Move` / `_Queue` / `_emit`）、
**白天**的黑板 `_Ctx`、状态基类 `State`、挖矿排序 `_priciest_ore`、岗位几何。
**上层是 `day` / `night` / `planner`**（单向 import）—— 本模块绝不 import 它们（那就是循环导入）。

"两个模块都要问"就是进这里的门槛：只有一个消费者的判据留在各自的模块里 —— 白天那条四级链
（收工门 / 建武器 / 建墙 / 挖矿买券）连同买卖与顺路的助手全在 `day.py`，操炮与弹道全在
`night.py`。只剩三族共用：常量、走路/指令、岗位几何（白天收工门与夜里操炮共用 `_post_spots`），
外加一条**挖矿排序**（白天第 1/3 级与夜里清场后都是"挖最值钱的矿"）。

两个距离口径别混：回合预算一律用 BFS 真实步数（`steps_between`，-1 = 走不到）；选点/贴着
用切比雪夫 `Pos.dist`（`dist <= 1` 是"站在建造位/采集位/炮位旁"的判据，不是步数）。
"""

import logging
from collections.abc import Callable, Mapping, Set
from typing import Any, NamedTuple

from ..protocol import actions  # 指令只能经 Action 产出
from .grid import STEPS, Pos
from .path import steps_between
from .roles import BaseRole, Worker
from .utils import _passable
from .world import Turn, Weapon

LOGGER = logging.getLogger(__name__)


#: 建一座武器的金币（三种同价）。
WEAPON_COST = 25


#: 围墙的 `name` 与代价：石头×1，从建造者自己的背包扣（不是全队共享），拆了不返还。
WALL = "wall"
WALL_COST = 1


#: 升级券的商品名（`weaponShopList.name` 那套词；价目逐回合从载荷读，样例实证 100/150）。
VOUCHER = {2: "WeaponUpgradeVoucher1", 3: "WeaponUpgradeVoucher2"}


#: 围墙升级券：键 = 目标等级（与 `VOUCHER` 同一口径）。升级同时把墙回满血并把上限抬高一档。
WALL_VOUCHER = {2: "WallUpgradeVoucher1", 3: "WallUpgradeVoucher2"}


#: 买券的优先级（用户口径）：武器二级 > 武器三级 > 围墙二级 > 围墙三级。
#: 它是**目标清单**（取第一张"还有东西可升"的券去攒钱），不是"哪张便宜买哪张"。
VOUCHER_CHAIN = (VOUCHER[2], VOUCHER[3], WALL_VOUCHER[2], WALL_VOUCHER[3])


#: 建筑满血基准。基地 1500/3000/4500 是表格实证；墙 L2/L3 的 1500/2000 按每级 +500 推断
#: （待实盘校准）—— 推断偏小的方向是"晚修"，安全。
WALL_MAX_HP = {1: 1000, 2: 1500, 3: 2000}


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
    """**白天**的决策黑板：工人链与开拓者那两支共用的那几本账。

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


# ── 挖矿：白天与夜里同一个排序 ──────────────────────────────────────


def _priciest_ore(role: Worker, turn: Turn, ore_taken: set[Pos]) -> Pos | None:
    """当前最值钱的那座矿（并列取近的、再取坐标序）；一座都不值钱 / 都走不到 ⇒ `None`。

    只看 `turn.vendor_prices`（小贩收购价），**不读新闻修正**、也不看路程远近 —— 两个时段
    都是这个口径：白天第 1/3 级与夜里清场后都按它挑。`ore_taken` 是本回合已被别人认领的
    矿格（两个工人才不会都奔同一座）。卖不掉的矿（价 ≤ 0）不为它多走一步。"""
    walk, size = _passable(turn), turn.map.size
    best: tuple[int, int, Pos] | None = None
    for pos, kind in turn.map.ores.items():
        if pos in ore_taken:
            continue
        price = turn.vendor_prices.get(kind, 0)
        if price <= 0:
            continue
        hops = steps_between(role.pos, pos, walk, size)
        if hops < 0:
            continue
        key = (-price, hops, pos)
        if best is None or key < best:
            best = key
    return best[2] if best else None


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
