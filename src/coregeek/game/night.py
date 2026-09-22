"""夜里：一个角色操三座火箭、一个工人守着正面列修墙、其余工人出门采矿，外加基地升级与
"清场没有"。

入口（`planner._night_intents` 按这个顺序问，那条链的代码在 `planner.py`）：`upgrade_station`
（基地残血 + 持券）→ `is_cleared`（没有还会打我方的活机器人 ⇒ 整夜改走白天那两条线）→
`defend`（炮手回岗位开火）/ `repair_wall`（持包的工人修正面列）/ `mine_ore`（其余工人采最值钱的
矿）。**判据顺序就是夜里的策略**，改序先看 `strategy.md` §5。

修墙线由 `repairer()` 认人（第 4 夜起 + 手里有修复包的非炮手工人）；它只发 `move` / `use` ——
夜里 `build` 与 `remove` 都非法，被打穿的墙只能等白天重砌。

岗位几何在 `core`（`_post_spots` / `_operator_spots` / `_near_spots`）：白天收工闸门与
这里必须同一个口径，只改一处会让白天把人送进岗位、夜里又不认领，那格被占着、整组没人操。
开火只经 `_emit`（`attack` 的 key 是武器 id，操控者在 `controllerId`）。

`_fired` 是本模块唯一的跨回合状态（本地开火账 —— 判题器不发 `cooldown` 字段）。
"""

from typing import Any

from ..protocol import actions  # 指令只能经 Action 产出
from .grid import STEPS, Pos, base_cells
from .roles import BaseRole, Worker
from .core import (
    POST_MARGIN,
    WALL_FIXER,
    _Queue,
    _collect,
    _emit,
    _near_spots,
    _operator_spots,
    _post_spots,
    _priciest_ore,
    _steps_to_post,
    _wall_post,
    _weapon_groups,
    front_wall_cells,
    needs_repair,
)
from .utils import _passable
from .world import ROUNDS_PER_DAY, Robot, Turn, Weapon


#: 三种武器的 L1 伤害：加特林每颗子弹 10（沿弹道命中最近一台即消耗）；电磁能量 10（沿弹道
#: 穿透、逐台扣减）；火箭中心 20、落点周围 8 格溅射 10。每升一级多发一份（个数 = 等级，见
#: `_fire`；电磁恒 1 个目标但能量翻倍）。机器人血量 40+ 全都 ≥ 20 ⇒ 一发不过量。
GATLING_SHOT = 10
RAILGUN_ENERGY = 10
ROCKET_CENTER = 20
ROCKET_SPLASH = 10


#: 本地开火账（跨回合观测状态）：{武器 id: 发出 attack 的回合号}。判题器不发 cooldown 字段
#: ⇒ 火箭冷却只能自己记：发出那回合记下，`ROCKET_COOLDOWN` 回合内不选它。按 id 键控、期满
#: 自动过期 ⇒ 武器换 id 旧账自愈，卡住的最坏代价 = 某座火箭少打 ≤3 回合。
_fired: dict[int, int] = {}


#: 火箭发射后的冷却回合数（任务书 §4.5.1：发射后 3 回合空窗）。
ROCKET_COOLDOWN = 3

#: 夜里挖矿要离机器人多远（切比雪夫）：工人这一夜在盒外采，机器人周围这一圈当走不通。
DANGER = 2


#: 第 4 夜起派一个工人守着正面列修墙（用户口径"第四晚开始"）。
WALL_REPAIR_FROM_DAY = 4


#: 机器人的攻击距离（任务书 §4.7.2：四种都是 3）—— 够不到某格就不急着修它。
ROBOT_RANGE = 3


#: 基地升级券（夜里基地升级用）。
STATION_VOUCHERS = ("StationUpgradeVoucher1", "StationUpgradeVoucher2")

STATION_MAX_HP = {1: 1500, 2: 3000, 3: 4500}


def is_cleared(turn: Turn) -> bool:
    """场上没有还会打我方的活机器人 —— 夜里"清场了没有"的唯一判据。

    不能照"场上全空"判：机器人**全图可见**（任务书 L95），对方那一波也在 `turn.robots` 里，
    而我们从不打它（`_foe_robots` 按 `targetTeam` 滤）⇒ 照全空判的话对方机器人活多久，
    工人就在炮位上钉多久、夜里这条线永远进不去。"""
    return not _alive(_foe_robots(turn))


