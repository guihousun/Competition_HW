"""game/grid.py 的用例：Pos / 8 方向 / 两个距离口径（切比雪夫 vs BFS）/ `step_toward` /
基地几何（武器环、围墙环、门、防御盒）。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import json
import sys
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用（discover 不一定把它放进 sys.path）；src/ 给 coregeek 用
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _fixtures import SAMPLE  # noqa: E402
from coregeek.game.grid import Pos, STEPS, base_cells, door_cells, step_outside, steps_between, step_toward, wall_cells, weapon_cells, weapon_sites  # noqa: E402
from coregeek.game.map import LEGEND, _NAMES, _RENDER_NEUTRAL, _RENDER_SIDED, Map, _char  # noqa: E402
from coregeek.game.planner import plan  # noqa: E402
from coregeek.game.roles import Worker  # noqa: E402
from coregeek.game.world import Turn  # noqa: E402
from coregeek.protocol import model  # noqa: E402


class GridTest(unittest.TestCase):
    """格子矩阵本身 —— `Turn.map` 的交付物。

    `cells` 才是真相，`render()` 只是给人看的（那张字符表是有损的）。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.grid = model.load(json.loads(SAMPLE.read_text(encoding="utf-8"))).map

    def test_station_is_expanded_to_four_cells(self):
        """基地 2×2，`pos` 给的是左上角 ⇒ 占 `y` 和 `y-1`。

        接口文档写的是"双方"基地大小都是 2*2，敌我都要展成 4 格：只标一格的话角色会
        一头撞进基地里 —— 那是一条判题器不收的指令。
        """
        for corner, kind in ((Pos(10, 24), "station"), (Pos(30, 10), "enemy:station")):
            with self.subTest(corner=corner):
                for cell in base_cells(corner):
                    self.assertEqual(
                        self.grid.cells[cell.y][cell.x], kind, f"{cell} 应是基地的一部分"
                    )

    def test_enemy_is_prefixed_and_ours_is_not(self):
        """两方都有 `wall` / `station`，不区分敌我在网格里就撞车。"""
        self.assertEqual(self.grid.cells[20][5], "wall")  # 我方 (5,20)
        self.assertEqual(self.grid.cells[7][28], "enemy:wall")  # 敌方 (28,7)
        self.assertEqual(self.grid.cells[24][10], "station")  # 我方 (10,24)
        self.assertEqual(self.grid.cells[10][30], "enemy:station")  # 敌方 (30,10)

    def test_blocking_is_exactly_non_empty(self):
        """`blocked` 必须恰好等于"非空格子"。

        `expected` 在这里独立按任务书 L85 重算一遍（四路来源 + 基地 2×2），完全不走
        `Map` 的代码 —— 漏掉任何一类挡路物，症状都是"以为能走、其实撞墙"。
        """
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        expected: set[Pos] = set()

        def add(node: dict) -> None:
            pos = Pos(node["pos"]["x"], node["pos"]["y"])
            # 基地 2×2 而 pos 只给左上角：与实现用的是同一个约定，但独立写一遍
            expected.update(base_cells(pos) if node["roleType"] == "station" else (pos,))

        for node in raw["mapInfo"]["zones"]:
            expected.add(Pos(node["pos"]["x"], node["pos"]["y"]))
        for path in ("teamOur", "teamEnemy", "robot"):
            for node in raw[path]["roles"]:
                add(node)

        self.assertEqual(self.grid.blocked, expected)
        # 手数一遍的交叉核对：14 zones + (我方 8 单位 + 基地 4 格) + (敌方 1 墙 + 基地 4 格) + 4 机器人
        self.assertEqual(len(self.grid.blocked), 35)

    def test_render_shape(self):
        """打印出来的图给调试用 —— 错了最坑：y 翻反了图上照样"像张地图"。

        布局：上下各一行 `—` 标尺 + 32 行网格，每行 43 列 = `"│"` + 41 列 + `"|"`。
        下面全是裸下标断言：它们把"字符落在第几列、第几行"钉死。
        """
        lines = self.grid.render().splitlines()
        self.assertEqual(len(lines), 34)
        self.assertEqual({len(line) for line in lines}, {43})
        self.assertEqual(lines[0], "—" * 43, "上标尺")
        self.assertEqual(lines[-1], "—" * 43, "下标尺")
        # 行 = height-1-y（y 向上、终端从上往下印）；列 = 1+x（第 0 列是左边框）
        # 小写 = 我方，大写 = 敌方
        self.assertEqual(lines[8][11], "s")  # 我方基地左上角 (10,24)
        self.assertEqual(lines[9][12], "s")  # 我方基地右下角 (11,23)
        self.assertEqual(lines[8][10], "g")  # 加特林 (9,24)，与基地同一行
        self.assertEqual(lines[7][11], "r")  # 电磁狙击炮 (10,25)，在基地上方一行
        self.assertEqual(lines[22][31], "S")  # 敌方基地左上角 (30,10)
        self.assertEqual(lines[8][5], "o")  # 石矿 (4,24)
        self.assertEqual(lines[25][29], "%")  # 敌方围墙 (28,7) —— 墙不走字母，是例外
        self.assertEqual(lines[28][5], "x")  # 机器人 (4,4)
        self.assertEqual(lines[32][1], " ")  # (0,0) 空地 —— 最后一行是最底下的 y=0

    def test_render_keeps_y_pointing_up(self):
        """行自上而下 = y 由大到小，且每行的列偏移 = `1+x`。

        只钉"最后一行是 y=0"的话，中间那些行翻反了照样过 —— 这里用斜线地图：
        第 y 行只有 `(y,y)` 是矿 ⇒ 每行矿的列位置就等于那一行的 y，翻反或错位一格立刻挂。
        """
        width, height = 4, 3
        grid = Map((width, height), {Pos(y, y): "stone" for y in range(height)})
        lines = grid.render().splitlines()
        self.assertEqual(len(lines), height + 2)
        for i, line in enumerate(lines[1:-1]):
            y = height - 1 - i
            self.assertEqual(line.index("o"), 1 + y, line)
        # 左边框那一列是空的（x=-1 没有格子），最底下一行才是 (0,0)
        self.assertEqual(lines[-2][1], "o")

    def test_render_degrades_when_there_are_no_cells(self):
        """没有格子 ⇒ 空串，`blocked` 也为空 ⇒ 不挡路也不动（见 `Map.__init__`）。

        判据是 `cells` 为空而不是"尺寸非法"：高/宽为 0 时 `size` 看着合法，
        但同样一格都没有 —— 两种情形走的是同一条早返回。
        """
        for size in ((-1, -1), (41, 0), (0, 32)):
            with self.subTest(size=size):
                empty = Map(size, {Pos(0, 0): "wall"})
                self.assertEqual(empty.cells, ())
                self.assertEqual(empty.blocked, frozenset())
                self.assertEqual(empty.render(), "")

    def test_the_legend_covers_every_category(self):
        """图例必须覆盖字符表里的每一个类别。

        守的是"加了新中立元素却忘了往 `_NAMES` 里补" —— 漏掉的症状是复盘时把新元素看成
        `?`，而 `?` 在地图上到处都是。集合相等比循环 `assertIn` 更强：多一个少一个都挂。
        """
        self.assertEqual(set(_NAMES), set(_RENDER_SIDED) | set(_RENDER_NEUTRAL))
        for kind in _NAMES:
            self.assertIn(f"{_char(kind)}=", LEGEND)
        # 这几样不在 `_NAMES` 里 —— 它们不是"某个类别"，而是 `_char` 的兜底与大小写规则
        for token in ("x=机器人", "空格=空地", "?=未知", "大写=敌方", "%=敌方围墙"):
            self.assertIn(token, LEGEND)

        # 一个字符只能代表一样东西：共用的症状是图上分不出来（我方围墙整圈沉进空地
        # 背景），而图例看上去只是重复一项、上面那些 `assertIn` 一条都不会挂。`_char`
        # 恒返回 1 字符 ⇒ 比长度就够。守的是"任何两个类别都不许共用"，不是某一对具体值。
        chars = [_char(kind) for kind in _NAMES] + ["x", " ", "?"]
        self.assertEqual(len(set(chars)), len(chars), sorted(chars))

    def test_the_legend_names_the_task_points_by_faction(self):
        """`1`-`4` 是阵营的任务点，不是"我方/敌方"。

        它们来自 `zones` 的 `challengerTaskPoint*` / `defenderTaskPoint*`，两队同时存在；
        样例里我方恰好是挑战者、两套重合 ⇒ 写错也测不出来。我方可接的那两个点在
        `Turn.task_points`（来自 `teamOur.playerTasks`），别混为一谈。
        """
        self.assertIn("挑战方任务点", LEGEND)
        self.assertIn("防守方任务点", LEGEND)
        self.assertNotIn("我方任务点", LEGEND)
        self.assertNotIn("敌方任务点", LEGEND)


