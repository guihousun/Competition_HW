"""game/planner.py 建造线的用例：建武器 / 砌墙（沿环走一圈的顺序）/ 防关人闸门 /
拆墙与临时门 / 救援 / 收工闸门。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
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
from coregeek.game.planner import HOLE_MIN_LEFT, HOLE_MIN_SAVING, HOLE_PATCH_LEFT, WALL, WEAPONS_BY_SITE, plan  # noqa: E402
from coregeek.game.roles import BaseRole, Pioneer, Worker  # noqa: E402
from coregeek.game.world import DAY_ROUNDS, ROUNDS_PER_DAY, Robot, Turn, Wall, Weapon  # noqa: E402
from coregeek.protocol import model  # noqa: E402


def _day2(within: int) -> int:
    """第 2 天白天第 `within` 回合的回合号（within=1 即 131，白天共 70 回合）。"""
    return ROUNDS_PER_DAY + within


def _left(round_no: int) -> int:
    """`round_no` 那一回合白天还剩几个回合（夜里会算出负数，本文件的用例只在白天用它）。

    与 `Turn.day_rounds_left` 同一个式子，独立写一遍：用例要卡"还剩正好 N 回合"的边界时，
    自己推回合号极易差一，索性由它来定位。
    """
    return DAY_ROUNDS - ((round_no - 1) % ROUNDS_PER_DAY + 1) + 1


class BuildWeaponTest(unittest.TestCase):
    """合成开局：白天、0 武器、基地在左半 —— 工人真的会建满三座、建在该建的地方、然后收手。

    把回合串起来跑：单帧"目标格算得对"证明不了收不收得住（`MineApproachTest` 同理）。
    结算照判题器的口径来 —— 一回合一步，建起来的武器下一回合就挡路、也占掉那个格子。
    """

    #: 三个落点 = (9,25) 后列上角 + (12,22)/(12,25) 前排两角（见 `weapon_sites`）
    BASE = Pos(10, 24)
    MINE = Pos(4, 24)

    def setUp(self) -> None:
        self.gold = 75  # 开局：恰好买满三座（25×3）
        #: 已建成的武器名册 —— 唯一真相，地形由它推（见 `_terrain`）。
        #: 直接往 `entries` 里塞一把 `"gatling"` 的话，`turn.weapons` 是空的、
        #: 名额永远算成"还差三座"，而那一格又挡路 —— 夹具与真实局面不同形。
        self.weapons: list[Weapon] = []
        self.walls: dict[Pos, str] = {}
        #: 静态地形（基地 + 矿），删掉基地就等于"基地没了"那个降级局面
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
        """三座各自落在自己的落点上：火箭进后列、加特林与电磁炮分居两个前角。

        断言用集合而不是建成的先后 —— 两个工人谁先走到哪一格是路径决定的，
        那不是这条用例要钉的东西；要钉的是"种类 ↔ 落点"这层绑定。
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
        """只有 25 金：只建一座（前排排第一的火箭），金花光就停手。

        金币按递减预算扣。写成 `gold >= 25 * 待建数`（25 < 75 ⇒ 一座都不建）就全错了。
        跑满到建成 1 座为止 —— 前排落点离工人出生位远，单回合够不着。
        """
        self.gold = 25
        self._settle(want=1)
        self.assertEqual(len(self._weapons()), 1)
        self.assertEqual(self._weapons()[0], "rocket")
        self.assertEqual(self.gold, 0, "25 金正好建一座，花光才停")

    def test_a_blocked_site_drops_only_its_own_weapon(self):
        """前排第一格（第一座火箭）被占了 ⇒ 只有那一座不建，另两座照落在各自的位置上。

        这条钉的是 `_slots` 里"落点与种类绑死、只滤 `blocked`"的顺序。反过来写
        （先滤空再 `zip`）的话，"少一座"会让 `zip` 整体前移 —— 第二座火箭落到第一座的
        落点、加特林落到第二座火箭的落点，每个种类都挪到了别人家的落点上，而报文
        完全合法、本地全绿。这里挡住它的是墙，走 `blocked` 那一道 —— 顺带守住
        "覆盖会把原武器打成 level1"（§4.5.1 补充说明）。
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
        """完整的 20 格围墙环（实际只砌其中 18 格，门那 2 格不砌）—— 两条用例的对照物。"""
        return {
            Pos(x, y)
            for x in range(base.x - 2, base.x + 4)
            for y in range(base.y - 3, base.y + 3)
            if x in (base.x - 2, base.x + 3) or y in (base.y - 3, base.y + 2)
        }

    def test_ring_is_eighteen_cells_free_of_the_base_and_the_weapons(self):
        cells = wall_cells(self.BASE, 41)
        self.assertEqual(len(cells), 18, "6×6 边框 20 格减去背面中间那 2 格（门）")
        self.assertEqual(len(set(cells)), 18, "不该有重复格")
        self.assertEqual(set(cells) & base_cells(self.BASE), set(), "不能落在基地身上")
        self.assertEqual(set(cells) & set(weapon_cells(self.BASE)), set(), "不能占武器环")

    def test_only_the_two_middle_back_cells_are_left_open(self):
        """背面只留中间 2 格当门，其余 4 格照砌。

        环一旦闭合工人就进出不得 —— 既采不了矿，也回不到环内操炮，所以门必须留。
        收窄的理由：入口从 6 路并行变 2 路串行（机器人只能挤在同一处进，
        火箭溅射与加特林双弹的价值都翻倍），同时"堵门"从 6 格降到 2 格。
        """
        cells = set(wall_cells(self.BASE, 41))
        door = {Pos(8, 23), Pos(8, 24)}  # 基地在左半 ⇒ 背面是 x = bx-2，正中那 2 格
        self.assertEqual(cells & door, set(), "门那 2 格一格都不砌")
        ring20 = self._ring20(self.BASE)
        self.assertEqual(len(ring20), 20)
        self.assertEqual(ring20 - cells, door, "少掉的正好是门，不是别的")

    def test_the_front_column_comes_first_and_the_seal_comes_last(self):
        """前 5 格 = 迎着机器人那一列（不含封口格），封口格 `(13,24)` 排在最末。

        策略指导：「墙建立在面向机器人进攻的方向（保护基地）」。判反了墙就砌在机器人
        不来的一侧 —— 不报错、不违规，只是整段白砌，而且石头是工人一块块背回来的。
        封口格排最末就是"白天开门通行、天黑前砌上封死"的落地方式（`_build_walls`
        取的就是 `free[0]`，排最后 ⇒ 最后一格才砌它）。
        """
        ring = wall_cells(self.BASE, 41)
        self.assertEqual(
            ring[:5],
            (Pos(13, 21), Pos(13, 22), Pos(13, 23), Pos(13, 25), Pos(13, 26)),
            "左半 ⇒ 正面是 bx+3，从一端扫到另一端（跳过封口格）",
        )
        self.assertEqual({c.x for c in ring[:5]}, {13}, "前 5 格全在正面那一列")
        self.assertEqual(ring[-1], Pos(13, 24), "封口格必须排最后 —— 白天最后才砌它")
        self.assertEqual(
            set(ring[:5]) | {ring[-1]},
            {Pos(13, y) for y in range(21, 27)},
            "正面 6 格一个不少（封口格在末尾）",
        )

    def test_the_order_walks_the_ring_in_one_sweep(self):
        """顺序 = 沿环走一圈（起点在正面列的一端、终点紧挨封口格）。

        这不是好看，是回合预算（工人一天只有 70 回合）：按"正面列 → 顶行 → 底行 → 背面"
        分段走，横穿盒子再折回来的来回收回的就是砌墙的回合，封口格会当天砌不上；
        沿环一圈 18 格全砌得上。

        允许的 3 处"不挨着"各有理由：跳过封口格那一步（它排在最后）、穿过背面那道门、
        从另一侧行末尾走到正面列正中的封口格。跳得比这更多 ⇒ 又开始绕远路。
        """
        ring = wall_cells(self.BASE, 41)
        jumps = [(prev, nxt) for prev, nxt in zip(ring, ring[1:]) if prev.dist(nxt) > 1]
        self.assertEqual(len(set(ring)), 18, "每一格只走一次")
        self.assertEqual(jumps, [
            (Pos(13, 23), Pos(13, 25)),
            (Pos(8, 25), Pos(8, 22)),
            (Pos(12, 21), Pos(13, 24)),
        ], "只该有这 3 处跳步")

    def test_the_ring_mirrors_for_a_right_half_base(self):
        """基地在右半 ⇒ 正面是 `bx-2`、背面是 `bx+3`（换边后自动跟着翻）。

        按基地坐标判而不用 `teamOur.type` —— 下半场换边后队伍身份不变、基地会挪。
        """
        ring = wall_cells(Pos(30, 10), 41)
        self.assertEqual(len(ring), 18)
        self.assertEqual({c.x for c in ring[:5]}, {28}, "右半 ⇒ 正面是 bx-2")
        self.assertEqual(ring[-1], Pos(28, 10), "封口格 = 正面列正中（bx-2, by）")
        self.assertEqual({c.x for c in ring}, {28, 29, 30, 31, 32, 33}, "侧面两列都在（各缺中间 2 格）")
        self.assertEqual(
            ring[10:13], (Pos(33, 11), Pos(33, 8), Pos(33, 7)), "背面自上而下：先收门、再落角"
        )

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

    def test_the_door_itself_never_holds_a_building(self):
        """门那 2 格里没有任何建筑 —— 基地 / 武器 / 墙都不在。

        闸门"会不会把人关住"靠的就是这一条：门那 2 格空着 ⇒ 只有单位能堵门
        （2 格门 = 2 个单位就能堵满）。`wall_cells` 的背面列与 `weapon_sites` 的后列
        是两个不同的变量（`near - 2d` vs `near - d`），钉成断言，别靠脑补。
        """
        for base in (Pos(10, 24), Pos(30, 10)):
            with self.subTest(base=base):
                door_x = min(c.x for c in box_cells(base))
                if base.x * 2 >= 41:
                    door_x = max(c.x for c in box_cells(base))  # 右半场镜像：门在最外那一列
                door = {Pos(door_x, base.y - 1), Pos(door_x, base.y)}  # 背面列正中那 2 格
                self.assertEqual(len(door), 2, "门是背面列正中那 2 格")
                built = set(wall_cells(base, 41)) | set(weapon_sites(base, 41)) | base_cells(base)
                self.assertEqual(built & door, set(), "门里不该有基地 / 武器 / 墙")


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
        """把白天串起来跑到砌满：18 格全砌上，且顺序与 `wall_cells` 逐格一致。

        这一条把"回合预算 → 采矿 → 砌墙"整条线钉在一起：预算算大了天黑砌不完，
        算小了石头不够、工人在工地干等；顺序错了则会先把背面砌满、正面空着。
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
        self.assertEqual(stone, 0, "别多采 —— 白天总共就 18 格墙可砌")

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

    def test_a_finished_ring_stops_the_stone_mining(self):
        """18 格都砌满了 ⇒ 不再采石头（多采的只会压在背包里）。

        这一支会转去采最值钱的矿（`SpareOreTest`），而本夹具没有价目表
        （`vendor_prices` 缺省为空）⇒ 挑不出"最值钱的矿" ⇒ 一条都不发。这是有意的降级方向：
        没有价格就无从挑，宁可不动，与 `_gold` / `_size` 同源。
        """
        self.entries.update({c: WALL for c in wall_cells(self.BASE, 41)})
        self.assertEqual(plan(self._turn(stone=0)), {})

    def test_no_base_means_no_wall_and_no_mining(self):
        """基地没了就没有围墙环（坐标全由它推）⇒ 连矿都不去采，而不是瞎找一个坐标。"""
        del self.entries[self.BASE]
        self.assertEqual(plan(self._turn(stone=0)), {})