def _safe(turn: Turn) -> set[Pos]:
    """夜里挖矿用的地形：`_passable` **再加上每个机器人周围切比雪夫 ≤ `DANGER` 的格子**。

    工人这一夜在盒外采，机器人很可能就在旁边 —— 挑矿与"走得到吗"都按这份地形算，等于把
    机器人附近当成走不通（用户口径"保证安全"）。⚠️ 只是**挑矿与估算**用；真迈步那一步把它
    当**软避让**（绕不开退回硬障碍照走，绝不原地卡死）。"""
    blocked = _passable(turn)
    for robot in _alive(turn.robots):
        blocked |= {
            Pos(robot.pos.x + dx, robot.pos.y + dy)
            for dx in range(-DANGER, DANGER + 1)
            for dy in range(-DANGER, DANGER + 1)
        }
    return blocked


def _danger_cells(turn: Turn) -> frozenset[Pos]:
    """机器人周围那圈格子（切比雪夫 ≤ `DANGER`）—— 挖矿路上当软避让用。"""
    return frozenset(_safe(turn) - _passable(turn))


def mine_ore(role: Worker, turn: Turn, q: _Queue, ore_taken: set[Pos]) -> None:
    """夜里（炮手之外的）工人去采最值钱的那座矿 —— 囤到第二天由白天那条链卖掉。

    排序与白天第 1/3 级共用一处（`core._priciest_ore`：按实际单价，不读新闻修正）；
    区别只在白天那一支动身前会看一眼顺路买卖、挖到的货是拿去凑券钱的。

    夜里这条**不买、不回炮位**：天亮前赶不回去也没有代价（白天还有 70 个回合走回来）⇒
    只问"走得到吗"，连时间预算都不算。清场之后才轮到它的是 `planner`：清场后走
    `day.sell_or_mine`（会卖也会买）。

    ⚠️ **机器人还活着时按 `_safe` 挑矿**（把机器人周围 `DANGER` 格内当走不通）⇒ 矿被机器人
    占着就换下一座；迈步那一步把它们当**软避让**（绕不开照走，绝不为躲机器人原地卡死）。"""
    foes = _alive(turn.robots)
    area = _safe(turn) if foes else None
    mine = _priciest_ore(role, turn, ore_taken, walk=area)
    if mine is None:
        return
    ore_taken.add(mine)
    if role.pos.dist(mine) <= 1:
        _collect(q.cmds, role, mine)
        return
    q.step(
        role,
        mine,
        avoid=_danger_cells(turn) if foes else frozenset(),
        with_paths=True,
        reserve=True,
    )


def _day_index(turn: Turn) -> int:
    """这一回合是第几天（1 起算）。`roundNo` 缺失（-1）⇒ 0：算不上"第几天"。"""
    return (turn.round_no - 1) // ROUNDS_PER_DAY + 1 if turn.round_no > 0 else 0


def repairer(turn: Turn, gunner: int | None) -> int | None:
    """这一夜谁去修墙 ⇒ 角色 id；不派 ⇒ `None`。

    第 `WALL_REPAIR_FROM_DAY`(4) 夜起才派，且**手里得有修复包**（谁买谁用、包不能转手 ⇒
    一个包都没有的夜里，工人照旧出门挖矿）。候选 = 非炮手的工人：包多的优先，并列按 id。"""
    if _day_index(turn) < WALL_REPAIR_FROM_DAY:
        return None
    carriers = [
        r
        for r in turn.roles
        if isinstance(r, Worker) and r.id != gunner and r.bag.get(WALL_FIXER, 0) > 0
    ]
    if not carriers:
        return None
    return min(carriers, key=lambda r: (-r.bag.get(WALL_FIXER, 0), r.id)).id


def hold_the_wall(role: Worker, turn: Turn, q: _Queue) -> bool:
    """白天末尾：这一夜要修墙的那个人先往待命位挪（与 `day.BackToPost` 同一个思路）。

    天黑时人才动身就晚了 —— 白天采矿/卖矿/买货都在盒外，正面墙在他走到之前就会被打穿。
    `回待命位步数 + POST_MARGIN(3) >= 白天剩余` ⇒ 动身（步数实时 BFS）；已经在待命位上 ⇒
    返回 `True` 但什么都不发（站着等天黑）。判据与夜里的 `repair_wall` 共用 `core._wall_post`。

    调用方（`planner._day_intents`）负责认人：是不是这一夜的修墙工由 `repairer()` 说了算。"""
    post = _wall_post(turn, role)
    if post is None:
        return False
    steps = _steps_to_post(role.pos, post, True, _passable(turn), turn.map.size)
    if steps < 0 or turn.day_rounds_left > steps + POST_MARGIN:
        return False
    if steps == 0:
        return True
    return q.step(role, post, onto=True, with_paths=True, reserve=True)