class BuildGeometryTest(unittest.TestCase):
    """可建造区与武器落点。

    公式来源是图不是正文（`docs/pic/build_map.png`）：任务书没写坐标公式，
    接口文档的 `mapInfo` 里也没有可建造区字段。算错 ⇒ `build` 落点非法 ⇒ 那 25 金币白花。
    """

    BASE = Pos(10, 24)  # 样例里的我方基地

    def test_weapon_cells_are_the_ring_around_the_base(self):
        """12 格 = 基地外圈 4×4 减去基地自己，且一格都不和基地重叠。"""
        cells = weapon_cells(self.BASE)
        own = base_cells(self.BASE)
        self.assertEqual(len(cells), 12)
        self.assertEqual(set(cells) & own, set(), "武器格不能落在基地身上")
        self.assertEqual({c.x for c in cells}, {9, 10, 11, 12})
        self.assertEqual({c.y for c in cells}, {22, 23, 24, 25})
        # 样例那三座武器都该落在这个环上 —— 唯一的"外部"交叉验证
        for pos in (Pos(9, 24), Pos(9, 25), Pos(10, 25)):
            self.assertIn(pos, cells)

    def test_all_sites_are_on_the_front_column(self):
        """三座武器全在迎着机器人的前排（左半 `x=bx+2`、右半 `x=bx-1`）。

        阵形：前排上方相邻两格放 2 火箭、前排下方一格放加特林 —— 两火箭相邻 ⇒
        一个角色可同时操作两座。
        """
        left = weapon_sites(Pos(10, 24), 41)  # 左半 ⇒ 前排 x=12
        self.assertEqual([c.x for c in left], [12, 12, 12])
        right = weapon_sites(Pos(30, 10), 41)  # 右半 ⇒ 前排 x=29
        self.assertEqual([c.x for c in right], [29, 29, 29])
        self.assertEqual(len(left), len(right), "两侧都该是整整齐齐三个落点")

    def test_the_three_sites_are_two_adjacent_rockets_plus_a_gatling(self):
        """前排上方相邻两格（2 火箭）+ 前排下方一格（加特林），顺序即建造顺序。

        两火箭相邻是关键：一个角色站在内侧那一格就能同时贴着两座，利用 3 回合冷却交替开火。
        """
        self.assertEqual(
            weapon_sites(Pos(10, 24), 41),
            (Pos(12, 24), Pos(12, 25), Pos(12, 22)),
        )
        # 换边后整套落点自动跟着翻 —— 按基地坐标判而不用 `teamOur.type`
        self.assertEqual(
            weapon_sites(Pos(30, 10), 41),
            (Pos(29, 10), Pos(29, 11), Pos(29, 8)),
        )

    def test_the_two_rockets_share_an_operator_spot(self):
        """两火箭之间存在一个同时贴着两座的环内空格 ⇒ 一个角色能操作两座。

        左半基地两火箭在 (12,24)/(12,25) ⇒ 操作位是 (11,25)（内侧、非基地、非墙）。
        """
        sites = weapon_sites(self.BASE, 41)
        rockets = sites[:2]
        # 两火箭必须相邻（切比雪夫距离 1）
        self.assertEqual(rockets[0].dist(rockets[1]), 1, "两火箭必须相邻才能共享操作位")
        # 求同时贴着两座火箭的格子
        nbrs0 = {Pos(rockets[0].x + d.x, rockets[0].y + d.y) for d in STEPS}
        nbrs1 = {Pos(rockets[1].x + d.x, rockets[1].y + d.y) for d in STEPS}
        common = nbrs0 & nbrs1
        ring = set(weapon_cells(self.BASE))
        shared = common & ring  # 只看环内空格（操作位必须走得进去）
        self.assertTrue(shared, f"两火箭之间该有共享操作位，交集={common}")
        for spot in shared:
            self.assertEqual(spot.dist(rockets[0]), 1)
            self.assertEqual(spot.dist(rockets[1]), 1)

    def test_every_site_touches_the_base(self):
        """这个阵形的理由：三个落点各自与基地的一格切比雪夫距离 1。

        升级券/维修包要在目标建筑周围一格内用（任务书 L292 / L314），`attack` 也要求站在
        炮旁 ⇒ 站上落点就够得着脚下的炮与旁边的基地。顺带钉住"落点在武器环上"。
        """
        for base, width in ((Pos(10, 24), 41), (Pos(30, 10), 41)):
            with self.subTest(base=base):
                own = base_cells(base)
                ring = weapon_cells(base)
                for cell in weapon_sites(base, width):
                    self.assertIn(cell, ring, "落点必须在武器环上")
                    self.assertEqual(
                        min(cell.dist(b) for b in own),
                        1,
                        f"{cell} 该贴着基地的一角，否则升级/维修券够不着",
                    )


