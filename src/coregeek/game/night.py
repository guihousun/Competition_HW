"""夜里：认领武器组、走到岗位、按最大伤害落点开火，外加基地升级与"清场没有"。

三个入口（`planner._intents` 按这个顺序问）：`upgrade_station`（基地残血 + 持券）→
`is_cleared`（没有还会打我方的活机器人）→ `defend`（其余角色回炮位开火）。**判据顺序就是
夜里的策略**，改序先看 `strategy.md` §5。

岗位几何在 `states`（`_post_spots` / `_operator_spots` / `_near_spots`）：白天收工闸门与
这里必须同一个口径，只改一处会让白天把人送进岗位、夜里又不认领，那格被占着、整组没人操。
开火只经 `_emit`（`attack` 的 key 是武器 id，操控者在 `controllerId`）。

`_fired` 是本模块唯一的跨回合状态（本地开火账 —— 判题器不发 `cooldown` 字段）。
"""

from collections.abc import Mapping
from typing import Any

from ..protocol import actions  # 指令只能经 Action 产出
from .grid import STEPS, Pos, base_cells
from .roles import BaseRole, Pioneer
from .states import _Queue, _emit, _near_spots, _operator_spots, _post_spots
from .utils import _passable, _pioneer_mans_guns, _weapon_groups
from .world import Robot, Turn, Weapon


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


#: 基地升级券（夜里基地升级用）。
STATION_VOUCHERS = ("StationUpgradeVoucher1", "StationUpgradeVoucher2")

STATION_MAX_HP = {1: 1500, 2: 3000, 3: 4500}


def is_cleared(turn: Turn) -> bool:
    """场上没有还会打我方的活机器人 —— 夜里"清场了没有"的唯一判据。

    不能照"场上全空"判：机器人**全图可见**（任务书 L95），对方那一波也在 `turn.robots` 里，
    而我们从不打它（`_foe_robots` 按 `targetTeam` 滤）⇒ 照全空判的话对方机器人活多久，
    工人就在炮位上钉多久、夜里的经济线永远进不去。"""
    return not _alive(_foe_robots(turn))


def _stands_on_a_post(role: BaseRole, turn: Turn) -> bool:
    """这个角色是不是正站在某组的操作位上（多座组那格要踩上去；单座组的岗位就是炮自己）。"""
    blocked = _passable(turn)
    return any(
        role.pos in _operator_spots(g, blocked, turn.map.size) for g in _weapon_groups(turn)
    )


def _cooling(weapon: Weapon, round_no: int) -> bool:
    """这座炮这回合打不得吗：payload 的 `cooldown` 优先；火箭在字段缺失（-1）时查本地
    开火账 `_fired`（判题器不发这个字段）。加特林/电磁恒 0，不查。"""
    if weapon.cooldown > 0:
        return True
    if weapon.kind != "rocket":
        return False
    last = _fired.get(weapon.id)
    return last is not None and round_no - last <= ROCKET_COOLDOWN


def defend(
    role: BaseRole,
    turn: Turn,
    q: _Queue,
    taken: set[Pos],
    assigned: dict[Pos, int],
) -> None:
    """夜里：认领一组还没被本回合别人认领的武器，走到操作位，开火。所有角色都走这里。

    回合号缺失（`round_no < 0`）时一发不发：`is_day` 把缺失判成夜里，那个降级方向对
    "白天不许建造"安全、对 `attack`（白天开火非法）就反了。

    选组：最近且没人认领的（组内任一座被认领 = 整组被认领）。先开火、打不了才挪岗：贴着
    组内某座就先打它（只贴一座也打），站上岗位又都打不了 ⇒ 待命；没贴着 ⇒ 朝共用操作位走。
    不换组 —— 每回合重挑会让角色在炮位之间来回走。开拓者是补位炮手（`_pioneer_mans_guns`）：
    工人够操满所有组时它一个组都不认领；例外：它已站在某组操作位上 ⇒ 认领那一组（那格被
    它占着、工人站不上去，再不打整组白丢一夜）。

    岗位去不了时不许整组无人可打：主岗位被非我方单位堵死 / 走不进那条一格宽的走廊 ⇒ 退到
    `_near_spots` 的邻座格上，只打得了其中一座也照打。岗位被同事占着不在此列 —— 那组归他。"""
    if turn.round_no < 0:
        return
    # 工人够操满所有组 ⇒ 炮位留给工人，开拓者一个组都不认领（补位炮手）
    if (
        isinstance(role, Pioneer)
        and not _pioneer_mans_guns(turn)
        and not _stands_on_a_post(role, turn)
    ):
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
                if _fire(role, w, turn, q.cmds, assigned):
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