def repair_wall(role: Worker, turn: Turn, q: _Queue, ore_taken: set[Pos]) -> None:
    """守着正面列修墙：把血 < `WALL_REPAIR_HP` 的那一格修回满，没事就待在正面墙后方。

    目标由 `_repair_target` 挑（血最少、且够得着的那一格）；到位就 `use` 修复包。没有要修的
    就去待命位站着（`core._wall_post`：那里零步够着三格正面墙），下一回合就能出包。待命位走不到
    ⇒ 出门挖矿，不原地干等。只发 `move` / `use`。"""
    target = _repair_target(turn)
    if target is not None:
        if role.pos.dist(target) <= 1:
            _emit(q.cmds, role, actions.Use, WALL_FIXER, target)
            return
        if q.step(role, target, with_paths=True, reserve=True):
            return
    post = _wall_post(turn, role)
    if post is not None:
        if role.pos == post:
            return
        if q.step(role, post, onto=True, with_paths=True, reserve=True):
            return
    mine_ore(role, turn, q, ore_taken)


def _repair_target(turn: Turn) -> Pos | None:
    """该修的那一格正面墙：血最少的（并列按坐标序）；没有 ⇒ `None`。

    只认 `turn.walls` 里真有的活墙 —— 已毁的（血 0）在 `model._walls` 就丢了，那是一格缺口、
    夜里砌不了，只能等白天。够不着的也不修：一回合只修得一格，留给正在挨打的那格。"""
    front = set(front_wall_cells(turn))
    hurt = [
        w for w in turn.walls if w.pos in front and needs_repair(w) and _threatened(w.pos, turn)
    ]
    return min(hurt, key=lambda w: (w.health, w.pos)).pos if hurt else None


def _threatened(cell: Pos, turn: Turn) -> bool:
    """有活机器人够得着这一格吗（切比雪夫 ≤ `ROBOT_RANGE`）。"""
    return any(cell.dist(r.pos) <= ROBOT_RANGE for r in _alive(_foe_robots(turn)))


def _cooling(weapon: Weapon, round_no: int) -> bool:
    """这座炮这回合打不得吗：payload 的 `cooldown` 优先；火箭在字段缺失（-1）时查本地
    开火账 `_fired`（判题器不发这个字段）。加特林/电磁恒 0，不查。"""
    if weapon.cooldown > 0:
        return True
    if weapon.kind != "rocket":
        return False
    last = _fired.get(weapon.id)
    return last is not None and round_no - last <= ROCKET_COOLDOWN