class StepOutsideTest(unittest.TestCase):
    """`step_outside` 的网格级契约 —— 闸门两半唯一的判据，脱离游戏局面单独钉一遍。

    两条 `None` 合流是有意的（调用方只问"这一步迈不迈得出去"），代价是
    "谁在盒子里面"必须由调用方自己筛。三种返回值各钉一条。
    """

    #: 手搭的 2×2 小盒子 —— 与基地几何无关，测的是基元本身
    BOX = frozenset({Pos(1, 1), Pos(2, 1), Pos(1, 2), Pos(2, 2)})
    SIZE = (41, 32)

    def test_a_pos_already_outside_returns_none(self):
        """已经在外面 ⇒ `None`（不是"走不出去"）—— 所以调用方必须自己筛 `pos in box`。"""
        self.assertIsNone(step_outside(Pos(5, 5), self.BOX, set(), self.SIZE))

    def test_a_sealed_box_returns_none(self):
        """四面围死 ⇒ `None`。"""
        ring = {Pos(x, y) for x in range(0, 4) for y in range(0, 4)} - self.BOX
        self.assertIsNone(step_outside(Pos(1, 1), self.BOX, ring, self.SIZE))

    def test_the_step_lands_outside_and_moves_exactly_one_cell(self):
        """返回的是从 pos 迈出的第一步：与 pos 切比雪夫距离恒为 1、且落点在盒外。"""
        step = step_outside(Pos(1, 1), self.BOX, {Pos(1, 2)}, self.SIZE)
        self.assertIsNotNone(step)
        self.assertEqual(Pos(1, 1).dist(step), 1, "一步一格")
        self.assertNotIn(step, self.BOX, "迈出去的这一步必须是盒外的格")

    def test_the_map_edge_never_lets_a_step_off_the_map(self):
        """贴着地图角、又只有越界一条路 ⇒ `None`（不许走出地图）。

        边界不在任务书 L85 的阻挡清单里（越界算"指令非法"还是"执行失败"文档没写），
        不走一定安全 —— `step_toward` 就是这么挡的，这里是同一条。
        """
        box = frozenset({Pos(0, 0)})
        self.assertIsNone(step_outside(Pos(0, 0), box, {Pos(1, 0), Pos(0, 1), Pos(1, 1)}, (2, 2)))


