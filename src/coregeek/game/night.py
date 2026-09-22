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
    WALL_VOUCHER,
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
from .world import ROUNDS_PER_DAY, Robot, Turn, Wall, Weapon


#: 三种武器**每发/每枚**的 L1 伤害：加特林每颗子弹 10（沿弹道命中最近一台即消耗）；火箭中心 20、
#: 落点周围 8 格溅射 10；电磁能量 10（沿弹道穿透、逐台扣减）。等级放的是**发数/枚数** ——
#: `targetPos` 的个数 = 等级（接口文档 L218），多发可以打**不同**的格子（任务书 §4.5.1 L250–254），
#: 挑谁见 `_volley`；电磁是唯一单发武器，等级翻的是能量（10/20/30）。
GATLING_SHOT = 10
RAILGUN_ENERGY = 10
ROCKET_CENTER = 20
ROCKET_SPLASH = 10


#: 打谁更值钱的权重（用户口径"优先攻击最低级的机器人"）：低级机器人每点伤害更值钱 ——
#: 小型 40 血/5 攻击、中型 60/10、大型 500/20、BOSS 800/40（任务书 §4.7.2）⇒ 打死小型的
#: 性价比最高。**只影响挑目标，不影响报文形状**；`kind` 缺失（空串）⇒ 当最低级（照打）。
#: ⚠️ 4/3/2/1 是拍的（见 `code-task.md` 本步的不确定性）。
TIER_VALUE = {"smallRobot": 4, "middleRobot": 3, "largeRobot": 2, "bossRobot": 1}
UNKNOWN_VALUE = 4


