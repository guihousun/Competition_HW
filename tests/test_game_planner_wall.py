"""game/planner.py 建造线的用例：建武器 / 砌墙（沿环走一圈的顺序）/ 防关人闸门 /
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

from _fixtures import _records, _terrain  # noqa: E402
from coregeek.agent import AGENT  # noqa: E402
from coregeek.game.grid import STEPS, Pos, base_cells, box_cells, door_cells, step_outside, steps_between, step_toward, wall_cells, weapon_cells, weapon_sites  # noqa: E402
from coregeek.game.map import Map  # noqa: E402
from coregeek.game import planner  # noqa: E402
from coregeek.game.planner import (  # noqa: E402
    POST_MARGIN,
    STONE_RESERVE,
    WALL,
    WEAPONS_BY_SITE,
    plan,
)
from coregeek.game.roles import BaseRole, Pioneer, Worker  # noqa: E402
from coregeek.game.world import DAY_ROUNDS, ROUNDS_PER_DAY, Robot, Turn, Wall, Weapon  # noqa: E402
from coregeek.protocol import model  # noqa: E402


class BuildWeaponTest(unittest.TestCase):
    """合成开局：白天、0 武器、基地在左半 —— 工人会建满三座、建在该建的地方、然后收手。

    必须把回合串起来跑：单帧"目标格算得对"证明不了收不收得住（`MineApproachTest` 同理）。
    结算照判题器口径来 —— 一回合一步，建起来的武器下一回合就挡路、也占掉那个格子。
    """

    #: 三个落点由 `weapon_sites` 给（左半基地 (10,24) ⇒ (12,24)/(12,25)/(12,22)）
    BASE = Pos(10, 24)
    MINE = Pos(4, 24)

    def setUp(self) -> None:
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

    def test_a_short_budget_builds_only_one(self):
        """只有 25 金：只建一座（排第一的火箭），金花光就停手。

        金币按递减预算扣；写成 `gold >= 25 * 待建数`（25 < 75 ⇒ 一座都不建）就全错了。
        跑满到建成 1 座为止 —— 那个落点离工人出生位远，单回合够不着。
        """
        self.gold = 25
        self._settle(want=1)
        self.assertEqual(len(self._weapons()), 1)
        self.assertEqual(self._weapons()[0], "rocket")
        self.assertEqual(self.gold, 0, "25 金正好建一座，花光才停")

    def test_a_blocked_site_drops_only_its_own_weapon(self):
        """排第一的火箭落点被墙占了 ⇒ 只有那一座不建，另两座照落在各自的位置上。

        钉的是 `_slots` 里"落点与种类绑死、再滤 `blocked`"的顺序：反过来（先滤空再
        `zip`）整体前移，每个种类都挪到别人家的落点上，而报文完全合法、本地全绿。
        顺带守住"覆盖会把原武器打成 level1"（§4.5.1 补充说明）。
        """
        self.walls[Pos(12, 24)] = WALL  # 第一座火箭的落点被墙占了
        self._settle(want=2)
        self.assertEqual(
            {(name, cell) for name, cell in self.builds},
            {("rocket", Pos(12, 25)), ("gatling", Pos(12, 22))},
        )
        built_cells = {cell for _, cell in self.builds}
        self.assertNotIn(Pos(12, 24), built_cells, "落点被占 ⇒ 那一座就是不建，不换地方")

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

        这一条把"回合预算 → 采矿 → 砌墙"整条线钉在一起：预算算大了天黑砌不完，
        算小了石头不够、工人在工地干等；顺序错了则会先把背面砌满、正面空着。
        收工时手里剩 `STONE_RESERVE` 块 —— 每多采一块净花 3 回合，攒到上限就该收手。
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
        self.assertEqual(stone, STONE_RESERVE, "砌完手里正好留 3 块存货，一块不多")

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
        stone = len(corners) + STONE_RESERVE  # 够那两格 + 存货 ⇒ 不采也不卖
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

    def test_a_late_start_stops_mining_and_goes_to_build(self):
        """白天快过完了（roundNo=60 ⇒ 只剩 11 回合）⇒ 不再采矿，拿着手里的石头直接去工地。

        「必须在晚上到来前将墙建好，注意计算回合数」（策略指导）—— 这是唯一的时间硬约束。
        两个局面只差 `roundNo`，走的方向必须反过来。
        """
        pos, stone = Pos(6, 24), 3
        early = plan(self._turn(round_no=1, stone=stone, pos=pos))["1"]
        late = plan(self._turn(round_no=60, stone=stone, pos=pos))["1"]
        self.assertEqual({early["action"], late["action"]}, {"move"})
        early_cell = Pos(early["targetPos"][0]["x"], early["targetPos"][0]["y"])
        late_cell = Pos(late["targetPos"][0]["x"], late["targetPos"][0]["y"])
        self.assertLess(early_cell.dist(self.MINE), pos.dist(self.MINE), "白天还长 ⇒ 继续朝矿走")
        self.assertGreater(late_cell.dist(self.MINE), pos.dist(self.MINE), "时间不够 ⇒ 掉头去工地")

    def test_the_last_cell_still_mines_three_extra_stones(self):
        """墙上只剩一格、手里一块石头都没有 ⇒ 也采够"那一格 + `STONE_RESERVE`"块再回去砌。

        存货只能在这一趟里攒：环砌满之后墙线整个不参与（`target is None`），白天再没人采石头。
        这几块是给"夜里被打掉一格、第二天立刻补上"备的（用户口径）。
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
        self.assertEqual(collects, 1 + STONE_RESERVE, "多采的正好是存货那 3 块")
        self.assertEqual(stone, STONE_RESERVE, "砌完手里留着 3 块")

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