def defend(role: BaseRole, turn: Turn, q: _Queue, taken: set[Pos]) -> None:
    """夜里：认领一组还没被本回合别人认领的武器，走到操作位，开火。所有角色都走这里。

    回合号缺失（`round_no < 0`）时一发不发：`is_day` 把缺失判成夜里，那个降级方向对
    "白天不许建造"安全、对 `attack`（白天开火非法）就反了。

    选组：最近且没人认领的（组内任一座被认领 = 整组被认领）。先开火、打不了才挪岗：贴着
    组内某座就先打它（只贴一座也打），站上岗位又都打不了 ⇒ 待命；没贴着 ⇒ 朝共用操作位走。
    不换组 —— 每回合重挑会让角色在炮位之间来回走。

    岗位去不了时不许整组无人可打：主岗位被非我方单位堵死 / 走不进那条一格宽的走廊 ⇒ 退到
    `_near_spots` 的邻座格上，只打得了其中一座也照打。岗位被同事占着不在此列 —— 那组归他。

    本模块只被 `planner._night_intents` 调用、且只调**炮手一个人**（`utils._night_gunner`：
    开拓者，没有就第一个工人）—— 三座火箭共用一个操作位（`core._weapon_groups` 并成一组），
    站上去按冷却轮换就能全操，其余角色出门挖矿。"""
    if turn.round_no < 0:
        return
    groups = _weapon_groups(turn)
    blocked = _passable(turn)
    # 退路（贴着组内任意一座的格子）：主岗位一个都站不上时才用 —— 主岗位能站就先站
    # （多座组那格交替得起来，退路只守得了一座）
    fallbacks: list[tuple[Pos, tuple[Weapon, ...]]] = []
    for group in sorted(groups, key=lambda g: (min(role.pos.dist(w.pos) for w in g), min(w.id for w in g))):
        if any(w.pos in taken for w in group):
            continue
        # 多座组共用一个岗位格（要站上去）；单座组的"岗位"就是那座炮，停在它旁边就够
        onto = len(group) > 1
        # 已贴着组内某座 ⇒ 认领整组开火。就绪的先挑（含本地开火账：发过的 3 回合内不算
        # 就绪 —— 打冷却炮是执行失败、白丢一回合火力）；就绪的并列按回合号轮转；
        # 都冷却 ⇒ 一发不发（待命，空指令合法）
        adjacent = [w for w in group if role.pos.dist(w.pos) <= 1]
        if adjacent:
            for w in group:
                taken.add(w.pos)
            ready = sorted((w for w in adjacent if not _cooling(w, turn.round_no)), key=lambda w: w.id)
            cooling = sorted((w for w in adjacent if _cooling(w, turn.round_no)), key=lambda w: w.id)
            if ready:
                k = turn.round_no % len(ready)
                ready = ready[k:] + ready[:k]
            for w in ready + cooling:
                if _fire(role, w, turn, q.cmds):
                    return
            if len(adjacent) == len(group):
                return  # 站在岗位上了、这回合又打不了 ⇒ 原地待命（不换组）
        spare = _operator_spots(group, blocked, turn.map.size)
        spots = _post_spots(group, turn, role)
        if spots:
            spot = min(spots, key=lambda s: (role.pos.dist(s), s))
            if q.step(role, spot, onto=onto):
                for w in group:
                    taken.add(w.pos)  # 认领发生在动身之后
                return
        elif spare and not adjacent:
            continue  # 岗位被同事占着 ⇒ 那一组归他，换下一组
        if not adjacent:
            # 主岗位站不上（被堵死 / 走不到）⇒ 记下退路，别的组都没得站时再用
            near = _near_spots(group, blocked, turn.map.size)
            if near:
                fallbacks.append((min(near, key=lambda s: (role.pos.dist(s), s)), group))
    for spot, group in fallbacks:
        if any(w.pos in taken for w in group):
            continue
        if q.step(role, spot, onto=True):
            for w in group:
                taken.add(w.pos)
            return


def upgrade_station(
    role: BaseRole, turn: Turn, q: _Queue
) -> bool:
    """夜里基地升级：持基地券 + 基地血量 < 满血 1/4 ⇒ 贴基地 `use`。`True` = 这一轮归它了。

    升级 + 回满血一次到位，是要塌的基地最好的救兵；那个回合放弃开火。血量缺失（-1）⇒
    未知 ⇒ 不动。"""
    voucher = next((v for v in STATION_VOUCHERS if v in role.bag), None)
    if voucher is None or turn.station_health < 0:
        return False
    station = turn.map.station
    if station is None:
        return False
    if turn.station_health * 4 >= STATION_MAX_HP.get(turn.station_level, STATION_MAX_HP[1]):
        return False  # 还不残血
    if any(role.pos.dist(c) <= 1 for c in base_cells(station)):
        return _emit(q.cmds, role, actions.Use, voucher, station)
    return q.step(role, station)


def _alive(robots: tuple[Robot, ...]) -> tuple[Robot, ...]:
    """这一回合真在场上（`health != 0`）的机器人。

    `model._robots` 不丢已毁的 ⇒ 判空、看射程、记账一律过这道筛子，别直接读 `turn.robots`。
    `health` 缺失给 -1（不是 0）⇒ 未知的照旧算活着（少打不如照打）。"""
    return tuple(r for r in robots if r.health != 0)


def _foe_robots(turn: Turn) -> tuple[Robot, ...]:
    """打我方基地的机器人；`our_team` 为空（字段缺失）⇒ 不过滤，全部照打（安全降级）。

    `target_team` 为空或等于我方 ⇒ 照打；明确打对方 ⇒ 跳过：打它们既浪费火力、又帮对方
    减轻基地压力（火箭射程远，尤其需要这道过滤）。"""
    our = turn.our_team
    if not our:
        return turn.robots
    return tuple(
        r for r in turn.robots if not r.target_team or r.target_team == our
    )


