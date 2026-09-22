"""game/day.py 建造线的用例：建武器 / 砌墙（沿环走一圈的顺序）/ 防关人闸门 /
拆墙放人 / 修墙 / 收工闸门。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件 `py tests/<本文件>`）。
必须用 `py` —— 本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import sys
import unittest
from collections.abc import Iterable
from pathlib import Path

# tests/ 给 `_fixtures` 用（discover 不一定把它放进 sys.path）；src/ 给 coregeek 用
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _fixtures import _reset_ledgers  # noqa: E402
from _fixtures import _records, _terrain  # noqa: E402
from coregeek.agent import AGENT  # noqa: E402
from coregeek.game.grid import STEPS, Pos, base_cells, box_cells, door_cells, wall_cells, weapon_cells, weapon_sites  # noqa: E402
from coregeek.game.map import Map  # noqa: E402
from coregeek.game.path import step_outside, step_toward, steps_between  # noqa: E402
from coregeek.game import core, day, planner  # noqa: E402
from coregeek.game.day import WEAPONS_BY_SITE  # noqa: E402
from coregeek.game.planner import plan  # noqa: E402
from coregeek.game.task import task_channel  # noqa: E402
from coregeek.game.core import WALL, WALL_FIXER  # noqa: E402
from coregeek.game.roles import BaseRole, Pioneer, Worker  # noqa: E402
from coregeek.game.world import DAY_ROUNDS, ROUNDS_PER_DAY, Robot, Turn, Wall, Weapon  # noqa: E402
from coregeek.protocol import model  # noqa: E402


def _guns_at_sites(base: Pos) -> dict[Pos, str]:
    """三座武器按**新阵形**摆好（种类与落点由 `WEAPONS_BY_SITE` / `weapon_sites` 绑定）——
    夹具不再手抄坐标，换阵形只改这一处。"""
    return dict(zip(weapon_sites(base, 41), WEAPONS_BY_SITE))
def _operator_post(base: Pos) -> Pos:
    """三座火箭共用的操作位（`core._operator_spots` 的几何等价）：三座邻域交集里去掉基地那格。"""
    sites = weapon_sites(base, 41)
    common: set[Pos] | None = None
    for site in sites:
        nbrs = {Pos(site.x + d.x, site.y + d.y) for d in STEPS}
        common = nbrs if common is None else common & nbrs
    assert common is not None
    return next(iter(common - base_cells(base)))

class BuildWeaponTest(unittest.TestCase):
    """合成开局：白天、0 武器、基地在左半 —— 工人会建满三座、建在该建的地方、然后收手。

    必须把回合串起来跑：单帧"目标格算得对"证明不了收不收得住（`MineApproachTest` 同理）。
    结算照判题器口径来 —— 一回合一步，建起来的武器下一回合就挡路、也占掉那个格子。
    """

    #: 三个落点由 `weapon_sites` 给（左半基地 (10,24) ⇒ (12,24)/(12,25)/(12,22)）
    BASE = Pos(10, 24)
    MINE = Pos(4, 24)

    def setUp(self) -> None:
        _reset_ledgers()
        self.gold = 75  # 开局：恰好买满三座（25×3）
        # 已建成的武器名册 —— 唯一真相，地形由它推（见 `_terrain`）。直接往 `entries`
        # 里塞一把 `"gatling"` 的话 `turn.weapons` 是空的、名额算成"还差三座"，而那一格
        # 又挡路 —— 夹具与真实局面不同形。
        self.weapons: list[Weapon] = []
        self.walls: dict[Pos, str] = {}
        # 静态地形（基地 + 矿），删掉基地就是"基地没了"那个降级局面
        self.ground: dict[Pos, str] = {self.BASE: "station", self.MINE: "stone"}
        self.roles: dict[int, BaseRole] = {
            1: Pioneer(1, Pos(20, 20)),
            # 两个工人都贴着后方那一列（各差一格），第 1 回合就能动手
            2: Worker(2, Pos(8, 25)),
            3: Worker(3, Pos(8, 24)),
        }
        self.builds: list[tuple[str, Pos]] = []

    def _turn(self, round_no: int = 1) -> Turn:
        return Turn(
            round_no=round_no,
            map=Map((41, 32), _terrain(tuple(self.weapons), self.walls, self.ground)),
            roles=tuple(self.roles.values()),
            gold=self.gold,
            weapons=tuple(self.weapons),
        )

    def _weapons(self) -> list[str]:
        return [name for name, _ in self.builds if name != WALL]

    def _settle(self, round_no: int = 1, limit: int = 20, want: int | None = None) -> None:
        """跑到建满 `want` 座为止（默认三座都建上）。墙不归这个类管（见 `BuildWallTest`），
        但会顺路砌起来 —— 所以 `builds` 里混着墙，统计武器时要滤掉，金币也只按武器扣。"""
        want = len(WEAPONS_BY_SITE) if want is None else want
        for _ in range(limit):
            if len(self._weapons()) >= want:
                return
            cmds = plan(self._turn(round_no))
            if not cmds:
                return
            for key, cmd in cmds.items():
                role_id = int(key)
                target = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
                if cmd["action"] == "build":
                    self.builds.append((cmd["name"], target))
                    if cmd["name"] == WALL:
                        self.walls[target] = WALL
                    else:
                        self._add_weapon(cmd["name"], target)
                        self.gold -= 25
                else:
                    self.roles[role_id] = type(self.roles[role_id])(role_id, target)
        self.fail(f"{limit} 回合还没把 {want} 座武器建完，说明在原地绕圈")

    def _add_weapon(self, kind: str, cell: Pos) -> None:
        """照格式记一座建成的武器（id 递增即可 —— 这里没人按 id 排序）。"""
        self.weapons.append(
            Weapon(id=200 + len(self.weapons), kind=kind, pos=cell, attack_range=4, cooldown=0)
        )

    def _builds(self, cmds: dict) -> list[dict]:
        return [c for c in cmds.values() if c["action"] == "build"]

    def test_builds_the_three_weapons_on_their_own_sites(self):
        """三座各自落在自己的落点上，种类与落点的绑定与 `WEAPONS_BY_SITE` 一致。

        断言用集合而不是建成的先后：谁先走到哪一格是路径决定的，不是这条用例要钉的东西。
        """
        self._settle()
        self.assertEqual(len(self._weapons()), 3)
        self.assertEqual(
            {(name, cell) for name, cell in self.builds},
            set(zip(WEAPONS_BY_SITE, weapon_sites(self.BASE, 41))),
        )
        self.assertEqual(self.gold, 0)

    def test_stops_at_three_even_with_lots_of_gold(self):
        """停手是因为份额（角色数），不是因为钱花光了 —— "建立多了没有意义"。"""
        self.gold = 200
        self._settle()
        self.assertEqual(len(self._weapons()), len(WEAPONS_BY_SITE))
        self.assertGreater(self.gold, 0, "这次不是钱见底才停的")

    def test_a_short_budget_builds_nothing_and_mines_instead(self):
        """只有 25 金（三座要 75）⇒ **一座都不建**，改去挖矿/砌墙（用户口径 1.2.2）。

        旧口径是"钱少就先建一座"（递减预算）；新口径要求"钱够建满才动手"（`can_build_all`），
        不够就两个工人一起去挖矿凑钱、凑够了再回来建。本夹具没有小贩（`_can_fund` 假）⇒
        筹资不可行 ⇒ 落回墙线：手里 0 块石头、环上还有 14 格 ⇒ 去采石头。
        """
        self.gold = 25
        cmd = plan(self._turn())["2"]
        self.assertNotEqual(cmd["action"], "build", f"钱不够建满 ⇒ 一座都不建：{cmd}")
        self.assertIn(cmd["action"], ("move", "collect"))

    def test_a_blocked_site_drops_only_its_own_weapon(self):
        """排第一的落点被墙占了 ⇒ 只有那一座不建，另两座照落在各自的位置上。

        钉的是 `slots` 里"落点与种类绑死、再滤 `blocked`"的顺序：反过来（先滤空再
        `zip`）整体前移，每个种类都挪到别人家的落点上，而报文完全合法、本地全绿。
        顺带守住"覆盖会把原武器打成 level1"（§4.5.1 补充说明）。
        """
        sites = weapon_sites(self.BASE, 41)
        self.walls[sites[0]] = WALL  # 第一座火箭的落点被墙占了
        self._settle(want=2)
        self.assertEqual(
            {(name, cell) for name, cell in self.builds},
            {("rocket", sites[1]), ("rocket", sites[2])},
        )
        built_cells = {cell for _, cell in self.builds}
        self.assertNotIn(sites[0], built_cells, "落点被占 ⇒ 那一座就是不建，不换地方")

    def test_night_builds_nothing(self):
        """夜里 `build` 不可用（任务书 §4.4）—— 0 武器、75 金也一座都不许建。"""
        self.assertNotIn("build", {c["action"] for c in plan(self._turn(round_no=85)).values()})

    def test_no_base_means_no_build(self):
        """基地没了就没有可建造区（坐标全由它推）⇒ 不建，而不是瞎猜一个坐标。"""
        del self.ground[self.BASE]
        self.assertNotIn("build", {c["action"] for c in plan(self._turn()).values()})


class WallRingTest(unittest.TestCase):
    """围墙环的几何与建造顺序。

    公式来源是图不是正文（`docs/pic/build_map.png`）：任务书只说了"蓝色区域只能建造武器、
    黄色区域只能建造围墙"，没有任何坐标。算错 ⇒ `build` 落点非法。
    """

    BASE = Pos(10, 24)

    def setUp(self) -> None:
        _reset_ledgers()

    @staticmethod

    def _ring20(base: Pos) -> set[Pos]:
        """完整的 20 格围墙环（实际只砌其中 14/16 格，背面整列不砌）—— 对照物。"""
        return {
            Pos(x, y)
            for x in range(base.x - 2, base.x + 4)
            for y in range(base.y - 3, base.y + 3)
            if x in (base.x - 2, base.x + 3) or y in (base.y - 3, base.y + 2)
        }

    def test_the_ring_is_fourteen_cells_free_of_the_base_and_the_weapons(self):
        cells = wall_cells(self.BASE, 41)
        self.assertEqual(len(cells), 14, "6×6 边框 20 格减去背面整列 6 格")
        self.assertEqual(len(set(cells)), 14, "不该有重复格")
        self.assertEqual(set(cells) & base_cells(self.BASE), set(), "不能落在基地身上")
        self.assertEqual(set(cells) & set(weapon_cells(self.BASE)), set(), "不能占武器环")

    def test_the_third_day_seals_the_two_back_corners(self):
        """第 3 天起补上背面两个角格（16 格）—— 后方通道从 6 格收窄到 4 格。

        `sealed` 只往队尾插两格、不改已有那些格的相对次序：前 10 格逐格相同。
        """
        open_ring = wall_cells(self.BASE, 41)
        sealed = wall_cells(self.BASE, 41, sealed=True)
        self.assertEqual(len(sealed), 16)
        self.assertEqual(
            set(sealed) - set(open_ring), {Pos(8, 21), Pos(8, 26)}, "多出来的正好是背面两角"
        )
        self.assertEqual(open_ring[:10], sealed[:10], "已有的格照旧在自己位置上")

    def test_the_back_column_is_left_open(self):
        """背面整列常年敞开（14 格时 6 格、16 格时 4 格）—— 盒子唯一的进出口。

        敞口放到一整列是这一版的取舍：白天的出行与回程不必再靠"拆一格当门"（那套机制整套
        删了），代价是机器人也能从这一侧进来 —— 要守住的是正面那 6 格。
        """
        cells = set(wall_cells(self.BASE, 41))
        back = {Pos(8, y) for y in range(21, 27)}  # 基地在左半 ⇒ 背面是 x = bx-2
        self.assertEqual(cells & back, set(), "背面整列一格都不砌")
        ring20 = self._ring20(self.BASE)
        self.assertEqual(len(ring20), 20)
        self.assertEqual(ring20 - cells, back, "少掉的正好是背面整列，不是别的")
        self.assertEqual(set(door_cells(self.BASE, 41)), back, "通道 = 背面列里不砌的格")
        self.assertEqual(
            set(door_cells(self.BASE, 41, sealed=True)),
            back - {Pos(8, 21), Pos(8, 26)},
            "补上两角之后通道只剩中段 4 格",
        )

    def test_the_front_column_comes_first(self):
        """前 6 格 = 迎着机器人那一列 —— 回合数不够时先砌的就是它。

        判反了墙就砌在机器人不来的一侧 —— 不报错、不违规，只是整段白砌，而石头是工人
        一块块背回来的。
        """
        ring = wall_cells(self.BASE, 41)
        self.assertEqual(
            ring[:6],
            (Pos(13, 21), Pos(13, 22), Pos(13, 23), Pos(13, 24), Pos(13, 25), Pos(13, 26)),
            "左半 ⇒ 正面是 bx+3，一端扫到另一端",
        )
        self.assertEqual({c.x for c in ring[:6]}, {13}, "前 6 格全在正面那一列")

    def test_the_order_walks_the_ring_in_one_sweep(self):
        """顺序 = 沿环走一圈：14 格与 16 格都只跳一次。

        这不是好看，是回合预算（工人一天只有 70 回合）：来回横穿吃掉的是砌墙的回合。唯一
        那处跳步是跨过后方那道敞口（5 步，绕不掉）；两行都从正面列那一端接着铺，就是为了
        把跳步压到这一处。
        """
        for sealed, jump in (
            (False, (Pos(9, 26), Pos(9, 21))),
            (True, (Pos(8, 26), Pos(8, 21))),
        ):
            with self.subTest(sealed=sealed):
                ring = wall_cells(self.BASE, 41, sealed=sealed)
                self.assertEqual(len(set(ring)), len(ring), "每一格只走一次")
                jumps = [(a, b) for a, b in zip(ring, ring[1:]) if a.dist(b) > 1]
                self.assertEqual(jumps, [jump], "只该有这一处跳步")

    def test_the_ring_mirrors_for_a_right_half_base(self):
        """基地在右半 ⇒ 正面是 `bx-2`、背面是 `bx+3`（换边后自动跟着翻）。

        按基地坐标判而不用 `teamOur.type` —— 下半场换边后队伍身份不变、基地会挪。
        """
        base = Pos(30, 10)
        ring = wall_cells(base, 41)
        self.assertEqual(len(ring), 14)
        self.assertEqual({c.x for c in ring[:6]}, {28}, "右半 ⇒ 正面是 bx-2")
        self.assertEqual(ring[:6], tuple(Pos(28, y) for y in range(7, 13)), "正面列自上而下")
        self.assertEqual({c.x for c in ring}, {28, 29, 30, 31, 32}, "侧面两列都在（背面整列不砌）")
        self.assertEqual(
            set(door_cells(base, 41)), {Pos(33, y) for y in range(7, 13)}, "背面整列是 x = bx+3"
        )
        self.assertEqual(len(wall_cells(base, 41, sealed=True)), 16)

    def test_the_box_is_the_whole_buildable_area(self):
        """盒子 = `base_cells` ∪ `weapon_cells` ∪ 完整 20 格围墙环 = 36 格（蓝圈那一块）。

        闸门问的是"这个人在不在即将被墙围起来的那片区域里"，判据就是它。不取 `width`：
        盒子在基地两侧各外扩 2，左右半场是同一个矩形（与正面/背面那两条镜像的边相反）。
        """
        for base in (Pos(10, 24), Pos(30, 10)):
            with self.subTest(base=base):
                box = box_cells(base)
                ring20 = self._ring20(base)
                self.assertEqual(len(ring20), 20)
                self.assertEqual(len(box), 36)
                self.assertEqual(
                    box, base_cells(base) | set(weapon_cells(base)) | ring20, "三块拼起来正好是它"
                )

    def test_the_back_column_never_holds_a_building(self):
        """后方通道里没有任何建筑 —— 基地 / 武器 / 墙都不在。

        闸门"会不会把人关住"靠的就是这一条：通道空着 ⇒ 只有单位能堵它。
        `wall_cells` 的背面列与 `weapon_sites` 的前排列由两个不同的式子给出，钉成断言，
        别靠脑补。
        """
        for base in (Pos(10, 24), Pos(30, 10)):
            with self.subTest(base=base):
                built = (
                    set(wall_cells(base, 41, sealed=True))
                    | set(weapon_sites(base, 41))
                    | base_cells(base)
                )
                self.assertEqual(
                    built & set(door_cells(base, 41, sealed=True)), set(), "通道里不该有建筑"
                )


class BuildWallTest(unittest.TestCase):
    """合成开局：白天、武器已建满 → 工人去采石、回来砌墙，落点严格按优先级。

    把白天串起来跑：单帧看不出"采够了没有、砌到哪一格、会不会来回抖"。
    每回合重算预算，所以必须真的过一遍时间。
    """

    BASE = Pos(10, 24)
    MINE = Pos(4, 24)  # 基地左侧的石矿
    #: 三座武器先摆好 —— 否则金币/名额会先把工人抽去建武器（那是 `BuildWeaponTest` 的事）。
    #: 记录是唯一真相（`_records`），地形由 `_terrain` 推 —— 只有网格没有名册的话，
    #: "还差几座"会算成还差三座，而降级方向恰好也是"不建"（金币 0），症状就藏起来了。
    WEAPONS = _records({Pos(9, 23): "gatling", Pos(9, 24): "railgun", Pos(9, 22): "rocket"})

    def setUp(self) -> None:
        _reset_ledgers()
        self.entries: dict[Pos, str] = _terrain(
            self.WEAPONS, {self.BASE: "station", self.MINE: "stone"}
        )
        self.worker = Worker(1, Pos(6, 24))

    def _turn(self, round_no: int = 1, stone: int = 0, pos: Pos | None = None) -> Turn:
        self.worker = Worker(1, pos or self.worker.pos, {"stone": stone})
        return Turn(
            round_no=round_no,
            map=Map((41, 32), self.entries),
            roles=(self.worker,),
            gold=0,
            weapons=self.WEAPONS,
        )

    def test_day_one_mines_then_walls_the_whole_ring_in_order(self):
        """把白天串起来跑到砌满：14 格全砌上，且顺序与 `wall_cells` 逐格一致。

        这一条把"算缺口 → 采矿 → 砌墙"整条线钉在一起：算少了工人在工地干等，顺序错了则会
        先把背面砌满、正面空着。采量是**每回合现算的"还差几块"**（新口径"够了就行"）⇒
        砌完手里正好 0 块。
        """
        built: list[Pos] = []
        stone = 0
        for _ in range(70):  # 一个白天 70 回合
            cmd = plan(self._turn(stone=stone)).get("1")
            if cmd is None:
                break
            cell = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
            if cmd["action"] == "collect":
                self.assertEqual(cell, self.MINE, "只能采石头，且 `collect` 给的是矿的坐标")
                stone += 1
            elif cmd["action"] == "build":
                self.assertEqual(cmd["name"], WALL)
                # 石头是攒够了才回来砌的（这正是回合预算要的效果：少跑冤枉路），
                # 所以这里只要求"有得扣"，扣多少由下面每砌一座减 1 来核。
                self.assertGreaterEqual(stone, 1, "砌一座墙的代价是**石头×1**，背包里得有")
                self.entries[cell] = WALL
                built.append(cell)
                stone -= 1
                self.worker = Worker(1, self.worker.pos, {"stone": stone})
            else:
                self.worker = Worker(1, cell, {"stone": stone})

        self.assertEqual(built, list(wall_cells(self.BASE, 41)), "顺序必须与优先级表一致")
        self.assertEqual(stone, 0, "采够就好：砌完手里不留存货（旧口径的 `STONE_RESERVE` 删了）")

    def test_the_third_day_seals_the_back_corners_without_rebuilding(self):
        """第 3 天起补背面两个角格（14 → 16），已砌的那 14 格一格都不重砌。

        "补哪两格"只能由回合号判（环上"没砌"与"砌了又被拆"在地图上同形）⇒ 这一条同时钉住
        `_sealed_back` 的时间边界、两格的补齐顺序、以及"背面整列其余 4 格永远不砌"。
        """
        open_ring = wall_cells(self.BASE, 41)
        corners = [c for c in wall_cells(self.BASE, 41, sealed=True) if c not in open_ring]
        self.assertEqual(len(corners), 2, "只补两个角格")
        self.entries.update({c: WALL for c in open_ring})
        built: list[Pos] = []
        stone = len(corners)  # 正好够那两格（新口径不留存货）⇒ 不采也不卖
        day3 = 2 * ROUNDS_PER_DAY + 1
        for _ in range(70):
            cmd = plan(self._turn(round_no=day3, stone=stone)).get("1")
            if cmd is None:
                break
            cell = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
            if cmd["action"] == "build":
                self.assertIn(cell, corners, f"只许补那两个角格，却砌了 {cell}")
                self.entries[cell] = WALL
                built.append(cell)
                stone -= 1
            else:
                self.worker = Worker(1, cell, {"stone": stone})

        self.assertEqual(built, corners, "两个角格按顺序补上、补完就收手")

    def test_the_stone_trip_is_decided_by_the_shortfall_not_by_the_clock(self):
        """采不采石头只看"还差几块"，**不看还剩多少回合**（新口径：时间只有第 0 级那一道门）。

        旧口径按"白天剩余 − `TIME_MARGIN`"解一个采量、快天黑就掉头去工地 —— 那条线删了。
        对照：手里 3 块、环上还差 14 格 ⇒ 两个回合号下**都朝矿走**；手里给够 ⇒ 掉头去工地。
        """
        pos, stone = Pos(6, 24), 3
        for round_no in (1, 60):
            cmd = plan(self._turn(round_no=round_no, stone=stone, pos=pos))["1"]
            self.assertEqual(cmd["action"], "move")
            cell = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
            self.assertLess(cell.dist(self.MINE), pos.dist(self.MINE), "石头不够 ⇒ 照旧朝矿走")
        enough = plan(self._turn(round_no=60, stone=14, pos=pos))["1"]
        cell = Pos(enough["targetPos"][0]["x"], enough["targetPos"][0]["y"])
        self.assertGreater(cell.dist(self.MINE), pos.dist(self.MINE), "石头够 ⇒ 掉头去工地")

    def test_the_last_cell_needs_exactly_one_stone(self):
        """墙上只剩一格、手里一块石头都没有 ⇒ 采一块就够（新口径"够了就行"，不留存货）。

        旧口径会多采 `STONE_RESERVE`(3) 块备着 —— 那条删了：没有转移物品的指令，多采的
        只能自己背着，而环砌满之后这一支就再也不采石头了。
        """
        self.entries.update({c: WALL for c in wall_cells(self.BASE, 41)[:-1]})
        stone, collects, built = 0, 0, 0
        for _ in range(70):
            cmd = plan(self._turn(stone=stone)).get("1")
            if cmd is None:
                break
            cell = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
            if cmd["action"] == "collect":
                self.assertEqual(cell, self.MINE)
                collects += 1
                stone += 1
            elif cmd["action"] == "build":
                self.assertEqual(cell, wall_cells(self.BASE, 41)[-1], "砌的是最后那一格")
                self.entries[cell] = WALL
                built += 1
                stone -= 1
            else:
                self.worker = Worker(1, cell, {"stone": stone})

        self.assertEqual(built, 1, "最后那一格砌上了")
        self.assertEqual(collects, 1, "就差一块 ⇒ 只采一块，不多采")
        self.assertEqual(stone, 0, "砌完手里空着")

    def test_a_finished_ring_stops_the_stone_mining(self):
        """14 格都砌满了 ⇒ 不再采石头（多采的只会压在背包里）。

        这一支会转去采最值钱的矿（`SpareOreTest`），而本夹具没有价目表
        （`vendor_prices` 缺省为空）⇒ 挑不出"最值钱的矿" ⇒ 一条都不发：没有价格就无从挑，
        宁可不动，这是有意的降级方向。
        """
        self.entries.update({c: WALL for c in wall_cells(self.BASE, 41)})
        self.assertEqual(plan(self._turn(stone=0)), {})

    def test_no_base_means_no_wall_and_no_mining(self):
        """基地没了就没有围墙环（坐标全由它推）⇒ 连矿都不去采，而不是瞎找一个坐标。"""
        del self.entries[self.BASE]
        self.assertEqual(plan(self._turn(stone=0)), {})


class DayEndGateTest(unittest.TestCase):
    """第 0 级收工门：`回岗步数 + POST_MARGIN(3) ≥ 白天剩余` ⇒ 回岗位（走它的只有夜里上炮
    的那个人，`utils._night_gunner`）。它**不问环砌完没有** —— 到点就停下手里的活。

    步数当场算（BFS 真实步数，"回岗要走多久就得多早动身"）。目标与夜里 `night.defend` 的岗位
    同一个（`core._post_spots`）：三火箭共用的操作位要**站上去** —— 天黑时人已经在岗上，夜里
    第一回合就能开火。这一支**只许发 `move`/`use`**：`attack` 仅黑夜可用（§4.4），白天发一条
    就是一次异常、累计 5 次整场不再被调度，而复用 `night.defend` 是这里最容易犯的错
    （它贴近炮位会调 `_fire`）⇒ 有一条扫白天各回合、各站位的守门员。
    """

    BASE = Pos(10, 24)
    #: 与 `weapon_sites` 同序的三座火箭（这里只要"有三座炮"就够）
    WEAPONS = _records(_guns_at_sites(Pos(10, 24)))
    SIZE = (41, 32)
    #: 收工门的容错余量 = `day.POST_MARGIN`
    MARGIN = 3

    def setUp(self) -> None:
        _reset_ledgers()

    def _turn(
        self,
        *,
        round_no: int = 1,
        ring: bool = True,
        at: Pos = Pos(20, 24),
        stone: int = 0,
        bag: dict[str, int] | None = None,
        weapons: tuple[Weapon, ...] | None = None,
        pioneer: Pos | None = None,
    ) -> Turn:
        """环默认砌满；没有矿、没有小贩、没有金 —— 只留收工门这一支。

        `bag` 给工人背包；`weapons` 换名册；`pioneer` 给一名开拓者，**排在 payload 最前面**
        —— 用它验"谁去收工"这类顺序相关的判据。
        """
        weapons = self.WEAPONS if weapons is None else weapons
        walls = {c: WALL for c in wall_cells(self.BASE, 41)} if ring else {}
        worker = Worker(1, at, dict(bag) if bag is not None else ({"stone": stone} if stone else {}))
        roles: tuple[BaseRole, ...] = (
            (worker,) if pioneer is None else (Pioneer(2, pioneer), worker)
        )
        return Turn(
            round_no=round_no,
            map=Map(
                self.SIZE,
                _terrain(
                    weapons,
                    {self.BASE: "station"},
                    walls,
                    {r.pos: "worker" for r in roles},
                ),
            ),
            roles=roles,
            gold=0,
            weapons=weapons,
        )

    def _steps_home(self, at: Pos) -> int:
        """从 `at` 走到**岗位**的真实步数（与 `planner` 同一个障碍、同一个口径）。

        岗位 = 三座火箭共用的那个操作位 —— 它是空地、要**站上去**（比"贴着"多一步）。
        障碍取 `Map.blocked`（含基地与武器格）：只在墙环上算的话，BFS 会穿过基地与炮位抄近路。
        """
        walk = self._turn(at=at).map.blocked
        post = _operator_post(self.BASE)
        if at == post:
            return 0  # 已经在岗位上
        steps = steps_between(at, post, walk, self.SIZE)
        return steps + 1 if steps >= 0 else -1  # 多座组的岗位是空地、要站上去

    def _gate(self, *, round_no: int, at: Pos, bag: dict[str, int] | None = None):
        """直接问 `day.BACK_TO_POST.run` —— 它返回 True 就是"这一回合归收工门了"。

        报文分不出是谁发的指令（券线也会发 `move`），问返回值才干净。
        """
        turn = self._turn(round_no=round_no, at=at, bag=bag)
        role = next(r for r in turn.roles if isinstance(r, Worker))
        return day.BACK_TO_POST.run(role, core._Ctx(turn, core._Queue(turn)))

    def test_the_gate_counts_the_walk_home_plus_the_margin(self):
        """门 = **实时的回岗步数 + `POST_MARGIN`(3)**，手里有几张券都不改它（用户口径）。

        步数当场算：`剩余 == 步数 + 3` 正好动身，多剩一回合不动 —— 站在十几步开外的人必须
        更早动身（"固定几回合、不看距离"那版正是这里出的问题）。
        """
        at = Pos(20, 24)
        steps = self._steps_home(at)
        self.assertGreater(steps, 3, "这个站位本来就要走好几步")
        gate = DAY_ROUNDS - steps - self.MARGIN + 1  # 这一回合的 `day_rounds_left` 正好是 steps + 3
        self.assertTrue(self._gate(round_no=gate, at=at), "正好到点 ⇒ 回岗位")
        self.assertFalse(self._gate(round_no=gate - 1, at=at), "还富余一回合 ⇒ 照常干活")
        # 券不参与判据：手里握着券也照样按步数走（到岗之后再一张一张用掉）
        self.assertFalse(
            self._gate(round_no=gate - 1, at=at, bag={"WeaponUpgradeVoucher1": 2}),
            "券不提前开闸",
        )

    def test_a_held_weapon_voucher_is_used_once_at_the_post(self):
        """到岗之后**先用券**（用户口径"到了位置先用券"）：站在岗位上、目标贴着就 `use`。

        不满足（手里没券 / 要升的那座离得远）⇒ 什么都不发（待命），这一支照旧只发 `move`/`use`。
        """
        at = _operator_post(self.BASE)  # 三火箭共用的操作位（岗位本身）
        late = DAY_ROUNDS - self.MARGIN  # 已经在岗 ⇒ 门怎么算都命中
        cmds = plan(self._turn(round_no=late, at=at, bag={"WeaponUpgradeVoucher1": 1}))
        self.assertEqual(cmds["1"]["action"], "use", f"到岗先用券：{cmds}")
        self.assertEqual(cmds["1"]["name"], "WeaponUpgradeVoucher1")
        # 手里没券 ⇒ 在岗待命（空指令合法）
        self.assertEqual(plan(self._turn(round_no=late, at=at)), {})
    def test_the_gate_aims_at_the_shared_operator_spot(self):
        """收工的落点是那一组的**岗位**，不是"最近那座炮"：火箭对站到共用的操作位上去。

        "站上去"是这件事的全部意义（`(10,25)` 只是进那个口袋的必经格）：天黑时人已经在岗，
        夜里第一回合两座火箭就能交替；停在邻格则永远只贴着其中一座。
        """
        post = _operator_post(self.BASE)
        at = Pos(post.x - 1, post.y)  # 后方通道那一格（其余邻格是武器/基地/墙）
        cmds = plan(self._turn(round_no=DAY_ROUNDS - 1, at=at))
        self.assertEqual(cmds["1"]["action"], "move", f"该走上岗位：{cmds}")
        cell = Pos(cmds["1"]["targetPos"][0]["x"], cmds["1"]["targetPos"][0]["y"])
        self.assertEqual(cell, post, "落点就是共用操作位本身")

    def test_a_day_round_never_fires(self):
        """红线守门员：白天任何回合、任何站位（含贴着炮）都只许有 `move`。

        "复用 `night.defend`"会在这里立刻现形 —— 那正是 5 次异常直通出局的写法。
        """
        for round_no in (1, 35, 60, 66, 69, DAY_ROUNDS):
            for at in (Pos(20, 24), Pos(14, 24), Pos(10, 25), Pos(9, 24), Pos(9, 26)):
                with self.subTest(round_no=round_no, at=at):
                    cmds = plan(self._turn(round_no=round_no, at=at))
                    self.assertNotIn(
                        "attack", {c["action"] for c in cmds.values()}, f"白天开火非法：{cmds}"
                    )

    def test_the_pioneer_is_the_one_who_goes_to_the_post(self):
        """收工回岗位的是**夜里上炮的那个角色**（`utils._night_gunner`）：有开拓者就是它，
        工人不收工（他们夜里挖矿）。

        与夜里同一个判据 —— 只改一处会让白天把人送进岗位、夜里又没人认领（或反过来：
        工人回了岗位却在夜里出门挖矿）。
        """
        late = DAY_ROUNDS - 1
        cmds = plan(self._turn(round_no=late, pioneer=Pos(20, 28)))
        self.assertIn("2", cmds, f"开拓者该回岗位：{cmds}")
        self.assertNotIn("1", cmds, "工人不收工（夜里挖矿，不用站岗）")

    def test_the_gate_stops_the_wall_work(self):
        """环没砌完也照样回岗位 —— 第 0 级在最前面，没有"补墙优先于收工"这个例外了。

        旧口径里环上有缺口时这一支整个不生效。对照：同一站位（贴着待砌的那一格）、同一回合，
        缺口还在时发出的是回岗位那条 `move`，不是 `build`。
        """
        gap = wall_cells(self.BASE, 41)[0]
        at = next(
            Pos(gap.x + d.x, gap.y + d.y)
            for d in STEPS
            if 0 <= gap.x + d.x < self.SIZE[0] and 0 <= gap.y + d.y < self.SIZE[1]
        )
        cmds = plan(self._turn(round_no=DAY_ROUNDS - 1, ring=False, at=at, stone=1))
        self.assertEqual(cmds["1"]["action"], "move", f"到点就回岗位、不再砌墙：{cmds}")


class TwoWallBuildersTest(unittest.TestCase):
    """两个工人同时在环上：不能对着改目标来回踱步。两个同症状的死循环都要守住：

    ① `_ring` 把"工人脚下那一格"划掉 ⇒ 另一个工人的 `free[0]` 整体后移，等前一个一挪窝
    目标又变回来 —— 两人在两格之间转到天黑。② `_walled` 把自己人一律算成障碍，门收成 2 格
    后环内只剩几条走廊，"有人恰好站在走廊格上"变成常事 ⇒ 另一个被判成"砌满就出不去"、
    被闸门送出去一格，下回合又走回来。单帧断言看不出来，必须串跑。

    角色必须自己铺进地图（真实路径里 `_entries` 会写）：不铺就复现不出这个循环，
    用例连坏的实现都放不过去。
    """

    BASE = Pos(10, 24)
    MINE = Pos(4, 24)
    WEAPONS = _records({Pos(9, 23): "gatling", Pos(9, 24): "railgun", Pos(9, 22): "rocket"})
    #: 触发死循环的局面：正面列已砌三格，两个工人卡在基地与正面列之间的夹缝里
    FRONT_BUILT = {Pos(13, 22): WALL, Pos(13, 23): WALL, Pos(13, 24): WALL}

    def setUp(self) -> None:
        _reset_ledgers()

    def test_two_workers_keep_building_in_the_pocket(self):
        entries = _terrain(
            self.WEAPONS, {self.BASE: "station", self.MINE: "stone"}, self.FRONT_BUILT
        )
        # 石头给够：环 14 格 ⇒ 还剩 11 格要砌，兜里少于 11 块时
        # `_stones_to_mine` 会把两人派去矿上（那时测的就不是"打不打转"了）。
        roles = {
            10010: Worker(10010, Pos(13, 21), {"stone": 20}),
            10012: Worker(10012, Pos(11, 22), {"stone": 20}),
        }
        built: list[Pos] = []
        for rnd in range(1, 13):  # 12 回合够砌掉头几格（死循环下一次都砌不上）
            # 角色压在静态层之上 —— 与 `model._entries` 的写入顺序一致（单位盖过地形）
            grid = {**entries, **{r.pos: "worker" for r in roles.values()}}
            turn = Turn(
                round_no=rnd,
                map=Map((41, 32), grid),
                roles=tuple(roles.values()),
                gold=0,
                weapons=self.WEAPONS,
            )
            for key, cmd in plan(turn).items():
                role_id = int(key)
                cell = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
                role = roles[role_id]
                if cmd["action"] == "build":
                    self.assertEqual(cmd["name"], WALL)
                    entries[cell] = WALL
                    built.append(cell)
                    roles[role_id] = Worker(role_id, role.pos, {"stone": role.stone - 1})
                elif cmd["action"] == "collect":
                    roles[role_id] = Worker(role_id, role.pos, {"stone": role.stone + 1})
                else:
                    roles[role_id] = Worker(role_id, cell, {"stone": role.stone})

        order = [c for c in wall_cells(self.BASE, 41) if c not in self.FRONT_BUILT]
        self.assertGreaterEqual(len(built), 4, "两个工人在原地打转 ⇒ 一座墙都砌不上")
        self.assertEqual(len(built), len(set(built)), "同一格砌了两遍（石头白花）")
        self.assertTrue(set(built) <= set(order), f"砌到环外去了：{set(built) - set(order)}")
        # 切段分工契约：A（先处理的工人）从缺口队头砌起 —— 第一格必是正面列；
        # B 从中点砌起（首格落在后段，这局是 x=8 的那一格），两人不挤同一段墙。
        # 再往后锁死集合只会把"顺序微调"误报成回归，不钉。
        self.assertEqual(built[0].x, 13, "A 的第一格 = 缺口队头（正面列）")
        self.assertTrue({Pos(13, 21), Pos(13, 25)} <= set(built), "夹缝里那两格得砌上")


class StandingOnTheTargetTest(unittest.TestCase):
    """工人站在目标格上时不许对着它 `build`，先挪开一格。

    `model._entries` 把单位铺在最后 ⇒ 人站在墙上时网格里只剩 `worker`，墙被盖掉了
    （那格照旧在 `Map.blocked` 里，但看不出是墙还是空地）；而 `_ring` 的"自己人算路过"
    又把它复活成候选 ⇒ `free[0]` 永远是脚下这一格，`dist == 0` 也是合法建造位，
    于是每回合 `build` 同一格、永远轮不到下一格：石头白花，`free` 也永不为空。
    """

    BASE = Pos(10, 24)
    WEAPONS = _records({Pos(9, 23): "gatling", Pos(12, 22): "railgun", Pos(12, 25): "rocket"})
    #: 已砌好的那些（正面列 6 + 顶行 4 + 底行头一格）—— 一个能触发卡死的中间状态
    BUILT = {c for c in wall_cells(BASE, 41) if c.y == 26 or c.x == 13} | {Pos(12, 21)}
    ON = Pos(11, 21)  # 工人站的那一格 —— 也是 `wall_cells` 里下一个该砌的

    def setUp(self) -> None:
        _reset_ledgers()

    def _turn(self, walls: Iterable[Pos], worker: Worker, round_no: int = 40) -> Turn:
        return Turn(
            round_no=round_no,
            map=Map(
                (41, 32),
                # 角色压在静态层之上 —— 与 `model._entries` 的写入顺序一致（单位盖过地形）
                _terrain(
                    self.WEAPONS,
                    {self.BASE: "station"},
                    {c: WALL for c in walls},
                    {worker.pos: "worker"},
                ),
            ),
            roles=(worker,),
            gold=0,
            weapons=self.WEAPONS,
        )

    def test_a_worker_on_its_own_wall_steps_aside(self):
        """"墙就在脚下"（砌过、被自己盖住）⇒ 这一回合是 `move`，不是又一次 `build`
        —— 否则每回合发的都是与上一回合逐字段相同的一条 `build`，无限重复。"""
        worker = Worker(1, self.ON, {"stone": 5})
        turn = self._turn(self.BUILT | {self.ON}, worker)
        cmds = plan(turn)
        self.assertEqual(len(cmds), 1, f"只有一个工人、只该有一条指令：{cmds}")
        self.assertEqual(cmds["1"]["action"], "move", "站着的那一格不许再砌一遍")
        cell = Pos(cmds["1"]["targetPos"][0]["x"], cmds["1"]["targetPos"][0]["y"])
        self.assertEqual(worker.pos.dist(cell), 1, "挪开是**一步一格**，不能瞬移")
        self.assertNotIn(cell, turn.map.blocked, "落点得是可走的格")

    def test_a_worker_on_the_last_cell_still_finishes_the_ring(self):
        """脚下是最后一格没砌的墙 ⇒ 这一回合让开、下一回合照样砌上（收敛，不是干等）。

        这一条钉的是"先挪开"没有把闸门变成拖延：挪开之后就贴着它了（`dist == 1`），
        下一回合 `free[0]` 还是它、位置却合法 ⇒ 稳稳砌完。
        """
        worker = Worker(1, self.ON, {"stone": 5})
        built = {c for c in wall_cells(self.BASE, 41) if c != self.ON}
        cmds = plan(self._turn(built, worker))
        self.assertEqual(cmds["1"]["action"], "move", "先让开")
        aside = Pos(cmds["1"]["targetPos"][0]["x"], cmds["1"]["targetPos"][0]["y"])

        worker = Worker(1, aside, {"stone": 5})  # 判题器照做，下一回合
        cmds = plan(self._turn(built, worker))
        self.assertEqual(cmds["1"]["action"], "build", "下一回合就该把这一格砌上，不能干等")
        self.assertEqual(cmds["1"]["name"], WALL)
        self.assertEqual(
            Pos(cmds["1"]["targetPos"][0]["x"], cmds["1"]["targetPos"][0]["y"]),
            self.ON,
            "砌的还是原来那一格（绕一圈回到同一格 ⇒ 不是收敛，是踱步）",
        )


class LastCellFromOutsideTest(unittest.TestCase):
    """环上只剩最后一格 ⇒ 站到**盒子外面**去砌（用户口径）。

    站在环里把最后一格盖上，砌墙的人自己就被封在封死的环里了（盒内只剩基地 + 武器环那点
    走廊，而出入口在后方通道）。只在"环上只剩一格"时生效 —— 平常照旧就近站，否则每一格
    都要先绕到盒外，第 1 天根本砌不完（`BuildWallTest` 那条整网用例钉的就是这个）。
    """

    BASE = Pos(10, 24)
    WEAPONS = _records({Pos(9, 25): "rocket", Pos(12, 22): "rocket", Pos(12, 25): "gatling"})
    LAST = Pos(13, 24)  # 正面列中间那一格
    INSIDE = Pos(12, 24)  # 贴着 `LAST` 的盒内格 —— 旧口径就在这儿砌

    def setUp(self) -> None:
        _reset_ledgers()

    def _turn(self, built: set[Pos], worker: Worker, extra: dict[Pos, str] | None = None) -> Turn:
        return Turn(
            round_no=40,
            map=Map(
                (41, 32),
                _terrain(
                    self.WEAPONS,
                    {self.BASE: "station"},
                    extra or {},
                    {c: WALL for c in built},
                    {worker.pos: "worker"},
                ),
            ),
            roles=(worker,),
            gold=0,
            weapons=self.WEAPONS,
        )

    def test_the_last_cell_is_built_from_outside_the_box(self):
        """一路走到砌上，且**砌它的人站在盒子外面**。

        这一条只能串起来跑：第一回合只是往门外挪一步（人还在盒里），得走到位才看得出
        "从哪儿砌的"。旧口径下 `step_toward` 会把它停在盒内的 `(12,24)` 上就地砌上。

        回合数卡了个上限：环上那个缺口**就是**出盒子的近路（穿过它 2 步），走去后方通道绕
        整圈是 12 步 —— 把 `target` 圈进软避让就会退化成绕圈，卡住这条退化。
        """
        built = {c for c in wall_cells(self.BASE, 41) if c != self.LAST}
        worker = Worker(1, self.INSIDE, {"stone": 5})
        for rounds in range(40):
            cmd = plan(self._turn(built, worker)).get("1")
            self.assertIsNotNone(cmd, "最后一格必须有人去砌 —— 不能干等")
            if cmd["action"] == "build":
                self.assertEqual(
                    Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"]),
                    self.LAST,
                    "砌的还是最后那一格",
                )
                self.assertEqual(worker.pos.dist(self.LAST), 1, "站位即建造位")
                self.assertNotIn(
                    worker.pos, box_cells(self.BASE), "砌最后一格的人得站在盒子外面"
                )
                self.assertLessEqual(rounds, 4, "出盒子走的是环上那个缺口，不是绕后方通道整圈")
                return
            worker = Worker(1, Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"]), {"stone": 5})
        self.fail("40 回合都没把最后一格砌上")

    def test_a_second_last_cell_is_still_built_from_the_inside(self):
        """环上还剩**两格**时照旧就近砌 —— 判据是"只剩一格"，不是"快砌完了"。

        提前绕盒外的话每一步墙都要多走十几步，第 1 天砌不满 14 格。
        """
        built = {c for c in wall_cells(self.BASE, 41) if c not in (self.LAST, Pos(11, 26))}
        cmds = plan(self._turn(built, Worker(1, self.INSIDE, {"stone": 5})))
        self.assertEqual(
            cmds["1"],
            {"action": "build", "name": WALL, "targetPos": [{"x": 13, "y": 24}]},
            "两格时照旧就近砌（就近 = 盒内的 (12,24)）",
        )

    def test_without_an_outside_cell_it_builds_from_the_inside_anyway(self):
        """盒外三个邻格全被占 ⇒ 照旧就近砌：环该封还得封，不因为绕不出去就永远不封。

        收尾动作是用户口径的**尽力而为**，不是闸门 —— 真要拦"封了会把人关住"的是
        `_trapped` / `gated` 那套（见 `WallGateTest`）。
        """
        built = {c for c in wall_cells(self.BASE, 41) if c != self.LAST}
        blocked = {Pos(14, 23): "robot:1", Pos(14, 24): "robot:1", Pos(14, 25): "robot:1"}
        cmds = plan(self._turn(built, Worker(1, self.INSIDE, {"stone": 5}), extra=blocked))
        self.assertEqual(cmds["1"]["action"], "build", "没地方站就就近砌上，不能干等")


class SegmentSplitTest(unittest.TestCase):
    """两个工人的切段分配：A 领前段首格、B 领后段首格，沿环同向推进。

    不切段的话两人都从 `free[0]` 取、靠认领错开一格 ⇒ B 的目标贴着 A 的目标，两人在
    同一段墙上挤（抢位 / 堵路 / B 的 BFS 路径横穿 A 的工地）。切段后 A 从队头往后砌、
    B 从中点往后砌：后段的任何一格都不高于前段还剩下的 —— 优先级保住、互不抢同一格。
    """

    BASE = Pos(10, 24)
    WEAPONS = _records({Pos(9, 23): "gatling", Pos(9, 24): "railgun", Pos(9, 22): "rocket"})

    def setUp(self) -> None:
        _reset_ledgers()

    def _spot(self, cell: Pos, occupied: set[Pos]) -> Pos:
        """`cell` 的某个空邻格（站位即建造位 —— 贴着目标即可 `build`）。"""
        for d in STEPS:
            p = Pos(cell.x + d.x, cell.y + d.y)
            if 0 <= p.x < 41 and 0 <= p.y < 32 and p not in occupied:
                return p
        raise AssertionError(f"{cell} 找不到空邻格")

    def _turn(self, *workers: Worker) -> Turn:
        occupied = set(base_cells(self.BASE)) | {w.pos for w in self.WEAPONS}
        grid = _terrain(self.WEAPONS, {self.BASE: "station"})
        grid.update({w.pos: "worker" for w in workers})
        return Turn(
            round_no=1,
            map=Map((41, 32), grid),
            roles=workers,
            gold=0,
            weapons=self.WEAPONS,
        )

    def test_two_workers_take_different_segments(self):
        gaps = list(wall_cells(self.BASE, 41))
        mid = (len(gaps) + 1) // 2
        occupied = set(base_cells(self.BASE)) | {w.pos for w in self.WEAPONS}
        a = Worker(10010, self._spot(gaps[0], occupied), {"stone": 5})
        b = Worker(10012, self._spot(gaps[mid], occupied | {a.pos}), {"stone": 5})
        cmds = plan(self._turn(a, b))

        builds = {
            Pos(v["targetPos"][0]["x"], v["targetPos"][0]["y"])
            for v in cmds.values()
            if v["action"] == "build"
        }
        self.assertEqual(
            builds,
            {gaps[0], gaps[mid]},
            "A 领前段首格、B 领后段首格 —— 旧机制下 B 只会跟着 A 砍 free[1]",
        )

    def test_a_lone_worker_still_sweeps_the_whole_ring(self):
        """只有一个工人 ⇒ 整段环都归它 —— 切段按在场工人数算，不把环掐掉一半。"""
        gaps = list(wall_cells(self.BASE, 41))
        occupied = set(base_cells(self.BASE)) | {w.pos for w in self.WEAPONS}
        roles = {10010: Worker(10010, self._spot(gaps[0], occupied), {"stone": 30})}
        entries = _terrain(self.WEAPONS, {self.BASE: "station"})
        built: list[Pos] = []
        for rnd in range(1, 13):
            grid = {**entries, **{r.pos: "worker" for r in roles.values()}}
            turn = Turn(
                round_no=rnd, map=Map((41, 32), grid), roles=tuple(roles.values()), gold=0,
                weapons=self.WEAPONS,
            )
            for key, cmd in plan(turn).items():
                role = roles[int(key)]
                if cmd["action"] == "build":
                    cell = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
                    entries[cell] = WALL
                    built.append(cell)
                    roles[int(key)] = Worker(int(key), role.pos, {"stone": role.stone - 1})
                elif cmd["action"] == "move":
                    cell = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
                    roles[int(key)] = Worker(int(key), cell, {"stone": role.stone})
        self.assertGreaterEqual(
            len({c for c in built}), 5, "切段把环掐成一半的话，独工砌到半环就停了"
        )
        self.assertEqual(len(built), len(set(built)), "同一格砌两遍（石头白花）")


class PathReserveTest(unittest.TestCase):
    """路径预留与软避让：A 认领差事时把 BFS 路径记进黑板，
    B 选路时当 `avoid` 绕开；绕不开就退回硬障碍照走 —— 让路的代价不能是原地卡死
    （双双停住比擦肩而过更亏，§4.5.4 碰撞两败）。"""

    def setUp(self) -> None:
        _reset_ledgers()

    def test_reserve_path_records_the_intermediate_cells(self):
        turn = Turn(round_no=1, map=Map((41, 32), {Pos(10, 24): "station"}), roles=(), gold=0)
        walker = Worker(1, Pos(5, 5), {})
        goal = Pos(9, 5)
        paths: set[Pos] = set()
        planner._reserve_path(walker, goal, turn, set(), paths)
        # 别钉死具体格：`step_toward` 的并列最短路会走对角，坐标取决于实现细节。
        # 钉契约：恰好走 `dist−1` 步、每步把切比雪夫距离缩 1、终点贴着目标。
        self.assertEqual(len(paths), 3)
        self.assertEqual({p.dist(goal) for p in paths}, {3, 2, 1})

    def test_step_treats_avoid_as_soft(self):
        """B 的唯一路线被 A 的路径盖住 ⇒ 退回硬障碍照走（擦肩），而不是不动。"""
        walls = (
            {Pos(x, 4): "rock" for x in range(3, 10)}
            | {Pos(x, 6): "rock" for x in range(3, 10)}
            | {Pos(3, 5): "rock"}  # 封死左端 —— avoid 盖住走廊中段后真的无路可绕
        )
        grid = {Pos(10, 24): "station", **walls}
        walker = Worker(1, Pos(5, 5), {})
        grid[walker.pos] = "worker"
        turn = Turn(round_no=1, map=Map((41, 32), grid), roles=(walker,), gold=0)
        q = core._Queue(turn)
        self.assertTrue(q.step(walker, Pos(9, 5), avoid={Pos(6, 5), Pos(7, 5), Pos(8, 5)}))
        planner._walk_out(turn, q)
        self.assertEqual(
            q.cmds["1"],
            {"action": "move", "targetPos": [{"x": 6, "y": 5}]},
            "avoid 只是软的：绕不开就退回硬障碍，绝不原地卡死",
        )


class DemolishTest(unittest.TestCase):
    """第 2 级的另一半：**L1 弱墙直接拆了重砌**（用户口径 2）。

    弱墙 = 血 < `WALL_REPAIR_HP`(200)。拆一回合、砌一回合 = 2 回合 1 块石头，比 20 金的围墙升级券
    便宜。**L2/L3 的损伤不碰**：拆了只能重砌回 L1（掉一级），它们的损伤留给第 3 级的升级券
    （升级同时回满血）与夜里的修复包（`night.repair_wall`）。
    """

    BASE = Pos(10, 24)

    def setUp(self) -> None:
        _reset_ledgers()

    def _turn(self, weak: tuple[Wall, ...], *, stone: int, at: Pos, gaps: Iterable[Pos] = ()):
        ring = wall_cells(self.BASE, 41)
        walls = {c: WALL for c in ring if c not in set(gaps)}
        walls.update({w.pos: WALL for w in weak})
        grid = {**walls, self.BASE: "station", at: "worker"}
        worker = Worker(10010, at, {"stone": stone} if stone else {})
        return Turn(
            round_no=1,
            map=Map((41, 32), grid),
            roles=(worker,),
            gold=0,
            weapons=_records({Pos(12, 24): "rocket"}),  # 武器齐了 ⇒ 轮得到第 2 级
            walls=weak,
        )

    def _beside(self, cell: Pos) -> Pos:
        for d in STEPS:
            p = Pos(cell.x + d.x, cell.y + d.y)
            if 0 <= p.x < 41 and 0 <= p.y < 32 and p != cell:
                return p
        raise AssertionError("没有邻居格")

    def test_a_level_one_wall_below_the_threshold_is_demolished(self):
        """L1 墙血低于 `WALL_REPAIR_HP`(200) ⇒ 直接拆（贴着就 `remove`），下一回合再砌回满血。"""
        spot = wall_cells(self.BASE, 41)[0]
        wall = Wall(40000, spot, 199, 1)  # < WALL_REPAIR_HP(200)
        cmd = plan(self._turn((wall,), stone=1, at=self._beside(spot)))[str(10010)]
        self.assertEqual(cmd["action"], "remove", f"该拆了重砌：{cmd}")
        self.assertEqual(Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"]), spot)

    def test_a_level_one_wall_above_the_threshold_is_left_alone(self):
        """血不低于 200 ⇒ 环是完备的，这一级整个不生效（不拆、也不用券）。"""
        spot = wall_cells(self.BASE, 41)[0]
        wall = Wall(40000, spot, 200, 1)  # 正好 200 ⇒ 不拆
        cmds = plan(self._turn((wall,), stone=1, at=self._beside(spot)))
        self.assertNotIn(cmds.get("10010", {}).get("action"), ("remove", "build"), f"不碰它：{cmds}")

    def test_a_level_two_wall_below_the_threshold_is_never_touched(self):
        """L2 弱墙不归第 2 级管（拆了只能重砌回 L1，掉一级）⇒ 不拆也不修（用户拍板）。"""
        spot = wall_cells(self.BASE, 41)[0]
        wall = Wall(40000, spot, 150, 2)
        cmds = plan(self._turn((wall,), stone=1, at=self._beside(spot)))
        self.assertNotIn(
            cmds.get("10010", {}).get("action"), ("remove", "use", "build"), f"不碰它：{cmds}"
        )

    def test_a_gap_comes_before_the_demolition(self):
        """缺口比弱墙急：环上敞着一格时先补那格，弱墙下一回合再说（缺口是夜里机器人进来的门）。"""
        ring = wall_cells(self.BASE, 41)
        gap, weak_spot = ring[0], ring[-1]
        wall = Wall(40000, weak_spot, 199, 1)
        at = self._beside(gap)
        cmd = plan(self._turn((wall,), stone=1, at=at, gaps=(gap,)))[str(10010)]
        self.assertEqual(cmd["action"], "build", f"先补缺口：{cmd}")
        self.assertEqual(Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"]), gap)

    def test_a_worker_without_a_stone_does_not_demolish(self):
        """手里没石头就不拆：拆完那格是敞着的，砌不回来等于白开一个洞。"""
        spot = wall_cells(self.BASE, 41)[0]
        wall = Wall(40000, spot, 199, 1)
        cmds = plan(self._turn((wall,), stone=0, at=self._beside(spot)))
        self.assertNotIn("remove", {c["action"] for c in cmds.values()}, f"没石头不许拆：{cmds}")


class WallPriorityTest(unittest.TestCase):
    """白天工人的优先级链：建武器 > 筹资 > 砌墙 > 修墙 > 经济线 —— 一件不行才轮到下一件。

    武器最高（口径：无论哪一天，武器没了先建）：份额有缺且钱够 ⇒ 建/走向落点；
    钱不够但筹资可行（有小贩、有价可卖）⇒ 整条墙线让位，先卖背包的货、再采最值钱的
    矿，凑够 25 金币回来建；筹资不可行 ⇒ 照旧走墙线。墙线内部**先补缺口、后补血**：
    环砌满了（`target` 是 `None`）才轮到修弱墙。升级线只在武器齐了之后才跑。
    `_build_walls` 返回 `True` = 本回合指令已由墙线产出（或 gated 待命）；`False` =
    什么都没发过，兜底链接手。
    """

    BASE = Pos(10, 24)

    def setUp(self) -> None:
        _reset_ledgers()
        AGENT.reset()  # `_mine_spare_ore` 读价格期望（跨回合状态），不清会跨用例串味

    def _turn(
        self,
        roles: tuple[BaseRole, ...],
        ground: dict[Pos, str],
        *,
        gold: int = 0,
        prices: dict[str, int] | None = None,
        walls: Iterable[Pos] = (),
        weak: tuple[Wall, ...] = (),
        phase_task: str = "",
        weapons: tuple[Weapon, ...] = (),
    ) -> Turn:
        grid = {**ground, **{c: WALL for c in walls}, **{w.pos: w.kind for w in weapons}}
        grid |= {r.pos: r.type_name for r in roles}  # 单位铺在最后（与 model._entries 一致）
        return Turn(
            round_no=1,
            map=Map((41, 32), grid),
            roles=roles,
            gold=gold,
            phase_task=phase_task,
            weapons=weapons,
            walls=weak,
            vendor_prices=prices if prices is not None else {},
        )

    def test_weapons_beat_walls_when_affordable(self):
        """顺序锁：武器是最高优先级 —— 份额有缺且钱够（75 金）时，哪怕环上一格没砌、
        石矿贴着脚，工人也先建武器。武器排后的话这里会发出"采石"，立即挂。"""
        worker = Worker(10010, Pos(9, 24))  # 站在共用操作位上（贴着三个落点）
        turn = self._turn(
            (worker,),
            {self.BASE: "station", Pos(4, 24): "stone", Pos(6, 24): "copper"},
            gold=75,
            prices={"stone": 1, "copper": 5},
        )
        cmd = plan(turn)[str(10010)]
        self.assertEqual(cmd["action"], "build", "先建武器，不是先采石/采铜")
        self.assertEqual(cmd["name"], "rocket")
        self.assertEqual(
            Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"]),
            weapon_sites(self.BASE, 41)[0],
        )

    def test_a_weapon_gap_without_gold_fundraises(self):
        """份额有缺、钱不够、筹资可行（有小贩有价）⇒ 整条墙线让位：先卖背包的货
        （空）、再采最值钱的矿 —— 铜比石头值钱就去采铜，凑够 25 金币回来建武器。"""
        worker = Worker(10010, Pos(5, 23))
        turn = self._turn(
            (worker,),
            {
                self.BASE: "station", Pos(4, 24): "stone", Pos(6, 24): "copper",
                Pos(7, 26): "vendor",
            },
            prices={"stone": 1, "copper": 5},
        )
        cmd = plan(turn)[str(10010)]
        self.assertEqual(cmd["action"], "collect", "筹资采最值钱的矿，不是先采石砌墙")
        cell = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertEqual(cell, Pos(6, 24), "铜（5 金）优先于石头（1 金）")

    def test_fundraising_sells_the_whole_load(self):
        """筹资那一趟把货**卖光**（新口径：石头留底那两档都删了）。

        旧口径在这里留 1 块（`STONE_KEEP_RAISING`）、平常留 3 块（`STONE_RESERVE`）—— 两条
        都随"够了就行"的采石口径一起删了：手里几块就卖几块。
        """
        worker = Worker(10010, Pos(7, 25), {"stone": 8})  # 贴着小贩 (7,26)
        turn = self._turn(
            (worker,),
            {self.BASE: "station", Pos(4, 24): "stone", Pos(7, 26): "vendor"},
            gold=20,  # 差 5 金建武器；背包里的石头（8）凑得上
            prices={"stone": 1},
        )
        cmd = plan(turn)[str(10010)]
        self.assertEqual(
            cmd, {"action": "sell", "name": "stone", "num": 8}, "一次卖光，不留底"
        )

    def test_fundraising_beats_repairing_a_weak_wall(self):
        """武器有缺 + 钱不够 + 筹资可行 ⇒ 连**修墙**也让位（用户口径：武器 > 筹资 > 墙）。

        弱墙就贴在脚边、包里还有升级券 —— 筹资让到墙线后面的话，这条会去砌墙（环上一格
        没砌）或者 `use` 那面弱墙，立即挂。
        """
        worker = Worker(10010, Pos(12, 23), {"WallUpgradeVoucher2": 1, "stone": 1})
        turn = self._turn(
            (worker,),
            {self.BASE: "station", Pos(6, 24): "copper", Pos(7, 26): "vendor"},
            prices={"copper": 5},
            walls=[Pos(13, 22)],  # 那格墙照旧挡路
            weak=(Wall(40000, Pos(13, 22), 299, 2),),
        )
        cmd = plan(turn)[str(10010)]
        self.assertNotIn(cmd["action"], ("use", "remove"), f"别碰那面弱墙：{cmd}")
        self.assertEqual(cmd["action"], "move", f"该往矿那边去：{cmd}")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(Pos(6, 24)), worker.pos.dist(Pos(6, 24)), "朝铜矿走一格")

    def test_a_gap_is_filled_before_a_weak_wall_is_repaired(self):
        """墙线内部：**先补缺口、后补血**。

        环上敞着两个缺口、脚边就有一面弱墙，手里正好有那面墙要的券**和**一块石头 —— 先砌墙。
        补洞比补血急：缺口是敞开的（夜里机器人从那儿进来），弱墙好歹还挡着；补一格又只要
        1 块石头、当场就能合上。修墙线排到 `_build_walls` 前面时这条会发出 `use`，立即挂。

        缺口留**两格**（一格会走"从盒外砌"那一支，发出的是 `move`）。
        """
        worker = Worker(10010, Pos(12, 23), {"WallUpgradeVoucher2": 1, "stone": 1})
        gaps = {Pos(13, 24), Pos(13, 26)}  # 正面列上的两格
        weak = Pos(13, 22)  # 也是正面列，与工人切比雪夫 1（修墙线一开火就是它）
        self.assertEqual(worker.pos.dist(weak), 1, "夹具前提：弱墙与缺口都贴着工人")
        turn = self._turn(
            (worker,),
            {self.BASE: "station", **{c: WALL for c in set(wall_cells(self.BASE, 41)) - gaps}},
            weak=(Wall(40000, weak, 299, 2),),
        )
        cmd = plan(turn)[str(10010)]
        self.assertEqual(cmd["action"], "build", f"先补环上的缺口：{cmd}")
        self.assertEqual(cmd["name"], "wall")
        self.assertEqual(Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"]), Pos(13, 24))

    def test_both_workers_fundraise_instead_of_walling(self):
        """用户口径 1.2.2：钱不够建武器 ⇒ **两个工人一起挖矿**，谁都不去砌墙。

        环上一格没砌（`target` 都非空）、石矿贴着脚 —— 顺序反了两人都会去砌墙/采石。
        """
        a = Worker(10010, Pos(5, 23))
        b = Worker(10012, Pos(5, 25))
        turn = self._turn(
            (a, b),
            {
                self.BASE: "station",
                Pos(4, 24): "stone", Pos(6, 24): "copper", Pos(6, 26): "iron",
                Pos(7, 26): "vendor",
            },
            prices={"stone": 1, "iron": 3, "copper": 5},
        )
        cmds = plan(turn)
        actions = {cid: cmd["action"] for cid, cmd in cmds.items()}
        self.assertEqual(set(actions.values()), {"collect"}, f"两个人都该去挖矿筹资：{cmds}")
        cells = {
            Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"]) for cmd in cmds.values()
        }
        self.assertEqual(len(cells), 2, f"各挖一座，不挤同一座矿：{cmds}")
        self.assertEqual(cells, {Pos(6, 24), Pos(6, 26)}, "挑最值钱的两座（铜 5、铁 3）")

    def test_fundraising_yields_to_walls_when_no_vendor(self):
        """筹资不可行（没小贩，矿卖不出去）⇒ 不筹资，照旧走墙线 —— 没有通往
        25 金币的路时，墙是剩下最值得干的事。"""
        worker = Worker(10010, Pos(5, 23))
        turn = self._turn(
            (worker,),
            {self.BASE: "station", Pos(4, 24): "stone", Pos(6, 24): "copper"},
            prices={"stone": 1, "copper": 5},
        )
        cmd = plan(turn)[str(10010)]
        self.assertEqual(cmd["action"], "collect", "筹资不可行 ⇒ 走墙线采石")
        cell = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertEqual(cell, Pos(4, 24))

    def test_a_stopped_ore_does_not_count_as_a_way_to_raise_money(self):
        """能卖的矿全被新闻钉了停工 ⇒ 筹资判成**不可行**（`_can_fund`）⇒ 整条墙线接回来
        （该采石采石），而不是派个人出去干站一回合。

        对照：同一局面去掉新闻 ⇒ 奔铜矿筹资（第 1 级"钱不够 ⇒ 挖最贵的矿"）。
        """
        ground = {
            self.BASE: "station", Pos(4, 24): "stone", Pos(6, 24): "copper",
            Pos(7, 26): "vendor",
        }
        worker = Worker(10010, Pos(5, 23))
        prices = {"stone": 1, "copper": 5}
        today = self._turn((worker,), ground, prices=prices)
        cmd = plan(today)["10010"]
        self.assertEqual(
            Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"]),
            Pos(6, 24),
            "没人停工 ⇒ 筹资采最值钱的铜",
        )
        core.record_news([("copper", "stop", 0, 1)], today)  # 起始天 0 = 今天起
        cmd = plan(self._turn((worker,), ground, prices=prices))["10010"]
        self.assertEqual(
            Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"]),
            Pos(4, 24),
            f"铜卖不掉了 ⇒ 退出筹资、走墙线采石：{cmd}",
        )

    def test_a_stopped_stone_mine_is_not_walked_to(self):
        """石矿被新闻钉了停工（今天起）⇒ 采石那一趟不发，改走链尾那条兜底（朝缺口挪一格）。

        新闻说的是**矿种**，同一种矿在地图上整类停 —— 所以这里没有"改去另一座石矿"这条退路，
        只剩"拿手里的先砌"（手里没有 ⇒ 就是朝缺口走）。对照：没这条新闻 ⇒ 贴着矿当场
        `collect`，一步都不多走。
        """
        worker = Worker(10010, Pos(5, 23))
        mine = Pos(4, 24)
        ground = {self.BASE: "station", mine: "stone"}
        today = self._turn((worker,), ground, prices={"stone": 1})
        self.assertEqual(plan(today)["10010"]["action"], "collect", "对照：贴着矿就采")
        core.record_news([("stone", "stop", 0, 1)], today)
        cmd = plan(self._turn((worker,), ground, prices={"stone": 1}))["10010"]
        self.assertNotEqual(cmd["action"], "collect", f"不为停工的矿发采集：{cmd}")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertGreaterEqual(step.dist(mine), worker.pos.dist(mine), f"也不朝它走：{cmd}")


    def test_no_stone_mine_means_build_with_what_is_in_hand(self):
        """环上有缺口、手里就一块石头、地图上**没有石矿** ⇒ 拿这点石头先砌（能做多少做多少）。

        不这么写的话工人会空转 —— 采不到石头 ≠ 这一回合没事干。别处的早退（`_mine_stone`
        返回 False）都不能把这一支带走。
        """
        gap = wall_cells(self.BASE, 41)[0]
        walls = {c: WALL for c in wall_cells(self.BASE, 41) if c != gap}
        worker = Worker(10010, Pos(5, 23), {"stone": 1})
        at = next(
            Pos(gap.x + d.x, gap.y + d.y)
            for d in STEPS
            if 0 <= gap.x + d.x < 41 and 0 <= gap.y + d.y < 32 and Pos(gap.x + d.x, gap.y + d.y) not in walls
        )
        worker = Worker(10010, at, {"stone": 1})
        grid = {**walls, self.BASE: "station", at: "worker"}
        turn = Turn(
            round_no=1,
            map=Map((41, 32), grid),
            roles=(worker,),
            gold=0,
            weapons=_records({Pos(12, 24): "rocket"}),
        )
        cmd = plan(turn)[str(10010)]
        self.assertEqual(cmd["action"], "build", f"采不到石就用手里的砌：{cmd}")
        self.assertEqual(
            Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"]), gap, "砌的是那个缺口"
        )

    def test_no_buying_or_upgrading_while_the_weapons_are_incomplete(self):
        """武器没建满 ⇒ 券线整个不跑（第 3 级的前提）：持券、贴着小贩也不许 `use`/`buy`。

        拿建武器的钱去买券是本末倒置（旧口径"武器 ok 才升级"）。这一局面里武器差一座、
        钱不够、又没有小贩 ⇒ 谁也帮不上忙，工人就该什么都不发。
        """
        weapons = (Weapon(10020, "rocket", Pos(12, 24), 10, 0),)
        worker = Worker(10010, Pos(9, 24), {"WeaponUpgradeVoucher1": 1})
        turn = self._turn(
            (Pioneer(10011, Pos(20, 20)), worker),  # 两个角色一座武器 ⇒ 还缺一座
            {self.BASE: "station", Pos(15, 24): "copper"},
            walls=wall_cells(self.BASE, 41),  # 环是满的：没有墙线的事
            prices={"copper": 5},
            weapons=weapons,
        )
        cmd = plan(turn).get(str(10010))
        self.assertIsNone(cmd, f"武器有缺 ⇒ 这一回合不发指令，更不许用券：{cmd}")

    def test_a_full_worker_does_not_hoard_the_stone_mine(self):
        """石矿认领只发生在"真要采"之后：石头已够的工人（want=0）不许占住矿格 ——
        否则缺石的同事这一回合采不到石、被挤去经济线，环白白慢一拍。
        认领发生在 `want` 计算之前的话，B（空手）会去采铜，立即挂。

        ⚠️ 这块铜矿的位置是**故意**的：它离 A 更近 ⇒ A 走经济线时挑的是它（不是石矿），
        B 才拿得到那座石矿。往远处挪一格，A 的经济线就会改挑石矿、这条用例立刻失去鉴别力
        （顺手采那一支另有用例 `OnTheWayTest`，且**只在去砌墙的路上**生效，不掺采石那一趟）。"""
        gaps = {Pos(13, 21), Pos(13, 24)}
        walls = set(wall_cells(self.BASE, 41)) - gaps
        full = Worker(10010, Pos(5, 23), {"stone": 9})   # 段里只剩 1 格 ⇒ want=0
        empty = Worker(10012, Pos(6, 24))                # 还差 1 块石头
        turn = self._turn(
            (Pioneer(10011, Pos(20, 20)), full, empty),
            {self.BASE: "station", Pos(4, 24): "stone", Pos(7, 24): "copper"},
            prices={"stone": 1, "copper": 5},
            walls=walls,
        )
        cmd = plan(turn)[str(10012)]
        self.assertEqual(cmd["action"], "move", "B 该去采石，不是被挤去采铜")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertEqual(step.dist(Pos(4, 24)), 1, "这一步朝石矿迈（贴着矿那一格）")


class OnTheWayTest(unittest.TestCase):
    """赶路时的"顺手采"（用户口径：正事优先，最多顺手拿一次）。

    只挂在**去砌墙的路上**（`_build_gap`）：脚边有矿、这一趟还没顺手采过、且"到工地 + 这 1 回合
    + 余量"仍在白天预算内 ⇒ `collect` 一回合再走。砌墙缺石时**石头优先**（一块石 = 省下专程采石
    的 2 回合）。采石那一趟（`_mine_stone`）**不掺**：那一趟的正事就是石料。
    """

    BASE = Pos(10, 24)
    GAP = Pos(13, 23)  # 环上留一个缺口 ⇒ `WallLine` 要砌它
    STAND = Pos(6, 24)  # 工人站位（盒外西侧）
    COPPER = Pos(7, 24)  # 他脚边的铜矿（切比雪夫 1）
    STONE = Pos(5, 24)  # 他另一侧的石矿（切比雪夫 1）

    def setUp(self) -> None:
        _reset_ledgers()

    def _turn(
        self,
        worker: Worker,
        *,
        ores: dict[Pos, str] | None = None,
        round_no: int = 1,
        extra: tuple[BaseRole, ...] = (),
    ) -> Turn:
        ring = [c for c in wall_cells(self.BASE, 41) if c != self.GAP]
        grid: dict[Pos, str] = {self.BASE: "station"}
        grid.update({c: WALL for c in ring})
        grid.update(ores if ores is not None else {self.COPPER: "copper"})
        for role in (worker, *extra):
            grid[role.pos] = role.type_name
        return Turn(
            round_no=round_no,
            map=Map((41, 32), grid),
            roles=(worker, *extra),
            gold=0,
            vendor_prices={"stone": 1, "copper": 5},
        )

    def test_a_walk_to_the_wall_picks_up_the_ore_underfoot(self):
        """去砌墙的路上、脚边正好有矿 ⇒ 先采一回合（用户口径的"顺手采了，然后再去修墙"）。"""
        worker = Worker(2, self.STAND, {"stone": 3})  # 石头够砌那一个缺口 ⇒ 走 `_build_gap`
        cmds = plan(self._turn(worker))
        self.assertEqual(
            cmds["2"],
            {"action": "collect", "targetPos": [{"x": 7, "y": 24}]},
            f"该顺手采铜：{cmds}",
        )

    def test_the_quota_is_one_collect_per_trip(self):
        """一趟只顺手采一次：同一趟里再规划一次就不再采，朝工地走（收工时这一趟结束、下趟重新数）。"""
        worker = Worker(2, self.STAND, {"stone": 3})
        turn = self._turn(worker)
        self.assertEqual(plan(turn)["2"]["action"], "collect", "第一回合顺手采")
        cmd = plan(turn)["2"]  # 同一趟（同一个目标格）再算一次
        self.assertEqual(cmd["action"], "move", f"额度用完 ⇒ 专心赶路：{cmd}")

    def test_a_tight_day_walks_on_instead_of_detouring(self):
        """时间不宽裕（白天只剩 1 回合）⇒ 不顺手采，直接朝工地走：正事优先。"""
        worker = Worker(2, self.STAND, {"stone": 3})
        cmd = plan(self._turn(worker, round_no=70))["2"]
        self.assertEqual(cmd["action"], "move", f"该走路：{cmd}")

    def test_a_short_wall_gets_the_stone_underfoot(self):
        """砌墙缺石 ⇒ 脚边有石就采石（顺手采的物料优先级）。"""
        worker = Worker(2, self.STAND, {"stone": 0})  # 一块都没有 ⇒ 缺石
        cmds = plan(self._turn(worker, ores={self.STONE: "stone", self.COPPER: "copper"}))
        self.assertEqual(cmds["2"]["action"], "collect")
        self.assertEqual(
            cmds["2"]["targetPos"], [{"x": 5, "y": 24}], f"缺石时顺手采石、不采铜：{cmds}"
        )

    def test_a_stocked_worker_takes_the_pricier_ore(self):
        """石头够 ⇒ 顺手采按收购价挑（铜 5 > 石 1）。"""
        worker = Worker(2, self.STAND, {"stone": 3})
        cmds = plan(self._turn(worker, ores={self.STONE: "stone", self.COPPER: "copper"}))
        self.assertEqual(
            cmds["2"]["targetPos"], [{"x": 7, "y": 24}], f"石头够 ⇒ 顺手采铜：{cmds}"
        )


class SellCargoTest(unittest.TestCase):
    """第 3 级里的"卖货"那一支：贴着就卖光，隔着就走一步。

    只留两道门：**有货**、**有小贩且走得到** —— 旧的"够本门"（货值 ≥ 2 × 到小贩的步数）
    与"石头留底"两档都删了（用户口径 3）。**卖不卖由券价触发**：只有"现钱 + 背包货值够买
    下一张券"（或已经绕到小贩旁边）才去卖，其余时候接着挖矿（见 `VoucherLineTest`）。
    """

    BASE = Pos(10, 24)
    WEAPONS = _records(_guns_at_sites(Pos(10, 24)))
    SHOP = Pos(22, 18)
    VENDOR = Pos(20, 16)
    PRICES = {"stone": 1, "iron": 3, "copper": 5}
    SHOP_PRICES = {"WeaponUpgradeVoucher1": 100, "WeaponUpgradeVoucher2": 150}
    MINE = Pos(20, 30)

    def setUp(self) -> None:
        _reset_ledgers()

    def _turn(self, *, at: Pos, bag: dict[str, int], gold: int = 0, vendor: bool = True, ring: bool = True) -> Turn:
        walls = {c: WALL for c in wall_cells(self.BASE, 41)} if ring else {}
        grid = _terrain(
            self.WEAPONS,
            {self.BASE: "station"},
            walls,
            {self.MINE: "copper"},
            {self.SHOP: "weaponShop"},
            {self.VENDOR: "vendor"} if vendor else {},
        )
        grid[at] = "worker"
        return Turn(
            round_no=1,
            map=Map((41, 32), grid),
            roles=(Worker(10010, at, dict(bag)),),
            gold=gold,
            weapons=self.WEAPONS,
            vendor_prices=self.PRICES,
            shop_prices=self.SHOP_PRICES,
        )

    def test_standing_next_to_the_vendor_sells_the_whole_load(self):
        """贴着小贩卖光那一种货（一次卖光，`num` = 手上全部件数）。"""
        turn = self._turn(at=Pos(19, 16), bag={"copper": 3})  # 切比雪夫 1 ⇒ 贴着
        self.assertEqual(
            plan(turn)["10010"], {"action": "sell", "name": "copper", "num": 3}
        )

    def test_one_step_away_still_walks(self):
        """差一格还不许卖（`steps_between == 0` 才是"贴着"）—— 隔一格就发 `sell` 是非法指令。"""
        turn = self._turn(at=Pos(18, 16), bag={"copper": 3})
        cmd = plan(turn)["10010"]
        self.assertEqual(cmd["action"], "move")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(self.VENDOR), Pos(18, 16).dist(self.VENDOR), "朝小贩走一格")

    def test_the_pricier_ore_is_sold_first(self):
        """一回合只发得出一条 `sell` ⇒ 先卖收购价最高的那一种。"""
        turn = self._turn(at=Pos(19, 16), bag={"stone": 2, "copper": 1})
        self.assertEqual(
            plan(turn)["10010"], {"action": "sell", "name": "copper", "num": 1}
        )

    def test_no_vendor_means_no_selling(self):
        """地图上没有小贩 ⇒ 卖不出去，回去接着挖矿（不是发一条永远失败的空卖）。"""
        turn = self._turn(at=Pos(19, 16), bag={"copper": 3}, vendor=False)
        self.assertNotIn("sell", {c["action"] for c in plan(turn).values()})

    def test_the_wall_comes_before_the_selling(self):
        """环上还有缺口 ⇒ 先砌墙（卖货归第 3 级，第 2 级没做完就轮不到）。"""
        turn = self._turn(at=Pos(19, 16), bag={"copper": 3}, ring=False)
        cmd = plan(turn)["10010"]
        self.assertNotEqual(cmd["action"], "sell", f"墙没砌完不许卖：{cmd}")

    def test_a_load_that_cannot_pay_for_the_ticket_is_not_walked(self):
        """货值 + 现钱还不够一张券 ⇒ 不去卖，接着挖（1 块铜 = 5 金，离 100 金差得远）。"""
        turn = self._turn(at=Pos(15, 16), bag={"copper": 1})  # 离小贩 5 格
        cmd = plan(turn)["10010"]
        self.assertEqual(cmd["action"], "move", f"凑不够券钱就接着挖：{cmd}")


class DetourSellTest(unittest.TestCase):
    """去矿的路上**顺路卖矿**（`day._detour_sell`）—— 用户口径：这一条必须留着。

    离小贩切比雪夫 ≤ `DETOUR_MAX`(2) ⇒ 这一回合先朝小贩迈一步；**已经贴上了就当场卖**
    （贴着小贩的那一格就是 `sell` 的站位 —— 再"迈一步"是原地不动 = 这一回合空指令）。
    背包里没有卖得掉的货 ⇒ 不绕。

    局面：环砌满、三座炮（第 1/2 级都做完了），只有一张买不起的券 ⇒ 落到第 3 级的挖矿那一支
    （买券的钱离得远，所以不会先去卖 —— 那是另一条支路，`SellCargoTest` 钉着）。
    """

    BASE = Pos(10, 24)
    WEAPONS = _records(_guns_at_sites(Pos(10, 24)))
    MINE = Pos(16, 30)      # 铜矿，在工人南边
    NEAR_VENDOR = Pos(17, 26)  # 切比雪夫 2 格内的小贩
    FAR_VENDOR = Pos(30, 18)   # 离得远的小贩
    SHOP_PRICES = {"WeaponUpgradeVoucher1": 100}

    def setUp(self) -> None:
        _reset_ledgers()

    def _turn(
        self,
        *,
        at: Pos,
        bag: dict[str, int],
        vendor: Pos,
        weapons: tuple[Weapon, ...] | None = None,
        gold: int = 0,
    ) -> Turn:
        guns = self.WEAPONS if weapons is None else weapons
        ring = {c: WALL for c in wall_cells(self.BASE, 41)}
        grid = _terrain(guns, {self.BASE: "station"}, ring, {self.MINE: "copper"}, {vendor: "vendor"})
        grid[at] = "worker"
        return Turn(
            round_no=1,
            map=Map((41, 32), grid),
            roles=(Worker(10010, at, dict(bag)),),
            gold=gold,  # 默认买不起券 ⇒ 落到挖矿那一支
            weapons=guns,
            vendor_prices={"stone": 1, "copper": 5},
            shop_prices=self.SHOP_PRICES,
        )

    def test_it_detours_when_the_vendor_is_on_the_way(self):
        """路过小贩（切比雪夫 ≤ 2）⇒ 先朝小贩迈一步，而不是直奔矿。

        判据取"有货 / 空手两步必须不同"：这一步本身就朝小贩（离小贩 3 → 2，离矿 6 → 5，
        两个目标都变近）⇒ 只比"离小贩更近"是分不出绕没绕的。
        """
        at = Pos(16, 24)
        laden = plan(self._turn(at=at, bag={"copper": 1}, vendor=self.NEAR_VENDOR))["10010"]
        empty = plan(self._turn(at=at, bag={}, vendor=self.NEAR_VENDOR))["10010"]
        self.assertEqual(laden["action"], "move", laden)
        self.assertNotEqual(
            laden["targetPos"], empty["targetPos"], "有货才绕：这一步该与空手不同"
        )
        step = Pos(laden["targetPos"][0]["x"], laden["targetPos"][0]["y"])
        self.assertLess(
            step.dist(self.NEAR_VENDOR), at.dist(self.NEAR_VENDOR), "这一步朝小贩走"
        )

    def test_it_walks_straight_to_the_mine_when_the_vendor_is_far_off(self):
        """小贩绕得太远（> `DETOUR_MAX`）⇒ 不绕，直奔矿。"""
        at = Pos(16, 24)
        cmd = plan(self._turn(at=at, bag={"copper": 1}, vendor=self.FAR_VENDOR))["10010"]
        self.assertEqual(cmd["action"], "move", cmd)
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(self.MINE), at.dist(self.MINE), "这一步朝矿走")

    def test_an_empty_bag_never_detours(self):
        """背包里没有卖得掉的货 ⇒ 不绕：小贩摆在顺路的位置上，走出来的那一步也必须与
        "小贩离得远远的"那一次**完全一样**（绕过去也没东西可卖，白走）。"""
        at = Pos(16, 24)
        near = plan(self._turn(at=at, bag={}, vendor=self.NEAR_VENDOR))["10010"]
        far = plan(self._turn(at=at, bag={}, vendor=self.FAR_VENDOR))["10010"]
        self.assertEqual(near["action"], "move", near)
        self.assertEqual(
            near["targetPos"], far["targetPos"], "空手 ⇒ 小贩近不近都走同一步"
        )

    def test_a_laden_worker_beside_the_vendor_sells_instead_of_freezing(self):
        """第 1 级的筹资那一支（`RaiseForWeapons` → `_mine_ore`）前面**没有**"贴着小贩"那道
        闸门 ⇒ 贴着小贩又背着货时必须当场卖掉。

        旧口径在这一格上记了一条"朝小贩迈一步"的意图，而 `step_toward` 对"已经贴着 goal"
        返回 `None` ⇒ 这一回合一条指令都不发；局面不变就每回合重演（工人整局钉在小贩旁边）。
        """
        at = Pos(17, 25)  # 贴着 (17,26) 那个小贩
        turn = self._turn(at=at, bag={"copper": 1}, vendor=self.NEAR_VENDOR, weapons=())
        cmd = plan(turn).get("10010")
        self.assertIsNotNone(cmd, "贴着小贩又背着货 ⇒ 这一回合必须有指令")
        self.assertEqual(cmd["action"], "sell", f"贴上了 ⇒ 当场卖：{cmd}")


class VoucherLineTest(unittest.TestCase):
    """第 3 级：挖矿攒钱 → 卖 → 买 → **买到手就立刻用掉**。

    券按 `VOUCHER_CHAIN` 取第一张"还有东西可升"的：武器二级 > 武器三级 > 围墙二级 > 围墙三级
    （用户口径）—— 攒钱的目标就是它，不跳级去买更便宜的券。买入前从共享金币里**预扣券价**，
    另一条线（另一个工人 / 空闲的开拓者）这一回合就不会重复买同一张。
    """

    BASE = Pos(10, 24)
    #: 三个落点（顺序即券链优先级：非角上的两个在前、角上那个最后）
    SIDE_A, SIDE_B, CORNER = weapon_sites(Pos(10, 24), 41)
    SHOP = Pos(22, 18)
    PRICES = {"stone": 1, "copper": 5}
    SHOP_PRICES = {
        "WeaponUpgradeVoucher1": 100,
        "WeaponUpgradeVoucher2": 150,
        "WallUpgradeVoucher1": 20,
        "WallUpgradeVoucher2": 30,
    }

    def setUp(self) -> None:
        _reset_ledgers()

    def _turn(
        self,
        roles: tuple[BaseRole, ...],
        weapons: tuple[Weapon, ...],
        *,
        walls: tuple[Wall, ...] = (),
        gold: int = 0,
    ) -> Turn:
        ring = {c: WALL for c in wall_cells(self.BASE, 41)}
        ring.update({w.pos: WALL for w in walls})
        grid = _terrain(weapons, {self.BASE: "station"}, ring, {self.SHOP: "weaponShop"})
        grid |= {r.pos: r.type_name for r in roles}
        return Turn(
            round_no=1,
            map=Map((41, 32), grid),
            roles=roles,
            gold=gold,
            weapons=weapons,
            walls=walls,
            vendor_prices=self.PRICES,
            shop_prices=self.SHOP_PRICES,
        )

    def _gun(self, wid: int, kind: str, cell: Pos, level: int = 1) -> Weapon:
        return Weapon(id=wid, kind=kind, pos=cell, attack_range=4, cooldown=0, level=level)

    def _sides(self, level: int = 1) -> tuple[Weapon, ...]:
        """非角上那两座火箭 —— 券链第 1/2 步的目标就是它们。"""
        return (
            self._gun(10020, "rocket", self.SIDE_A, level),
            self._gun(10021, "rocket", self.SIDE_B, level),
        )

    def test_the_chain_asks_for_the_weapon_voucher_first(self):
        """武器二级券排在最前 —— 哪怕围墙券（20 金）更便宜、也买得起，也不许跳级买它。"""
        worker = Worker(10010, Pos(20, 24))  # 盒外空地（盒内去商店要先绕后方通道）
        turn = self._turn(
            (worker,), self._sides(), walls=(Wall(40000, Pos(13, 21), 1000, 1),), gold=200
        )
        cmd = plan(turn)["10010"]
        self.assertEqual(cmd["action"], "move", f"该朝商店走：{cmd}")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(self.SHOP), worker.pos.dist(self.SHOP), "朝武器商店走")

    def test_the_voucher_chain_is_weapons_only(self):
        """**券链里没有墙券**（第 146 步）：武器全到顶、正面列摆着一面 L1 墙、钱管够 ⇒
        券链给不出任何目标，也不再有人为墙券跑腿（墙券归修墙工那条差事）。"""
        worker = Worker(10010, Pos(13, 20))  # 离那面 L1 墙一格
        turn = self._turn(
            (worker,), self._sides(3), walls=(Wall(40000, Pos(13, 21), 1000, 1),), gold=200
        )
        self.assertIsNone(day._voucher_target(turn), "武器到顶 ⇒ 券链给不出目标")
        cmds = plan(turn)
        self.assertNotIn(
            "WallUpgradeVoucher1", {c.get("name") for c in cmds.values()}, f"别买墙券：{cmds}"
        )

    def test_it_never_buys_more_than_the_board_can_use(self):
        """**只买用得上的张数**（用户口径）：场上只有一座升得动的炮 ⇒ 最多买一张，钱再多也一样。"""
        worker = Worker(10010, Pos(21, 17))  # 贴着商店 (22,18)
        weapons = (self._gun(10020, "rocket", self.SIDE_A), self._gun(10021, "rocket", self.SIDE_B, 3))
        turn = self._turn((worker,), weapons, gold=900)
        cmd = plan(turn)["10010"]
        self.assertEqual(cmd["action"], "buy", cmd)
        self.assertEqual(cmd["num"], 1, f"只有一座升得动 ⇒ 只买一张：{cmd}")

    def test_two_buyers_never_buy_the_same_voucher_twice(self):
        """同一回合两条线都去买 ⇒ **只有一个人真买**（`ctx.bought` 那本预扣张数账管着）。

        场上只有一座升得动的炮、钱管够 —— 没有这道防护时两个角色各按 `len(spots)=1` 买一张，
        同一座炮收回两张券（多出来的那张永远用不掉，纯浪费金币）。
        """
        pioneer = Pioneer(10011, Pos(21, 17))  # 贴着商店
        worker = Worker(10010, Pos(21, 19))  # 也贴着商店
        weapons = (self._gun(10020, "rocket", self.SIDE_A), self._gun(10021, "rocket", self.SIDE_B, 3))
        cmds = plan(self._turn((pioneer, worker), weapons, gold=900))
        buys = [cid for cid, cmd in cmds.items() if cmd["action"] == "buy"]
        self.assertEqual(len(buys), 1, f"只该有一个人买：{cmds}")
        self.assertEqual(cmds[buys[0]]["num"], 1, "场上只有一座升得动 ⇒ 只买一张")

    def test_a_voucher_held_by_a_colleague_counts_against_the_purchase(self):
        """队友手里已经有这张券 ⇒ 我也不买（那张就够升那一座了）。

        券没有转移指令，但"能不能用"看的是全场还剩几个目标 ⇒ 买之前按**全队**手里的张数算。
        """
        holder = Worker(10012, Pos(20, 20), {"WeaponUpgradeVoucher1": 1})
        worker = Worker(10010, Pos(21, 17))  # 贴着商店
        weapons = (self._gun(10020, "rocket", self.SIDE_A), self._gun(10021, "rocket", self.SIDE_B, 3))
        cmds = plan(self._turn((holder, worker), weapons, gold=900))
        self.assertNotIn(
            "buy", {cmd["action"] for cmd in cmds.values()}, f"那张券够了，别再买：{cmds}"
        )


    def test_the_chain_upgrades_the_two_side_rockets_before_the_corner_one(self):
        """优先链的落点：非角上那两座先升（第 1/2 步），角上那座排在正面墙券**之后**（第 4 步）
        —— `weapon_sites` 的顺序（非角、非角、角）就是这条优先级，不另立一份判据。
        """
        weapons = (
            self._gun(10020, "rocket", self.SIDE_A),
            self._gun(10021, "rocket", self.SIDE_B),
            self._gun(10022, "rocket", self.CORNER),
        )
        turn = self._turn((Worker(10010, Pos(20, 24)),), weapons, gold=900)
        voucher, spots = day._voucher_target(turn)
        self.assertEqual(voucher, "WeaponUpgradeVoucher1", "第 1 步：武器二级、非角两座")
        self.assertEqual(set(spots), {self.SIDE_A, self.SIDE_B}, "先升非角上的两座")

        # 非角两座到顶 ⇒ 下一步是**角上那座**（券链只剩武器步骤，第 146 步）
        weapons = (
            self._gun(10020, "rocket", self.SIDE_A, 3),
            self._gun(10021, "rocket", self.SIDE_B, 3),
            self._gun(10022, "rocket", self.CORNER),
        )
        front = wall_cells(self.BASE, 41)[:6]  # 面向敌人的一列
        turn = self._turn(
            (Worker(10010, Pos(20, 24)),),
            weapons,
            walls=tuple(Wall(40000 + i, c, 1000, 1) for i, c in enumerate(front)),
            gold=900,
        )
        _ = front  # 正面列摆着也不影响：墙券不在这条链里
        voucher, spots = day._voucher_target(turn)
        self.assertEqual(voucher, "WeaponUpgradeVoucher1", "轮到角上那座火箭")
        self.assertEqual(set(spots), {self.CORNER}, "打的是角上那一格")

    def test_it_picks_the_weaker_of_the_two_rockets_first(self):
        """同一步里**血少的先升**（用户口径"先升级血少的"）；血量未知（-1）排最后。

        用户原话的后半是"相同血量随机"—— 本仓所有并列判据一律取坐标序（真随机会让用例
        与日志都没法复现），那条口径就此落成坐标序。
        """
        weapons = (
            self._gun(10020, "rocket", self.SIDE_A)._replace(health=-1),  # 未知
            self._gun(10021, "rocket", self.SIDE_B)._replace(health=300),
        )
        turn = self._turn((Worker(10010, Pos(20, 24)),), weapons, gold=900)
        _voucher, spots = day._voucher_target(turn)
        self.assertEqual(spots, (self.SIDE_B, self.SIDE_A), "血 300 的在前、未知的垫底")

    def test_a_voucher_in_hand_is_used_right_away(self):
        """手里已经持券 ⇒ 立刻走到目标用掉（买完就用，不囤）。"""
        worker = Worker(10010, Pos(9, 24), {"WeaponUpgradeVoucher1": 1})  # 站在共用操作位上
        weapons = (self._gun(10020, "rocket", self.SIDE_A),)
        turn = self._turn((worker,), weapons, gold=0)
        cmd = plan(turn)["10010"]
        self.assertEqual(cmd["action"], "use", f"持券就走到目标用掉：{cmd}")
        self.assertEqual(cmd["name"], "WeaponUpgradeVoucher1")
        self.assertEqual(
            Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"]), self.SIDE_A
        )

    def test_the_buy_reserves_the_gold_so_the_other_worker_stands_down(self):
        """预扣共享金币：钱只够一张券时，两个工人里只有一个去商店。

        没有预扣的话两人会在同一回合都判定"买得起"、各奔一座商店 —— 下一回合钱只够一张，
        另一个白跑一趟。
        """
        a = Worker(10010, Pos(20, 24))
        b = Worker(10012, Pos(24, 24))
        weapons = (self._gun(10020, "rocket", self.SIDE_A),)
        turn = self._turn((a, b), weapons, gold=100)  # 正好一张 WeaponUpgradeVoucher1
        cmds = plan(turn)
        buyers = [cid for cid, cmd in cmds.items() if cmd["action"] == "move"]
        self.assertEqual(len(buyers), 1, f"只该有一个人去商店：{cmds}")


    def test_it_buys_as_many_as_the_board_needs(self):
        """**按需要多少张就买多少张**（用户口径）：两座待升到二级的武器 + 200 金、券价 100
        ⇒ 一条 `buy num=2` 买够，而不是一回合一张地磨。

        需要几张是"这张券还升得动的目标有几个"（跟场上的武器等级一起算），买得起几张看金币 ——
        取小的那个。
        """
        worker = Worker(10010, Pos(21, 17))  # 贴着商店 (22,18)
        turn = self._turn(
            (worker,), self._sides(), walls=(Wall(40000, Pos(13, 21), 1000, 1),), gold=200
        )
        cmd = plan(turn)["10010"]
        self.assertEqual(
            cmd, {"action": "buy", "name": "WeaponUpgradeVoucher1", "num": 2}, cmd
        )

    def test_it_buys_only_what_the_purse_allows(self):
        """钱只够一张 ⇒ 就买一张（另一张等攒够了再买），不是"买不起两张就一张不买"。"""
        worker = Worker(10010, Pos(21, 17))
        turn = self._turn(
            (worker,), self._sides(), walls=(Wall(40000, Pos(13, 21), 1000, 1),), gold=100
        )
        cmd = plan(turn)["10010"]
        self.assertEqual(cmd["action"], "buy", cmd)
        self.assertEqual(cmd["num"], 1, f"钱只够一张：{cmd}")

    def test_each_held_voucher_gets_used_on_its_own_target(self):
        """手里攒着两张 ⇒ 一张一张用掉，每回合挑**还升得动的第一格**。"""
        worker = Worker(10010, Pos(9, 24), {"WeaponUpgradeVoucher1": 2})  # 站在共用操作位上
        turn = self._turn((worker,), self._sides(), gold=0)
        cmd = plan(turn)["10010"]
        self.assertEqual(cmd["action"], "use", f"该把手里那张用掉：{cmd}")
        # 同血（都未知）⇒ 坐标序：SIDE_B((9,23)) 排在 SIDE_A((10,25)) 前面（用户口径的
        # "相同血量随机"按本仓惯例落成坐标序，否则用例与日志都没法复现）
        self.assertEqual(Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"]), self.SIDE_B)

class NoTaskModeTest(unittest.TestCase):
    """无任务模式的白天（第 126 步用户口径）：**工人只挖矿卖矿，买卖券全归开拓者**。

    开关是 `Turn.tasks_exhausted`（两个任务点都 `coldDownRounds == 0` 且 `isValid is False`
    ⇒ 这一局再也接不到任务）。工人这条只剩"矿 → 金币"；券的买与用由空闲的开拓者全包
    （`day.voucher_errand`，见 `VoucherLineTest` / `PioneerErrandTest`）。
    """

    BASE = Pos(10, 24)
    WEAPONS = _records(_guns_at_sites(Pos(10, 24)))
    SHOP = Pos(22, 18)
    VENDOR = Pos(20, 16)
    MINE = Pos(20, 30)
    PRICES = {"stone": 1, "iron": 3, "copper": 5}
    SHOP_PRICES = {"WeaponUpgradeVoucher1": 100}

    def _turn(self, *, at: Pos, bag: dict[str, int], gold: int = 0, weapons=None) -> Turn:
        weapons = self.WEAPONS if weapons is None else weapons
        ring = {c: WALL for c in wall_cells(self.BASE, 41)}
        grid = _terrain(
            weapons, {self.BASE: "station"}, ring, {self.MINE: "copper"},
            {self.SHOP: "weaponShop"}, {self.VENDOR: "vendor"},
        )
        grid[at] = "worker"
        return Turn(
            round_no=1,
            map=Map((41, 32), grid),
            roles=(Worker(10010, at, dict(bag)),),
            gold=gold,
            weapons=weapons,
            tasks_exhausted=True,
            vendor_prices=self.PRICES,
            shop_prices=self.SHOP_PRICES,
        )

    def test_workers_never_buy_vouchers_here(self):
        """钱管够、就站在商店旁边、场上还有一座升得动的炮 —— 工人也**不买券**（那是开拓者的活）。

        没有这道分流的话，工人会拿着金币去商店买券、把挖矿卖矿晾在一边。
        """
        weapons = (Weapon(id=10020, kind="rocket", pos=Pos(12, 24), attack_range=10, cooldown=0, level=1),)
        cmd = plan(self._turn(at=Pos(21, 17), bag={}, gold=900, weapons=weapons))["10010"]
        self.assertNotEqual(cmd["action"], "buy", f"无任务模式里工人不买券：{cmd}")

    def test_a_load_that_does_not_pay_for_the_trip_is_not_walked(self):
        """够本门（用户口径）：货值 < 2 × 到小贩的步数 ⇒ 不跑这一趟，回去接着挖。

        1 块铜 = 5 金、小贩在 8 格外（阈值 16）⇒ 背过去是白跑。
        """
        at = Pos(12, 16)  # 离小贩切比雪夫 8
        cmd = plan(self._turn(at=at, bag={"copper": 1}))["10010"]
        self.assertEqual(cmd["action"], "move", cmd)
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(self.MINE), at.dist(self.MINE), "回去挖矿，不是走向小贩")

    def test_a_load_worth_the_trip_is_walked_to_the_vendor(self):
        """攒够了（4 块铜 = 20 ≥ 16）⇒ 背去卖。"""
        at = Pos(12, 16)
        cmd = plan(self._turn(at=at, bag={"copper": 4}))["10010"]
        self.assertEqual(cmd["action"], "move", cmd)
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(self.VENDOR), at.dist(self.VENDOR), "朝小贩走")


class PioneerErrandTest(unittest.TestCase):
    """空闲的开拓者也跑同一条买券差事（用户口径：开拓者做完任务就买券，且买完就用）。

    它与工人那条**各自独立跑**：谁有钱谁买，靠共享金币的预扣去重。
    """

    BASE = Pos(10, 24)
    SHOP = Pos(22, 18)

    def setUp(self) -> None:
        _reset_ledgers()

    def _turn(
        self,
        pioneer: Pioneer,
        *,
        gold: int,
        phase_task: str = "",
        llm_resp: str = "",
        weapons: tuple[Weapon, ...] = (),
        cooling_tasks: tuple[tuple[Pos, int], ...] = (),
        tasks_exhausted: bool = False,
    ) -> Turn:
        ring = {c: WALL for c in wall_cells(self.BASE, 41)}
        grid = _terrain(weapons, {self.BASE: "station"}, ring, {self.SHOP: "weaponShop"})
        grid[pioneer.pos] = pioneer.type_name
        return Turn(
            round_no=1,
            map=Map((41, 32), grid),
            roles=(pioneer,),
            gold=gold,
            phase_task=phase_task,
            llm_resp=llm_resp,
            weapons=weapons,
            cooling_tasks=cooling_tasks,
            tasks_exhausted=tasks_exhausted,
            vendor_prices={"stone": 1},
            shop_prices={"WeaponUpgradeVoucher1": 100},
        )

    def test_an_idle_pioneer_walks_to_the_shop_to_buy(self):
        """没任务点（任务做完/冷却中）⇒ 跑买券差事：钱够就朝商店走过去。"""
        pioneer = Pioneer(10011, Pos(20, 20))
        weapons = (
            Weapon(id=10020, kind="rocket", pos=Pos(12, 24), attack_range=4, cooldown=0, level=1),
        )
        cmd = plan(self._turn(pioneer, gold=200, weapons=weapons))["10011"]
        self.assertEqual(cmd["action"], "move", f"该朝商店走：{cmd}")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(self.SHOP), pioneer.pos.dist(self.SHOP), "朝武器商店走")


    def test_an_idle_pioneer_waits_at_the_soonest_task_point(self):
        """空闲、券买不起、任务点都在冷却 ⇒ 去**刷新最快**那个点旁边等着（用户口径）。

        到地方就待命（什么都不发）；这一条只钉"朝哪儿走"。
        """
        pioneer = Pioneer(10011, Pos(20, 20))
        turn = self._turn(
            pioneer,
            gold=0,  # 券买不起
            cooling_tasks=((Pos(14, 14), 5), (Pos(17, 17), 30)),
        )
        cmd = plan(turn)["10011"]
        self.assertEqual(cmd["action"], "move", f"该去任务点旁等着：{cmd}")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(
            step.dist(Pos(14, 14)), pioneer.pos.dist(Pos(14, 14)), "朝刷新最快（5 回合）那个点走"
        )

    def test_an_idle_pioneer_waits_at_the_shop_when_tasks_are_done(self):
        """任务全做完（`tasks_exhausted`）、券又买不起 ⇒ 去武器商店旁边等着（用户口径）。"""
        pioneer = Pioneer(10011, Pos(20, 20))
        turn = self._turn(pioneer, gold=0, tasks_exhausted=True)
        cmd = plan(turn)["10011"]
        self.assertEqual(cmd["action"], "move", f"该去商店旁等着：{cmd}")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(self.SHOP), pioneer.pos.dist(self.SHOP), "朝商店走")

    def test_a_pinned_pioneer_answers_instead_of_running_errands(self):
        """被任务钉死的开拓者只交答案（离开任务点一格任务就作废）—— 再有钱也不跑腿。"""
        pioneer = Pioneer(10011, Pos(20, 20))
        turn = self._turn(
            pioneer,
            gold=200,
            phase_task="题目",
            llm_resp="<tool><tool_name>submitAnswer</tool_name>"
            "<tool_param><answer>42</answer></tool_param></tool>",
        )
        task_channel(turn)  # 答卷变量在派工具那一行被写（与 `app.handle` 同序：先 task_channel）
        self.assertEqual(plan(turn)["10011"]["action"], "submitAnswer")


class DayOneFinishTest(unittest.TestCase):
    """第一天必须把墙建好、之后白天不许整天卡着 —— 全天模拟的验收网。

    可行地图（两座石矿 = 20 块石头 ≥ 14 格墙）上跑满 70 个白天回合，照判题器口径结算
    指令（move/build/collect/sell/buy、矿采满 `MINE_CHARGES` 次消失）：1) 环 14/14 砌完且同一格
    不许砌两遍（石头白花）；2) 三座武器建满（开局 75 金恰好三座，夜里第一波机器人之前要有炮）；
    3) 环砌完之前工人不许闲 —— 环砌完后的白天末尾，预算拦住回不来的远矿 ⇒ 待命是保守
    方向的合法行为，不在本网范围。

    `test_the_workers_never_stall_a_whole_day` 接着往下跑第 2 天（矿量因此开得比一天用得多）。
    """

    BASE = Pos(10, 24)
    STONE1, STONE2 = Pos(4, 24), Pos(4, 26)
    IRON, COPPER = Pos(6, 30), Pos(20, 30)
    VENDOR, SHOP, TASK = Pos(20, 16), Pos(22, 18), Pos(30, 30)
    PRICES = {"stone": 1, "iron": 3, "copper": 5}
    #: 一座矿采满 10 次就消失 —— 第 2 天那条网要跑 260 回合，给足免得"没矿可采"被当成停滞
    MINE_CHARGES = 40

    def setUp(self) -> None:
        _reset_ledgers()
        AGENT.reset()
        self.gold = 75  # 开局：恰好买满三座（25×3）
        self.ground: dict[Pos, str] = {
            self.BASE: "station",  # 只写左上角（与 model._units 一致，2×2 由 Map 展开）
            self.STONE1: "stone", self.STONE2: "stone",
            self.IRON: "iron", self.COPPER: "copper",
            self.VENDOR: "vendor", self.SHOP: "weaponShop", self.TASK: "challengerTaskPoint1",
        }
        self.roles: dict[int, BaseRole] = {
            10011: Pioneer(10011, Pos(12, 24)),
            10012: Worker(10012, Pos(11, 23)),
            10013: Worker(10013, Pos(11, 25)),
        }
        self.walls: dict[Pos, Wall] = {}
        self.weapons: list[Weapon] = []
        self.mine_left = {
            self.STONE1: self.MINE_CHARGES, self.STONE2: self.MINE_CHARGES,
            self.IRON: self.MINE_CHARGES, self.COPPER: self.MINE_CHARGES,
        }
        self.built: list[Pos] = []
        self.idle: dict[int, list[int]] = {10012: [], 10013: []}

    def _turn(self, round_no: int) -> Turn:
        grid = {**self.ground, **{w.pos: WALL for w in self.walls.values()}}
        grid |= {w.pos: w.kind for w in self.weapons}
        grid |= {r.pos: r.type_name for r in self.roles.values()}
        return Turn(
            round_no=round_no,
            map=Map((41, 32), grid),
            roles=tuple(self.roles.values()),
            gold=self.gold,
            weapons=tuple(self.weapons),
            walls=tuple(self.walls.values()),
            task_points=(self.TASK,) if round_no <= 2 else (),
            phase_task="题目" if round_no >= 3 else "",  # 开拓者第 3 回合起被任务钉住
            vendor_prices=self.PRICES,
            shop_prices={"WallFixer": 10, "WeaponUpgradeVoucher1": 100},
            station_health=1500,
        )

    def _settle(self, round_no: int) -> None:
        """照判题器口径结算一回合：move 挪人、build 落建筑、collect 出矿并计数
        （10 次消失）、sell/buy 走账；`use`/`acceptTask` 不影响本用例的断言。"""
        cmds = plan(self._turn(round_no))
        for key, cmd in cmds.items():
            rid = int(key)
            if rid not in self.roles:
                continue  # `attack` 的 key 是武器 id，不是角色 id
            role = self.roles[rid]
            bag = dict(role.bag)
            if cmd["action"] == "move":
                t = cmd["targetPos"][0]
                self.roles[rid] = type(role)(rid, Pos(t["x"], t["y"]), bag)
            elif cmd["action"] == "build":
                t = cmd["targetPos"][0]
                cell = Pos(t["x"], t["y"])
                if cmd["name"] == WALL:
                    self.walls[cell] = Wall(9000 + len(self.walls), cell, 1000, 1)
                    self.built.append(cell)
                    bag["stone"] = bag.get("stone", 0) - 1
                else:
                    self.weapons.append(Weapon(10020 + len(self.weapons), cmd["name"], cell, 4, 0))
                    self.gold -= 25
                self.roles[rid] = type(role)(rid, role.pos, bag)
            elif cmd["action"] == "collect":
                t = cmd["targetPos"][0]
                cell = Pos(t["x"], t["y"])
                kind = self.ground[cell]
                bag[kind] = bag.get(kind, 0) + 1
                self.mine_left[cell] -= 1
                if self.mine_left[cell] <= 0:
                    del self.ground[cell]
                self.roles[rid] = type(role)(rid, role.pos, bag)
            elif cmd["action"] == "sell":
                kind, num = cmd["name"], cmd["num"]
                self.gold += self.PRICES[kind] * num
                bag[kind] = 0
                self.roles[rid] = type(role)(rid, role.pos, bag)
            elif cmd["action"] == "buy":
                self.gold -= {"WallFixer": 10, "WeaponUpgradeVoucher1": 100}[cmd["name"]]
                bag[cmd["name"]] = bag.get(cmd["name"], 0) + cmd.get("num", 1)
                self.roles[rid] = type(role)(rid, role.pos, bag)
        for rid in (10012, 10013):
            if str(rid) not in cmds:
                self.idle[rid].append(round_no)

    def test_day_one_finishes_the_ring_and_three_weapons(self):
        ring = set(wall_cells(self.BASE, 41))
        done_at = None
        for round_no in range(1, 71):
            if done_at is None and ring <= set(self.walls):
                done_at = round_no
            self._settle(round_no)
        if done_at is None and ring <= set(self.walls):
            done_at = 70  # 恰好最后一回合砌完的情形
        self.assertIsNotNone(
            done_at, f"第 1 天没把 14 格墙砌完，缺：{sorted(ring - set(self.walls))}"
        )
        self.assertEqual(
            {(w.kind, w.pos) for w in self.weapons},
            set(zip(WEAPONS_BY_SITE, weapon_sites(self.BASE, 41))),
            "三座武器第 1 天就该建满、各在自己的落点上",
        )
        self.assertEqual(len(self.built), len(set(self.built)), "同一格墙砌了两遍（石头白花）")
        for rid, rounds in self.idle.items():
            early = [r for r in rounds if r < (done_at or 71)]
            self.assertEqual(
                early, [], f"工人 {rid} 在环砌完（R{done_at}）之前就闲着：{early}"
            )

    def _longest_idle_run(self) -> dict[int, int]:
        """每个工人最长的一段"连续空指令"（只数白天的回合）。

        夜里本来就该原地待命（到岗了就不再动），把它算进来，"白天整天不动"就淹在里面了。
        先滤掉夜里的回合号再连号 ⇒ 一段不会跨过夜跟第 2 天接上。
        """
        out: dict[int, int] = {}
        for rid, rounds in self.idle.items():
            best = run = 0
            prev = None
            for r in sorted(rounds):
                if (r - 1) % ROUNDS_PER_DAY + 1 > DAY_ROUNDS:
                    continue
                run = run + 1 if prev is not None and r == prev + 1 else 1
                prev = r
                best = max(best, run)
            out[rid] = best
        return out

    def test_the_workers_never_stall_a_whole_day(self):
        """第 2 天起白天不会整天卡在原地 —— 用户报的那条症状的整网。

        第 1 天之后环是满的、三座武器也在 ⇒ 工人白天的活只有经济线（卖矿 / 采矿 / 升级）。
        旧口径把我方角色算进估算距离：盒子内只剩几条一格宽的走廊，谁停在走廊上，同事的
        "矿 → 最近的炮位"就一律 -1 ⇒ 每座矿都不可行 ⇒ 整段放弃、一回合一条指令都不发 ——
        两个人一起卡死整整一天（实测第 2 天 70/70 回合空指令、金币停在 0）。

        待命本身是合法的（收工时在岗、gated 闸门、**今天已经来不及跑一趟的远矿**），所以钉的
        是"最长一段"而不是"一次都不许空"。本局面实测最长 12（第 2 天 R171-182）：两个工人都
        在 20 回合内卖了货、背包空了，剩下两座矿来回 26/31 步 > 当天剩余预算 ⇒ 蹲在小贩边上
        等天黑，是保守方向的合法行为。改前是 70（一整天），取 30 当天花板。
        """
        for round_no in range(1, 261):  # 2 天：白天干完夜里回炮位（`_settle` 认不出 attack）
            self._settle(round_no)
        runs = self._longest_idle_run()
        self.assertLess(
            max(runs.values()), 30, f"有工人整天没动过：最长空指令段 {runs}（回合数）"
        )


class RepairStockTest(unittest.TestCase):
    """第 2.5 级：修墙差事 —— 第 3 天起，**名册里最后一个工人**攒修复包、**墙券也归他**。

    口径出自用户：第 3 天开始攒、第 4 夜起用；墙券由他买也由他用（两条线打的是同一列正面墙）。
    东西**不能转手**（谁买谁用）⇒ 只让一个工人背，夜里背着包的那个就是修墙工。**顺路优先**
    （人已经贴在商店旁 ⇒ 当场买）；包里一张都没有时才肯专程跑一趟，且要求"来回 + 买"赶得回
    白天结束；买不起就一步都不走。手上已有的墙券先用掉 —— 目标就在这条差事的路上。
    """

    BASE = Pos(10, 24)
    SHOP = Pos(22, 18)
    BESIDE = Pos(21, 18)  # 贴着商店（`steps_between` 的 0 = 已经贴着）
    FAR = Pos(20, 24)  # 盒外空地，去商店要绕
    DAY3 = 261  # 第 3 天第一个白天回合（within = 1）
    DAY2 = 131
    DAY3_LAST = 330  # 第 3 天最后一个回合（`day_rounds_left` = 1）
    PACK = 10

    def setUp(self) -> None:
        _reset_ledgers()

    def _turn(
        self,
        *roles: BaseRole,
        gold: int = 40,
        round_no: int = DAY3,
        weapons: int = 3,
        level: int = 1,
        weak: tuple[Pos, int] | None = None,
        front_level: int = 1,
    ) -> Turn:
        # 第 3 天起环是 16 格（`utils._sealed_back` 把背面两个角格补上）—— 不照这个口径铺，
        # `_ring` 会把那两个角格当成缺口、把工人支去砌墙，这一级根本轮不到
        sealed = round_no > 2 * ROUNDS_PER_DAY
        ring = wall_cells(self.BASE, 41, sealed=sealed)
        guns = tuple(
            Weapon(
                id=10020 + i,
                kind="rocket",
                pos=cell,
                attack_range=10,
                cooldown=0,
                level=level,
            )
            for i, cell in enumerate(weapon_sites(self.BASE, 41)[:weapons])
        )
        grid = _terrain(guns, {self.BASE: "station"}, {c: WALL for c in ring}, {self.SHOP: "weaponShop"})
        grid |= {r.pos: r.type_name for r in roles}
        front = set(core.front_wall_cells(
            Turn(round_no=round_no, map=Map((41, 32), {self.BASE: "station"}), roles=(), gold=0)
        ))
        return Turn(
            round_no=round_no,
            map=Map((41, 32), grid),
            roles=roles,
            gold=gold,
            weapons=guns,
            walls=tuple(
                Wall(
                    40000 + i,
                    c,
                    weak[1] if weak and c == weak[0] else 1000,
                    front_level if c in front else 1,
                )
                for i, c in enumerate(ring)
            ),
            vendor_prices={"stone": 1, "copper": 5},
            shop_prices={
                "WallFixer": self.PACK,
                "WeaponUpgradeVoucher1": 100,
                "WallUpgradeVoucher1": 20,
                "WallUpgradeVoucher2": 30,
            },
        )

    def _packs(self, cmds: dict) -> list[dict]:
        return [c for c in cmds.values() if c.get("name") == WALL_FIXER]

    def test_the_third_day_stocks_packs_up_to_the_target(self):
        """第 3 天 + 金币够 + 已经贴着商店 ⇒ 一条 `buy num=3`（顺路，不额外走路）。"""
        pioneer = Pioneer(1, self.FAR)
        worker = Worker(2, self.BESIDE)
        cmds = plan(self._turn(pioneer, worker, gold=200))
        self.assertEqual(cmds["2"], {"action": "buy", "name": WALL_FIXER, "num": 5})

    def test_the_second_day_does_not_stock_packs(self):
        """第 2 天一张都不买（攒包从第 3 天起）。"""
        pioneer = Pioneer(1, self.FAR)
        worker = Worker(2, self.BESIDE)
        cmds = plan(self._turn(pioneer, worker, gold=200, round_no=self.DAY2))
        self.assertEqual(self._packs(cmds), [], f"第 2 天不该买包：{cmds}")

    def test_a_partial_stock_is_topped_up_to_five(self):
        """手上只有 2 张 ⇒ 这一趟补到 5 张（差 3 张就买 3 张，不是"一次买满"）。"""
        pioneer = Pioneer(1, self.FAR)
        worker = Worker(2, self.BESIDE, {WALL_FIXER: 2})
        cmds = plan(self._turn(pioneer, worker, gold=60))
        self.assertEqual(cmds["2"], {"action": "buy", "name": WALL_FIXER, "num": 3})

    def test_a_full_stock_stops_buying(self):
        """手里已经攒够 5 张 ⇒ 不再买（囤着等夜里用）。"""
        pioneer = Pioneer(1, self.FAR)
        worker = Worker(2, self.BESIDE, {WALL_FIXER: 5})
        cmds = plan(self._turn(pioneer, worker, gold=200))
        self.assertEqual(self._packs(cmds), [], f"存量够了还买：{cmds}")

    def test_only_the_last_worker_carries_the_packs(self):
        """只有名册里最后一个工人跑这条差事：另一个工人就算贴着商店也不买、不动。"""
        pioneer = Pioneer(1, self.FAR)
        first = Worker(2, self.BESIDE)
        last = Worker(3, self.FAR)
        cmds = plan(self._turn(pioneer, first, last, gold=40))
        self.assertEqual(self._packs(cmds), [], "贴商店的那个工人不该买包")
        self.assertNotIn("2", cmds, "跑差事的只有名册最后一个工人")
        self.assertEqual(cmds["3"]["action"], "move", "最后那个工人专程去商店")

    def test_a_worker_who_cannot_afford_it_does_not_walk(self):
        """买不起（5 金 < 10）⇒ 一步都不走（不为了 0 张包白跑一趟）。"""
        pioneer = Pioneer(1, self.FAR)
        worker = Worker(2, self.FAR)
        cmds = plan(self._turn(pioneer, worker, gold=5))
        self.assertNotIn("2", cmds, f"买不起就不该动：{cmds}")

    def test_the_special_trip_needs_the_round_trip_to_fit(self):
        """专程那一趟要赶得回白天结束：第 3 天最后一个回合（只剩 1 回合）⇒ 不走。"""
        pioneer = Pioneer(1, self.FAR)
        worker = Worker(2, self.FAR)
        cmds = plan(self._turn(pioneer, worker, gold=40, round_no=self.DAY3_LAST))
        self.assertNotIn("2", cmds, f"赶不回来就不该动：{cmds}")

    def test_weapons_first(self):
        """武器名额还有缺 ⇒ 整条不跑（别抢武器的钱，包排在武器之后）。"""
        pioneer = Pioneer(1, self.FAR)
        worker = Worker(2, self.BESIDE)
        cmds = plan(self._turn(pioneer, worker, gold=200, weapons=1))
        self.assertEqual(self._packs(cmds), [], f"武器没建满就不该买包：{cmds}")

    def test_the_carrier_buys_the_front_wall_voucher(self):
        """包攒满之后，**同一个工人**接着买正面墙的升级券（券链里已经没有它了）。"""
        pioneer = Pioneer(1, self.FAR)
        worker = Worker(2, self.BESIDE, {WALL_FIXER: 5})  # 包已满 ⇒ 轮到墙券
        cmds = plan(self._turn(pioneer, worker, gold=100, level=3))  # 武器到顶 ⇒ 无人抢钱
        self.assertEqual(cmds["2"]["action"], "buy", f"该去买墙券：{cmds}")
        self.assertEqual(cmds["2"]["name"], "WallUpgradeVoucher1")
        self.assertGreaterEqual(cmds["2"]["num"], 1, "正面列那 6 格都还是 L1 ⇒ 至少买一张")

    def test_the_pioneer_never_buys_a_wall_voucher(self):
        """开拓者不买墙券（第 146 步）：他在炮位上花不掉它，还会被它拽到墙边去。"""
        pioneer = Pioneer(1, self.BESIDE)  # 就贴在商店旁
        cmds = plan(self._turn(pioneer, gold=200, level=3))
        self.assertNotIn(
            "WallUpgradeVoucher1", {c.get("name") for c in cmds.values()}, f"开拓者别买墙券：{cmds}"
        )

    def test_a_worker_who_is_not_the_carrier_never_buys_a_wall_voucher(self):
        """墙券只由那一个承运人买：另一个工人就算贴着商店也不买。"""
        pioneer = Pioneer(1, self.FAR)
        first = Worker(2, self.BESIDE)  # 贴着商店，但不是名册最后一个
        last = Worker(3, self.FAR)
        cmds = plan(self._turn(pioneer, first, last, gold=40, level=3))
        self.assertNotIn(
            "WallUpgradeVoucher1", {c.get("name") for c in cmds.values()}, f"别买墙券：{cmds}"
        )
        self.assertNotIn("2", cmds, "跑差事的只有名册最后一个工人")

    def test_a_held_wall_voucher_is_spent_first(self):
        """手上那张墙券先用掉（目标就在这条差事的路上）—— 买完就用，不囤。"""
        pioneer = Pioneer(1, self.FAR)
        # 离中间那一段的第一格 (13,22) 一格（券链先砸中间三格，所以站位要贴着它）
        worker = Worker(2, Pos(12, 22), {"WallUpgradeVoucher1": 1})
        cmds = plan(self._turn(pioneer, worker, gold=0, level=3))
        self.assertEqual(cmds["2"]["action"], "use", f"持券就先花掉：{cmds}")
        self.assertEqual(cmds["2"]["name"], "WallUpgradeVoucher1")

    def test_a_dedicated_wall_voucher_trip_needs_the_time(self):
        """专程去买墙券也要赶得回白天结束：第 3 天最后一个回合（只剩 1 回合）⇒ 不走。"""
        pioneer = Pioneer(1, self.FAR)
        worker = Worker(2, self.FAR, {WALL_FIXER: 5})
        cmds = plan(self._turn(pioneer, worker, gold=100, round_no=self.DAY3_LAST, level=3))
        self.assertNotIn("2", cmds, f"赶不回来就不该动：{cmds}")

    def test_the_pioneer_never_carries_packs(self):
        """开拓者的差事里没有包这一条（他买券、接任务；包只能由工人背）。"""
        pioneer = Pioneer(1, self.BESIDE)
        cmds = plan(self._turn(pioneer, gold=200))
        self.assertEqual(self._packs(cmds), [], f"开拓者不该买包：{cmds}")
        self.assertEqual(cmds["1"]["action"], "buy", "他的钱花在券上")


    def test_the_pioneer_leaves_money_for_one_wall_voucher(self):
        """开拓者买武器券时要给"一张墙券"留钱（用户口径"提高墙体升级的优先级"）。

        210 金、包已攒够 3 张：不留手 ⇒ 他一次买 2 张武器券（200），承运人只剩 10 金、
        一张 20 金的墙券都买不到；留一张 ⇒ 他只买 1 张，承运人当场买得到墙券。
        """
        pioneer = Pioneer(1, self.BESIDE)  # 贴着商店
        worker = Worker(2, self.BESIDE, {WALL_FIXER: 5})  # 包够了 ⇒ 差事走到第 ③ 步
        cmds = plan(self._turn(pioneer, worker, gold=210, level=1))
        self.assertEqual(cmds["1"]["action"], "buy", f"开拓者买武器券：{cmds}")
        self.assertEqual(cmds["1"]["num"], 1, f"给墙券留一张，别一次买两张：{cmds}")
        self.assertEqual(
            (cmds["2"]["action"], cmds["2"]["name"]),
            ("buy", "WallUpgradeVoucher1"),
            f"承运人这一回合该买得起墙券：{cmds}",
        )

    def test_the_reserve_disappears_once_the_front_walls_are_maxed(self):
        """前排六格全 L3 ⇒ 不再锁钱：开拓者把两张武器券一次买满（预留不是长期占用）。"""
        pioneer = Pioneer(1, self.BESIDE)
        worker = Worker(2, self.BESIDE, {WALL_FIXER: 5})
        cmds = plan(self._turn(pioneer, worker, gold=210, level=1, front_level=3))
        self.assertEqual(cmds["1"]["action"], "buy", f"开拓者买武器券：{cmds}")
        self.assertEqual(cmds["1"]["num"], 2, f"墙都满了就不该再留钱：{cmds}")

    def test_a_held_wall_voucher_does_not_double_reserve(self):
        """手里已经持着一张墙券 ⇒ 不再为第二张留钱（先用掉手里那张，买第二张不着急）。"""
        pioneer = Pioneer(1, self.BESIDE)
        worker = Worker(2, self.BESIDE, {WALL_FIXER: 5, "WallUpgradeVoucher1": 1})
        cmds = plan(self._turn(pioneer, worker, gold=210, level=1))
        self.assertEqual(cmds["1"]["action"], "buy", f"开拓者买武器券：{cmds}")
        self.assertEqual(cmds["1"]["num"], 2, f"手里有券 ⇒ 不用再留一张的钱：{cmds}")


class DemolishRaceTest(unittest.TestCase):
    """拆墙线与墙券线**不许在同一回合打同一格**。

    两条线都优先挑"血最少的正面墙"⇒ 唯一那面残墙是两边的第一志愿。不挡一下就会同回合发出
    `remove` + `use`：`use` 先落地就是"券把墙升到满血、随即被拆掉"（看到的正是"工人拆满血墙"），
    反过来则是承运人那一趟白跑。判据：`_step_targets` / `_targets_for` / `_voucher_target` 收
    `exclude`（= `_Ctx.demolish_taken`），只作用于墙那几段。
    """

    BASE = Pos(10, 24)
    SHOP = Pos(22, 18)
    FAR = Pos(20, 24)
    DAY3 = 261
    WEAK = Pos(13, 23)  # 正面列中段那一格
    BESIDE_WEAK = Pos(12, 23)  # 它的待命位（`core._wall_post`），两个工人都够得着
    BESIDE_WEAK_2 = Pos(12, 22)

    def setUp(self) -> None:
        _reset_ledgers()

    def _turn(self, *roles: BaseRole, gold: int = 200, weak_hp: int = 150) -> Turn:
        ring = wall_cells(self.BASE, 41, sealed=True)
        guns = tuple(
            Weapon(id=10020 + i, kind="rocket", pos=c, attack_range=10, cooldown=0, level=3)
            for i, c in enumerate(weapon_sites(self.BASE, 41))
        )
        grid = _terrain(guns, {self.BASE: "station"}, {c: WALL for c in ring}, {self.SHOP: "weaponShop"})
        grid |= {r.pos: r.type_name for r in roles}
        return Turn(
            round_no=self.DAY3,
            map=Map((41, 32), grid),
            roles=roles,
            gold=gold,
            weapons=guns,
            walls=tuple(
                Wall(40000 + i, c, weak_hp if c == self.WEAK else 1000, 1)
                for i, c in enumerate(ring)
            ),
            vendor_prices={"stone": 1},
            shop_prices={
                WALL_FIXER: 10,
                "WeaponUpgradeVoucher1": 100,
                "WallUpgradeVoucher1": 20,
                "WallUpgradeVoucher2": 30,
            },
        )

    def test_the_demolisher_and_the_voucher_never_pick_the_same_wall(self):
        """第一个工人（有石头、贴着残墙）拆它，承运人（持券、也贴着）这一回合必须换一格。

        修复前：`2: remove (13,23)` 与 `3: use WallUpgradeVoucher1 (13,23)` 同时出现。
        """
        pioneer = Pioneer(1, self.FAR)
        first = Worker(2, self.BESIDE_WEAK, {"stone": 1})
        last = Worker(3, self.BESIDE_WEAK_2, {"WallUpgradeVoucher1": 1})
        cmds = plan(self._turn(pioneer, first, last))
        self.assertEqual(
            cmds["2"], {"action": "remove", "targetPos": [{"x": 13, "y": 23}]}, f"该拆：{cmds}"
        )
        self.assertNotEqual(
            [c for c in cmds["3"].get("targetPos", [])],
            cmds["2"]["targetPos"],
            "同一回合的 remove 与 use 不许落在同一格",
        )
        self.assertEqual(cmds["3"]["action"], "use", f"他手里那张券该换一面墙用掉：{cmds}")

    def test_the_voucher_yields_when_that_is_the_only_target(self):
        """残墙是唯一还能升的格子时，承运人这一回合**一格都不打**（不许打在正在被拆的那格上）。"""
        pioneer = Pioneer(1, self.FAR)
        first = Worker(2, self.BESIDE_WEAK, {"stone": 1})
        last = Worker(3, self.BESIDE_WEAK_2, {"WallUpgradeVoucher1": 1})
        turn = self._turn(pioneer, first, last)
        # 其余正面墙先升到 L2 ⇒ `WallUpgradeVoucher1` 只剩残墙这一个目标
        turn = turn._replace(
            walls=tuple(
                Wall(w.id, w.pos, w.health, 2 if w.pos != self.WEAK else 1) for w in turn.walls
            )
        )
        cmds = plan(turn)
        self.assertEqual(cmds["2"]["action"], "remove", f"该拆：{cmds}")
        self.assertNotIn("use", {c["action"] for c in cmds.values()}, f"券该让位：{cmds}")

    def test_a_later_day_demolishes_a_sturdier_l1_wall(self):
        """阈值随天数抬（第 5 天起每天 +50）：同一面 240 血的 L1 墙，第 3 天不动、第 6 天拆了重砌。"""
        first = Worker(2, self.BESIDE_WEAK, {"stone": 1})
        early = plan(self._turn(Pioneer(1, self.FAR), first, weak_hp=240))
        self.assertNotIn("remove", {c["action"] for c in early.values()}, f"第 3 天：240 > 200")
        late = plan(self._turn(Pioneer(1, self.FAR), first, weak_hp=240)._replace(round_no=651))
        self.assertEqual(late["2"]["action"], "remove", f"第 6 天：240 < 300 ⇒ 拆：{late}")
        self.assertEqual(late["2"]["targetPos"], [{"x": 13, "y": 23}])

    def test_a_demolished_cell_stops_being_a_voucher_target(self):
        """判据在 `_step_targets` 一层就成立（不必经过 planner）：排掉那一格，目标表里就没有它。"""
        turn = self._turn(Pioneer(1, self.FAR))
        self.assertEqual(day._targets_for("WallUpgradeVoucher1", turn)[0], self.WEAK, "残墙排第一")
        self.assertNotIn(
            self.WEAK,
            day._targets_for("WallUpgradeVoucher1", turn, frozenset({self.WEAK})),
            "认领要拆的格不该再被券挑中",
        )


class WallUpgradeBandTest(unittest.TestCase):
    """墙券链的两段与顺序（用户口径：重点升**中间三个**、边上的吃冗余券；六格先都到 2 级，
    再一起往 3 级走）。

    正面列 `x=13, y=21..26`（基地 `(10,24)`）：中间 = `22,23,24`（基地那两行 + 朝地图中心
    那一行）、边上 = `21,25,26`。链是 `(中间,2) (边上,2) (中间,3) (边上,3)` ⇒ 任何时刻都先
    砸中间那一段；中间升完才轮到边上，六格都到 2 级才开始 3 级。
    """

    BASE = Pos(10, 24)
    SHOP = Pos(22, 18)
    FAR = Pos(20, 24)
    DAY3 = 261
    MIDDLE = (Pos(13, 22), Pos(13, 23), Pos(13, 24))
    EDGE = (Pos(13, 21), Pos(13, 25), Pos(13, 26))

    def setUp(self) -> None:
        _reset_ledgers()

    def _turn(
        self, *roles: BaseRole, gold: int = 200, levels: dict[int, int] | None = None
    ) -> Turn:
        """`levels` = 正面列按 `y` 给等级（其余格一律 L1、满血）。"""
        levels = levels or {}
        ring = wall_cells(self.BASE, 41, sealed=True)
        front = {p.y for p in core.front_wall_cells(
            Turn(round_no=self.DAY3, map=Map((41, 32), {self.BASE: "station"}), roles=(), gold=0)
        )}
        guns = tuple(
            Weapon(id=10020 + i, kind="rocket", pos=c, attack_range=10, cooldown=0, level=3)
            for i, c in enumerate(weapon_sites(self.BASE, 41))
        )
        grid = _terrain(guns, {self.BASE: "station"}, {c: WALL for c in ring}, {self.SHOP: "weaponShop"})
        grid |= {r.pos: r.type_name for r in roles}
        return Turn(
            round_no=self.DAY3,
            map=Map((41, 32), grid),
            roles=roles,
            gold=gold,
            weapons=guns,
            walls=tuple(
                Wall(40000 + i, c, 1000, levels.get(c.y, 1) if c.y in front else 1)
                for i, c in enumerate(ring)
            ),
            vendor_prices={"stone": 1},
            shop_prices={
                WALL_FIXER: 10,
                "WeaponUpgradeVoucher1": 100,
                "WallUpgradeVoucher1": 20,
                "WallUpgradeVoucher2": 30,
            },
        )

    def test_the_front_column_splits_three_and_three(self):
        """中间三格与边上三格：互不相交、并起来正好是正面列那 6 格。"""
        turn = self._turn(Pioneer(1, self.FAR))
        middle = day._wall_band(turn, "wall-middle")
        edge = day._wall_band(turn, "wall-edge")
        self.assertEqual(middle, set(self.MIDDLE))
        self.assertEqual(edge, set(self.EDGE))
        self.assertEqual(set(), middle & edge)
        self.assertEqual(set(core.front_wall_cells(turn)), middle | edge)

    def test_the_middle_band_is_offered_first(self):
        """六格全是 L1 ⇒ 第一步是"中间的 L1→L2"，目标就是中间那三格。"""
        turn = self._turn(Pioneer(1, self.FAR))
        self.assertEqual(
            day._voucher_target(turn, core.WALL_CHAIN),
            ("WallUpgradeVoucher1", self.MIDDLE),
            "第一步该是中间三格",
        )
        self.assertEqual(day._targets_for("WallUpgradeVoucher1", turn), self.MIDDLE)

    def test_the_edges_wait_until_the_middle_is_two(self):
        """中间三格到 L2 ⇒ 才轮到边上的 L1→L2（不是跳过它们去升 3 级）。"""
        turn = self._turn(Pioneer(1, self.FAR), levels={22: 2, 23: 2, 24: 2})
        self.assertEqual(
            day._voucher_target(turn, core.WALL_CHAIN), ("WallUpgradeVoucher1", self.EDGE)
        )

    def test_level_three_only_starts_after_all_six_are_two(self):
        """六格全到 L2 ⇒ 第一步变成"中间的 L2→L3"（口径 B：先都到 2 级，再一起往 3 级走）。"""
        turn = self._turn(Pioneer(1, self.FAR), levels={21: 2, 22: 2, 23: 2, 24: 2, 25: 2, 26: 2})
        self.assertEqual(
            day._voucher_target(turn, core.WALL_CHAIN), ("WallUpgradeVoucher2", self.MIDDLE)
        )
        self.assertEqual(day._targets_for("WallUpgradeVoucher2", turn), self.MIDDLE)

    def test_a_held_level_three_voucher_goes_to_the_middle_first(self):
        """六格全 L2、承运人手里有一张 L2→L3 券 ⇒ 这一回合打中间那格，不是边上。"""
        pioneer = Pioneer(1, self.FAR)
        worker = Worker(2, Pos(12, 23), {"WallUpgradeVoucher2": 1})  # 待命位，够得着中间三格
        cmds = plan(
            self._turn(
                pioneer, worker, levels={21: 2, 22: 2, 23: 2, 24: 2, 25: 2, 26: 2}
            )
        )
        self.assertEqual(cmds["2"]["action"], "use", f"持券就该用掉：{cmds}")
        self.assertEqual(cmds["2"]["name"], "WallUpgradeVoucher2")
        target = Pos(cmds["2"]["targetPos"][0]["x"], cmds["2"]["targetPos"][0]["y"])
        self.assertIn(target, self.MIDDLE, "重点升级的是中间三格")

    def test_the_edges_are_only_upgraded_with_a_spare_voucher(self):
        """中间三格 L2、边上 L1、承运人手里有一张 L1→L2 券 ⇒ 这一回合才轮到边上那一格。"""
        pioneer = Pioneer(1, self.FAR)
        worker = Worker(2, Pos(12, 22), {"WallUpgradeVoucher1": 1})  # 够得着 (13,21)/(13,22)/(13,23)
        cmds = plan(self._turn(pioneer, worker, levels={22: 2, 23: 2, 24: 2}))
        self.assertEqual(cmds["2"]["action"], "use", f"中间的升满了就该用掉：{cmds}")
        target = Pos(cmds["2"]["targetPos"][0]["x"], cmds["2"]["targetPos"][0]["y"])
        self.assertIn(target, self.EDGE, "轮到边上三格")


if __name__ == "__main__":
    unittest.main()