class WallGateTest(unittest.TestCase):
    """建墙原则：不能把工人关起来。闸门两半：`_ring` 里"会关人就一格都不砌"
    （判据 = 砌满这一圈墙之后谁出不去），`plan` 里"要被关住的人先走出来"（仅白天）。

    局面得手工搭：36 格的盒子里不可能有矿（任务书 L78），后方通道里也没有建筑 ⇒
    现实里只有单位能堵它，6 个单位才够合上闸门；不摆机器人，有几条用例连坏的实现
    都放不过去（夹具与真实路径不同形）。
    """

    BASE = Pos(10, 24)
    WEAPONS = _records({Pos(9, 23): "gatling", Pos(9, 24): "railgun", Pos(9, 22): "rocket"})
    #: 后方通道 = 背面整列 6 格（基地在左半 ⇒ 背面是 x = bx-2）。`wall_cells` 一格都不砌它。
    DOOR = {Pos(8, y) for y in range(21, 27)}
    #: 盒内一格空地（武器环上、没摆炮）、盒外一格
    INSIDE, OUTSIDE = Pos(12, 24), Pos(5, 24)

    def _turn(
        self,
        walls: Iterable[Pos] = (),
        door_blocked: bool = False,
        *,
        at1: Pos | None = None,
        at2: Pos | None = None,
        round_no: int = 1,
        robots: tuple[Robot, ...] = (),
        ores: dict[Pos, str] | None = None,
        prices: dict[str, int] | None = None,
        robot_cells: Iterable[Pos] = (),
    ) -> Turn:
        """`at1` / `at2` = 两个工人的站位（默认 1 号在盒内、2 号在盒外且手里有一块石头）。

        站位是可以换到盒外的 —— "盒外的人不许否决这一圈墙"那条就得两个都在外面才测得出。
        `robot_cells` 是额外的挡路机器人（"堵掉通道里的哪几格"要精确到格时才用）。
        """
        workers = (
            Worker(1, at1 or self.INSIDE, {}),
            Worker(2, at2 or self.OUTSIDE, {"stone": 1}),
        )
        robots_on_map = {c: "robot" for c in (self.DOOR if door_blocked else ())}
        robots_on_map.update({c: "robot" for c in robot_cells})
        return Turn(
            round_no=round_no,
            map=Map(
                (41, 32),
                # 角色铺进地形（`model._entries` 就是这么干的）—— "谁站在哪"是真实输入的一部分
                _terrain(
                    self.WEAPONS,
                    {self.BASE: "station"},
                    {c: WALL for c in walls},
                    robots_on_map,
                    ores or {},
                    {w.pos: "worker" for w in workers},
                ),
            ),
            roles=workers,
            gold=0,
            weapons=self.WEAPONS,
            robots=robots,
            vendor_prices=prices or {},
        )

    def test_a_walled_box_never_holds_a_worker_in(self):
        """14 格全砌满、通道开着 ⇒ 盒里的人照样走得出去。这就是"正面砌满也关不住人"。

        不能拿 `step_toward` 顶替：它的契约是"贴着 goal 即到"（`dist(goal) <= 1 ⇒ None`），
        用来测"出不出得去"时，一个贴着通道口、而那格被堵住的角色会被判成"到不了"。
        """
        turn = self._turn(walls=wall_cells(self.BASE, 41))
        self.assertNotIn(Pos(8, 24), turn.map.blocked, "背面那一列连砌满之后也不该有东西")

        step = step_outside(self.INSIDE, box_cells(self.BASE), turn.map.blocked, turn.map.size)
        self.assertIsNotNone(step, "14 格砌满 + 通道开着 ⇒ 出得去")
        self.assertEqual(self.INSIDE.dist(step), 1, "返回的是从 pos 迈出的第一步")

    def test_a_blocked_door_holds_the_walls_back(self):
        """通道被堵满 ⇒ 盒外那个工人手里攥着石头也不许砌（砌下去就把 1 号关死了）。

        少砌这一回合的代价是墙晚砌完；砌下去的代价是把人关死在盒里。
        """
        turn = self._turn(door_blocked=True)
        cmds = plan(turn)
        self.assertNotIn(
            "2", cmds, "盒外那个工人该原地不动（没矿可采、价目表也空着），而不是去砌墙"
        )
        # 顺带钉住"整面墙"这个判据：通道一堵，14 格一格都不该砌
        self.assertNotIn("build", {c["action"] for c in cmds.values()})

    def test_a_blocked_door_sends_the_worker_out_first(self):
        """同上通道被堵，但墙上还一个缺口都没砌 ⇒ 被围的人趁缺口先出去。

        闸门两半的关键差别在两套障碍：`leaving` 按"假设墙砌满"判（砌完就真出不去了），
        而这迈出去的一步按"现在"的障碍算（缺口就是出路）。
        若这里也按砌满算，`step_outside` 必然返回 None ⇒ 这一支永远空转、白写。
        """
        cmds = plan(self._turn(door_blocked=True))
        self.assertEqual(set(cmds), {"1"}, "要被关住的人这一回合必须动，盒外那个该原地待命")
        self.assertEqual(cmds["1"]["action"], "move")
        cell = Pos(cmds["1"]["targetPos"][0]["x"], cmds["1"]["targetPos"][0]["y"])
        self.assertEqual(self.INSIDE.dist(cell), 1, "一步一格，不能瞬移")
        # 通道整列全堵着 ⇒ 出路只能是没砌的墙那几条边（正面 x = bx+3 最直接）
        self.assertIn(cell, box_cells(self.BASE), "迈出去的这一步还在盒内，方向朝缺口")

    def test_the_night_guard_never_walks_off_its_cannon(self):
        """夜里不送人出去：夜里不砌墙（`build` 仅白天）⇒ 没有"会被关住"这回事，
        这一支唯一的效果是把人从炮位上拉走。判据里那个 `turn.is_day` 就是为它写的。

        局面：门被堵 + 盒内工人贴着一座炮，射程内有个机器人 ⇒ 该开火，不该挪窝。
        """
        turn = self._turn(
            door_blocked=True,
            at1=Pos(9, 25),  # 贴着 (9,24) 那座轨炮，且在盒内
            round_no=85,  # 夜里（白天 70 回合）
            robots=(Robot(pos=Pos(13, 24), health=10),),
        )
        cmds = plan(turn)
        # `attack` 的 key 是武器 id，操控者在 `controllerId` 里（与下面那些 `move` 不同）
        fired = [c for c in cmds.values() if c.get("controllerId") == "1"]
        self.assertEqual(len(fired), 1, f"1 号该开火，而不是往盒子外走：{cmds}")
        self.assertEqual(fired[0]["action"], "attack")

    def test_a_worker_outside_never_vetoes_the_ring(self):
        """两个工人都在盒外 ⇒ 门堵着也照砌（没人会被关住）。

        钉的坑：`step_outside` 对"本来就在外面"与"走不出去"都返回 `None`，筛"谁在盒子里"
        时漏掉 `r.pos in box` 就会把所有在盒外干活的人判成要被关住 ⇒ 闸门永远合上、
        墙一格都砌不上，症状还极安静（不报错、不违规，只是工人站着不动）。
        2 号工人（后处理的那个）分到后段首格，站位就贴那一格。
        """
        gaps = wall_cells(self.BASE, 41)
        target = gaps[(len(gaps) + 1) // 2]
        occupied = (
            set(base_cells(self.BASE)) | {w.pos for w in self.WEAPONS} | self.DOOR | {Pos(5, 24)}
        )
        # 站位必须在**盒外** —— 盒里那个会被防关人闸门当成"砌满就出不去"、改走迈出盒子那一支
        spot = next(
            Pos(target.x + d.x, target.y + d.y)
            for d in STEPS
            if 0 <= target.x + d.x < 41 and 0 <= target.y + d.y < 32
            and Pos(target.x + d.x, target.y + d.y) not in occupied
            and Pos(target.x + d.x, target.y + d.y) not in box_cells(self.BASE)
        )
        turn = self._turn(door_blocked=True, at1=Pos(5, 24), at2=spot)
        cmds = plan(turn)
        self.assertEqual(cmds["2"]["action"], "build", "盒外那个手里有石头、又贴着分到的目标 ⇒ 该砌")
        self.assertEqual(cmds["2"]["name"], WALL)
        self.assertEqual(
            Pos(cmds["2"]["targetPos"][0]["x"], cmds["2"]["targetPos"][0]["y"]),
            target,
            "砌的正是分给它的后段首格",
        )
        self.assertNotIn("1", cmds, "1 号没石头、也没石矿可采 ⇒ 空指令")

    def test_two_workers_walking_out_never_aim_at_the_same_cell(self):
        """两个人都被围 ⇒ 各走各的格：闸门 (2) 的障碍里必须含 `claimed`。

        漏了它两条 `move` 指向同一格，撞上 §4.5.4 的"目标点争夺"—— 两个人都不动，
        而日志上只是"这回合没动"，看不出是撞车。

        站位是挑过的：`(9,25)` 与 `(10,25)` 按现在的障碍算第一步都指向 `(9,26)`
        （穷举盒内空地找到的唯一一对）。随手摆两个人在盒内，两条指令本来就不会撞。
        """
        cmds = plan(self._turn(door_blocked=True, at1=Pos(9, 25), at2=Pos(10, 25)))
        self.assertEqual(len(cmds), 2, f"两个人的处境一样，都该往外走：{cmds}")
        self.assertEqual({c["action"] for c in cmds.values()}, {"move"})
        cells = {Pos(c["targetPos"][0]["x"], c["targetPos"][0]["y"]) for c in cmds.values()}
        self.assertEqual(len(cells), 2, "落点必须分开")

    def test_a_gated_round_never_sends_a_worker_mining_far_away(self):
        """闸门挡住的那一回合是"墙还没砌完"，不是"砌完了" ⇒ 盒外那个什么都不做。

        两种"没得砌"混在一起的话，它会掉头奔向地图另一头，或者干脆就地砌一格 ——
        而那一格砌下去正好把这一回合往外走的 1 号关在墙里。局面：门被机器人堵满、
        环上一格没砌、盒外那个手里有石头、地图另一头有一座贵的铜矿。
        """
        turn = self._turn(
            door_blocked=True,
            at1=Pos(12, 24),
            at2=Pos(14, 23),
            ores={Pos(36, 24): "copper"},
            prices={"stone": 1, "copper": 5},
        )
        cmds = plan(turn)
        self.assertEqual(set(cmds), {"1"}, f"盒外那个该原地待命：{cmds}")
        self.assertEqual(cmds["1"]["action"], "move", "被围的那个趁缺口先出来")

    def test_a_colleague_in_the_door_still_holds_the_wall_back(self):
        """自己人站的那格也算障碍：通道 6 格里 5 格被机器人堵着、剩下一格站着同事 ⇒ 谁都砌不了。

        与 `_ring` 里"自己人算路过"故意相反：那处问"这一格要不要砌"（排掉路过的人，
        否则两个工人对着改目标来回踱步），这里问"会不会有人出不来"。

        光有同事挡路测不出这条 —— 他站在待砌的墙格上时那格本来就是"假设砌满"里的墙，
        两种口径下都是障碍；必须是永远不砌的通道格才算数（夹具让 2 号站到通道的一格上）。
        """
        door = sorted(self.DOOR)
        turn = self._turn(at1=Pos(12, 24), at2=door[0], robot_cells=door[1:])
        self.assertEqual(self.DOOR - turn.map.blocked, set(), "通道 6 格都该挡着")
        cmds = plan(turn)
        self.assertEqual(set(cmds), {"1"}, f"盒内那个趁缺口走，站通道上的那个不许砌：{cmds}")
        self.assertEqual(cmds["1"]["action"], "move")


class DayEndGateTest(unittest.TestCase):
    """白天末尾回炮位：离天黑只剩回程步数时就往回走。

    回程步数走 BFS：环砌满之后回炮位要绕背面那道门、再横穿盒子，切比雪夫直线 5 步的路
    真实十几步（`StepsBetweenTest` 钉的就是这一条）。这一支只许发 `move`：`attack` 仅黑夜
    可用（§4.4），白天发一条就是一次异常、累计 5 次整场不再被调度，而复用 `_defend` 是
    这里最容易犯的错（它贴近炮位会调 `_fire`）⇒ 有一条扫白天各回合、各站位的守门员。
    """

    BASE = Pos(10, 24)
    #: 与 `weapon_sites` 同序的两火箭 + 一加特林（这里只要"有三座炮"就够）
    WEAPONS = _records({Pos(12, 24): "rocket", Pos(12, 25): "rocket", Pos(12, 22): "gatling"})
    SIZE = (41, 32)

    def _turn(self, *, round_no: int = 1, ring: bool = True, at: Pos = Pos(20, 24), stone: int = 0) -> Turn:
        """环默认砌满（闸门只在砌完之后生效）；没有矿、没有小贩、没有金 —— 只留闸门这一支。"""
        walls = {c: WALL for c in wall_cells(self.BASE, 41)} if ring else {}
        worker = Worker(1, at, {"stone": stone} if stone else {})
        return Turn(
            round_no=round_no,
            map=Map(
                self.SIZE,
                _terrain(
                    self.WEAPONS, {self.BASE: "station"}, walls, {worker.pos: "worker"}
                ),
            ),
            roles=(worker,),
            gold=0,
            weapons=self.WEAPONS,
        )

    def _steps_home(self, at: Pos) -> int:
        """从 `at` 走到最近那个**岗位**的真实步数（与 `planner` 同一个障碍、同一个口径）。

        岗位两处：火箭对的共用操作位 `(11,25)` —— 它是空地、要**站上去**（比"贴着"多一步）；
        加特林单列一组，岗位就是它的炮位 `(12,22)`（贴着它即可）。障碍取 `Map.blocked`
        （含基地与武器格）：只在墙环上算的话，BFS 会穿过基地与炮位抄近路。
        """
        walk = self._turn(at=at).map.blocked
        hops = []
        for post, onto in ((Pos(11, 25), True), (Pos(12, 22), False)):
            if at == post:
                hops.append(0)  # 已经在岗位上
                continue
            steps = steps_between(at, post, walk, self.SIZE)
            if steps >= 0:
                hops.append(steps + 1 if onto else steps)
        return min(hops)

    def test_the_gate_aims_at_the_shared_operator_spot(self):
        """收工的落点是那一组的**岗位**，不是"最近那座炮"：火箭对站到共用的操作位上去。

        "站上去"是这件事的全部意义（`(10,25)` 只是进那个口袋的必经格）：天黑时人已经在岗，
        夜里第一回合两座火箭就能交替；停在邻格则永远只贴着其中一座。
        """
        at = Pos(10, 25)
        self.assertEqual(self._steps_home(at), 1, "就差站上操作位这一步")
        cmds = plan(self._turn(round_no=DAY_ROUNDS - 1, at=at))
        self.assertEqual(cmds["1"]["action"], "move", f"该走上岗位：{cmds}")
        cell = Pos(cmds["1"]["targetPos"][0]["x"], cmds["1"]["targetPos"][0]["y"])
        self.assertEqual(cell, Pos(11, 25), "落点就是共用操作位本身")

    def test_the_gate_opens_exactly_when_the_walk_home_eats_the_day(self):
        """回程步数 + `POST_MARGIN` ≥ 白天剩余 ⇒ 这一回合就往炮位走。卡在边界上测。

        边界 = `day_rounds_left == steps + POST_MARGIN`（正好合上）；再多剩一回合就不许动身
        （否则整个白天都在炮位上干等）。
        """
        at = Pos(20, 24)
        steps = self._steps_home(at)
        self.assertGreater(steps, 0, "这个站位本来就该离炮位远一点")
        gate = DAY_ROUNDS - steps - POST_MARGIN + 1  # 这一回合的 day_rounds_left 正好是 steps + 3
        cmds = plan(self._turn(round_no=gate, at=at))
        self.assertEqual(cmds["1"]["action"], "move", f"该往回赶：{cmds}")
        cell = Pos(cmds["1"]["targetPos"][0]["x"], cmds["1"]["targetPos"][0]["y"])
        self.assertEqual(at.dist(cell), 1, "一步一格")
        self.assertLess(
            self._steps_home(cell), steps, "这一格必须真的离家更近（不许在原地打转）"
        )
        # 白天还富裕一回合 ⇒ 不许动身
        self.assertEqual(plan(self._turn(round_no=gate - 1, at=at)), {})

    def test_a_day_round_never_fires(self):
        """红线守门员：白天任何回合、任何站位（含贴着炮）都只许有 `move`。

        "复用 `_defend`"会在这里立刻现形 —— 那正是 5 次异常直通出局的写法。
        """
        for round_no in (1, 35, 60, 66, 69, DAY_ROUNDS):
            for at in (Pos(20, 24), Pos(14, 24), Pos(10, 25), Pos(9, 24), Pos(9, 26)):
                with self.subTest(round_no=round_no, at=at):
                    cmds = plan(self._turn(round_no=round_no, at=at))
                    self.assertNotIn(
                        "attack", {c["action"] for c in cmds.values()}, f"白天开火非法：{cmds}"
                    )

    def test_the_gate_never_preempts_the_wall(self):
        """环没砌完 ⇒ 闸门不生效，照旧砌墙（补墙优先于收工）。

        这是有意的顺序：防御靠的是天黑前把墙封死（含正面那个口），
        早了就没人砌了。对照是同一天同一站位、环砌满时那一支确实动身。
        """
        at = Pos(14, 21)  # 贴着待砌的第一格 (13,21)
        late = DAY_ROUNDS - 1
        cmds = plan(self._turn(round_no=late, ring=False, at=at, stone=1))
        self.assertEqual(cmds["1"]["action"], "build", f"环没砌完 ⇒ 继续砌：{cmds}")
        self.assertEqual(
            plan(self._turn(round_no=late, at=at))["1"]["action"], "move", "环砌满 ⇒ 同一格是往回赶"
        )










class RescueTest(unittest.TestCase):
    """有人被关在盒子里 ⇒ 工人去拆一格放人。

    与 `plan` 里的防关人闸门是预防 vs 补救：闸门管"还没砌完时别把谁关进去"，这一支管
    "已经被关住了怎么办" —— 后方通道那一列被机器人堵死，是闸门拦不住的。

    工人自己被关住时也走这里（它在差事之前的 `_rescue` 被 `_stuck_inside` 捞出来）；
    被任务钉死的开拓者只能靠别人救（守门：`TaskHoldTest`）。
    """

    BASE = Pos(10, 24)
    SIZE = (41, 32)
    WEAPONS = _records({Pos(9, 25): "rocket", Pos(12, 22): "gatling", Pos(12, 25): "railgun"})
    PRICES = {"stone": 1, "iron": 3, "copper": 5}
    #: 后方通道 = 背面整列 6 格（唯一进出口，14 格墙一格都不砌它）
    DOOR = door_cells(Pos(10, 24), 41)

    def _turn(
        self,
        *,
        round_no: int = ROUNDS_PER_DAY + 1,
        rescuer: Pos = Pos(14, 24),
        boxed: Pos = Pos(12, 24),
        stone: int = 1,
        robots: bool = True,
        colleagues: tuple[Pos, ...] = (),
        gap_robot: Pos | None = None,
        phase_task: str = "把石头运回基地",
    ) -> Turn:
        """默认：通道被 6 台机器人堵死、开拓者被关在盒子里、工人站在正面墙外 `(14,24)`。

        `gap_robot` = 环上留一格不砌、并在那一格里放一台机器人（缺口被堵住 ⇒ 环敞着人也出不去）。
        """
        roles: list[BaseRole] = [Worker(1, rescuer, {"stone": stone} if stone else {}), Pioneer(2, boxed, {})]
        obstacles: dict[Pos, str] = {}
        if robots:
            for cell in self.DOOR:
                obstacles[cell] = "robot:small"
        for i, colleague in enumerate(colleagues):
            obstacles[colleague] = "worker"
            roles.append(Worker(3 + i, colleague, {}))
        if gap_robot is not None:
            obstacles[gap_robot] = "robot:small"
        # 墙既是地形也是实体（`teamOur.roles` 里那份）：`_rescue` 按实体挑要拆的那一格
        built = [c for c in wall_cells(self.BASE, 41) if c != gap_robot]
        return Turn(
            round_no=round_no,
            walls=tuple(Wall(40000 + i, c, 1000, 1) for i, c in enumerate(built)),
            map=Map(
                self.SIZE,
                _terrain(
                    self.WEAPONS,
                    {c: WALL for c in built},
                    {self.BASE: "station"},
                    {rescuer: "worker"},
                    {boxed: "pioneer"},
                    obstacles,
                ),
            ),
            roles=tuple(roles),
            gold=0,
            weapons=self.WEAPONS,
            vendor_prices=self.PRICES,
            phase_task=phase_task,
            llm_resp="<answer>42</answer>",
        )

    @staticmethod
    def _holes(cmds: dict[str, dict]) -> dict[str, Pos]:
        return {
            rid: Pos(c["targetPos"][0]["x"], c["targetPos"][0]["y"])
            for rid, c in cmds.items()
            if c["action"] == "remove"
        }

    def test_a_worker_opens_the_wall_to_free_a_boxed_pioneer(self):
        """通道被机器人堵死 + 开拓者在盒内 ⇒ 工人拆一格放人，这一格是 `(13,23)`（并列取坐标序，与 `_rescue` 的 min 同序）。

        `(13,23)` 是墙外那位工人贴着的墙格（`remove` 的站位契约）：拆了它，盒内的人就能
        从 `(14,23)` 一带迈出去。开拓者这一回合照旧只有 `submitAnswer` —— 它被钉在任务上，
        谁也挪不动它。
        """
        cmds = plan(self._turn())
        self.assertEqual(self._holes(cmds), {"1": Pos(13, 23)})
        self.assertEqual(cmds["2"]["action"], "submitAnswer", "开拓者只交答案，不动")
        self.assertNotIn("attack", {c["action"] for c in cmds.values()})

    def test_the_rescuer_walks_to_the_wall_first(self):
        """隔着半个盒子 ⇒ 这一回合只挪一格，贴近了下一回合才拆（拆墙要贴着）。

        要拆哪一格由**被困者**定（开拓者贴着 `(13,23)`），不由救援者定 —— 救援者只负责朝它
        走。候选只认当前已经贴着的墙格（`remove` 的站位契约），刻意不做"走到最优那一格再拆"。
        判"更近"必须走 BFS：切比雪夫会被绕行骗过（第一步横着没动、直线距离一格不减）。
        """
        start = Pos(5, 24)
        site = Pos(13, 23)  # 被困的开拓者贴着的那一格墙
        turn = self._turn(rescuer=start)
        cmds = plan(turn)
        self.assertEqual(cmds["1"]["action"], "move", f"还没贴近 ⇒ 只能挪一格：{cmds}")
        cell = Pos(cmds["1"]["targetPos"][0]["x"], cmds["1"]["targetPos"][0]["y"])
        self.assertEqual(start.dist(cell), 1, "一步一格")
        walk, size = turn.map.blocked, turn.map.size
        self.assertLess(
            steps_between(cell, site, walk, size),
            steps_between(start, site, walk, size),
            "这一格必须真的更近",
        )

    def test_colleagues_in_the_doorway_are_not_walls(self):
        """同事把整列通道都堵上 ⇒ 照旧不拆（他们只是路过，下一回合就走）。

        判据里"自己人一律不算障碍"（把全部角色从障碍里摘掉，起点除外）：算成墙就会白拆
        一次 —— 1 回合 + 1 块不回收的石头，换来一个下一回合就自动失效的洞。
        夹具必须堵满整列：只堵几格时盒内的人本来就走得出去，用例会空转
        （反向验证：只堵几格的夹具在"自己人算障碍"的写法下也照样过）。
        """
        cmds = plan(self._turn(robots=False, colleagues=self.DOOR))
        self.assertEqual(self._holes(cmds), {}, f"同事堵通道不算被关：{cmds}")

    def test_the_first_day_rescues_once_the_ring_is_complete(self):
        """第 1 天环砌完了照救（用户口径）：环满 ⇒ 上面那个缺口只可能是自己拆出来的。

        按回合号一刀切（第 1 天一律不救）的话，第 1 天被关住的人整整一天没人管。⚠️ 这一天救得了
        还得赶早：`HOLE_MIN_LEFT`(30) 要求白天还剩 30 回合以上 ⇒ 环得在**第 40 回合前**砌完，
        否则第 1 天照旧救不了（第 41 回合起 `day_rounds_left` 掉到 30 以下）。
        """
        self.assertEqual(self._holes(plan(self._turn(round_no=1))), {"1": Pos(13, 23)})

    def test_the_rescue_never_targets_a_cell_without_a_wall(self):
        """拆的必须是**真有墙**的那一格：环上没砌的格被机器人踩住时，`_ring` 看不见它（挡路 ⇒ 当已砌）。

        这一幕是第 68 步放开第 1 天救援之后才够得着的：缺口被堵 ⇒ 盒里的人**真的**被困住、
        `_ring` 也**真的**以为环齐了（没错，那一刻盒子确实是封的）⇒ 救援该来；但它拆的必须是
        一格墙，不是那个没砌的空地（拆空地 = 白丢一回合，人还在里面）。夹具必须把那格堵上：
        缺口敞着时人本来就走得出去，用例连坏的实现都放过去。
        """
        gap = Pos(13, 23)
        cmds = plan(self._turn(round_no=1, gap_robot=gap))
        holes = self._holes(cmds)
        self.assertTrue(holes, f"人真被困住了，该来救：{cmds}")
        self.assertNotIn(gap, set(holes.values()), f"没墙的那格不能拆：{cmds}")

    def test_a_stone_short_worker_cannot_rescue(self):
        """没石头救不了：拆一块少一块，补不回来就是整夜的洞。"""
        cmds = plan(self._turn(stone=0))
        self.assertEqual(self._holes(cmds), {}, f"手里没石头不许拆：{cmds}")

    def test_a_boxed_worker_frees_itself(self):
        """被关住的是工人（盒内、没有差事）⇒ 它自己去拆一格：`(13,23)`。

        这条走的是 `_rescue`：`_stuck_inside` 把"眼下真出不去"的人捞出来，
        而工人是唯一能发 `remove` 的角色 ⇒ 自己就是救援者。
        """
        turn = self._turn(boxed=Pos(7, 24), rescuer=Pos(12, 24))
        self.assertEqual(self._holes(plan(turn)), {"1": Pos(13, 23)})






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
        q = planner._Queue(turn)
        self.assertTrue(q.step(walker, Pos(9, 5), avoid={Pos(6, 5), Pos(7, 5), Pos(8, 5)}))
        planner._walk_out(turn, q, set())
        self.assertEqual(
            q.cmds["1"],
            {"action": "move", "targetPos": [{"x": 6, "y": 5}]},
            "avoid 只是软的：绕不开就退回硬障碍，绝不原地卡死",
        )


class RepairTest(unittest.TestCase):
    """弱墙（血 < 满血 1/4 = 不完备）的修复差事，按等级分派修法。

    文档事实：WallFixer 10 金、目标墙回满血（任务书消耗品表），站墙一格内 `use`。L2+ 回血
    （1 回合 + 10 金、墙不塌、不开洞）优于推倒重建；L1 反着来 —— 一块 1000 血的墙不值得
    25 金的包，拆掉让 `_ring` 重砌（2 石头 + 2 回合）。守门：`_weak_walls` 的阈值 +
    `_repair_line` 的分派。
    """

    BASE = Pos(10, 24)
    WEAPONS = _records({Pos(9, 23): "gatling", Pos(9, 24): "railgun", Pos(9, 22): "rocket"})
    SHOP = Pos(20, 16)
    WEAK = Pos(13, 22)  # 正面列的一格（弱墙）
    OTHER = Pos(13, 26)  # 正面列的另一格（第二面弱墙）
    L2 = 300  # L2 满血 1500 ⇒ 300×4 < 1500
    L1 = 200  # L1 满血 1000 ⇒ 200×4 < 1000

    def _turn(self, worker: Worker, *, walls=None, shop=True, gold=0, prices=None, ores=()):
        grid = _terrain(self.WEAPONS, {self.BASE: "station", self.WEAK: "wall"})
        grid |= {p: WALL for p in ores}
        if shop:
            grid[self.SHOP] = "weaponShop"
        grid[worker.pos] = "worker"
        if walls is None:
            walls = (Wall(40000, self.WEAK, self.L2, 2),)
        return Turn(
            round_no=1,
            map=Map((41, 32), grid),
            roles=(worker,),
            gold=gold,
            weapons=self.WEAPONS,
            shop_prices=prices if prices is not None else {"WallFixer": 10},
            walls=walls,
        )

    def test_a_worker_with_a_pack_repairs_a_second_level_wall(self):
        """持包 + 贴着 L2 弱墙 ⇒ `use WallFixer`（墙回满血、不塌、1 回合）。"""
        worker = Worker(10010, Pos(12, 22), {"WallFixer": 1})
        cmd = plan(self._turn(worker))[str(10010)]
        self.assertEqual(cmd["action"], "use")
        self.assertEqual(cmd["name"], "WallFixer")
        self.assertEqual(
            Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"]), self.WEAK
        )

    def test_a_worker_without_a_pack_buys_one_for_a_second_level_wall(self):
        """没包 ⇒ 去商店买（贴着就买）；`collect`/挖矿都排在修墙之后。"""
        worker = Worker(10010, Pos(20, 15), {})
        cmd = plan(self._turn(worker, gold=10))[str(10010)]
        self.assertEqual(cmd["action"], "buy")
        self.assertEqual(cmd["name"], "WallFixer")

    def test_a_worker_two_cells_from_the_shop_walks_instead_of_buying(self):
        """差一格还不许买：`steps_between` 的 **0** 才是"贴着商店"（1 = 还要走一格）。

        `buy` 要在商店周围一格内。站在切比雪夫 2 的地方发出去是非法指令，而且每回合算出来
        还是 1、下一回合原样再发 —— 一次判错换算成几十次异常，红线只有 5 次。
        """
        worker = Worker(10010, Pos(22, 16), {})  # 与商店 (20,16) 切比雪夫 2
        self.assertEqual(worker.pos.dist(self.SHOP), 2, "夹具前提：与商店差一格")
        cmd = plan(self._turn(worker, gold=10))[str(10010)]
        self.assertEqual(cmd.get("action"), "move", f"该走过去，不是隔着一格买：{cmd}")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(self.SHOP), worker.pos.dist(self.SHOP), "朝商店走一格")

    def test_a_worker_walks_to_a_second_level_wall_it_cannot_reach_yet(self):
        """没贴着也照走：差事的目标就是那面墙（不是原地等它自己贴过来）。"""
        worker = Worker(10010, Pos(5, 22), {"WallFixer": 1})
        cmd = plan(self._turn(worker))[str(10010)]
        self.assertEqual(cmd["action"], "move")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(self.WEAK), worker.pos.dist(self.WEAK), "朝弱墙走一格")

    def test_a_level_one_wall_is_demolished_even_with_a_pack_in_hand(self):
        """L1 弱墙 ⇒ `remove`，包在手里也照样拆（等级定修法，不看包）。"""
        worker = Worker(10010, Pos(12, 22), {"WallFixer": 1, "stone": 1})
        cmd = plan(self._turn(worker, walls=(Wall(40000, self.WEAK, self.L1, 1),)))[str(10010)]
        self.assertEqual(cmd["action"], "remove", "L1 拆掉重建，不花 25 金的包")
        self.assertEqual(Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"]), self.WEAK)

    def test_a_level_one_wall_is_demolished_from_a_distance(self):
        """没贴着 ⇒ 先走过去（拆墙的站位与 `build` 同一条：切比雪夫 ≤1）。"""
        worker = Worker(10010, Pos(5, 22), {"stone": 1})
        cmd = plan(self._turn(worker, walls=(Wall(40000, self.WEAK, self.L1, 1),)))[str(10010)]
        self.assertEqual(cmd["action"], "move")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(self.WEAK), worker.pos.dist(self.WEAK), "朝那面墙走一格")

    def test_a_level_one_wall_is_left_alone_without_stone(self):
        """手里没石头 ⇒ 不拆：拆了那格不回收，砌不回来就只是白开一个洞。"""
        worker = Worker(10010, Pos(12, 22), {})
        cmd = plan(
            self._turn(worker, shop=False, gold=0, prices={},
                       walls=(Wall(40000, self.WEAK, self.L1, 1),))
        ).get(str(10010), {})
        self.assertNotEqual(cmd.get("action"), "remove")

    def test_a_second_level_wall_is_never_demolished(self):
        """L2 弱墙没包又买不起 ⇒ 什么都不拆（推倒会把等级赔进去），也照样不待命到天荒地老
        —— 这里只钉住"不发 remove"这一条。"""
        worker = Worker(10010, Pos(12, 22), {"stone": 1})
        cmd = plan(
            self._turn(worker, shop=False, gold=0, prices={})
        ).get(str(10010), {})
        self.assertNotEqual(cmd.get("action"), "remove")
        self.assertNotEqual(cmd.get("action"), "use", "没包就没得 use")

    def test_a_wall_above_a_quarter_health_is_never_touched(self):
        """阈值是"不到满血 1/4"，不是"不到满"、也不是"不到一半"：过线的墙照旧算完备。

        400（L1 的 1/4 = 250）与 600（L2 的 1/4 = 375）都在正中间那一档 —— 按"半血"判就会
        被拉去修/拆，正是这条钉住的分界。满血那两条顺带一起过。
        """
        worker = Worker(10010, Pos(12, 22), {"WallFixer": 1, "stone": 1})
        for level, hp in ((1, 400), (1, 1000), (2, 600), (2, 1500)):
            with self.subTest(level=level, hp=hp):
                cmd = plan(
                    self._turn(worker, walls=(Wall(40000, self.WEAK, hp, level),))
                ).get(str(10010), {})
                self.assertNotIn(cmd.get("action"), ("use", "remove"))

    def test_repair_beats_mining(self):
        """修墙 > 挖矿 —— 贴着弱墙又贴着矿，先用包（差事顺序，不是距离顺序）。"""
        worker = Worker(10010, Pos(12, 22), {"WallFixer": 1})
        cmd = plan(self._turn(worker, ores=(Pos(12, 20),)))[str(10010)]
        self.assertEqual(cmd["action"], "use")

    def test_two_workers_claim_different_weak_walls(self):
        """两面 L2 弱墙、两个持包工人 ⇒ 各修各的（认领账本，不挤同一面）。"""
        walls = (Wall(40000, self.WEAK, self.L2, 2), Wall(40001, self.OTHER, self.L2, 2))
        grid = _terrain(
            self.WEAPONS,
            {self.BASE: "station", self.WEAK: "wall", self.OTHER: "wall", self.SHOP: "weaponShop"},
        )
        a = Worker(10010, Pos(12, 22), {"WallFixer": 1})
        b = Worker(10012, Pos(12, 26), {"WallFixer": 1})
        grid |= {a.pos: "worker", b.pos: "worker"}
        cmds = plan(
            Turn(
                round_no=1, map=Map((41, 32), grid), roles=(a, b), gold=0,
                weapons=self.WEAPONS, shop_prices={"WallFixer": 10}, walls=walls,
            )
        )
        targets = {
            Pos(v["targetPos"][0]["x"], v["targetPos"][0]["y"]) for v in cmds.values()
        }
        self.assertEqual(targets, {self.WEAK, self.OTHER}, "各修各的，不挤同一面墙")