class StepsBetweenTest(unittest.TestCase):
    """`steps_between` —— 回合预算用的口径，与 `Pos.dist` 分家。

    `dist` 是切比雪夫直线（选点用），它是绕障的真实步数（"这趟来不来得及"用），独有一个
    `dist` 给不出的失败态 -1 ⇒ 每个调用点都得自己接住，三种返回值各钉一条。
    """

    BASE = Pos(10, 24)
    SIZE = (41, 32)

    def test_already_touching_the_goal_is_zero(self):
        """贴着 goal 一格，或就站在 goal 上 ⇒ 0（与 `step_toward` 同一个终点约定）。

        `goal` 自己当障碍：真实目标（矿、建筑、炮位）都是挡路的格，人只能停在它旁边。
        """
        for pos in (Pos(12, 24), Pos(13, 23), Pos(13, 24)):
            with self.subTest(pos=pos):
                self.assertEqual(steps_between(pos, Pos(13, 24), set(), self.SIZE), 0)

    def test_a_sealed_goal_is_unreachable(self):
        """goal 被整个围死 ⇒ -1。切比雪夫永远给不出这个值 —— 这就是这个口径独有的失败态。"""
        goal = Pos(20, 20)
        ring = {Pos(x, y) for x in range(19, 22) for y in range(19, 22)} - {goal}
        self.assertEqual(steps_between(Pos(5, 5), goal, frozenset(ring), self.SIZE), -1)

    def test_walking_home_costs_more_than_the_straight_line(self):
        """回炮位被低估的量：环砌满之后，从盒外回后列炮要绕背面那 2 格门。

        切比雪夫把 `(14,24) → (9,25)` 说成 5 步；真实步数是先绕到背面门口再横穿盒子。
        断言只钉"严格大于"，不钉具体步数 —— 步数是几何的，几何一改这条就得跟着改。
        """
        ring = wall_cells(self.BASE, self.SIZE[0])
        blocked = set(ring)
        post = Pos(9, 25)  # 后列炮的落点（`weapon_sites` 的第一个）
        start = Pos(14, 24)
        self.assertGreater(
            steps_between(start, post, frozenset(blocked), self.SIZE),
            start.dist(post),
            "绕障的真实步数必须严格大于直线 —— 否则这一步白改",
        )

    def test_the_door_is_the_only_way_in(self):
        """同一趟路的另一半：门那 2 格（`door_cells`）就是唯一的进出口。

        把门也堵上 ⇒ -1（盒子里的人出不来、盒外的人进不去）。这条同时钉住
        "门 = 背面中间那 2 格、环永远不闭合"这条不变量。
        """
        post, outside = Pos(9, 25), Pos(14, 24)
        ring = set(wall_cells(self.BASE, self.SIZE[0])) | set(door_cells(self.BASE, self.SIZE[0]))
        self.assertEqual(steps_between(outside, post, frozenset(ring), self.SIZE), -1, "进不去")
        self.assertEqual(steps_between(post, outside, frozenset(ring), self.SIZE), -1, "也出不来")
        # 对照：只砌那 18 格（门开着）⇒ 两个方向都走得通
        door_open = frozenset(wall_cells(self.BASE, self.SIZE[0]))
        self.assertGreater(steps_between(outside, post, door_open, self.SIZE), 0)
        self.assertGreater(steps_between(post, outside, door_open, self.SIZE), 0)


