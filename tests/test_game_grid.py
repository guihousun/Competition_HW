"""game/grid.py 的用例：Pos / 8 方向 / 基地几何（武器环、围墙环、门、防御盒）。

四个 BFS 的用例在 `test_game_path.py`。

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
from coregeek.game.grid import Pos, STEPS, base_cells, weapon_cells, weapon_sites  # noqa: E402
from coregeek.game.map import LEGEND, _NAMES, _RENDER_NEUTRAL, _RENDER_SIDED, Map, _char  # noqa: E402
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

    def test_all_sites_are_in_the_buildable_ring(self):
        """三个落点都在可建造的武器环里、且不落在基地身上（算错 ⇒ `build` 落点非法）。

        ⚠️ 新阵形**不在前排**：三座火箭全在**背面**那一侧（`back_x = near - d`）—— 前排
        留给围墙。换边后整套落点自动跟着翻（按基地坐标判，不看 `teamOur.type`）。
        """
        for base in (Pos(10, 24), Pos(30, 10)):
            with self.subTest(base=base):
                sites = weapon_sites(base, 41)
                self.assertEqual(len(sites), 3)
                ring = set(weapon_cells(base))
                for site in sites:
                    self.assertIn(site, ring, f"{site} 不在可建造环里")
                self.assertEqual(set(sites) & base_cells(base), set(), "别压在基地上")

    def test_the_three_sites_are_rockets_on_the_back_side(self):
        """三格 = **顶排后俩 + 基地后方下格**（用户给的示意图），顺序 = [非角, 非角, 角]。

        左半基地 `(10,24)` ⇒ 顶排 `y=25` 的两格 `(10,25)`/`(9,25)`、后方下格 `(9,23)`；
        其中 `(9,25)` 是武器环的**角**（券链把它排到最后）。右半镜像。
        """
        self.assertEqual(
            weapon_sites(Pos(10, 24), 41),
            (Pos(10, 25), Pos(9, 23), Pos(9, 25)),
        )
        self.assertEqual(
            weapon_sites(Pos(30, 10), 41),
            (Pos(31, 11), Pos(32, 9), Pos(32, 11)),
        )

    def test_the_three_rockets_share_one_operator_spot(self):
        """三座火箭的邻域交集**只有一格**（且是环内空地）⇒ 一个角色就能全操。

        左半基地三座在 (10,25)/(9,23)/(9,25) ⇒ 交集是 {(9,24), (10,24)}，其中 (10,24) 是基地
        ⇒ 只有 `(9,24)` 站得上。这是"一个角色按冷却轮换操三座"的几何前提。
        """
        sites = weapon_sites(self.BASE, 41)
        common: set[Pos] | None = None
        for site in sites:
            nbrs = {Pos(site.x + d.x, site.y + d.y) for d in STEPS}
            common = nbrs if common is None else common & nbrs
        assert common is not None
        ring = set(weapon_cells(self.BASE))
        shared = (common & ring) - base_cells(self.BASE)
        self.assertEqual(len(shared), 1, f"操作位该只有一格，实际 {sorted(shared)}")
        spot = next(iter(shared))
        for site in sites:
            self.assertEqual(spot.dist(site), 1, f"操作位要贴着每一座：{site}")

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


if __name__ == "__main__":
    unittest.main()
