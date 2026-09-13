#!/usr/bin/env python3
"""无 pytest 环境下的断言驱动 + 分层 import-lint。

    py -3.13 tools/selfcheck.py            # 跑全部
    py -3.13 tools/selfcheck.py crypto     # 只跑名字里含 crypto 的用例
    py -3.13 tools/selfcheck.py --list

设计见 docs/design/code-design.md §16 第 2、4 项。
"""

from __future__ import annotations

import ast
import json
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

CASES: list[tuple[str, object]] = []


def case(fn):
    CASES.append((fn.__name__, fn))
    return fn


class Failure(AssertionError):
    pass


def eq(actual, expected, what: str = "") -> None:
    if actual != expected:
        raise Failure(f"{what}: expected {expected!r}, got {actual!r}")


def ok(cond, what: str) -> None:
    if not cond:
        raise Failure(what)


def raises(exc_type, fn, what: str) -> None:
    try:
        fn()
    except exc_type:
        return
    except Exception as other:  # noqa: BLE001
        raise Failure(f"{what}: raised {type(other).__name__} ({other}), want {exc_type.__name__}") from None
    raise Failure(f"{what}: did not raise {exc_type.__name__}")


# ══ 加密 ═════════════════════════════════════════════════════════════
@case
def crypto_roundtrip() -> None:
    from coregeek.infra.crypto import Cipher

    c = Cipher.new("pw", 1000)
    for n in (0, 1, 100, 5000, 200_000):
        pt = ("中文测试🙂" * (n // 5 + 1))[:n].encode("utf-8")
        eq(c.unseal(c.seal(pt).encode()), pt, f"roundtrip n={n}")


@case
def crypto_tamper_every_byte() -> None:
    import base64

    from coregeek.infra.crypto import Cipher, CryptoError

    c = Cipher.new("pw", 1000)
    raw = bytearray(base64.b64decode(c.seal(b"attack at (29,7)")))
    for i in range(len(raw)):
        t = bytearray(raw)
        t[i] ^= 0x01
        raises(CryptoError, lambda t=t: c.unseal(base64.b64encode(bytes(t))), f"tamper byte {i}")


@case
def crypto_rejects_wrong_key_and_header() -> None:
    from coregeek.infra.crypto import Cipher, CryptoError, make_header

    c = Cipher.new("pw", 1000)
    blob = c.seal(b"payload").encode()
    raises(CryptoError, lambda: Cipher("other", c.salt, c.iters).unseal(blob), "wrong passphrase")
    altered = Cipher.from_header("pw", make_header(c.salt, c.iters + 1))
    raises(CryptoError, lambda: altered.unseal(blob), "altered header")


@case
def crypto_nonce_is_per_record() -> None:
    from coregeek.infra.crypto import Cipher

    c = Cipher.new("pw", 1000)
    ok(c.seal(b"same") != c.seal(b"same"), "per-record nonce must differ for identical plaintext")


# ══ 日志 ═════════════════════════════════════════════════════════════
@case
def logstore_roundtrip_and_no_loss() -> None:
    from coregeek.infra.crypto import Cipher
    from coregeek.infra.logstore import LogStore, wait_flushed

    with tempfile.TemporaryDirectory() as d:
        with LogStore(log_dir=d, encrypt=True, iters=1000) as s:
            for r in (1, 2, 85, 131):
                s.submit({"roundNo": r, "note": "中文🙂"})
            wait_flushed(s)
            eq(s.dropped, 0, "dropped")
            eq(s.accepted, 4, "accepted")
            path = s.path
            ok(path is not None and path.is_file(), "log file exists")

        lines = path.read_bytes().split(b"\n")
        cipher = Cipher.from_header(s.passphrase, lines[0])
        recs = [json.loads(cipher.unseal(x)) for x in lines[1:] if x.strip()]
        eq([r["roundNo"] for r in recs], [1, 2, 85, 131], "round numbers preserved in order")


@case
def logstore_unique_filenames() -> None:
    from coregeek.infra.logstore import LogStore, wait_flushed

    with tempfile.TemporaryDirectory() as d:
        paths = []
        for _ in range(3):
            with LogStore(log_dir=d, encrypt=True, iters=1000) as s:
                s.submit({"x": 1})
                wait_flushed(s)
                paths.append(s.path)
        eq(len(set(paths)), 3, f"three stores must not collide within one second: {paths}")


@case
def logstore_plain_mode() -> None:
    from coregeek.infra.logstore import LogStore, wait_flushed

    with tempfile.TemporaryDirectory() as d:
        with LogStore(log_dir=d, encrypt=False) as s:
            s.submit({"roundNo": 7, "plain": True})
            wait_flushed(s)
            path = s.path
        rec = json.loads(path.read_text(encoding="utf-8").split("\n")[0])
        eq(rec["roundNo"], 7, "plain record")


@case
def safe_emit_never_raises() -> None:
    from coregeek.infra.logstore import safe_emit

    safe_emit("中文 / ascii / 🙂")
    safe_emit("")


# ══ 解析 ═════════════════════════════════════════════════════════════
def _sample() -> dict:
    return json.loads((ROOT / "docs" / "request.txt").read_text(encoding="utf-8"))


@case
def model_parses_official_sample() -> None:
    from coregeek.protocol import model

    t = model.load(_sample())
    ok(t is not None, "sample must parse")
    eq(t.round_no, 85, "roundNo")
    eq((t.width, t.height), (41, 32), "map size")
    eq(t.our_type, "challenger", "type")
    eq(len(t.our_roles), 9, "our roles")
    eq(len(t.robots), 4, "robots")
    eq(len(t.player_tasks), 2, "player tasks")
    eq(len(t.mines()), 6, "all 6 mines (two per ore kind)")
    eq(len(t.task_points("challenger")), 3, "challenger task cells: point1(1) + point2(2)")
    eq(t.phase_task, "", "phaseTask")


@case
def model_uses_payload_range_over_table() -> None:
    """✅样例 attackRange=4/7/2147483647 与文档表 3/6/10 矛盾 → 必须 payload 优先。"""
    from coregeek.protocol import model

    t = model.load(_sample())
    got = {r.role_type: r.range_of_attack() for r in t.weapons}
    eq(got, {"gatling": 4, "railgun": 7, "rocket": 2147483647}, "payload range wins")

    # payload 缺失/为 0 时才回落到等级表
    fallback = model.load(
        {
            "roundNo": 1,
            "mapInfo": {"width": 10, "height": 10},
            "teamOur": {
                "roles": [
                    {"id": 1, "pos": {"x": 1, "y": 1}, "roleType": "gatling", "level": 2, "attackRange": 0}
                ]
            },
        }
    )
    eq(fallback.our_roles[0].range_of_attack(), 5, "table fallback gatling L2")


@case
def model_target_slots_railgun_always_one() -> None:
    """接口文档：电磁狙击炮只能攻击一个目标；加特林/火箭 = 当前等级。"""
    from coregeek.protocol import model

    rail = model.Role(id=1, pos=model.Pos(0, 0), role_type="railgun", level=3)
    gat = model.Role(id=2, pos=model.Pos(0, 0), role_type="gatling", level=3)
    rocket = model.Role(id=3, pos=model.Pos(0, 0), role_type="rocket", level=2)
    eq(rail.target_slots(), 1, "railgun L3 must still be 1")
    eq(gat.target_slots(), 3, "gatling L3")
    eq(rocket.target_slots(), 2, "rocket L2")


@case
def model_action_results_use_string_keys() -> None:
    """竞态 #5：用 int 查会全 miss，误判成"所有动作都失败"。"""
    from coregeek.protocol import model

    t = model.load(_sample())
    eq(t.action_ok(10030), False, "str-key lookup")
    eq(t.action_ok(10020), True, "str-key lookup true")
    eq(t.action_ok(99999), None, "missing id -> None, not False")


@case
def model_never_raises_on_garbage() -> None:
    from coregeek.protocol import model

    for bad in (
        None, [], 0, "x", {},
        {"roundNo": 1},                                   # 无 teamOur
        {"roundNo": 1, "teamOur": {}},                     # 无 roles
        {"roundNo": "x", "teamOur": {"roles": [{}]}},      # roundNo 类型错
        {"roundNo": 1, "teamOur": {"roles": [{"id": 1}]}},  # 缺 pos
        {"roundNo": 1, "teamOur": {"roles": "nope"}},
        {"roundNo": 1, "teamOur": {"roles": [{"id": True, "pos": {"x": 0, "y": 0}}]}},
    ):
        model.load(bad)  # 不得抛异常
    ok(model.load({}) is None, "missing required -> None")
    ok(model.load(None) is None, "None -> None")


@case
def model_sparse_payload_yields_anomalies_not_crash() -> None:
    from coregeek.protocol import model

    t = model.load(
        {
            "roundNo": 1,
            "teamOur": {"roles": [{"id": 10010, "pos": {"x": 1, "y": 1}, "roleType": "worker"}]},
            "lastRoundRoleActionResults": {"10010": "yes"},
        }
    )
    ok(t is not None, "sparse turn still parses")
    ok(len(t.anomalies) >= 2, f"expected anomalies recorded, got {t.anomalies}")
    ok(any("width" in a for a in t.anomalies), "map size anomaly recorded")
    ok(any("not bool" in a for a in t.anomalies), "bad action-result type recorded")


@case
def model_reports_documented_sample_gaps() -> None:
    """✅实测的 3 处样例/文档缺口必须在 anomalies 里显式出现。"""
    from coregeek.protocol import model

    t = model.load(_sample())
    joined = " | ".join(t.anomalies)
    ok("targetTeam absent" in joined, "targetTeam gap reported")
    ok("timeoutRounds absent" in joined, "timeoutRounds gap reported")
    ok("cooldown absent" in joined, "cooldown gap reported")


# ══ 几何 ═════════════════════════════════════════════════════════════
#: docs/request.txt 里两个基地的真实坐标（challenger / defender）
OUR_STATION = (10, 24)
ENEMY_STATION = (30, 10)
MAP_W, MAP_H = 41, 32


def _xy(cells) -> set[tuple[int, int]]:
    return {(p.x, p.y) for p in cells}


@case
def geometry_sample_station_yields_4_12_20() -> None:
    """✅以样例基地 (10,24) 锚定：基地 4 格、武器环 12 格、围墙环 20 格。"""
    from coregeek.domain import geometry
    from coregeek.domain.grid import Pos

    b = geometry.box_from_station(Pos(*OUR_STATION), MAP_W, MAP_H, "challenger")
    eq(_xy(b.footprint), {(10, 23), (10, 24), (11, 23), (11, 24)}, "footprint (pos = 左上角)")
    eq(len(b.weapon_ring), 12, "weapon ring")
    eq(len(b.wall_ring), 20, "wall ring")
    eq(b.wall_budget, 19, "wall budget = 20 - 1 门")
    eq(len(b.wall_order()), 19, "wall order length")
    ok(b.wall_budget == len(b.wall_order()), "wall_order must fill the budget exactly")

    # 门必须恰好是"环上被排除的那一格"，且落在背面
    eq(_xy(b.wall_order()), _xy(b.wall_ring) - {(b.door.x, b.door.y)}, "order = ring - door")
    ok(b.door in b.wall_ring_set, "door must be a ring cell")
    ok(b.door in b.sides[b.back], "door must sit on the back side")
    eq(b.front, geometry._opposite(b.back), "back is the opposite of front")

    # 四条边各 6 格（含两端拐角），并集恰为整个环
    for side, cells in b.sides.items():
        eq(len(cells), 6, f"side {side}")
    eq(set().union(*map(set, b.sides.values())), b.wall_ring_set, "sides cover the ring")

    # 全部格子必须在地图内
    for cell in b.wall_ring:
        ok(0 <= cell.x < MAP_W and 0 <= cell.y < MAP_H, f"{cell} off map")


@case
def geometry_enemy_station_matches_sample_wall() -> None:
    """✅样例敌方的真实围墙 (28,7) 必须落在敌方基地 (30,10) 的围墙环上。

    这条是**用真实数据**（而不是 demo 代码）交叉验证几何口径的独立证据。
    """
    from coregeek.domain import geometry
    from coregeek.domain.grid import Pos

    b = geometry.box_from_station(Pos(*ENEMY_STATION), MAP_W, MAP_H, "defender")
    eq(len(b.footprint), 4, "enemy footprint")
    eq(len(b.weapon_ring), 12, "enemy weapon ring")
    eq(len(b.wall_ring), 20, "enemy wall ring")
    ok(Pos(28, 7) in b.wall_ring_set, "sample enemy wall (28,7) must be on the ring")


@case
def geometry_our_weapons_sit_on_the_weapon_ring() -> None:
    """✅样例里已有的 3 座武器恰好都在武器环上 → 独立佐证 12 格口径。"""
    from coregeek.domain import geometry
    from coregeek.domain.grid import Pos

    b = geometry.box_from_station(Pos(*OUR_STATION), MAP_W, MAP_H, "challenger")
    ring_xy = _xy(b.weapon_ring)
    for raw in ((9, 24), (10, 25), (9, 25)):
        ok(raw in ring_xy, f"sample weapon {raw} must be on the weapon ring")
    ok(not (_xy(b.weapon_ring) & _xy(b.wall_ring)), "weapon ring and wall ring must be disjoint")


@case
def geometry_matches_official_demo() -> None:
    """交叉验证：直接跑官方 demo 的几何函数，逐格比对。缺 demo 时自动跳过。"""
    demo_src = ROOT / "example" / "CoreGeek" / "CoreGeek" / "src"
    if not demo_src.is_dir():
        print("      (skipped: official demo not present)")
        return

    from coregeek.domain import geometry
    from coregeek.domain.grid import Pos

    sys.path.insert(0, str(demo_src))
    try:
        from agent import brain as demo_brain
        from agent import protocol as demo_proto

        turn = demo_proto.Turn.load(_sample())
        station = turn.station()
        demo_fp = _xy(demo_proto.station_footprint(station.pos))
        demo_ring = {(p.x, p.y) for p in demo_brain._cells_at_distance(station.pos, 1)}
        demo_walls = {(p.x, p.y) for p in demo_brain._wall_order(turn)}
    finally:
        sys.path.remove(str(demo_src))

    b = geometry.box_from_station(Pos(*OUR_STATION), MAP_W, MAP_H, "challenger")
    eq(_xy(b.footprint), demo_fp, "footprint vs demo station_footprint")
    eq(_xy(b.weapon_ring), demo_ring, "weapon ring vs demo _cells_at_distance")

    # demo 的 _wall_order 是"环 − 门"；门由它自己选定，因此比的是"排除集恰好一格"
    ring = _xy(b.wall_ring)
    eq(ring - demo_walls, {(13, 22)}, "demo builds ring minus exactly its entrance (13,22)")
    eq(len(demo_walls), 19, "demo wall count")
    eq(ring - _xy(b.wall_order()), {(b.door.x, b.door.y)}, "our order excludes exactly our door")


@case
def geometry_front_is_configurable() -> None:
    from coregeek.domain import geometry
    from coregeek.domain.grid import Pos

    geometry.FORCED_FRONT_SIDE["challenger"] = "-y"
    try:
        b = geometry.box_from_station(Pos(*OUR_STATION), MAP_W, MAP_H, "challenger")
        eq(b.front, "-y", "forced front")
        eq(b.back, "+y", "back follows front")
        ok(b.door in b.sides["+y"], "door follows the new back")
        eq(len(b.wall_order()), 19, "geometry unchanged by door placement")
    finally:
        geometry.FORCED_FRONT_SIDE.clear()


# ══ 日历 ═════════════════════════════════════════════════════════════
@case
def calendar_day_night_boundaries() -> None:
    from coregeek.domain import calendar as cal

    eq([cal.within(r) for r in (1, 70, 71, 130, 131, 200, 1300)],
       [1, 70, 71, 130, 1, 70, 130], "within()")
    eq([cal.is_day(r) for r in (1, 70, 71, 130, 131)],
       [True, True, False, False, True], "day/night at boundaries")
    eq(cal.is_night(85), True, "✅样例 roundNo=85 是夜晚（有机器人 → 交叉验证）")
    eq([cal.day_index(r) for r in (1, 130, 131, 1300)], [1, 1, 2, 10], "day index")

    # 半场：取模口径 → roundNo 重置或连续，答案都一样
    eq(cal.rounds_left_in_half(1), 1300, "half length")
    eq(cal.rounds_left_in_half(1300), 1, "last round of the half")
    eq(cal.rounds_left_in_half(1301), 1300, "roundNo continues → half restarts")
    eq(cal.rounds_left_in_half(2600), 1, "end of the second half")
    eq([cal.within(r) for r in (1, 1301)], [1, 1], "within() identical across halves")
    eq([cal.days_remaining_in_half(r) for r in (1, 131, 1300)], [10, 9, 1], "days left")


# ══ 寻路 ═════════════════════════════════════════════════════════════
@case
def path_uses_chebyshev_cost() -> None:
    from coregeek.domain.grid import Pos
    from coregeek.domain.path import Board, distance_field

    b = Board(10, 10, frozenset())
    f = distance_field(b, Pos(0, 0))
    eq(len(f), 100, "open board fully reachable")
    eq(f[Pos(5, 5)], 5, "diagonal costs 1 → Chebyshev")
    eq(f[Pos(3, 7)], 7, "dy dominates")
    eq(f[Pos(9, 0)], 9, "dx dominates")


@case
def path_allows_diagonal_gap() -> None:
    """官方 demo 的 next_step 不检查切角 → 斜穿两个相邻障碍必须允许。"""
    from coregeek.domain.grid import Pos
    from coregeek.domain.path import Board, distance_field

    b = Board(3, 3, frozenset({Pos(1, 0), Pos(0, 1)}))
    f = distance_field(b, Pos(0, 0))
    eq(f.get(Pos(1, 1)), 1, "diagonal squeeze must be walkable")
    eq(f.get(Pos(2, 2)), 2, "and must reach the far corner")


@case
def path_goes_around_walls() -> None:
    from coregeek.domain.grid import Pos
    from coregeek.domain.path import Board, distance_field, path_to

    wall = frozenset(Pos(x, 6) for x in range(0, 9))
    b = Board(12, 12, wall)
    start, goal = Pos(1, 1), Pos(10, 10)
    p = path_to(b, start, goal)

    ok(p and p[-1] == goal, f"path must reach the goal, got {p}")
    ok(all(b.land(c) for c in p), "every step must be walkable")
    prev = start
    for c in p:
        eq(prev.distance(c), 1, f"steps must be 8-adjacent: {prev} -> {c}")
        prev = c
    eq(len(p), distance_field(b, start)[goal], "path_to must be a shortest path")

    # 直连被挡住 → 必须往右绕（x>=9 处通过 y=6）
    ok(not any(c.y == 6 and c.x < 9 for c in p), "must not walk through the wall")
    ok(max(c.x for c in p if c.y == 6) >= 9 if any(c.y == 6 for c in p) else True, "detour")


@case
def path_unreachable_returns_none() -> None:
    from coregeek.domain.grid import Pos
    from coregeek.domain.path import Board, distance_field, next_step_toward

    # 用墙把左上角整个封死
    wall = frozenset({Pos(x, 3) for x in range(0, 5)} | {Pos(3, y) for y in range(0, 4)})
    b = Board(8, 8, wall)
    f = distance_field(b, Pos(0, 0))
    eq(f.get(Pos(7, 7)), None, "pocket must be sealed")
    eq(next_step_toward(b, Pos(0, 0), Pos(7, 7)), None, "no step toward an unreachable goal")


@case
def path_start_may_itself_be_blocked() -> None:
    """单位被围死时必须仍能出发，否则会被误判成卡死而反复重发指令。"""
    from coregeek.domain.grid import Pos
    from coregeek.domain.path import Board, distance_field, next_step_toward

    b = Board(5, 5, frozenset(Pos(2, 2).neighbours()))
    f = distance_field(b, Pos(2, 2))
    eq(len(f), 1, "a sealed unit still gets a field containing itself")
    eq(next_step_toward(b, Pos(2, 2), Pos(4, 4)), None, "sealed in → no move")


@case
def path_approaches_unwalkable_target() -> None:
    """矿区/商店/任务点都不可通行 → 必须走到相邻格再动作（demo 的 land() 亦如此）。"""
    from coregeek.domain.grid import Pos
    from coregeek.domain.path import Board, best_stand, distance_field, next_step_toward

    mine = Pos(5, 5)
    b = Board(10, 10, frozenset({mine}))
    ok(not b.land(mine), "mine cell itself is not walkable")

    f = distance_field(b, Pos(0, 0))
    stand = best_stand(b, f, mine)
    ok(stand is not None and stand.distance(mine) == 1 and b.land(stand), f"stand {stand}")

    step = next_step_toward(b, Pos(0, 0), mine)
    ok(step is not None, "must issue a move")
    eq(step.distance(Pos(0, 0)), 1, "the step must be adjacent to the START")
    eq(f[step], 1, "and be on a shortest path")

    # 贴着矿格 → 不再移动，由调用方发 collect
    eq(next_step_toward(b, Pos(4, 5), mine), None, "already adjacent")
    eq(next_step_toward(b, Pos(4, 4), mine), None, "diagonally adjacent counts")
    # 差一格 → 必须真的迈出那一步
    eq(next_step_toward(b, Pos(3, 4), mine), Pos(4, 4), "one step out")


@case
def path_next_step_is_adjacent_to_start_not_goal() -> None:
    """回归：早先的实现返回的是路径**最后**一步（贴着 goal 的那格）。"""
    from coregeek.domain.grid import Pos
    from coregeek.domain.path import Board, next_step_toward

    b = Board(20, 20, frozenset())
    start, goal = Pos(0, 0), Pos(15, 15)
    step = next_step_toward(b, start, goal)
    eq(step, Pos(1, 1), "first step of the (0,0)->(15,15) diagonal")


# ══ 指令编码与校验 ═══════════════════════════════════════════════════
from coregeek.domain import intent as I  # noqa: E402
from coregeek.domain.grid import Pos  # noqa: E402
from coregeek.protocol import model  # noqa: E402


def _synthetic_turn(
    round_no: int = 85,
    roles: list[dict] | None = None,
    *,
    shop: list[dict] | None = None,
    our_type: str = "challenger",
):
    """最小可用回合。默认含一个 L2 加特林 + 一个工人 + 一个开拓者。"""
    if roles is None:
        roles = [
            _role(10013, "station", 10, 24, level=1),
            _role(10020, "gatling", 9, 24, level=2, attack_range=4),
            _role(10030, "railgun", 10, 25, level=1, attack_range=7),
            _role(10010, "worker", 5, 23),
            _role(10011, "pioneer", 10, 12),
        ]
    return model.load(
        {
            "roundNo": round_no,
            "mapInfo": {"width": 41, "height": 32},
            "teamOur": {
                "type": our_type,
                "teamId": "6324",
                "goldNum": 100,
                "roles": roles,
            },
            "weaponShopList": shop if shop is not None else [],
        }
    )


def _role(rid: int, kind: str, x: int, y: int, *, level: int = 1, attack_range: int = 0) -> dict:
    return {
        "id": rid,
        "pos": {"x": x, "y": y},
        "roleType": kind,
        "health": 1000,
        "level": level,
        "attackRange": attack_range,
    }


def _encode(intents, turn):
    from coregeek.protocol import commands

    return commands.encode_all(intents, turn)


def _non_json_leaves(node, path: str = "") -> list[str]:
    """找出树里**不是** JSON 原生类型的叶子（例如漏出来的 `Pos` 对象）。

    这类残留只有 `json.dumps` 真正执行时才会暴露，而线上的暴露方式是一条
    "响应格式错"——正是红线 ②。所以要在本地就把它挡下来。
    """
    if isinstance(node, dict):
        return [x for k, v in node.items() for x in _non_json_leaves(v, f"{path}.{k}")]
    if isinstance(node, list):
        return [x for i, v in enumerate(node) for x in _non_json_leaves(v, f"{path}[{i}]")]
    if node is None or isinstance(node, (str, int, float, bool)):
        return []
    return [f"{path or '<root>'}: {type(node).__name__}"]


@case
def commands_never_emit_a_command_the_validator_rejects() -> None:
    """构造一批**故意非法**的意图，断言全部被拦下、且一条都没上线路。

    这是红线 ③（指令非法）的直接守门测试：宁可全丢，也不许漏一条。
    """
    turn = _synthetic_turn()
    bad = [
        I.Move(role_id=10010, dest=Pos(1, 1)),                       # 合法 → 应通过
        I.Move(role_id=99999, dest=Pos(1, 1)),                       # 角色不存在
        I.Build(role_id=10010, target=Pos(1, 1), name="laser"),      # 建筑名非法
        I.Attack(role_id=10010, weapon_id=10020, targets=(Pos(1, 1),)),  # 工人不能操控武器
        I.Attack(role_id=10011, weapon_id=10020, targets=(Pos(1, 1),)),  # 白天攻击
        I.AcceptTask(role_id=10010),                                 # 非开拓者接任务
        I.Sell(role_id=10010, name="stone", num=True),               # num 是 bool
        I.Sell(role_id=10010, name="stone", num=0),                  # num 越界
        I.Use(role_id=10010, name="WeaponUpgradeVoucher1"),          # 升级券缺 targetPos
        I.SummonTreasure(role_id=10011, target=Pos(1, 1), items=()),  # item 为空
        I.SubmitAnswer(role_id=10011, answer=""),                    # 空答案
        I.Attack(role_id=10010, weapon_id=10010, targets=(Pos(1, 1),)),  # key 不是武器
    ]
    result = _encode(bad, turn)

    eq(set(result.commands), {"10010"}, "只有唯一那条合法指令能出去")
    eq(result.commands["10010"]["action"], "move", "合法指令内容")
    eq(len(result.rejected), len(bad) - 1, f"其余全部被拒：{result.rejected}")


@case
def commands_attack_target_count_follows_weapon_level() -> None:
    """接口文档 §2.2：加特林/火箭落点数 = 当前等级；电磁狙击炮恒为 1。"""
    turn = _synthetic_turn()
    gat = Pos(9, 24)

    # L2 加特林 + 2 个同向落点（都在东侧，夹角 0°）→ 合法
    ok_res = _encode(
        [I.Attack(role_id=10011, weapon_id=10020, targets=(Pos(11, 24), Pos(12, 24)))], turn
    )
    eq(ok_res.commands["10020"]["targetPos"], [{"x": 11, "y": 24}, {"x": 12, "y": 24}], "两个落点")
    eq(ok_res.commands["10020"]["controllerId"], "10011", "controllerId 是字符串形式的角色 ID")

    # L2 加特林只给 1 个落点 → 长度不符，丢弃
    one = _encode([I.Attack(role_id=10011, weapon_id=10020, targets=(Pos(11, 24),))], turn)
    eq(one.commands, {}, "落点数必须等于武器等级")
    ok(one.rejected and "长度" in one.rejected[0][1], f"理由应提到长度：{one.rejected}")

    # 电磁狙击炮 L1 → 恰好 1 个；给 2 个应被拒
    rail_ok = _encode([I.Attack(role_id=10011, weapon_id=10030, targets=(Pos(10, 30),))], turn)
    eq(list(rail_ok.commands), ["10030"], "电磁狙击炮 1 个落点合法")
    rail_bad = _encode(
        [I.Attack(role_id=10011, weapon_id=10030, targets=(Pos(10, 30), Pos(11, 30)))], turn
    )
    eq(rail_bad.commands, {}, "电磁狙击炮不接受 2 个落点")

    # 落点必须相对武器在 90° 锥内：东与西夹角 180° → 整次非法
    cone = _encode(
        [I.Attack(role_id=10011, weapon_id=10020, targets=(Pos(11, 24), Pos(7, 24)))], turn
    )
    eq(cone.commands, {}, f"夹角 180° 必须拒绝：{cone.rejected}")
    ok(cone.rejected and "夹角" in cone.rejected[0][1], "理由应提到夹角")

    # 90° 以内可以：东与东北（45°）
    fine = _encode(
        [I.Attack(role_id=10011, weapon_id=10020, targets=(Pos(11, 24), Pos(11, 22)))], turn
    )
    eq(list(fine.commands), ["10020"], "45° 夹角合法")


@case
def commands_attack_is_rejected_in_daytime() -> None:
    """接口文档 §2.3：attack 仅夜晚可用，白天使用非法。"""
    day = _synthetic_turn(round_no=30)          # within=30 ≤ 70 → 白天
    assert_day = _encode(
        [I.Attack(role_id=10011, weapon_id=10030, targets=(Pos(10, 30),))], day
    )
    eq(assert_day.commands, {}, "白天不许攻击")
    ok(assert_day.rejected and "白天" in assert_day.rejected[0][1], f"{assert_day.rejected}")

    night = _synthetic_turn(round_no=85)        # within=85 > 70 → 夜晚
    eq(list(_encode([I.Attack(role_id=10011, weapon_id=10030, targets=(Pos(10, 30),))], night).commands),
       ["10030"], "夜晚可以攻击")


@case
def commands_one_command_per_role() -> None:
    """每个角色每回合至多 1 条指令；key 必须是**字符串**。"""
    turn = _synthetic_turn()
    r = _encode(
        [I.Move(role_id=10010, dest=Pos(1, 1)), I.Collect(role_id=10010, target=Pos(4, 24))],
        turn,
    )
    eq(len(r.commands), 1, "同一角色只留第一条")
    eq(list(r.commands), ["10010"], "key 是字符串")
    ok(all(isinstance(k, str) for k in r.commands), "key 必须是字符串（int 查表会全 miss）")


@case
def commands_build_names_and_shop_whitelist() -> None:
    turn = _synthetic_turn(shop=[{"name": "WeaponUpgradeVoucher1", "price": 100}])

    wall = _encode([I.Build(role_id=10010, target=Pos(12, 22), name="wall")], turn)
    eq(wall.commands["10010"]["name"], "wall", "建墙合法")

    for bad_name in ("stone", "tower", ""):
        r = _encode([I.Build(role_id=10010, target=Pos(12, 22), name=bad_name)], turn)
        eq(r.commands, {}, f"非法建筑名 {bad_name!r} 必须被拒")

    # 商店清单存在时，买清单外的物品应被拒；清单缺失时不拦（无从判断）
    r = _encode([I.Buy(role_id=10010, name="Nope", num=1)], turn)
    eq(r.commands, {}, "清单内没有的物品必须被拒")
    eq(list(_encode([I.Buy(role_id=10010, name="WeaponUpgradeVoucher1", num=1)], turn).commands),
       ["10010"], "清单内的物品可以买")

    blind = _encode([I.Buy(role_id=10010, name="Anything", num=1)], _synthetic_turn())
    eq(list(blind.commands), ["10010"], "拿不到商店清单时不拦（指令失败不计异常）")


@case
def commands_encode_result_shape_is_wire_ready() -> None:
    """产物必须能直接 json.dumps 进 roleCommandMap（无 Pos 对象残留）。"""
    turn = _synthetic_turn()
    r = _encode(
        [
            I.Move(role_id=10010, dest=Pos(2, 3)),
            I.Collect(role_id=10011, target=Pos(4, 24)),
        ],
        turn,
    )
    text = json.dumps(r.commands, ensure_ascii=False)
    back = json.loads(text)
    eq(back["10010"], {"action": "move", "targetPos": [{"x": 2, "y": 3}]}, "move 报文形状")
    eq(back["10011"], {"action": "collect", "targetPos": [{"x": 4, "y": 24}]}, "collect 报文形状")
    # 深查"有没有 Pos 对象混进报文"。（别用 `"Pos" in text`——`targetPos` 里就有这个子串）
    ok(not _non_json_leaves(back), f"报文里不得残留非 JSON 叶子：{_non_json_leaves(back)}")


@case
def commands_idle_is_not_a_command() -> None:
    """Idle 只进日志，不上线路——否则会变成一条 action 非法的指令。"""
    turn = _synthetic_turn()
    r = _encode([I.Idle(role_id=10010, reason="没有可做的事")], turn)
    eq(r.commands, {}, "Idle 不产生指令")
    eq(len(r.warnings), 1, "Idle 记一条 warning")


@case
def commands_toplevel_three_fields_always_present() -> None:
    """红线 ②：三个顶层字段永远都在。demo 只发了 roleCommandMap 一个。"""
    from coregeek.app import App

    app = App(log_dir=str(ROOT / ".tmp-logs"), enable_log=False)
    for label, raw in (
        ("官方样例", json.dumps(_sample(), ensure_ascii=False).encode("utf-8")),
        ("垃圾输入", b"\xff\xfe not json"),
        ("空", b""),
        ("非对象", b"[1,2,3]"),
    ):
        doc = json.loads(app.handle(raw).decode("utf-8"))
        for field in ("roleCommandMap", "prompt", "executeCmd"):
            ok(field in doc, f"{label}: 缺顶层字段 {field}")
        ok(isinstance(doc["roleCommandMap"], dict), f"{label}: roleCommandMap 必须是对象")
        ok(doc["prompt"] == "", f"{label}: prompt 应为空字符串")
        ok(doc["executeCmd"] == "", f"{label}: executeCmd 应为空字符串")


# ══ 分层 import-lint ═════════════════════════════════════════════════
LAYER_FORBIDDEN: tuple[tuple[str, tuple[str, ...]], ...] = (
    # domain 只依赖 infra；不碰 transport/protocol，也不许自己落盘
    ("coregeek.domain", ("coregeek.transport", "coregeek.protocol", "coregeek.infra.logstore")),
    ("coregeek.protocol", ("coregeek.transport",)),
    ("coregeek.infra", ("coregeek.domain", "coregeek.protocol", "coregeek.transport")),
)


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(["coregeek"] + parts) if parts else "coregeek"


def _absolute(pkg_of_module: str, is_pkg: bool, level: int, module: str | None) -> str:
    pkg = pkg_of_module if is_pkg else pkg_of_module.rsplit(".", 1)[0]
    pkg_parts = pkg.split(".") if pkg else []
    if level == 0:
        return module or ""
    base = pkg_parts[: len(pkg_parts) - (level - 1)]
    return ".".join(base + ([module] if module else []))


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    mod = _module_name(path)
    is_pkg = path.name == "__init__.py"
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                found.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            target = _absolute(mod, is_pkg, node.level, node.module)
            if not target:
                continue
            found.add(target)
            for a in node.names:
                found.add(f"{target}.{a.name}")
    return found


@case
def layering_import_lint() -> None:
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        mod = _module_name(path)
        imported = _imports(path)
        for owner, forbidden in LAYER_FORBIDDEN:
            if mod != owner and not mod.startswith(owner + "."):
                continue
            for imp in imported:
                for bad in forbidden:
                    if imp == bad or imp.startswith(bad + "."):
                        violations.append(f"{mod}  ->  {imp}   (违反 {owner} 不得依赖 {bad})")
    ok(not violations, "layering violations:\n    " + "\n    ".join(violations))


@case
def src_compiles() -> None:
    import compileall

    ok(
        compileall.compile_dir(str(SRC), quiet=1, force=True),
        "src/ failed to compile",
    )


# ══ 经济 / 调度（第 5 步） ═══════════════════════════════════════════
# 全部用例都从**官方样例** docs/request.txt 出发改字段，而不是手搓 Turn 对象：
# 样例是唯一一份真实 payload，手搓的字段名/形状随时可能和判题器不一致，
# 那样测出来的"通过"没有任何意义。
SAMPLE = ROOT / "docs" / "request.txt"


def _sample_payload() -> dict:
    import io

    return json.loads(io.open(SAMPLE, encoding="utf-8").read())


def _payload(*, round_no: int = 30, gold: int | None = None, place: dict | None = None,
             backpack: dict | None = None) -> dict:
    """样例副本 + 定点修改。`place`/`backpack` 的 key 是角色 id。"""
    p = _sample_payload()
    p["roundNo"] = round_no
    if gold is not None:
        p["teamOur"]["goldNum"] = gold
    for r in p["teamOur"]["roles"]:
        if place and r["id"] in place:
            x, y = place[r["id"]]
            r["pos"] = {"x": x, "y": y}
        if backpack and r["id"] in backpack:
            r["backpack"] = list(backpack[r["id"]])
    return p


def _turn(**kw):
    from coregeek.protocol import model

    t = model.load(_payload(**kw))
    assert t is not None
    return t


def _only(intents, role_id: int):
    got = [i for i in intents if i.role_id == role_id]
    eq(len(got), 1, f"role {role_id} should get exactly one intent")
    return got[0]


@case
def economy_worker_builds_the_highest_priority_wall_gap() -> None:
    """带石头的筑墙手站在缺口旁 → 必须产出 `build wall`，且是 wall_order 的第一格。"""
    from coregeek.domain import planner
    from coregeek.domain.world import box_of, wall_gaps
    from coregeek.protocol import model

    t = _turn(place={10010: (14, 21)}, backpack={10010: ["stone", "stone"]})
    box = box_of(t)
    gap = wall_gaps(t, box)[0]
    eq(gap.x, 13, "样例基地的正面是 +x，第一个缺口应落在 x=13")
    # 站到缺口旁边
    t = _turn(place={10010: (14, gap.y)}, backpack={10010: ["stone", "stone"]})
    intent = _only(planner.plan(t, planner.PlanMemory()), 10010)
    eq(type(intent).__name__, "Build", "缺石料已备齐时应建墙")
    eq(intent.name, "wall", "建造名必须是线上的 'wall'")
    eq(intent.target, gap, "应建在 wall_order 的第一格")
    eq(model.load(_payload()) is not None, True, "sanity")


@case
def economy_worker_without_stone_goes_to_a_stone_mine() -> None:
    """没有石料的筑墙手 → 去石矿，且不会跑去挖铜（那是经济手的活）。"""
    from coregeek.domain import planner

    t = _turn(place={10010: (5, 23)}, backpack={10010: ["iron"]})
    intent = _only(planner.plan(t, planner.PlanMemory()), 10010)
    eq(type(intent).__name__, "Collect", "缺石料时应去采集")
    eq(t.mine_kind(intent.target), "stone", "缺石料时目标必须是石矿")


@case
def economy_sells_the_most_valuable_ore_first_and_keeps_wall_stone() -> None:
    """站在小贩旁边清仓：先卖铜（5 金），铁次之，**建墙用的石头一块不卖**。"""
    from coregeek.domain import planner
    from coregeek.domain.economy import sell_plan

    # 背包要**真的装满**（100 格容量的 85% = 85 件）才会触发清仓 ——
    # 背 9 件就跑去小贩那儿是浪费，策略会（正确地）继续挖矿。
    bag = ["copper"] * 90 + ["iron"] * 2 + ["stone"] * 4
    # ⚠️ 必须站在小贩**旁边**而不是**上面**：(20,16) 是小贩自己占的格，
    #    中立单位不可通行（任务书 L85）。早先把角色放在格子本身，
    #    `_step` 判 `here == target` 直接 Idle —— 那是测试摆错了位置，不是策略错。
    t = _turn(place={10012: (20, 17)}, backpack={10012: bag})
    intent = _only(planner.plan(t, planner.PlanMemory()), 10012)
    eq(type(intent).__name__, "Sell", "满载 + 站在小贩旁应清仓")
    eq(intent.name, "copper", "铜单价最高，必须先卖")
    eq(intent.num, 90, "应一次卖光全部铜")

    # 石头保留量由剩余缺口决定：这一格是"别把建墙料当废铁卖"的唯一防线
    plan = sell_plan(t, {"copper": 90, "iron": 2, "stone": 4}, stone_keep=22)
    eq([n for n, _ in plan], ["copper", "iron"], "贩卖顺序必须按单价降序")
    ok(
        "stone" not in [n for n, _ in plan],
        "石头必须为建墙留下（4 块 < 需保留 22 块）",
    )
    # 反过来：石头多到超出保留量时，多出来的必须卖掉，不能烂在背包里
    plan2 = sell_plan(t, {"stone": 30}, stone_keep=22)
    eq(plan2, [("stone", 8)], "超出保留量的 8 块石头应当卖掉")


@case
def economy_pioneer_buys_the_weapon_upgrade_voucher() -> None:
    """金币够 100 且武器还都是 L1 → 买武器升级券，而不是围墙券（20 金那种小件）。"""
    from coregeek.domain import planner

    t = _turn(round_no=30, gold=120, place={10011: (25, 21)}, backpack={10011: []})
    intent = _only(planner.plan(t, planner.PlanMemory()), 10011)
    eq(type(intent).__name__, "Buy", "金币够且站在商店旁应采购")
    eq(intent.name, "WeaponUpgradeVoucher1", "③号优先级：武器 L1→L2")
    eq(intent.num, 1, "一次买一张")


@case
def economy_pioneer_never_stacks_a_second_copy_of_one_voucher() -> None:
    """背包里已有一张券 → 不能再买第二张（券不叠放，买了纯浪费金币）。"""
    from coregeek.domain.economy import purchase_wish

    t = _turn(gold=300, backpack={10011: ["WeaponUpgradeVoucher1"]})
    wish = purchase_wish(t)
    ok(wish is None or wish.item != "WeaponUpgradeVoucher1", f"重复买了同一张券: {wish}")


@case
def economy_pioneer_uses_a_voucher_before_buying_more() -> None:
    """背包里的券是**已经花掉的钱** → 必须先就近用掉，再去补货。"""
    from coregeek.domain import planner

    t = _turn(gold=300, place={10011: (9, 26)}, backpack={10011: ["WeaponUpgradeVoucher1"]})
    intent = _only(planner.plan(t, planner.PlanMemory()), 10011)
    eq(type(intent).__name__, "Use", "手上有券就该先用")
    eq(intent.target, t.our_id[10040].pos, "一级券优先升火箭（伤害与落点数同时涨）")


@case
def planner_keeps_exactly_one_fortifier_and_sticks_to_it() -> None:
    """分工必须**恰好一个**筑墙手，且跨回合粘住（两个工人不会同时去砌墙）。

    ⚠️ 这里直接读 `PlanMemory.jobs`，而不是从意图**反推**岗位。
    早先试过反推（Build→筑墙、Buy/Use→军需），但角色离目标远时产出的都是
    `Move` —— 反推函数只能一律判成"经济手"，于是三种岗位全被认成同一个，
    测试就变成了永远为假的断言。意图可以观测，**岗位本身就是内部分工**，
    只能直接读。
    """
    from coregeek.domain import planner

    mem = planner.PlanMemory()
    t = _turn(place={10010: (5, 23), 10012: (10, 16)})
    planner.plan(t, mem)
    jobs = dict(mem.jobs)
    eq(len(jobs), 3, f"三名存活角色都该有岗位：{jobs}")
    eq(list(jobs.values()).count(planner.JOB_FORTIFY), 1, f"筑墙手数量必须是 1：{jobs}")
    eq(list(jobs.values()).count(planner.JOB_PIONEER), 1, f"开拓者岗位必须是 1：{jobs}")

    fortifier = [rid for rid, j in jobs.items() if j == planner.JOB_FORTIFY][0]
    for _ in range(5):  # 连续推演，岗位不得漂移
        planner.plan(t, mem)
    eq(mem.jobs.get(fortifier), planner.JOB_FORTIFY, "筑墙手的岗位必须粘住")

    # 行为面：把筑墙手挪到缺口旁，它必须真的开始砌墙
    from coregeek.domain.world import box_of, wall_gaps

    box = box_of(t)
    gap = wall_gaps(t, box)[0]
    t2 = _turn(place={10010: (gap.x + 1, gap.y)}, backpack={10010: ["stone"]})
    intent = _only(planner.plan(t2, mem), fortifier)
    eq(type(intent).__name__, "Build", f"筑墙手到位后应建墙，实际 {intent}")


@case
def planner_retreats_by_actual_walk_length_not_a_fixed_clock() -> None:
    """回防按**真实步数**触发：近距离的人继续干活，远距离的人提前撤。"""
    from coregeek.domain import planner

    # 工人 (5,23) 离门 (8,24) 只有 3 步 —— 白天第 65 回合（还剩 6 回合）时不该撤
    near = _turn(round_no=65, place={10010: (5, 23)})
    intent = _only(planner.plan(near, planner.PlanMemory()), 10010)
    ok("回防" not in intent.reason, f"近处角色过早回防：{intent.reason}")

    # 开拓者 (10,12) 离门 12 步 —— 同一回合必须已经在往门口收
    far = _turn(round_no=65, place={10011: (10, 12)})
    intent = _only(planner.plan(far, planner.PlanMemory()), 10011)
    ok("回防" in intent.reason, f"远处角色应当回防：{intent.reason}")

    # 白天最后一回合：无论远近一律收队
    tail = _turn(round_no=70, place={10010: (5, 23)})
    intent = _only(planner.plan(tail, planner.PlanMemory()), 10010)
    ok("回防" in intent.reason, f"白天尾巴应无条件回防：{intent.reason}")


@case
def planner_night_gives_each_role_a_distinct_weapon() -> None:
    """夜战：三名角色 ↔ 三座武器，**互不重复**，且配对跨回合粘住。"""
    from coregeek.domain import planner

    mem = planner.PlanMemory()
    t = _turn(round_no=71)
    planner.plan(t, mem)
    holders = mem.assignments.holders
    eq(len(holders), 3, f"三名可控角色都该认领武器：{holders}")
    eq(len(set(holders.values())), 3, f"一座武器不能被两人同时认领：{holders}")

    first = dict(holders)
    planner.plan(t, mem)
    eq(mem.assignments.holders, first, "配对必须跨回合粘住（否则整晚都在换位）")


@case
def planner_one_round_produces_build_sell_and_buy_together() -> None:
    """**第 5 步的验收用例**：一个白天回合同时产出建墙 / 贩卖 / 采购三条指令。

    三个人各就各位：筑墙手贴着缺口、经济手贴着小贩、军需官贴着武器商店。
    这是"经济线真的接通了"的最小证据 —— 少了任何一条，说明对应分支没落地。
    """
    from coregeek.domain import planner
    from coregeek.domain.world import box_of, wall_gaps
    from coregeek.protocol import commands

    probe = _turn()
    box = box_of(probe)
    gap = wall_gaps(probe, box)[0]

    t = _turn(
        round_no=30,
        gold=120,
        place={10010: (gap.x + 1, gap.y), 10012: (20, 17), 10011: (25, 21)},
        backpack={
            10010: ["stone", "stone"],
            10012: ["copper"] * 90,  # ≥ 背包 85% → 触发清仓
            10011: [],
        },
    )
    result = commands.encode_all(planner.plan(t, planner.PlanMemory()), t)
    eq(result.rejected, [], f"验收回合不该有被拒指令：{result.rejected}")

    actions = {c["action"] for c in result.commands.values()}
    eq(actions, {"build", "sell", "buy"}, f"三条经济线必须同时产出，实际 {actions}")

    by_action = {c["action"]: c for c in result.commands.values()}
    eq(by_action["build"]["name"], "wall", "建墙指令的 name 必须是 'wall'")
    eq(by_action["sell"]["name"], "copper", "先卖单价最高的铜")
    eq(by_action["buy"]["name"], "WeaponUpgradeVoucher1", "金币 120 应买武器一级升级券")


@case
def planner_whole_day_and_night_emit_only_valid_commands() -> None:
    """把样本世界逐回合过一遍编码器（白天 + 夜晚共 130 回合），`rejected` 必须为空。

    世界是**静止快照**（每回合都是同一份 payload），所以动作种类不会多 ——
    这个用例只证明一件事：**调度产出的每一条指令都合法**。
    动作覆盖由上面那个验收用例负责。
    """
    from coregeek.domain import planner
    from coregeek.protocol import commands, model

    mem = planner.PlanMemory()
    bad: list[str] = []
    for rn in range(1, 131):
        turn = model.load(_payload(round_no=rn, gold=100 + rn))
        assert turn is not None
        result = commands.encode_all(planner.plan(turn, mem), turn)
        for why in result.rejected:
            bad.append(f"round {rn}: {why}")

    ok(not bad, "编码器拒绝了调度产出的指令：\n    " + "\n    ".join(bad[:8]))


@case
def planner_new_match_clears_sticky_state() -> None:
    """换边 / 新一场必须**整体作废**粘性状态 —— 否则上一场的岗位会安到新角色 id 上。

    ⚠️ 断言的是 `sessions_seen` 计数器，不是"jobs 为空"：
    `reset_match()` 之后同一个回合就会重新分配岗位，jobs 立刻又被填满，
    "清空过"这件事只在计数器上看得见。

    两条信号都要覆盖（`roundNo` 在下半场是否重置**任务书没说**，所以两条都实现了）：
      · 换 `teamId`/`type` —— 换边最可靠的信号；
      · `roundNo` 倒退回 1 而 `type` 没变 —— 兜底信号，正是 code-design.md §15.2
        那个"静默归零整半场"的坑。
    """
    from coregeek.app import App

    app = App(enable_log=False)
    app.handle(json.dumps(_payload(round_no=30)).encode("utf-8"))
    eq(app.sessions_seen, 1, "首发 payload 建立第一场会话")
    ok(app.memory.jobs, "正常回合后应留下岗位分工")

    # 信号一：同一 roundNo 但换了 teamId/type
    p2 = _payload(round_no=30)
    p2["teamOur"]["teamId"] = "something-else"
    p2["teamOur"]["type"] = "defender"
    app.handle(json.dumps(p2).encode("utf-8"))
    eq(app.sessions_seen, 2, "换边必须作废粘性状态")

    # 信号二：type 完全不变，只是 roundNo 归 1（下半场 roundNo 重置的语义）
    p3 = _payload(round_no=1)
    p3["teamOur"]["teamId"] = "something-else"
    p3["teamOur"]["type"] = "defender"
    app.handle(json.dumps(p3).encode("utf-8"))
    eq(app.sessions_seen, 3, "roundNo 归 1 且 key 未变，兜底信号也必须命中")
    eq(app.state.last_round, 1, "新半场的第一回合必须被正常处理，而不是当成过期丢弃")
    ok(app.fallbacks == 0, f"不该出现兜底空响应，实际 {app.fallbacks} 次")


# ══ runner ═══════════════════════════════════════════════════════════
def main(argv: list[str]) -> int:
    if "--list" in argv:
        for name, _ in CASES:
            print(name)
        return 0

    needle = next((a for a in argv if not a.startswith("-")), None)
    selected = [(n, f) for n, f in CASES if needle is None or needle in n]
    if not selected:
        print(f"no cases match {needle!r}")
        return 2

    passed = failed = 0
    for name, fn in selected:
        try:
            fn()  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}")
            if isinstance(exc, Failure):
                print(f"      {exc}")
            else:
                print("      " + traceback.format_exc().replace("\n", "\n      ").rstrip())
        else:
            passed += 1
            print(f"ok    {name}")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
