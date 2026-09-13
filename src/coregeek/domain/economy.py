"""经济线：采矿 → 贩卖 → 采购 → 升级。

设计见 docs/design/code-design.md §4；策略稿 §4。

**本模块只回答"该做什么"，不回答"怎么走过去"**（那是 `planner.py` 的事），
也不产出 `Intent`（那个由 planner 组装）。所有函数都是**纯函数**：
输入 `Turn`，输出愿望，可以随便重放/单测。

⚠️ 开局事实（✅任务书 §4.5.1 / §4.5.3）决定整个支出顺序：

    三种武器**初始数量都是 0**，全局上限 3 座，单价 25 金；**开局金币 75**。

75 / 25 = 恰好 3 座 —— **第 1 天白天的第一优先动作就是把 3 座武器建满**，
否则当天入夜三人无武器可操控，基地要赤手空拳挨整晚。建满 3 座之后，
金币才轮到升级券（见 `purchase_wish` 的阶梯）。

⚠️ 本模块早期写反过：把 `docs/request.txt`（`roundNo=85` 的**中局**快照，
已有 3 座武器 + 20 金）误当成开局状态，于是判定"开局送满 3 座、造武器是死支出线"，
第 1 天一座武器都不造。25×3 = 75 = 初始金币正是那条推理错误的直接反证。

⚠️ `build` **仅工人、仅白天**可用（任务书 L137），所以造武器是
`planner` 里筑墙手的职责，**不是**军需官的采购 —— 商店里不卖武器。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..infra import config
from .grid import Pos
from .world import Turn, carried

__all__ = [
    "MINING_VALUE",
    "PurchaseWish",
    "mine_priority",
    "stone_need",
    "sell_plan",
    "purchase_wish",
    "upgrade_targets",
    "weapons_shortfall",
    "next_weapon_build",
    "MINE_YIELD_PER_TURN",
]

#: 每回合 `collect` 产出 1 个（任务书 L137）。
MINE_YIELD_PER_TURN = 1

#: 采矿优先级权重：**挖值钱的**。小贩收购价 stone=1 / iron=3 / copper=5，
#: 而围墙只吃石头 —— 所以石头是"够用就停"的物资，铁铜才是现金来源。
#: 数值取样例 `vendorShopList` 的默认价；实盘用 payload 的实时价覆盖（见 `sell_plan`）。
MINING_VALUE: dict[str, int] = {"stone": 1, "iron": 3, "copper": 5}

#: 小贩收购价的兜底（payload `vendorShopList` 缺失时用）。
DEFAULT_VENDOR_PRICE: dict[str, int] = dict(MINING_VALUE)


@dataclass(frozen=True, slots=True)
class PurchaseWish:
    """一次采购愿望。`item` 是**商店里逐字一致的商品名**（大小写敏感）。"""

    item: str
    reason: str


def mine_priority(kind: str, *, stone_short: bool) -> int:
    """给矿种打分，越大越该去挖。

    石头短缺时石头**无条件**优先 —— 断料会让整个建墙线停摆，而建墙是唯一
    能把夜战伤害挡在基地之外的手段，比多赚几个金币重要得多。

    否则按 `价值 × MINE_HORIZON` 打分。那个乘数是必要的：
    采矿是**长期**行为，一旦站定就每回合稳定 +1，路上多走 5 格只亏 5 回合、
    之后每回合都赚回差价。若拿 1:1 的距离惩罚去比（`价值 − 距离`），
    铜（5 金）在只远 5 格时就会输给石头（1 金）—— 实测样例里正是如此：
    经济手放着 (22,26) 的铜不挖，天天去挖 1 金的石头，金币永远停在开局那 20。
    """
    if kind == "stone" and stone_short:
        return 10_000
    return MINING_VALUE.get(kind, 0) * config.MINE_HORIZON


def stone_need(turn: Turn, gaps: int, held: int) -> int:
    """还需要多少石头。

    = 剩余缺口（每面墙 1 块）+ `STONE_BUFFER` 备料 − 背包里已有的。
    备料是给"墙被打掉后要立刻补"留的余地：夜间不能建，但白天开局那几回合
    必须能马上补上，否则等于白送对方一个整晚的破口。
    """
    want = max(0, gaps) + config.STONE_BUFFER
    return max(0, want - max(0, held))


def sell_plan(turn: Turn, role_carried: dict[str, int], *, stone_keep: int) -> list[tuple[str, int]]:
    """站在小贩身边时该卖什么，按**收益降序**返回 `(商品名, 数量)`。

    一条指令只能卖一个品种，而每回合只有一个动作 —— 所以顺序必须按价值排：
    先卖铜，再卖铁，最后才是多余的石头。这样即使小贩待不了多久，
    卖掉的也是账面上最值钱的部分。

    ⚠️ 石头**留 `stone_keep` 块**（= 剩余缺口 + 备料）。
    全卖光会让建墙线停摆，而一块石头只值 1 金 —— 这是全游戏最亏的一笔交易。
    """
    price = _vendor_prices(turn)
    plan: list[tuple[int, str, int]] = []  # (-价值, 名称, 数量)
    for name in ("copper", "iron", "stone"):
        have = role_carried.get(name, 0)
        if have <= 0:
            continue
        if name == "stone":
            have -= min(have, max(0, stone_keep))
        if have <= 0:
            continue
        plan.append((-(have * price.get(name, DEFAULT_VENDOR_PRICE.get(name, 1))), name, have))
    plan.sort()
    return [(name, num) for _, name, num in plan]


def _vendor_prices(turn: Turn) -> dict[str, int]:
    """实时收购价。`vendorShopList` 就是小贩的报价表，缺项走兜底。

    ❓价格会随世界新闻波动（任务书 §4.6.1 / §5.1），但**新闻解析属于第 7 步**；
    第 5 步直接用 payload 给的当前价，天然就是调整后的结果，无需自己推演。
    """
    out = dict(DEFAULT_VENDOR_PRICE)
    for item in turn.vendor_shop:
        if item.name in out and item.price > 0:
            out[item.name] = item.price
    return out


def weapons_shortfall(turn: Turn) -> int:
    """还差几座武器才到全局上限。

    **开局就是 3**（任务书 §4.5.1：初始数量 0，上限 3），不是 0 —— 这一点
    容易写反，写反的后果是第 1 天不造武器。数值下降只发生在武器被打掉之后。
    """
    alive = len([r for r in turn.weapons if r.alive])
    return max(0, config.MAX_WEAPONS - alive)


def next_weapon_build(turn: Turn, box) -> tuple[str, Pos] | None:
    """下一座该建的武器 `(类型, 位置)`；不需要建/建不起/无处可建时返回 None。

    三个条件缺一不可：金币够、还没满 3 座、环上还有空位。
    位置取 `Box.weapon_sites()`（由正面往后）里的**第一个空位**，
    类型按 `config.WEAPON_LOADOUT` 顺序给 —— 射程短的靠正面。

    白天没有机器人（任务书 L350），所以环上不会出现"位置被机器人占住"的情况，
    这里不必做可达性预检。
    """
    if turn.gold < config.WEAPON_BUILD_COST:
        return None
    alive = [r for r in turn.weapons if r.alive]
    if len(alive) >= config.MAX_WEAPONS or box is None:
        return None
    taken = {r.pos for r in alive}
    free = [p for p in box.weapon_sites() if p not in taken]
    if not free:
        return None
    order = config.WEAPON_LOADOUT
    kind = order[min(len(alive), len(order) - 1)]
    return kind, free[0]


def upgrade_targets(turn: Turn, name: str) -> tuple[Pos, ...]:
    """某张升级券可以作用的目标位置（按"最该升级的排最前"）。

    券要求使用者**站在目标建筑周围一格内**，所以这里返回的是**建筑格**，
    不是落脚格 —— 落脚点由 planner 用 `best_stand` 现算。

    排序理由：
      · 武器券 —— 先升**火箭**（伤害 20×等级、升级还加落点个数，是全队最大一击），
        再升加特林（同样吃落点个数）；电磁狙击炮永远是 1 个目标，升级只涨伤害，
        排最后。同类型里先升**血少的**（离被打掉最近）。
      · 基地券 —— 只有一座，无需排序。
      · 围墙券 —— 只升**正面**的墙（正对着机器人来路），背面基本不会被碰。
    """
    if name.startswith("WeaponUpgrade"):
        want_level = 1 if name.endswith("1") else 2
        cands = [r for r in turn.weapons if r.alive and r.level == want_level]
        rank = {"rocket": 0, "gatling": 1, "railgun": 2}
        return tuple(r.pos for r in sorted(cands, key=lambda r: (rank.get(r.role_type, 9), r.health, r.id)))
    if name.startswith("StationUpgrade"):
        want_level = 1 if name.endswith("1") else 2
        st = turn.station
        if st is not None and st.alive and st.level == want_level:
            return (st.pos,)
        return ()
    if name.startswith("WallUpgrade"):
        want_level = 1 if name.endswith("1") else 2
        from .world import box_of  # 局部 import：避免模块级循环

        box = box_of(turn)
        front = set(box.sides[box.front]) if box else set()
        cands = [r for r in turn.our_wall_alive if r.level == want_level]
        cands.sort(key=lambda r: (r.pos not in front, r.health, r.pos.x, r.pos.y))
        return tuple(r.pos for r in cands)
    return ()


def _weapon_reserve(turn: Turn) -> int:
    """还没建满的武器要**预留**的金币 —— 武器是全部支出里优先级最高的一笔。

    ⚠️ 没有它就会出现这种局面（实测）：第 1 天金币 75、武器 0 座，采购阶梯的
    ⑥ 档（`gold >= 60 + 10 = 70`）成立，军需官于是跑去买 20 金的围墙升级券，
    剩 55 金 —— **只够再建 2 座武器**，开局三座火力的设计意图被一张闲券挤掉。
    预留额 = `单价 × min(还差几座, 环上空位)`，所以环满了/推不出盒子时预留为 0，
    金币不会白白锁死。
    """
    from .world import box_of  # 局部 import：避免模块级循环

    box = box_of(turn)
    if box is None:
        return 0
    taken = {r.pos for r in turn.weapons if r.alive}
    free = sum(1 for p in box.weapon_sites() if p not in taken)
    return config.WEAPON_BUILD_COST * min(weapons_shortfall(turn), free)


def purchase_wish(turn: Turn) -> PurchaseWish | None:
    """本回合**最想买**的一样东西；没有值得买的返回 None。

    阶梯按"每金币换到的生存力"排序，而不是按价格。已在背包里的券不再重复买
    （券不叠放，买重了只是白花金币 —— 见 `world.carried`）。

    ⚠️ 比较用的是 `可支配金币 = 现有金币 − 武器预留`（见 `_weapon_reserve`）：
    券可以晚一天买，武器空着就整晚没有火力。
    """
    gold = turn.gold - _weapon_reserve(turn)
    have = carried(turn)

    def want(item: str) -> int:
        return have.get(item, 0)

    # ⚠️ 这里**没有**"造武器"这一档：商店里不卖武器，武器是工人白天在武器环上
    #    `build` 出来的（见 `next_weapon_build`）。早期这里有一档伪商品
    #    `_build_weapon`，但没有任何角色能执行它 —— 军需官拿到的是 Idle，
    #    筑墙手又没有这个分支，于是**第 1 天一座武器都不造**。
    #    支出优先级里"武器 ≫ 一切"这件事，由 planner 的筑墙手保证，不由采购阶梯保证。

    # ① 基地升级：1500 → 3000 血是**单项收益最大**的一笔钱（score₃ 直接看存活）。
    #    只在基地已受伤或已有武器满级时才排到武器券前面，否则先扩火力。
    if gold >= 100 and want("StationUpgradeVoucher1") == 0 and _station_at(turn, 1):
        if _station_hurt(turn) or not _has_weapon_at(turn, 1):
            return PurchaseWish("StationUpgradeVoucher1", "基地 1500→3000 血，生存分直接受益")

    # ② 武器升级券：一级券让伤害与射程同时翻档，是防线的核心投资。
    #    ⚠️ 前提是武器已经建满 —— `_has_weapon_at` 保证了这一点，
    #    而建满 3 座要花掉开局的 75 金，所以这条实际上是第 2 天以后的事。
    if gold >= 100 and want("WeaponUpgradeVoucher1") == 0 and _has_weapon_at(turn, 1):
        return PurchaseWish("WeaponUpgradeVoucher1", "武器 L1→L2：伤害与射程同时提升")

    # ③ 基地二级
    if gold >= 150 and want("StationUpgradeVoucher2") == 0 and _station_at(turn, 2):
        return PurchaseWish("StationUpgradeVoucher2", "基地 L2→L3：3000→4500 血")

    # ④ 武器二级
    if gold >= 150 and want("WeaponUpgradeVoucher2") == 0 and _has_weapon_at(turn, 2):
        return PurchaseWish("WeaponUpgradeVoucher2", "武器 L2→L3：满级火力")

    # ⑤ 围墙修复包：全店最便宜的保险（10 金），但只在**已经有墙要守**时留一件。
    #    开局那 75 金全部要投给 3 座武器（见模块头），所以这里加了 gold 门槛。
    if (
        gold >= config.WALLFIXER_PRICE + config.WALLFIXER_RESERVE_GOLD
        and want("WallFixer") < config.WALLFIXER_STOCK
        and len(turn.our_wall_alive) >= 3
    ):
        return PurchaseWish("WallFixer", "围墙修复包：夜间不可重建，破口只能靠它补")

    # ⑥ 围墙升级券（20/30 金）：便宜，但收益是"某一段墙多 500 血"——
    #    放在所有大件之后，用闲钱买。
    if gold >= 60 + config.WALLFIXER_PRICE and want("WallUpgradeVoucher1") == 0 and _wall_at(turn, 1):
        return PurchaseWish("WallUpgradeVoucher1", "围墙 L1→L2：闲钱换来的正面厚度")
    if gold >= 80 + config.WALLFIXER_PRICE and want("WallUpgradeVoucher2") == 0 and _wall_at(turn, 2):
        return PurchaseWish("WallUpgradeVoucher2", "围墙 L2→L3")

    return None


def _station_at(turn: Turn, level: int) -> bool:
    st = turn.station
    return st is not None and st.alive and st.level == level


def _station_hurt(turn: Turn) -> bool:
    st = turn.station
    if st is None:
        return False
    cap = {1: 1500, 2: 3000, 3: 4500}.get(min(max(st.level, 1), 3), 1500)
    return st.health < cap * config.STATION_HURT_RATIO


def _has_weapon_at(turn: Turn, level: int) -> bool:
    return any(r.alive and r.level == level for r in turn.weapons)


def _wall_at(turn: Turn, level: int) -> bool:
    return any(r.level == level for r in turn.our_wall_alive)


def wall_material_needed(turn: Turn, gaps: int) -> int:
    """建 `gaps` 面墙需要的石头数（每面 1 块，任务书 §4.5.1）。"""
    return max(0, gaps)