#: "正在啃墙"的火力加成（用在 `_weight` 里，十分之一为单位）：与我方**任意一格围墙**切比雪夫 ≤1 的
#: 目标更急，啃的又是一格残墙（`needs_repair`）的最急。×1.5 / ×2 写成整数 15 / 20 ⇒ 全整数比较、
#: 判据可复现，不用浮点。⚠️ 1.5/2 是拍的：温和档，只改"本来差不多"的取舍，不动"低级优先"的大方向。
WALL_THREAT_NEAR = 15
WALL_THREAT_HURT = 20
#: 没有贴墙加成时的那一档（十分之一，即 ×1）。
WALL_THREAT_NONE = 10


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

    目标由 `_repair_target` 挑（血最少、且够得着的那一格）；到位就 `use` —— **手里有打得上的
    墙券就先打券**（升级顺带回满血，比修复包更值），没有券才用修复包。没有要修的就去待命位
    站着（`core._wall_post`：那里零步够着三格正面墙），下一回合就能出手。待命位走不到 ⇒ 出门
    挖矿，不原地干等。只发 `move` / `use`。"""
    target = _repair_target(turn)
    if target is not None:
        wall = next((w for w in turn.walls if w.pos == target), None)
        item = _front_voucher(role, wall) if wall is not None else None
        if role.pos.dist(target) <= 1:
            _emit(q.cmds, role, actions.Use, item or WALL_FIXER, target)
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


def _front_voucher(role: Worker, wall: Wall) -> str | None:
    """手里那张能把这一格升上去的墙券（L1 用券1、L2 用券2）；没有 ⇒ `None`。

    券顺带回满血（任务书 L297）⇒ 对一面残墙比修复包更值：同样回满，还永久抬高一档上限。
    L3 到顶 ⇒ `WALL_VOUCHER.get(4)` 是 `None` ⇒ 退回用包。"""
    name = WALL_VOUCHER.get(wall.level + 1)
    return name if name is not None and role.bag.get(name, 0) > 0 else None


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
    """贴着炮了：算出这一炮的 `targetPos` 列表开火。打不了就什么都不发。

    `targetPos` 的个数必须等于武器等级（接口文档 L218，多一个少一个都是指令非法）：电磁恒 1、
    加特林/火箭 = 等级数。**多发可以打不同的格子**（任务书 §4.5.1 L250–254：每颗子弹沿自身
    落点的弹道、每枚导弹各自算中心与溅射）⇒ 由 `_volley` 逐发挑目标，该分点分点、该叠加叠加。
    只打打我方的（`_foe_robots`）。一个炮手一回合只发一座 ⇒ 没有"同回合两座挤同一个将死者"
    这回事，不需要跨炮的伤害记账。"""
    if _cooling(weapon, turn.round_no):
        return False
    foes = _foe_robots(turn)
    if not foes:
        return False
    targets = _volley(weapon, turn, foes)
    if not targets:
        return False
    # key 是武器 id，操控角色在报文的 `controllerId` 里
    fired = _emit(cmds, role, actions.Attack, str(role.id), targets, key=str(weapon.id))
    if fired and weapon.kind == "rocket":
        _fired[weapon.id] = turn.round_no  # 判题器不发 cooldown ⇒ 发出的那发自己记
    return fired


def _value(robot: Robot) -> int:
    """这台机器人值多少（`TIER_VALUE`）：等级越低越优先。种类缺失 ⇒ 当最低级。"""
    return TIER_VALUE.get(robot.kind, UNKNOWN_VALUE)


def _weight(robot: Robot, walls: tuple[Pos, ...], hurt: frozenset[Pos]) -> int:
    """这一台的火力权重 = 种类权重 × 贴墙加成（`WALL_THREAT_*`，十分之一为单位）。

    正在啃我方围墙的更急（≤1 格），啃的又是一格残墙的最急 —— 墙塌了就没得修了。"""
    value = _value(robot)
    near = [c for c in walls if robot.pos.dist(c) <= 1]
    if not near:
        return value * WALL_THREAT_NONE
    return value * (WALL_THREAT_HURT if any(c in hurt for c in near) else WALL_THREAT_NEAR)


def _weights(turn: Turn, robots: tuple[Robot, ...]) -> dict[Pos, int]:
    """候选机器人格的权重表（墙环最多 20 格 ⇒ 一炮算一次的开销可以忽略）。"""
    walls = tuple(w.pos for w in turn.walls)
    hurt = frozenset(w.pos for w in turn.walls if needs_repair(w))
    return {r.pos: _weight(r, walls, hurt) for r in robots}


def _score(hits: list[tuple[Robot, int]], weights: dict[Pos, int]) -> int:
    """一组命中值多少分：`min(伤害, 剩余血) × 权重` 之和（过量伤害不计分）。"""
    return sum(damage * weights[robot.pos] for robot, damage in hits)


def _volley(weapon: Weapon, turn: Turn, robots: tuple[Robot, ...]) -> tuple[Pos, ...]:
    """这一炮的 `targetPos`（个数 = 等级；电磁恒 1）。一个目标都够不着 ⇒ 空元组（不打）。"""
    weights = _weights(turn, robots)
    if weapon.kind == "rocket":
        return _rocket_volley(weapon, robots, turn.map.size, weights)
    if weapon.kind == "gatling":
        return _gatling_volley(weapon, robots, weights)
    return _railgun_volley(weapon, robots, weights)


def _blast(cell: Pos, robots: tuple[Robot, ...], hp: dict[Pos, int]) -> list[tuple[Robot, int]]:
    """这一枚导弹落在 `cell` 上打中谁、各扣多少（中心 20、8 邻格溅射 10；按剩余血封顶）。"""
    out = []
    for r in robots:
        damage = ROCKET_CENTER if cell == r.pos else ROCKET_SPLASH if cell.dist(r.pos) == 1 else 0
        hit = min(damage, hp[r.pos])
        if hit > 0:
            out.append((r, hit))
    return out


def _rocket_volley(
    weapon: Weapon, robots: tuple[Robot, ...], size: tuple[int, int], weights: dict[Pos, int]
) -> tuple[Pos, ...]:
    """火箭：**逐枚**挑"剩余有效伤害 × 权重"最大的落点，扣掉血再挑下一枚 —— 簇里叠加、
    散开分点，两种都对（现在的多发同点只是前者的特例）。

    候选 = 机器人格及其 8 邻格（别的格子摸不到伤害）∩ 射程内 ∩ 图内；越界落点 = 指令非法。
    并列取坐标序最小（可复现）。全场都被这轮打死 ⇒ 后面几枚接着打第一枚那个点（个数必须补齐）。"""
    alive = _alive(robots)
    width, height = size
    cands = sorted(
        {
            cell
            for r in alive
            for cell in (r.pos, *(Pos(r.pos.x + d.x, r.pos.y + d.y) for d in STEPS))
            if 0 <= cell.x < width and 0 <= cell.y < height
            and weapon.pos.dist(cell) <= weapon.attack_range
        }
    )
    if not cands:
        return ()
    hp = {r.pos: max(0, r.health) for r in alive}
    targets: list[Pos] = []
    for _ in range(max(1, weapon.level)):
        hits = {cell: _blast(cell, alive, hp) for cell in cands}
        cell = min(cands, key=lambda c: (-_score(hits[c], weights), c))
        if not _score(hits[cell], weights) and targets:
            targets.append(targets[0])  # 都打死了 ⇒ 接着打第一枚那个点，别换空落点
            continue
        for robot, hit in hits[cell]:
            hp[robot.pos] -= hit
        targets.append(cell)
    return tuple(targets)


def _gatling_volley(
    weapon: Weapon, robots: tuple[Robot, ...], weights: dict[Pos, int]
) -> tuple[Pos, ...]:
    """加特林：**逐颗**挑最值的目标格（子弹沿弹道飞，命中的是弹道上**最近**的那台）。

    ⚠️ 多个落点必须落在**同一个 90° 锥形**内，否则**整次攻击非法**（任务书 L250）⇒ 自查按
    **≤45°** 卡（`_in_cone`，整数判据）：任务书说 > 90° 才非法，取一半余量。加不进新目标就
    用第一格补齐（同格夹角 0）—— 凑不满等级数也是非法。够得着的目标全是将死的（有效 ≤ 0）
    ⇒ 不打。"""
    level = max(1, weapon.level)
    reach = [r for r in _alive(robots) if weapon.pos.dist(r.pos) <= weapon.attack_range]
    if not reach:
        return ()
    hp = {r.pos: max(0, r.health) for r in reach}
    targets: list[Pos] = []
    for _ in range(level):
        best: tuple[tuple[int, int, Pos], Pos, Robot] | None = None
        for r in reach:
            if hp[r.pos] <= 0:
                continue  # 这一轮之前就被打死了
            victim = _first_on_line(weapon.pos, r.pos, reach)
            if victim is None:
                continue
            hit = min(GATLING_SHOT, hp[victim.pos])
            if hit <= 0:
                continue
            if targets and not _in_cone(weapon.pos, (*targets, r.pos)):
                continue
            key = (-hit * weights[victim.pos], weapon.pos.dist(r.pos), r.pos)
            if best is None or key < best[0]:
                best = (key, r.pos, victim)
        if best is None:
            targets.append(targets[0] if targets else reach[0].pos)
            continue
        _key, cell, victim = best
        hp[victim.pos] -= min(GATLING_SHOT, hp[victim.pos])
        targets.append(cell)
    return tuple(targets)


def _railgun_volley(
    weapon: Weapon, robots: tuple[Robot, ...], weights: dict[Pos, int]
) -> tuple[Pos, ...]:
    """电磁：单目标（恒 1 格），挑"**沿弹道穿透**总和最值"的那一格 —— 等级翻的是能量
    （10/20/30），能量沿弹道逐台扣减（任务书 L252），所以打穿一串比只打前排值。"""
    energy = RAILGUN_ENERGY * max(1, weapon.level)
    alive = _alive(robots)
    aims = [r.pos for r in alive if weapon.pos.dist(r.pos) <= weapon.attack_range]
    best = min(
        aims,
        key=lambda aim: (-_score(_pierce(weapon.pos, aim, alive, energy), weights), aim),
        default=None,
    )
    if best is None or _score(_pierce(weapon.pos, best, alive, energy), weights) <= 0:
        return ()
    return (best,)


def _pierce(origin: Pos, aim: Pos, robots: tuple[Robot, ...], energy: int) -> list[tuple[Robot, int]]:
    """能量沿 `origin`→`aim` 穿透：共线的那几台按距离排队，各吃 `min(剩余能量, 血)`。
    已毁 / 血量未知（吸收 0）的照旧穿过去、不耗能量。"""
    line = sorted(
        (r for r in robots if _collinear(origin, aim, r.pos)),
        key=lambda r: (r.pos.x - origin.x) ** 2 + (r.pos.y - origin.y) ** 2,
    )
    out: list[tuple[Robot, int]] = []
    left = energy
    for robot in line:
        hit = min(left, max(0, robot.health))
        if hit <= 0:
            continue
        out.append((robot, hit))
        left -= hit
        if left <= 0:
            break
    return out


def _first_on_line(origin: Pos, aim: Pos, robots: tuple[Robot, ...]) -> Robot | None:
    """从 `origin` 射向 `aim` 的弹道上**最近**的那台机器人（子弹命中它即消耗）。

    只认格心**共线**的那些：判题器怎么栅格化这条线没有文档 ⇒ 只算"保证在线上"的，少算不多算
    （算漏的后果只是这发打在了一台没预料到的机器人身上，仍然合法）。"""
    return min(
        (r for r in robots if _collinear(origin, aim, r.pos)),
        key=lambda r: (r.pos.x - origin.x) ** 2 + (r.pos.y - origin.y) ** 2,
        default=None,
    )


def _collinear(a: Pos, b: Pos, p: Pos) -> bool:
    """`p` 的格心是否落在 `a`→`b` 这条**线段**上（含端点）：整数叉积为 0 且在包围盒内。"""
    if (b.x - a.x) * (p.y - a.y) - (b.y - a.y) * (p.x - a.x) != 0:
        return False
    return min(a.x, b.x) <= p.x <= max(a.x, b.x) and min(a.y, b.y) <= p.y <= max(a.y, b.y)


def _in_cone(origin: Pos, cells: tuple[Pos, ...]) -> bool:
    """这些落点相对炮位是否都落在同一个 90° 锥形内（任务书 L250）。

    ⚠️ 按 **≤45°** 自查：整数判据 `dot > 0` 且 `2·dot² ≥ |a|²·|b|²`（等价于 `0 < cos²` 且
    `cos² ≥ 1/2`）—— 少了 `dot > 0` 那半边，135°–180° 的钝角会因为 cos² 很大被放过。
    不用浮点、与坐标轴方向无关（镜像不改变夹角）。同格（模长 0）视为夹角 0，恒合法。"""
    for i, a in enumerate(cells):
        for b in cells[i + 1 :]:
            va, vb = (a.x - origin.x, a.y - origin.y), (b.x - origin.x, b.y - origin.y)
            na, nb = va[0] ** 2 + va[1] ** 2, vb[0] ** 2 + vb[1] ** 2
            if na == 0 or nb == 0:
                continue
            dot = va[0] * vb[0] + va[1] * vb[1]
            if dot <= 0 or 2 * dot * dot < na * nb:
                return False
    return True