class WallPriorityTest(unittest.TestCase):
    """白天工人的优先级链：建武器 > 修墙 > 砌墙 > 经济线 —— 一件不行才轮到下一件。

    武器最高（口径：无论哪一天，武器没了先建）：份额有缺且钱够 ⇒ 建/走向落点；
    钱不够但筹资可行（有小贩、有价可卖）⇒ 整条墙线让位，先卖背包的货、再采最值钱的
    矿，凑够 25 金币回来建；筹资不可行 ⇒ 照旧走墙线。升级线只在武器齐了之后才跑。
    `_build_walls` 返回 `True` = 本回合指令已由墙线产出（或 gated 待命）；`False` =
    什么都没发过，兜底链接手。
    """

    BASE = Pos(10, 24)

    def setUp(self) -> None:
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
        worker = Worker(10010, Pos(11, 25))  # 贴着 0 号落点 (12,24)
        turn = self._turn(
            (worker,),
            {self.BASE: "station", Pos(4, 24): "stone", Pos(6, 24): "copper"},
            gold=75,
            prices={"stone": 1, "copper": 5},
        )
        cmd = plan(turn)[str(10010)]
        self.assertEqual(cmd["action"], "build", "先建武器，不是先采石/采铜")
        self.assertEqual(cmd["name"], "rocket")
        self.assertEqual(Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"]), Pos(12, 24))

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

    def test_fundraising_sells_the_stone_reserve_down_to_one(self):
        """筹资路上石头只留 1 块（用户口径）：建武器差的是几十金币，留够封口的量就行。

        平常的砌墙线留 `STONE_RESERVE`(3) 块 —— 两条路共用一个 `keep` 默认值的话，这位
        工人会守着 3 块石头不动，25 金币永远凑不齐（它手里别的货一件没有）。
        """
        worker = Worker(10010, Pos(7, 25), {"stone": 8})  # 贴着小贩 (7,26)
        turn = self._turn(
            (worker,),
            {self.BASE: "station", Pos(4, 24): "stone", Pos(7, 26): "vendor"},
            prices={"stone": 1},
        )
        cmd = plan(turn)[str(10010)]
        self.assertEqual(
            cmd, {"action": "sell", "name": "stone", "num": 7}, "留 1 块就够，其余全换成钱"
        )

    def test_fundraising_beats_repairing_a_weak_wall(self):
        """武器有缺 + 钱不够 + 筹资可行 ⇒ 连**修墙**也让位（用户口径：武器 > 筹资 > 墙）。

        弱墙就贴在脚边、包里还有修复包 —— 顺序反了这条会发出 `use`/`remove`，立即挂。
        """
        worker = Worker(10010, Pos(12, 22), {"WallFixer": 1, "stone": 1})
        turn = self._turn(
            (worker,),
            {self.BASE: "station", Pos(6, 24): "copper", Pos(7, 26): "vendor"},
            prices={"copper": 5},
            walls=[Pos(13, 22)],  # 那格墙照旧挡路
            weak=(Wall(40000, Pos(13, 22), 300, 2),),
        )
        cmd = plan(turn)[str(10010)]
        self.assertNotIn(cmd["action"], ("use", "remove"), f"别碰那面弱墙：{cmd}")
        self.assertEqual(cmd["action"], "move", f"该往矿那边去：{cmd}")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(Pos(6, 24)), worker.pos.dist(Pos(6, 24)), "朝铜矿走一格")

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

    def test_no_voucher_runs_while_weapons_are_pending(self):
        """升级线只在武器齐了之后才跑：份额有缺（两角色一武器 ⇒ 缺一）时，持券工人
        不去用券 —— 它的时间该花在筹资上（等级是武器齐了以后的事）。"""
        weapons = (Weapon(10020, "rocket", Pos(12, 24), 10, 0),)
        worker = Worker(10010, Pos(16, 24), {"WeaponUpgradeVoucher1": 1})
        turn = self._turn(
            (Pioneer(10011, Pos(20, 20)), worker),
            {self.BASE: "station", Pos(15, 24): "copper"},
            walls=wall_cells(self.BASE, 41),  # 环是满的：没有墙线的事
            prices={"copper": 5},
            weapons=weapons,
        )
        cmd = plan(turn)[str(10010)]
        self.assertEqual(cmd["action"], "collect", "武器有缺 ⇒ 筹资采铜，不是去用券")

    def test_no_reachable_stone_falls_to_the_economy(self):
        """环没砌完但没石矿可采 ⇒ 落经济线采铜，不是原地待命 ——
        "工人不能什么都不做"。"""
        worker = Worker(10010, Pos(5, 23))
        turn = self._turn(
            (worker,),
            {self.BASE: "station", Pos(6, 24): "copper"},
            prices={"stone": 1, "copper": 5},
        )
        cmd = plan(turn)[str(10010)]
        self.assertEqual(cmd["action"], "collect", "采不了石就去采能卖的铜")
        cell = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertEqual(cell, Pos(6, 24))

    def test_a_gated_worker_stands_by(self):
        """`gated`（砌下去会把人关在墙里）⇒ 待命：开拓者被任务钉死在盒内（它走 ① 支路，
        轮不到"先出来"那道闸），两个工人又恰好占住后方通道的两格（通道共 6 格）⇒ 盒外有墙格要砌的工人
        这一回合不发指令（口径："安全：别跑远，下回合缺口还在" —— 比落经济线更保环的速度）。
        对照：没分到墙格的另一个工人照常落经济线干活。"""
        walls = set(wall_cells(self.BASE, 41)) - {Pos(13, 24)}  # 只剩封口格
        pinned = Pioneer(10011, Pos(11, 23))   # 盒内、被任务钉死（⇒ 会被墙关住）
        door_a = Worker(10012, Pos(8, 23))     # 占住门口之一（自己出得去 ⇒ 不在 leaving）
        door_b = Worker(10013, Pos(8, 24))     # 占住门口之二、领到封口格 ⇒ gated
        turn = self._turn(
            (pinned, door_a, door_b),
            {self.BASE: "station", Pos(11, 22): "copper"},
            prices={"copper": 5},
            walls=walls,
            phase_task="题目",
        )
        cmds = plan(turn)
        self.assertNotIn(str(10013), cmds, "gated ⇒ 待命（不发指令，也不跑去采铜）")
        self.assertIn(str(10012), cmds, "没分到墙格的工人照常落经济线")

    def test_a_full_worker_does_not_hoard_the_stone_mine(self):
        """石矿认领只发生在"真要采"之后：石头已够的工人（want=0）不许占住矿格 ——
        否则缺石的同事这一回合采不到石、被挤去经济线，环白白慢一拍。
        认领发生在 `want` 计算之前的话，B（空手）会去采铜，立即挂。"""
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


if __name__ == "__main__":
    unittest.main()