def _fire(role: BaseRole, weapon: Weapon, turn: Turn, cmds: dict[str, dict[str, Any]]) -> bool:
    """贴着炮了：按最大伤害落点开火。打不了就什么都不发。

    `targetPos` 的个数必须等于武器等级（接口文档 L218，多一个少一个都是指令非法）：电磁恒 1、
    加特林/火箭 = 等级数，多发全指同一个最优落点。火箭（`_rocket_site`）落点任选、中心 +
    溅射全场算账；加特林/电磁（`_beam_site`）落点打在某台身上（终点必在弹道 ⇒ 必命中），
    取有效伤害最高的、并列打近的。只打打我方的（`_foe_robots`）。一个炮手一回合只发一座
    ⇒ 没有"同回合两座挤同一个将死者"这回事，不需要跨炮的伤害记账。"""
    if _cooling(weapon, turn.round_no):
        return False
    foes = _foe_robots(turn)
    if not foes:
        return False
    if weapon.kind == "rocket":
        target = _rocket_site(weapon, foes, turn.map.size)
        if target is None:
            return False
    else:
        victim = _beam_site(weapon, foes)
        if victim is None:
            return False
        target = victim.pos
    count = 1 if weapon.kind == "railgun" else weapon.level
    # key 是武器 id，操控角色在报文的 `controllerId` 里
    fired = _emit(
        cmds, role, actions.Attack, str(role.id), (target,) * count, key=str(weapon.id)
    )
    if fired and weapon.kind == "rocket":
        _fired[weapon.id] = turn.round_no  # 判题器不发 cooldown ⇒ 发出的那发自己记
    return fired


def _beam_damage(weapon: Weapon) -> int:
    """加特林/电磁这一炮打在一个落点上的总伤害：每级 +10（份数 = 等级）。只用来挑目标与
    记账，实际命中由判题器算。"""
    base = GATLING_SHOT if weapon.kind == "gatling" else RAILGUN_ENERGY
    return base * weapon.level


def _rocket_damage(weapon: Weapon) -> tuple[int, int]:
    """火箭这一炮的 `(中心, 溅射)` 伤害：导弹数 = 等级、同落点叠加 ⇒ 每级 +20 / +10。"""
    return ROCKET_CENTER * weapon.level, ROCKET_SPLASH * weapon.level


def _beam_site(weapon: Weapon, robots: tuple[Robot, ...]) -> Robot | None:
    """加特林/电磁的目标：有效伤害最高的那台（并列打近的、再并列按坐标序）。

    "有效伤害" = min(伤害, 剩余血)：差别只在将死者 —— 别把整发浪费在已被打得差不多的人
    身上。够得着的目标全是将死的（有效 ≤ 0）⇒ 不打。"""
    shot = _beam_damage(weapon)

    def effective(r: Robot) -> int:
        return min(shot, max(0, r.health))

    reach = [r for r in _alive(robots) if weapon.pos.dist(r.pos) <= weapon.attack_range]
    best = max(reach, key=lambda r: (effective(r), -weapon.pos.dist(r.pos), r.pos), default=None)
    return best if best is not None and effective(best) > 0 else None


def _rocket_site(
    weapon: Weapon, robots: tuple[Robot, ...], size: tuple[int, int]
) -> Pos | None:
    """火箭的最大伤害落点：候选 = 机器人占的格及其 8 邻格（别的格子摸不到伤害），且落点
    须在射程内 —— 溅射可以够到射程之外的机器人。评分 = Σ min(伤害, 剩余血)，并列取坐标
    序最小（可复现）。越界格不进候选（越界落点 = 指令非法）。"""
    alive = _alive(robots)
    center, splash = _rocket_damage(weapon)
    width, height = size
    cands = {
        cell
        for r in alive
        for cell in (r.pos, *(Pos(r.pos.x + d.x, r.pos.y + d.y) for d in STEPS))
        if 0 <= cell.x < width and 0 <= cell.y < height
    }

    def score(cell: Pos) -> int:
        return sum(
            min(
                center if cell == r.pos else splash if cell.dist(r.pos) == 1 else 0,
                max(0, r.health),
            )
            for r in alive
        )

    in_range = [cell for cell in cands if weapon.pos.dist(cell) <= weapon.attack_range]
    return min(in_range, key=lambda cell: (-score(cell), cell), default=None)

