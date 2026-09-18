"""game/path.py 的用例：四个 BFS —— `step_toward` / `step_onto` / `step_outside` / `steps_between`。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import sys
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用（discover 不一定把它放进 sys.path）；src/ 给 coregeek 用
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coregeek.game.grid import Pos, door_cells, wall_cells  # noqa: E402
from coregeek.game.map import Map  # noqa: E402
from coregeek.game.path import step_onto, step_outside, steps_between, step_toward  # noqa: E402
from coregeek.game.planner import plan  # noqa: E402
from coregeek.game.roles import Worker  # noqa: E402
from coregeek.game.world import Turn  # noqa: E402


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
        """回炮位被低估的量：环砌满之后，从盒外回后列炮要绕到后方通道再横穿盒子。

        切比雪夫把 `(14,24) → (9,25)` 说成 5 步；真实步数是先绕到背面敞口再横穿盒子。
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
        """同一趟路的另一半：后方通道（`door_cells`）就是唯一的进出口。

        把通道也堵上 ⇒ -1（盒子里的人出不来、盒外的人进不去）。这条同时钉住
        "背面整列不砌、环永远不闭合"这条不变量。
        """
        post, outside = Pos(9, 25), Pos(14, 24)
        ring = set(wall_cells(self.BASE, self.SIZE[0])) | set(door_cells(self.BASE, self.SIZE[0]))
        self.assertEqual(steps_between(outside, post, frozenset(ring), self.SIZE), -1, "进不去")
        self.assertEqual(steps_between(post, outside, frozenset(ring), self.SIZE), -1, "也出不来")
        # 对照：只砌那 14 格（通道开着）⇒ 两个方向都走得通
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


class StepOntoTest(unittest.TestCase):
    """`step_onto` —— 走到 goal **自己**那一格上，`step_toward` 只走到它旁边。

    两座火箭共用的那个操作位就是这种格子：它是空地，而角色要站在**上面**才同时贴着两座
    （`BuildGeometryTest.test_the_two_rockets_share_an_operator_spot` 钉的是这格存在）。
    拿 `step_toward` 顶替会停死在它旁边 —— 那格已经 `dist(goal) <= 1`，每回合都返回 None，
    目标又每回合重算 ⇒ 角色永远走不到、永远开不了第二座炮。
    """

    BASE = Pos(10, 24)
    SIZE = (41, 32)

    def test_it_walks_across_the_box_onto_the_shared_spot(self):
        """从盒子另一头一路走上去：跟着它走必然**落在 goal 上**，不是停在旁边。

        终点 `(11,25)` = 左半基地那两座火箭的共用操作位（环内、非基地、非墙）。
        """
        ring = frozenset(wall_cells(self.BASE, self.SIZE[0]))
        goal = Pos(11, 25)
        at = Pos(11, 22)
        for _ in range(10):
            if at == goal:
                break
            step = step_onto(at, goal, ring, self.SIZE)
            self.assertIsNotNone(step, f"{at} 该走得到 {goal}")
            self.assertEqual(at.dist(step), 1, "一步一格")
            at = step
        self.assertEqual(at, goal, "十步之内必须站上去")
        self.assertIsNone(step_onto(at, goal, ring, self.SIZE), "已经在上面 ⇒ None")
        self.assertIsNone(
            step_toward(at, goal, ring, self.SIZE), "对照：`step_toward` 在这格上只会返回 None"
        )

    def test_a_sealed_goal_is_unreachable(self):
        """goal 被整个围死 ⇒ `None`（与"贴着 goal 即到"合流成一个值，调用方自己分得清）。"""
        goal = Pos(20, 20)
        ring = frozenset({Pos(x, y) for x in range(19, 22) for y in range(19, 22)} - {goal})
        self.assertIsNone(step_onto(Pos(5, 5), goal, ring, self.SIZE))

    def test_a_goal_off_the_map_is_unreachable(self):
        """goal 在地图外 ⇒ `None`：BFS 不许越界（越界算不算非法文档没写，不走一定安全）。"""
        self.assertIsNone(step_onto(Pos(1, 1), Pos(5, 5), frozenset(), (3, 3)))


if __name__ == "__main__":
    unittest.main()