class PathTest(unittest.TestCase):
    """寻路：BFS 最短路。这两个局面上贪心版都会挂（贪心只会挑"确实更近"的格子）。"""

    #: 合成局面里也得摆个基地，否则 `_ring` 是空的、工人压根不动（矿只服务于砌墙）
    BASE = Pos(20, 20)

    def _walk(self, turn: Turn, limit: int) -> tuple[int, Turn]:
        """把回合串起来走，返回 `(实际走了几步, 走完的局面)`。

        发 `collect` 就算走到了 —— 那正是"贴着矿"的唯一标志。
        """
        for moves in range(limit + 1):
            cmds = plan(turn)
            if not cmds:
                return moves, turn
            cmd = cmds["1"]
            if cmd["action"] == "collect":
                return moves, turn
            t = cmd["targetPos"][0]
            turn = turn._replace(roles=(Worker(1, Pos(t["x"], t["y"])),))
        self.fail(f"{limit} 回合还没走到，说明在原地绕圈或卡死")

    def test_walks_around_a_wall(self):
        """一堵横墙把直路封死，只能绕到墙的右端过去。

        贪心会在 `(4,4)` 停死：那里没有任何一格更近（更近的三格全在 y=3 的墙上），
        而它只会挑"确实更近"的格子。BFS 绕得过。
        """
        wall = {Pos(x, 3) for x in range(8)}  # x=0..7 一整行
        mine = Pos(5, 1)
        turn = Turn(
            round_no=1,
            map=Map((41, 32), {self.BASE: "station", mine: "stone", **{p: "wall" for p in wall}}),
            roles=(Worker(1, Pos(5, 5)),),
            gold=0,
        )

        # 最短路 5 步：(5,5)→(6,4)→(7,4)→(8,3)[绕过墙]→(7,2)→(6,2)，(6,2) 距矿 1
        moves, final = self._walk(turn, 20)
        self.assertEqual(moves, 5, "步数不等于最短路 ⇒ 找的不是最短路")
        self.assertEqual(final.roles[0].pos.dist(mine), 1, "绕过去了但没停在矿边")

    def test_never_leaves_the_map(self):
        """地图边界不在任务书 L85 的阻挡清单里，得自己挡。

        整列 `x=1` 封死，工人被关在 `x=0` 这一列；能到达的格子没有一个贴着目标。
        去掉边界检查这里会返回 `(-1,4)` —— 一条走出地图的指令。
        """
        goal = Pos(0, 1)
        blocked = {Pos(1, y) for y in range(10)}  # 整列封死
        blocked |= {Pos(0, y) for y in (2, 3, 4)}  # 再堵掉本列的直路
        blocked.add(goal)

        self.assertIsNone(step_toward(Pos(0, 5), goal, frozenset(blocked), (10, 10)))


if __name__ == "__main__":
    unittest.main()