class WallGateTest(unittest.TestCase):
    """建墙原则：不能把工人关起来。闸门两半，各测各的。

    - `_ring` 里"会关人就一格都不砌"（判据 = 砌满这一圈墙之后谁出不去）；
    - `plan` 里"要被关住的人先走出来"（仅白天）。

    这个局面得手工搭：36 格的盒子里不可能有矿（任务书 L78 说矿区不生成在可建造区域内），
    门那 2 格里也没有任何建筑（后列炮位在武器环的后列，不在那一列）⇒
    现实里只有单位能把门堵满，2 个单位就够把闸门合上。不摆机器人的话，
    有几条用例连坏的实现都放不过去（夹具与真实路径不同形）。
    """

    BASE = Pos(10, 24)
    WEAPONS = _records({Pos(9, 23): "gatling", Pos(9, 24): "railgun", Pos(9, 22): "rocket"})
    #: 门 = 背面列正中的 2 格（基地在左半 ⇒ 背面是 x = bx-2）。`wall_cells` 一格都不砌它。
    DOOR = {Pos(8, 23), Pos(8, 24)}
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
    ) -> Turn:
        """`at1` / `at2` = 两个工人的站位（默认 1 号在盒内、2 号在盒外且手里有一块石头）。

        站位是可以换到盒外的 —— "盒外的人不许否决这一圈墙"那条就得两个都在外面才测得出。
        """
        workers = (
            Worker(1, at1 or self.INSIDE, {}),
            Worker(2, at2 or self.OUTSIDE, {"stone": 1}),
        )
        robots_on_map = {c: "robot" for c in (self.DOOR if door_blocked else ())}
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
        """18 格全砌满、门开着 ⇒ 盒里的人照样走得出去。这就是"正面砌满也关不住人"。

        不能拿 `step_toward` 顶替：它的契约是"贴着 goal 即到"（`dist(goal) <= 1 ⇒ None`），
        用来测"出不出得去"时，一个贴着门口、而门那格被堵住的角色会被判成"到不了"。
        """
        turn = self._turn(walls=wall_cells(self.BASE, 41))
        self.assertNotIn(Pos(8, 24), turn.map.blocked, "门那一列连砌满之后也不该有东西")

        step = step_outside(self.INSIDE, box_cells(self.BASE), turn.map.blocked, turn.map.size)
        self.assertIsNotNone(step, "18 格砌满 + 门开着 ⇒ 出得去")
        self.assertEqual(self.INSIDE.dist(step), 1, "返回的是从 pos 迈出的第一步")

    def test_a_blocked_door_holds_the_walls_back(self):
        """门被堵满 ⇒ 盒外那个工人手里攥着石头也不许砌（砌下去就把 1 号关死了）。

        少砌这一回合的代价是墙晚砌完；砌下去的代价是把人关死在盒里。
        """
        turn = self._turn(door_blocked=True)
        cmds = plan(turn)
        self.assertNotIn(
            "2", cmds, "盒外那个工人该原地不动（没矿可采、价目表也空着），而不是去砌墙"
        )
        # 顺带钉住"整面墙"这个判据：门一堵，18 格一格都不该砌
        self.assertNotIn("build", {c["action"] for c in cmds.values()})

    def test_a_blocked_door_sends_the_worker_out_first(self):
        """同上门被堵，但墙上还一个缺口都没砌 ⇒ 被围的人趁缺口先出去。

        闸门两半的关键差别在两套障碍：`leaving` 按"假设墙砌满"判（砌完就真出不去了），
        而这迈出去的一步按"现在"的障碍算（缺口就是出路）。
        若这里也按砌满算，`step_outside` 必然返回 None ⇒ 这一支永远空转、白写。
        """
        cmds = plan(self._turn(door_blocked=True))
        self.assertEqual(set(cmds), {"1"}, "要被关住的人这一回合必须动，盒外那个该原地待命")
        self.assertEqual(cmds["1"]["action"], "move")
        cell = Pos(cmds["1"]["targetPos"][0]["x"], cmds["1"]["targetPos"][0]["y"])
        self.assertEqual(self.INSIDE.dist(cell), 1, "一步一格，不能瞬移")
        # 门那 2 格全堵着 ⇒ 出路只能是没砌的墙那几条边（正面 x = bx+3 最直接）
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

        这条钉的坑：`step_outside` 对"本来就在外面"与"走不出去"都返回 `None`
        （它的契约），所以筛"谁在盒子里"时漏掉 `r.pos in box`，就会把所有在盒外
        干活的人判成要被关住 ⇒ 闸门永远合上、墙一格都砌不上。症状极安静：
        不报错、不违规，只是工人站在工地上一动不动。
        2 号工人（后处理的那个）分到的是后段首格 —— 站位贴那一格
        （意图：有石头、贴着自己被分到的目标 ⇒ 该砌）。
        """
        gaps = wall_cells(self.BASE, 41)
        target = gaps[(len(gaps) + 1) // 2]
        occupied = (
            set(base_cells(self.BASE)) | {w.pos for w in self.WEAPONS} | self.DOOR | {Pos(5, 24)}
        )
        spot = next(
            Pos(target.x + d.x, target.y + d.y)
            for d in STEPS
            if 0 <= target.x + d.x < 41 and 0 <= target.y + d.y < 32
            and Pos(target.x + d.x, target.y + d.y) not in occupied
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

        漏了它，两条 `move` 会指向同一格，正好撞上任务书 §4.5.4 的"目标点争夺"——
        两个人都不动，而日志上只是"这回合没动"，看不出是撞车。

        站位是挑过的：`(9,25)` 与 `(10,25)` 按现在的障碍算，第一步都指向 `(9,26)`
        （穷举盒内空地找出来的唯一一对可行站位）。随手摆两个人在盒内，两条指令
        本来就不会撞 —— 那种用例测不出这个坑。
        """
        cmds = plan(self._turn(door_blocked=True, at1=Pos(9, 25), at2=Pos(10, 25)))
        self.assertEqual(len(cmds), 2, f"两个人的处境一样，都该往外走：{cmds}")
        self.assertEqual({c["action"] for c in cmds.values()}, {"move"})
        cells = {Pos(c["targetPos"][0]["x"], c["targetPos"][0]["y"]) for c in cmds.values()}
        self.assertEqual(len(cells), 2, "落点必须分开")

    def test_a_gated_round_never_sends_a_worker_mining_far_away(self):
        """闸门挡住的那一回合是"墙还没砌完"，不是"砌完了" ⇒ 盒外那个什么都不做。

        两种"没得砌"混在一起的话，它会掉头奔向地图另一头（几十回合回不来、还得再走回来），
        或者干脆就地砌一格 —— 而那一格砌下去，正好把这一回合往外走的 1 号关在墙里。
        局面：门被 2 个机器人堵满（1 号出不去了，只能趁缺口走一格）+ 环上一格都没砌
        + 盒外那个手里有石头 + 地图另一头有一座贵的铜矿。
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
        """自己人站的那格也算障碍：门那 2 格，一格被机器人堵着、另一格站着同事 ⇒ 谁都砌不了。

        与 `_ring` 里"自己人算路过"故意相反。那处问的是"这一格要不要砌"（把路过的人排掉，
        否则两个工人对着改目标来回踱步）；这里问的是"会不会有人出不来"——
        同事此刻真的堵在门那一列上，把他当路过而放行，砌下去就把里面那个封死了。
        宁可用一个回合的工期换这条不可逆的错。

        光有同事挡路测不出这条：他站在待砌的墙格上时，那格本来就是"假设砌满"里的墙，
        两种口径下都是障碍。必须是门那 2 格（永远不砌的）才算数 —— 所以让 2 号站到门的
        一格上：`door_blocked` 铺的机器人里，那一格被角色层覆盖（`_turn` 里角色铺在最后，
        与 `model._entries` 一致）⇒ 门上 1 个机器人 + 我们自己人 1 个。
        """
        turn = self._turn(door_blocked=True, at1=Pos(12, 24), at2=Pos(8, 24))
        self.assertEqual(self.DOOR - turn.map.blocked, set(), "门那 2 格都该挡着")
        cmds = plan(turn)
        self.assertEqual(set(cmds), {"1"}, f"盒内那个趁缺口走，站门上的那个不许砌：{cmds}")
        self.assertEqual(cmds["1"]["action"], "move")


class DayEndGateTest(unittest.TestCase):
    """白天末尾回炮位：离天黑只剩回程步数时就往回走。

    症状是"晚上机器人已经到跟前了，各个角色还没走到武器旁边"。根因是预算按切比雪夫
    直线算，而环砌满之后回炮位必须绕到背面那道门、再横穿整个盒子：直线 5 步的路
    真实是十几步（`StepsBetweenTest` 钉的就是这一条），所以回程步数走 BFS。

    这一支只许发 `move`，是全类最重要的断言。`attack` 仅黑夜可用（§4.4），
    白天发一条就是一次异常，累计 5 次整场不再被调度 ⇒ 复用 `_defend` 是这里最容易
    犯的错（它贴近炮位会调 `_fire`），所以专门有一条扫白天各回合、各站位的守门员。
    """

    BASE = Pos(10, 24)
    #: 与 `weapon_sites` 同序：前排上方两格火箭 + 前排下方加特林（这里只要"有三座炮"就够）
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
        """从 `at` 走到最近一座炮位的真实步数（与 `planner` 同一个口径）。"""
        walk = frozenset(wall_cells(self.BASE, 41))
        return min(steps_between(at, p, walk, self.SIZE) for p in weapon_sites(self.BASE, 41))

    def test_the_gate_opens_exactly_when_the_walk_home_eats_the_day(self):
        """回程步数 ≥ 白天剩余 − 1 ⇒ 这一回合就往炮位走。卡在边界上测（早一回合不动身）。"""
        at = Pos(20, 24)
        steps = self._steps_home(at)
        self.assertGreater(steps, 0, "这个站位本来就该离炮位远一点")
        # `day_rounds_left - 1 == steps` ⇒ 正好是闸门该合上的那一回合
        cmds = plan(self._turn(round_no=DAY_ROUNDS - steps, at=at))
        self.assertEqual(cmds["1"]["action"], "move", f"该往回赶：{cmds}")
        cell = Pos(cmds["1"]["targetPos"][0]["x"], cmds["1"]["targetPos"][0]["y"])
        self.assertEqual(at.dist(cell), 1, "一步一格")
        self.assertLess(
            self._steps_home(cell), steps, "这一格必须真的离家更近（不许在原地打转）"
        )
        # 白天还富裕一回合 ⇒ 不许动身（否则整个白天都在炮位上干等）
        self.assertEqual(plan(self._turn(round_no=DAY_ROUNDS - steps - 1, at=at)), {})

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


class DigTest(unittest.TestCase):
    """绕路 ≥5 就拆墙（开洞能省下的步数 ≥ `HOLE_MIN_SAVING` 才拆）。

    `_dig` 钩在 `_step` 里 —— 那是所有差事（采矿 / 卖矿 / 顺路卖 / 升级 / 砌墙 / 回炮位）
    走路的公共出口，一处覆盖全部调用点。本类的夹具一律走真差事
    （环砌满 ⇒ 只剩盒子外那唯一一座矿可去），不直接调私有函数。

    夹具必须给价目表：砌满环之后工人靠 `_mine_spare_ore` 才动得起来，而它按收购价挑矿
    —— 价目为空 ⇒ 挑不出来 ⇒ 工人原地待命 ⇒ 压根走不到 `_step`，用例就成了空转。
    每一种"不命中"都要有独立的一条：这道门有六重（第 1 天 / 夜里 / 环没砌满 / 没石头 /
    窗口关了 / 不贴墙），任何一重写漏，症状都是"墙被反复拆、石头白花"，
    长期串跑的那条在 `HoleLifecycleTest`。
    """

    BASE = Pos(10, 24)
    SIZE = (41, 32)
    #: 三座炮先摆好，否则金币会先把工人抽去建武器（那是 `BuildWeaponTest` 的事）
    WEAPONS = _records({Pos(9, 25): "rocket", Pos(12, 22): "gatling", Pos(12, 25): "railgun"})
    #: 价目表（见类 docstring：没有它工人不动）
    PRICES = {"stone": 1, "iron": 3, "copper": 5}

    def _turn(
        self,
        *,
        round_no: int = ROUNDS_PER_DAY + 1,
        at: Pos = Pos(12, 24),
        mine: Pos | None = Pos(18, 24),
        stone: int = 1,
        missing: tuple[Pos, ...] = (),
    ) -> Turn:
        """默认：第 2 天白天第一回合、环砌满、工人贴着东面那 3 格墙、手里 1 块石头。

        `round_no = ROUNDS_PER_DAY + 1` ⇒ 当天第 1 回合 ⇒ 白天还剩 70 回合（窗口全开）。
        """
        layers = [
            {c: WALL for c in wall_cells(self.BASE, 41) if c not in missing},
            {self.BASE: "station"},
            {at: "worker"},
        ]
        if mine is not None:
            layers.append({mine: "stone"})
        worker = Worker(1, at, {"stone": stone} if stone else {})
        return Turn(
            round_no=round_no,
            map=Map(self.SIZE, _terrain(self.WEAPONS, *layers)),
            roles=(worker,),
            gold=0,
            weapons=self.WEAPONS,
            vendor_prices=self.PRICES,
        )

    @staticmethod
    def _hole(turn: Turn) -> Pos | None:
        """这一回合拆的那一格；别的动作一律算"没拆"（`None`）。"""
        cmd = plan(turn).get("1") or {}
        if cmd.get("action") != "remove":
            return None
        point = cmd["targetPos"][0]
        return Pos(point["x"], point["y"])

    def test_walking_all_the_way_around_is_worth_a_hole(self):
        """贴着墙、去盒子外那唯一一座矿：绕 16 步 vs 开洞后 5 步 ⇒ 拆。

        省下 11 步，远超门槛 `HOLE_MIN_SAVING`。墙砌满之后盒子只有背面那 2 格门，
        要往东就得先往西绕出去 —— 这正是"多绕 5 格以上"的典型场面。
        """
        at, goal, hole = Pos(12, 24), Pos(18, 24), Pos(13, 23)
        turn = self._turn()
        blocked = turn.map.blocked
        now = steps_between(at, goal, blocked, self.SIZE)
        after = steps_between(at, goal, blocked - {hole}, self.SIZE)
        self.assertEqual((now, after), (16, 5), "先钉住两边的真实步数（不是切比雪夫那 6 步）")
        self.assertGreaterEqual(now - after, HOLE_MIN_SAVING, "省的步数必须过门槛")
        self.assertEqual(self._hole(turn), hole)

    def test_the_hole_is_the_cell_that_leaves_the_fewest_steps(self):
        """逐格试算 ⇒ 挑开洞后步数最少的那一格，并列时取坐标序（可复现）。

        夹具特意挑成三个候选收益各不相同（6 / 7 / 8 步）的样子 —— 三格一样的话，
        这条用例就分不出"挑最省的"与"挑第一个"。
        """
        at, mine = Pos(11, 25), Pos(18, 26)
        turn = self._turn(at=at, mine=mine)
        blocked = turn.map.blocked
        table = {
            c: steps_between(at, mine, blocked - {c}, self.SIZE)
            for c in wall_cells(self.BASE, 41)
            if at.dist(c) <= 1
        }
        self.assertEqual(sorted(table.values()), [6, 7, 8], "夹具必须让候选格彼此不同")
        self.assertEqual(self._hole(turn), min(table, key=lambda c: (table[c], c)))
        self.assertEqual(self._hole(turn), Pos(12, 26))

    def test_a_short_detour_is_not_worth_a_hole(self):
        """绕路不到 5 步 ⇒ 不拆。

        矿在西边 `(4,24)`：背面那道门本来就在西侧，走它一点也不绕（7 步 vs 7 步）。
        拆一格是净支出（1 回合 + 1 块不回收的石头，任务书 L209），不值得。
        """
        at, goal = Pos(12, 24), Pos(4, 24)
        blocked = self._turn(mine=goal).map.blocked
        savings = [
            steps_between(at, goal, blocked, self.SIZE)
            - steps_between(at, goal, blocked - {c}, self.SIZE)
            for c in wall_cells(self.BASE, 41)
            if at.dist(c) <= 1
        ]
        self.assertLess(max(savings), HOLE_MIN_SAVING, "夹具前提：每个候选省的步数都不过门槛")
        self.assertIsNone(self._hole(self._turn(mine=goal)))

    def test_the_first_day_never_digs(self):
        """第 1 天不拆 —— 整套机制成立的前提。

        环上"孤零零一个缺口"这一个状态，既是"刚挖的洞"也是"还差一格没砌"，地图上
        逐字节同形，任何无状态判据都分不开。区分只能靠时间：第 1 天环在建、缺口是真的没砌；
        第 2 天起环在开局时必然是满的，于是环上一切缺口只可能来自我们自己的 `remove`。
        这一条写漏的后果：第 1 天砌到只剩中段某一格时，那一格会被当成"洞"，
        白天再也不补（`test_a_worker_on_the_last_cell_still_finishes_the_ring` 会跟着挂）。
        """
        turn = self._turn(round_no=1)
        self.assertEqual(turn.round_no, 1, "第 1 天白天第一回合")
        self.assertIsNone(self._hole(turn))

    def test_the_night_never_digs(self):
        """夜里不拆（仅白天）—— 与 `build` 同一条：闸门不管昼夜，由 `planner` 把关。

        任务书 `remove` 那一格没写昼夜（`build` 写了"仅白天"），按保守口径执行：
        夜里不发最多少拆一次；反过来若实际禁夜，发出去就是一次指令非法 —— 红线优先。
        """
        self.assertIsNone(self._hole(self._turn(round_no=85)))

    def test_a_worker_without_a_spare_stone_never_digs(self):
        """没石头不拆：拆掉的那块不回收，手里没石头就补不回来 —— 墙上留着洞过夜。

        对照是同一天同一站位、手里有石头那一条（拆）。
        """
        self.assertIsNone(self._hole(self._turn(stone=0)))
        self.assertIsNotNone(self._hole(self._turn(stone=1)), "对照：有石头就拆")

    def test_an_unfinished_ring_is_never_dug(self):
        """环没砌满 ⇒ 不拆（缺口本来就能当通道用，而"没砌到"与"挖出来的"分不开）。

        夹具把缺口放在西侧 `(8,25)`：它帮不了东边那趟路的忙，所以"省不到 5 步"那道门
        拦不住这一条 —— 拦住的只能是 `_ring`。对照：同一局面环砌满就拆。
        """
        self.assertIsNone(self._hole(self._turn(missing=(Pos(8, 25),))))
        self.assertEqual(self._hole(self._turn()), Pos(13, 23), "对照：环砌满就拆")

    def test_the_window_closes_thirty_rounds_before_night(self):
        """白天剩 ≤ `HOLE_MIN_LEFT`(30) 回合就不再开洞（洞要开得够久才回本）。

        这个窗口与补墙窗口（剩 ≤ `HOLE_PATCH_LEFT`=15）不相交 ⇒ 一天最多拆一次。
        防"拆了补、补了拆"净亏的真正机制是这个，不是门槛 5（门槛只管单趟划不划算）。
        夹具用近矿 `(14,24)`：远矿在这个时点早就被"回得来"滤掉了（`_mine_spare_ore`），
        那样就分不出到底是哪道门拦的。
        """
        mine = Pos(14, 24)
        # 白天 70 回合 ⇒ 一定有"还剩正好 HOLE_MIN_LEFT 回合"的那一回合，由 `_left` 定位
        boundary = next(r for r in range(_day2(1), _day2(DAY_ROUNDS) + 1) if _left(r) == HOLE_MIN_LEFT)
        self.assertEqual(_left(boundary - 1), HOLE_MIN_LEFT + 1, "夹具前提：差一回合就是边界")
        self.assertEqual(
            self._hole(self._turn(round_no=boundary - 1, mine=mine)),
            Pos(13, 23),
            "多剩一回合还能拆",
        )
        self.assertIsNone(self._hole(self._turn(round_no=boundary, mine=mine)), "正好卡在门槛上就不拆")

    def test_a_worker_away_from_the_wall_never_digs(self):
        """候选只取当前已经贴着的墙格 —— 人不在墙边就没得拆。

        `remove` 要求"指定与自身距离一格内的围墙"（§4.4），与 `build` 同一条站位契约，
        所以"走到最优的那一格再拆"是被刻意排除的（那会引入"在路上"的中间态，目标每回合
        重算 ⇒ 来回抖，而且省下的步数没扣掉走过去的回合 ⇒ 系统性高估收益）。
        这一条只能用盒外的站位测：环是 6×6 的边框，而距离是切比雪夫（含斜角）
        ⇒ 盒子里每一个能站人的格子都贴着墙。
        """
        at = Pos(16, 24)
        self.assertEqual(
            [c for c in wall_cells(self.BASE, 41) if at.dist(c) <= 1], [], "夹具前提：不在墙边"
        )
        self.assertIsNone(self._hole(self._turn(at=at, mine=Pos(4, 24))))

    def test_a_digging_round_never_fires(self):
        """红线守门员：拆墙只发生在白天，那一回合一条 `attack` 都不许有。

        `attack` 仅黑夜可用（§4.4），白天发一条就是一次异常 —— 5 次整场不再被调度。
        `_step` 是"走路"的公共出口，夜里回炮位也走它 ⇒ 拆墙的门必须自己把昼夜判死。
        """
        for round_no in (1, 85, ROUNDS_PER_DAY + 1, ROUNDS_PER_DAY + 40, ROUNDS_PER_DAY + 41, 200):
            with self.subTest(round_no=round_no):
                cmds = plan(self._turn(round_no=round_no, mine=Pos(14, 24)))
                self.assertNotIn(
                    "attack", {c["action"] for c in cmds.values()}, f"第 {round_no} 回合：{cmds}"
                )


class DoorTest(unittest.TestCase):
    """环上那个缺口 = 白天的临时门：白天中段不补、天黑前补回去。

    这与封口格 `(13,24)` 是同一个语义（"白天开着通行、天黑前砌上封死"），只是第 2 天起
    开口不再靠建造顺序（封口格排在 `wall_cells` 最后）表达，而靠时间窗表达：
    `_door_open` 一开，`_build_walls` 把 `free` 清空 ⇒ 工人自然掉进"卖 → 升级 → 采闲矿"
    那一支 —— 不需要单加一个"别补墙"的分支。
    """

    BASE = Pos(10, 24)
    SIZE = (41, 32)
    WEAPONS = _records({Pos(9, 25): "rocket", Pos(12, 22): "gatling", Pos(12, 25): "railgun"})
    PRICES = {"stone": 1, "iron": 3, "copper": 5}
    #: 白天开口的位置 = 封口格（`wall_cells` 的最后一格，正面列正中）
    SEAL = wall_cells(Pos(10, 24), 41)[-1]

    def _turn(
        self,
        *,
        round_no: int = ROUNDS_PER_DAY + 1,
        stone: int = 1,
        at: Pos = Pos(12, 24),
        missing: tuple[Pos, ...] = (SEAL,),
    ) -> Turn:
        layers = [
            {c: WALL for c in wall_cells(self.BASE, 41) if c not in missing},
            {self.BASE: "station"},
            {at: "worker"},
            {Pos(4, 24): "stone"},  # 环外西侧那座矿：环满了也够得着，用来证明"工人去干别的了"
        ]
        worker = Worker(1, at, {"stone": stone} if stone else {})
        return Turn(
            round_no=round_no,
            map=Map(self.SIZE, _terrain(self.WEAPONS, *layers)),
            roles=(worker,),
            gold=0,
            weapons=self.WEAPONS,
            vendor_prices=self.PRICES,
        )

    def test_the_hole_stays_open_through_the_day(self):
        """白天中段（还剩 70 / 17 回合）⇒ 不补，工人掉头去采那座矿。

        最后那一回合仍在补墙窗口之外（剩 17 > `HOLE_PATCH_LEFT`）—— 差一回合的对照在
        `test_the_last_fifteen_rounds_patch_it_back` 里。
        """
        for round_no in (_day2(1), _day2(DAY_ROUNDS - HOLE_PATCH_LEFT - 1)):
            with self.subTest(round_no=round_no):
                self.assertGreater(_left(round_no), HOLE_PATCH_LEFT, "夹具前提：在窗外")
                cmds = plan(self._turn(round_no=round_no))
                acts = {c["action"] for c in cmds.values()}
                self.assertNotIn("build", acts, f"白天中段不许补墙：{cmds}")
                self.assertIn("move", acts, f"该去干别的（采矿）：{cmds}")

    def test_the_last_fifteen_rounds_patch_it_back(self):
        """白天剩 ≤ `HOLE_PATCH_LEFT`(15) 回合 ⇒ 补回去，落点正好是那格洞。

        "白天开着通行、天黑前封死"就靠这两个窗口表达；这一条守着后半句 ——
        补不回去的话，墙上就是一个整夜的洞（机器人从正面长驱直入）。
        """
        first_patch = next(r for r in range(_day2(1), _day2(DAY_ROUNDS) + 1) if _left(r) <= HOLE_PATCH_LEFT)
        for round_no in (first_patch, _day2(DAY_ROUNDS)):
            with self.subTest(round_no=round_no):
                cmd = plan(self._turn(round_no=round_no))["1"]
                self.assertEqual(cmd["action"], "build")
                self.assertEqual(cmd["name"], WALL)
                self.assertEqual(
                    Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"]), self.SEAL
                )

    def test_a_stone_short_worker_cannot_patch(self):
        """补墙同样要 1 块石头 —— 没石头时那格只能空着过夜（空指令合法，不是崩）。

        这也是 `_dig` 为什么要求手里先有一块石头（见 `DigTest`）：补不回来就别开洞。
        """
        self.assertEqual(plan(self._turn(round_no=ROUNDS_PER_DAY + DAY_ROUNDS, stone=0)), {})

    def test_the_first_day_has_no_door_to_keep_open(self):
        """第 1 天没有"洞"这回事 —— 那格只是还没砌到（封口格排在建墙顺序最后）⇒ 照砌。

        首日豁免是整套机制的前提（见 `DigTest.test_the_first_day_never_digs`）。
        """
        cmd = plan(self._turn(round_no=1))["1"]
        self.assertEqual(cmd["action"], "build")
        self.assertEqual(Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"]), self.SEAL)


class RescueTest(unittest.TestCase):
    """有人被关在盒子里 ⇒ 工人去拆一格放人。

    与 `plan` 里的防关人闸门是预防 vs 补救的关系：闸门管"还没砌完时别把谁关进去"，
    这一支管"已经被关住了怎么办" —— 背面那 2 格门被机器人堵死，是闸门拦不住的。

    只有"被关住的不是工人"才走得到这里：工人自己出不来时，朝差事目标的步数是 -1，
    `_dig` 那一支已经在它的 `_step` 里接住了；被任务钉死的开拓者更是必须靠别人救
    —— 它走 `plan` 里最早那一支 `continue`，永远到不了这一段（`TaskHoldTest` 守着那条）。
    """

    BASE = Pos(10, 24)
    SIZE = (41, 32)
    WEAPONS = _records({Pos(9, 25): "rocket", Pos(12, 22): "gatling", Pos(12, 25): "railgun"})
    PRICES = {"stone": 1, "iron": 3, "copper": 5}
    #: 背面那 2 格门（唯一进出口）
    DOOR = door_cells(Pos(10, 24), 41)

    def _turn(
        self,
        *,
        round_no: int = ROUNDS_PER_DAY + 1,
        rescuer: Pos = Pos(7, 24),
        boxed: Pos = Pos(12, 24),
        stone: int = 1,
        robots: bool = True,
        colleagues: tuple[Pos, ...] = (),
        phase_task: str = "把石头运回基地",
    ) -> Turn:
        """默认：门被两台机器人堵死、开拓者被关在盒子里、工人站在门外 `(7,24)`。"""
        roles: list[BaseRole] = [Worker(1, rescuer, {"stone": stone} if stone else {}), Pioneer(2, boxed, {})]
        obstacles: dict[Pos, str] = {}
        if robots:
            obstacles[self.DOOR[0]] = "robot:small"
            obstacles[self.DOOR[1]] = "robot:small"
        for i, colleague in enumerate(colleagues):
            obstacles[colleague] = "worker"
            roles.append(Worker(3 + i, colleague, {}))
        return Turn(
            round_no=round_no,
            map=Map(
                self.SIZE,
                _terrain(
                    self.WEAPONS,
                    {c: WALL for c in wall_cells(self.BASE, 41)},
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
        """门被机器人堵死 + 开拓者在盒内 ⇒ 工人拆一格放人，这一格是 `(8,25)`。

        `(8,25)` 是门外那位工人唯一贴着的墙格（`remove` 的站位契约）：拆了它，
        盒内的人就能从 `(7,25)` 一带迈出去。开拓者这一回合照旧只有 `submitAnswer`
        —— 它被钉在任务上，谁也挪不动它。
        """
        cmds = plan(self._turn())
        self.assertEqual(self._holes(cmds), {"1": Pos(8, 25)})
        self.assertEqual(cmds["2"]["action"], "submitAnswer", "开拓者只交答案，不动")
        self.assertNotIn("attack", {c["action"] for c in cmds.values()})

    def test_the_rescuer_walks_to_the_wall_first(self):
        """隔着几格 ⇒ 这一回合只挪一格，贴近了下一回合才拆（拆墙要贴着）。

        与 `_dig` 里"不做走到最优格再拆"是同一条取舍：候选只认当前已经贴着的墙格。
        """
        start = Pos(5, 24)
        cmds = plan(self._turn(rescuer=start))
        self.assertEqual(cmds["1"]["action"], "move", f"还没贴近 ⇒ 只能挪一格：{cmds}")
        cell = Pos(cmds["1"]["targetPos"][0]["x"], cmds["1"]["targetPos"][0]["y"])
        self.assertEqual(start.dist(cell), 1, "一步一格")
        self.assertLess(cell.dist(Pos(8, 25)), start.dist(Pos(8, 25)), "这一格必须真的更近")

    def test_colleagues_in_the_doorway_are_not_walls(self):
        """同事把两格门都堵上 ⇒ 照旧不拆（他们只是路过，下一回合就走）。

        判据里"自己人一律不算障碍"（把全部角色从障碍里摘掉，起点除外）：把同事算成墙
        就会白拆一次 —— 1 回合 + 1 块不回收的石头，换来一个下一回合就自动失效的洞。
        夹具必须堵满两格门：只堵一格时盒内的人本来就走得出去，用例会空转
        （反向验证：只堵一格的夹具在"自己人算障碍"的写法下也照样过）。
        """
        cmds = plan(self._turn(robots=False, colleagues=self.DOOR))
        self.assertEqual(self._holes(cmds), {}, f"同事堵门不算被关：{cmds}")

    def test_the_first_day_never_rescues(self):
        """第 1 天不救 —— 与 `_dig` 同源：那天环还在建，缺口本来就能走人。"""
        cmds = plan(self._turn(round_no=1))
        self.assertEqual(self._holes(cmds), {}, f"第 1 天不拆：{cmds}")

    def test_a_stone_short_worker_cannot_rescue(self):
        """没石头救不了：拆一块少一块，补不回来就是整夜的洞。"""
        cmds = plan(self._turn(stone=0))
        self.assertEqual(self._holes(cmds), {}, f"手里没石头不许拆：{cmds}")

    def test_a_boxed_worker_frees_itself(self):
        """被关住的是工人（盒内、没有差事）⇒ 它自己去拆一格：`(13,23)`。

        这条走的是 `_rescue`：`_stuck_inside` 把"眼下真出不去"的人捞出来，
        而工人是唯一能发 `remove` 的角色 ⇒ 自己就是救援者。
        （有差事的工人走另一条：朝目标的步数是 -1，`_dig` 的 `now < 0` 那一支接住。）
        """
        turn = self._turn(boxed=Pos(7, 24), rescuer=Pos(12, 24))
        self.assertEqual(self._holes(plan(turn)), {"1": Pos(13, 23)})


class HoleLifecycleTest(unittest.TestCase):
    """第 2 天整白天串跑：拆一次 → 当门用一天 → 天黑前补回去。

    单帧看不出"一天拆了几次、收工时墙上有没有洞、同一格会不会拆两回" —— 必须真的过一遍
    时间（与 `BuildWallTest.test_day_one_mines_then_walls_the_whole_ring_in_order` 同一个套路）。

    一天最多拆一次靠的不是门槛 5，而是两个窗口不相交（`HOLE_MIN_LEFT` 30 >
    `HOLE_PATCH_LEFT` 15）：拆只发生在"还剩 ≥31 回合"、补只发生在"还剩 ≤15 回合"。
    这条用例就是它的守门员 —— 两个数一旦被改成相交，这里会看到第二个洞，
    而墙上会一直开着口过夜。
    """

    BASE = Pos(10, 24)
    SIZE = (41, 32)
    WEAPONS = _records({Pos(9, 25): "rocket", Pos(12, 22): "gatling", Pos(12, 25): "railgun"})
    PRICES = {"stone": 1, "iron": 3, "copper": 5}
    #: 盒子外面、东侧那座石矿 —— 环砌满之后唯一值得跑的一趟（绕出背面那道门 → 再往东）
    MINE = Pos(18, 24)

    def test_one_hole_a_day_and_it_is_patched_before_night(self):
        """第 2 天跑满 70 回合：恰好拆 1 次、下一回合就从洞里穿过去、收工时环 18/18。"""
        entries = dict(
            _terrain(
                self.WEAPONS,
                {c: WALL for c in wall_cells(self.BASE, 41)},
                {self.BASE: "station"},
                {self.MINE: "stone"},
            )
        )
        stone, pos = 1, Pos(12, 24)
        log: list[tuple[int, str, Pos]] = []
        for round_no in range(ROUNDS_PER_DAY + 1, ROUNDS_PER_DAY + DAY_ROUNDS + 1):
            worker = Worker(1, pos, {"stone": stone} if stone else {})
            cmds = plan(
                Turn(
                    round_no=round_no,
                    map=Map(self.SIZE, entries),
                    roles=(worker,),
                    gold=0,
                    weapons=self.WEAPONS,
                    vendor_prices=self.PRICES,
                )
            )
            self.assertNotIn(
                "attack", {c["action"] for c in cmds.values()}, f"第 {round_no} 回合是白天，不许开火"
            )
            cmd = cmds.get("1")
            if cmd is None:
                continue
            cell = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
            log.append((round_no, cmd["action"], cell))
            if cmd["action"] == "move":
                pos = cell
            elif cmd["action"] == "collect":
                stone += 1
            elif cmd["action"] == "build":
                stone -= 1
                entries[cell] = WALL
            elif cmd["action"] == "remove":
                del entries[cell]

        holes = [(r, c) for r, a, c in log if a == "remove"]
        self.assertEqual(len(holes), 1, f"一天最多拆一次（两个窗口不相交）：{holes}")
        hole_round, hole = holes[0]
        self.assertEqual(hole, Pos(13, 23))
        # 洞是通道，不只是一个缺口：紧接着那一回合就从它身上穿过去
        index = [i for i, (_, a, _) in enumerate(log) if a == "remove"][0]
        self.assertEqual(log[index + 1][1:], ("move", hole), "下一回合要真的用它")
        # 天黑前补回去（补墙窗口 = 白天最后 HOLE_PATCH_LEFT 回合）
        patch = [r for r, a, c in log if a == "build" and c == hole]
        self.assertTrue(patch, f"收工前必须补回那一格：{log}")
        self.assertLessEqual(_left(patch[0]), HOLE_PATCH_LEFT, "补墙必须落在夜里之前的窗口里")
        self.assertGreater(_left(hole_round), HOLE_MIN_LEFT, "拆墙必须落在白天足够早的窗口里")
        # 收工时环是满的（拆了不补就是整夜的洞，机器人从正面长驱直入）
        self.assertEqual(
            [c for c in wall_cells(self.BASE, 41) if c not in entries], [], "收工时 18/18"
        )


class TwoWallBuildersTest(unittest.TestCase):
    """两个工人同时在环上：不能对着改目标来回踱步。

    两个同症状的死循环都得守住：① `_ring` 把"工人脚下那一格"也当成挡路的格划掉
    ⇒ 另一个工人的 `free[0]` 整体后移、掉头去砌更靠后的墙；等前一个工人一挪窝
    目标又变回来 —— 两人在两格之间转到天黑，一格墙都没砌。② `_walled` 把自己人
    一律算成障碍，而门收成 2 格之后环内只剩几条走廊 ⇒ "一个工人恰好站在走廊格上"
    变成常事 ⇒ 另一个工人被判成"砌满就出不去"，闸门把他送出去一格、下一回合他又
    走回来砌墙 —— 同样是一直来回、一座不砌。单帧断言看不出来，必须把回合串起来跑。

    角色必须自己铺进地图：真实路径里 `protocol.model._entries` 会把 `teamOur.roles`
    写进网格，"谁站在哪"进 `blocked`。手搓 `Map` 时不铺角色就复现不出这个循环 ——
    不铺的话这条用例连坏的实现都放不过去。
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
        # 石头给够：环 18 格 ⇒ 还剩 15 格要砌，兜里少于 15 块时
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
    （那一格照旧在 `Map.blocked` 里，但看不出是墙还是空地）；而 `_ring` 的"自己人算路过"
    又会把它复活成候选 ⇒ `free[0]` 永远是脚下这一格，`dist == 0` 也是合法建造位
    ⇒ 每回合 `build` 同一格，永远轮不到下一格 —— 石头白花，且 `free` 永不为空，
    连"砌完了去采矿"那一支都进不去。
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
        #: 别钉死具体格：`step_toward` 的并列最短路会走对角，坐标取决于实现细节。
        #: 钉契约：恰好走 `dist−1` 步、每步把切比雪夫距离缩 1、终点贴着目标。
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
    """半血墙的修复差事（口径：包优先、重建兜底）。

    文档事实：WallFixer 10 金、目标墙回满血（任务书消耗品表），站墙一格内 `use`。
    修复（1 回合 + 10 金、墙不塌）全面占优推倒重建（2 石头 + 2 回合 + 洞开 2 回合），
    重建只在"没包且买不起"时兜底。
    """

    BASE = Pos(10, 24)
    WEAPONS = _records({Pos(9, 23): "gatling", Pos(9, 24): "railgun", Pos(9, 22): "rocket"})
    SHOP = Pos(20, 16)
    HALF = Pos(13, 22)  # 正面列的一格（半血墙）
    OTHER = Pos(13, 26)  # 另一面墙（第二面半血墙）

    def _turn(self, worker: Worker, *, walls=None, shop=True, gold=0, prices=None, stone=0):
        grid = _terrain(self.WEAPONS, {self.BASE: "station", self.HALF: "wall"})
        if shop:
            grid[self.SHOP] = "weaponShop"
        grid[worker.pos] = "worker"
        if walls is None:
            walls = (Wall(40000, self.HALF, 400, 1),)
        return Turn(
            round_no=1,
            map=Map((41, 32), grid),
            roles=(worker,),
            gold=gold,
            weapons=self.WEAPONS,
            shop_prices=prices if prices is not None else {"WallFixer": 10},
            walls=walls,
        )

    def test_a_worker_with_a_pack_repairs_the_half_wall(self):
        """持包 + 贴着半血墙 ⇒ `use WallFixer`（墙回满血、不塌、1 回合）。"""
        worker = Worker(10010, Pos(12, 22), {"WallFixer": 1})
        cmd = plan(self._turn(worker))[str(10010)]
        self.assertEqual(cmd["action"], "use")
        self.assertEqual(cmd["name"], "WallFixer")
        self.assertEqual(
            Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"]), self.HALF
        )

    def test_a_worker_without_a_pack_buys_one_when_affordable(self):
        """没包 ⇒ 去商店买（贴着就买）；`collect`/挖矿都排在修墙之后。"""
        worker = Worker(10010, Pos(20, 15), {})
        cmd = plan(self._turn(worker, gold=10))[str(10010)]
        self.assertEqual(cmd["action"], "buy")
        self.assertEqual(cmd["name"], "WallFixer")

    def test_without_pack_or_gold_it_falls_back_to_rebuild(self):
        """没包且买不起（没钱/没商店）⇒ 贴着半血墙 `remove`（下一回合那格进 `_ring` 重建）。"""
        worker = Worker(10010, Pos(12, 22), {"stone": 1})
        cmd = plan(self._turn(worker, shop=False, gold=0, prices={}))[str(10010)]
        self.assertEqual(cmd["action"], "remove")
        self.assertEqual(Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"]), self.HALF)

    def test_repair_beats_mining(self):
        """修墙 > 挖矿 —— 贴着半血墙又贴着矿，先用包。"""
        worker = Worker(10010, Pos(12, 22), {"WallFixer": 1})
        cmd = plan(self._turn(worker))[str(10010)]  # 没摆矿 ⇒ 更没有别的可干
        self.assertEqual(cmd["action"], "use")

    def test_two_workers_claim_different_half_walls(self):
        """两面半血墙、两个持包工人 ⇒ 各修各的（认领账本，不挤同一面）。"""
        walls = (Wall(40000, self.HALF, 400, 1), Wall(40001, self.OTHER, 300, 1))
        grid = _terrain(
            self.WEAPONS,
            {self.BASE: "station", self.HALF: "wall", self.OTHER: "wall", self.SHOP: "weaponShop"},
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
        self.assertEqual(targets, {self.HALF, self.OTHER}, "各修各的，不挤同一面墙")

    def test_a_fully_healthy_wall_is_never_repaired(self):
        """满血墙（health*2 >= 基准）不在修复名单 —— 别为好墙白花钱。"""
        worker = Worker(10010, Pos(12, 22), {"WallFixer": 1})
        walls = (Wall(40000, self.HALF, 1000, 1),)
        grid = _terrain(self.WEAPONS, {self.BASE: "station", self.HALF: "wall"})
        grid[worker.pos] = "worker"
        cmds = plan(
            Turn(
                round_no=1, map=Map((41, 32), grid), roles=(worker,), gold=0,
                weapons=self.WEAPONS, walls=walls,
            )
        )
        self.assertNotEqual(cmds.get(str(10010), {}).get("action"), "use")


class WallPriorityTest(unittest.TestCase):
    """白天工人的优先级链：**建武器 > 修墙 > 砌墙 > 经济线**，且工人不能什么都不做
    —— 一件不行才轮到下一件。

    武器是最高优先级（口径：无论哪一天，武器没了先建）：份额有缺且钱够 ⇒ 建/走向
    落点；钱不够但**筹资可行**（有小贩、有价可卖）⇒ 整条墙线让位，先卖背包的货、
    再采最值钱的矿，凑够 25 金币回来建；筹资不可行（没小贩/没价目）⇒ 照旧走墙线。
    升级线只在武器齐了之后才跑。`_build_walls` 返回 `True` = 本回合的指令已由
    墙线产出（或 gated 待命）；`False` = 什么都没发过，兜底链接手。
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
        """`gated`（砌下去会把人关在墙里）⇒ 待命：开拓者被任务钉死在盒内（它走
        ① 支路，永远轮不到"先出来"那道闸），两个工人又恰好把 2 格门口都占住 ⇒
        盒外有墙格要砌的工人这一回合不发指令（口径："安全：别跑远，下回合缺口还在"
        —— 比落经济线更保环的速度）。
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
    """第一天必须把墙建好 —— 全天模拟的验收网：

    可行地图（两座石矿 = 20 块石头 ≥ 18 格墙）上跑满 70 个白天回合，照判题器口径
    结算指令（move/build/collect/sell/buy、矿采 10 次消失），必须做到：

    1. 环 18/18 砌完，且同一格不许砌两遍（石头白花）；
    2. 三座武器建满（开局 75 金恰好三座，夜里第一波机器人之前得有炮）；
    3. 环砌完之前工人不许闲（"工人不能什么都不做"；环砌完后的白天末尾，
       预算拦住回不来的远矿 ⇒ 待命是保守方向的合法行为，不在本网范围）。
    """

    BASE = Pos(10, 24)
    STONE1, STONE2 = Pos(4, 24), Pos(4, 26)
    IRON, COPPER = Pos(6, 30), Pos(20, 30)
    VENDOR, SHOP, TASK = Pos(20, 16), Pos(22, 18), Pos(30, 30)
    PRICES = {"stone": 1, "iron": 3, "copper": 5}

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
        self.mine_left = {self.STONE1: 10, self.STONE2: 10, self.IRON: 10, self.COPPER: 10}
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
            done_at, f"第 1 天没把 18 格墙砌完，缺：{sorted(ring - set(self.walls))}"
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


if __name__ == "__main__":
    unittest.main()