def _fire(
    role: BaseRole,
    weapon: Weapon,
    turn: Turn,
    cmds: dict[str, dict[str, Any]],
    assigned: dict[Pos, int],
) -> bool:
    """贴着炮了：按最大伤害落点开火。打不了就什么都不发。

    `targetPos` 的个数必须等于武器等级（接口文档 L218，多一个少一个都是指令非法）：电磁恒 1、
    加特林/火箭 = 等级数，多发全指同一个最优落点。火箭（`_rocket_site`）落点任选、中心 +
    溅射全场算账；加特林/电磁（`_beam_site`）落点打在某台身上（终点必在弹道 ⇒ 必命中），
    取有效伤害最高的、并列打近的。`assigned` 记账：先开火的把估计伤害记在机器人身上，
    后开的按剩余血算 —— 不挤同一个将死的目标。只打打我方的（`_foe_robots`）。"""
    if _cooling(weapon, turn.round_no):
        return False
    foes = _foe_robots(turn)
    if not foes:
        return False
    if weapon.kind == "rocket":
        target = _rocket_site(weapon, foes, turn.map.size, assigned)
        if target is None:
            return False
        _book_rocket(weapon, target, foes, assigned)
    else:
        victim = _beam_site(weapon, foes, assigned)
        if victim is None:
            return False
        target = victim.pos
        assigned[victim.pos] = assigned.get(victim.pos, 0) + _beam_damage(weapon)
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


def _beam_site(
    weapon: Weapon, robots: tuple[Robot, ...], assigned: Mapping[Pos, int]
) -> Robot | None:
    """加特林/电磁的目标：有效伤害最高的那台（并列打近的、再并列按坐标序）。

    "有效伤害" = min(伤害, 剩余血)：差别只在将死者 —— 别把整发浪费在已被打得差不多的人
    身上。够得着的目标全是将死的（有效 ≤ 0）⇒ 不打。"""
    shot = _beam_damage(weapon)

    def effective(r: Robot) -> int:
        return min(shot, max(0, r.health - assigned.get(r.pos, 0)))

    reach = [r for r in _alive(robots) if weapon.pos.dist(r.pos) <= weapon.attack_range]
    best = max(reach, key=lambda r: (effective(r), -weapon.pos.dist(r.pos), r.pos), default=None)
    return best if best is not None and effective(best) > 0 else None


def _rocket_site(
    weapon: Weapon,
    robots: tuple[Robot, ...],
    size: tuple[int, int],
    assigned: Mapping[Pos, int],
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
                max(0, r.health - assigned.get(r.pos, 0)),
            )
            for r in alive
        )

    in_range = [cell for cell in cands if weapon.pos.dist(cell) <= weapon.attack_range]
    return min(in_range, key=lambda cell: (-score(cell), cell), default=None)


def _book_rocket(
    weapon: Weapon, target: Pos, robots: tuple[Robot, ...], assigned: dict[Pos, int]
) -> None:
    """把火箭这一发的估计伤害记到账上（同回合后开的炮按剩余血挑目标）。"""
    center, splash = _rocket_damage(weapon)
    for r in _alive(robots):
        hit = center if r.pos == target else splash if r.pos.dist(target) == 1 else 0
        if hit:
            assigned[r.pos] = assigned.get(r.pos, 0) + hit
