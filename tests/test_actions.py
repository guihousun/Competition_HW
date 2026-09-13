"""权限闸门与报文的用例。

**为什么值得单独写**（`CLAUDE.md` 编码规则第 3 条）：权限类 bug 的特征是**本地全绿** ——
格式完全合法、自检全过，只有判题器会说"不"，而那条路直通红线（累计 5 次异常即整场不再被调度）。
所以"格式对"证明不了"这角色有权这么做"，必须单独钉一遍。

用标准库 `unittest`，不引依赖。跑法（**用 `py`，本地 `python` 是 3.7.1**）：

    py -m unittest discover -s tests -v
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coregeek.app import handle  # noqa: E402
from coregeek.game.grid import (  # noqa: E402
    Pos,
    back_weapon_cells,
    base_cells,
    step_toward,
    wall_cells,
    weapon_cells,
)
from coregeek.game.map import Map  # noqa: E402
from coregeek.game.planner import TIME_MARGIN, WALL, WEAPON_ORDER, plan  # noqa: E402
from coregeek.game.roles import BaseRole, Pioneer, Worker  # noqa: E402
from coregeek.game.world import Turn  # noqa: E402
from coregeek.protocol import actions, model  # noqa: E402

SAMPLE = Path(__file__).resolve().parents[1] / "docs" / "request.txt"

#: 官方样例（roundNo=85）的落点。**样例是夜里**，所以两个工人都朝最近的武器走一格：
#: 10010 在 (5,23) → 最近的是 (9,24)（切比雪夫 4）；10012 在 (10,16) → 同样是 (9,24)（距离 8）。
#: 开拓者 10011 不发指令；夜里 `build`/`collect` 一条都不该有（`build` 仅白天）。
#: ⚠️ 第 7 步这里是 `{"10012": [9, 17]}`（那时夜里的兜底是"朝石矿走"，而 10010 正好贴着矿）——
#: **这条断言按设计变了**：夜里改成"回基地操炮"。
EXPECTED_MOVES = {"10010": [6, 22], "10012": [9, 17]}


class MoveWireTest(unittest.TestCase):
    def test_wire_shape_is_the_flat_record(self):
        self.assertEqual(
            actions.Move("worker", Pos(1, 2)).to_wire(),
            {"action": "move", "targetPos": [{"x": 1, "y": 2}]},
        )

    def test_move_is_allowed_for_both_roles(self):
        """§4.4 里 `move` 的可用角色是"全部" —— 别把两种角色误拦了。"""
        for role_type in ("worker", "pioneer"):
            with self.subTest(role_type=role_type):
                self.assertEqual(actions.Move(role_type, Pos(0, 0)).to_wire()["action"], "move")

    def test_build_wire_shape(self):
        """`name` 是**建筑名**（与 `roleType` 同名），不是动作名；`targetPos` 永远是数组。

        字段名照 `docs/response.txt` 里唯一那条 `build` —— 形状写错就是一次"指令非法"。
        """
        self.assertEqual(
            actions.Build("worker", "gatling", Pos(9, 23)).to_wire(),
            {"action": "build", "name": "gatling", "targetPos": [{"x": 9, "y": 23}]},
        )

    def test_collect_wire_shape(self):
        """`collect` 的 `targetPos` 是**矿的坐标**，形状与 `move` 同（只有 action + targetPos）。

        ⚠️ 这一条**没有实证报文**：`docs/response.txt` 里只有 `move`/`build`/`remove`。
        形状是从接口文档 §2.2/§2.3 推的，写错就是一次"指令非法"（直通红线）。
        """
        self.assertEqual(
            actions.Collect("worker", Pos(4, 24)).to_wire(),
            {"action": "collect", "targetPos": [{"x": 4, "y": 24}]},
        )


class GateTest(unittest.TestCase):
    """`BaseAction` 的校验机制本身 —— 这一步的主要交付物。"""

    def test_roles_classvar_rejects_unauthorized_role(self):
        class WorkerOnly(actions.BaseAction):
            code, roles = "build", actions.WORKER

            def to_wire(self):
                return {"action": self.code}

        self.assertEqual(WorkerOnly("worker").to_wire(), {"action": "build"})
        with self.assertRaises(PermissionError):
            WorkerOnly("pioneer")

    def test_buildings_cannot_act(self):
        """基地/武器/围墙不是角色，产生不了指令（payload 里它们和角色同列，别混）。"""
        for role_type in ("station", "gatling", "railgun", "rocket", "wall"):
            with self.subTest(role_type=role_type):
                with self.assertRaises(PermissionError):
                    actions.Move(role_type, Pos(0, 0))

    def test_pioneer_cannot_build(self):
        """**闸门第一次真的挡住东西**：`build` 仅在工人那一行（任务书 §4.4 最右列）。

        开拓者误发 `build` 是典型的"本地全绿"bug —— 报文格式挑不出毛病，只有判题器
        会说"不"，而那条路直通红线（5 次异常即整场不再被调度）。
        """
        with self.assertRaises(PermissionError):
            actions.Build("pioneer", "gatling", Pos(9, 23))

    def test_pioneer_cannot_collect(self):
        """`collect` 同样只在工人那一行（任务书 §4.4 最右列）。

        ⚠️ `collect` 的昼夜限制**表里没写**（`build`/`remove`/`attack` 都写了），
        所以这里只拦角色、不拦时段 —— 闸门只管"谁"。**每加一个受限动作都要补一条**，
        否则开拓者每天吃一个异常，而红线只有 5 次。
        """
        with self.assertRaises(PermissionError):
            actions.Collect("pioneer", Pos(4, 24))

    def test_typo_role_type_is_rejected(self):
        """角色类型拼错 → 造不出动作，而不是发一条判题器认不出的指令。"""
        with self.assertRaises(PermissionError):
            actions.Move("workre", Pos(0, 0))


class HandleTest(unittest.TestCase):
    """端到端：`app.handle` 是红线所在，改坏了要立刻知道。"""

    def _handle(self, raw: bytes) -> dict:
        return json.loads(handle(raw).decode("utf-8"))

    def test_sample_payload_moves_both_workers_to_a_weapon(self):
        body = self._handle(SAMPLE.read_bytes())
        self.assertEqual(set(body), {"roleCommandMap", "prompt", "executeCmd"})
        cmds = body["roleCommandMap"]
        self.assertEqual(
            {k: [v["targetPos"][0]["x"], v["targetPos"][0]["y"]] for k, v in cmds.items()},
            EXPECTED_MOVES,
        )
        # 样例是**夜里**：两个工人都只走一格，没有 build / collect（`build` 仅白天）
        self.assertEqual({v["action"] for v in cmds.values()}, {"move"})

    def test_bad_json_falls_back_to_empty_commands(self):
        """红线兜底：任何失败都退化成**合法空指令**（空指令合法且不计异常）。"""
        body = self._handle(b"{oops")
        self.assertEqual(body, {"roleCommandMap": {}, "prompt": "", "executeCmd": ""})


class ParseTest(unittest.TestCase):
    def _turn(self) -> Turn:
        turn = model.load(json.loads(SAMPLE.read_text(encoding="utf-8")))
        self.assertIsNotNone(turn)
        return turn

    def test_turn_holds_characters_only(self):
        """建筑（基地/武器/墙）不是可操控单位，不进 `roles` —— 它们只在地图网格里。"""
        turn = self._turn()
        self.assertEqual({r.type_name for r in turn.roles}, {"worker", "pioneer"})
        self.assertEqual(len(turn.roles), 3)

    def test_map_size_is_read(self):
        """寻路靠它挡界外，读错了不会有任何症状——只会静悄悄地一步不动或走出去。"""
        self.assertEqual(self._turn().map.size, (41, 32))

    def test_only_stone_enters_stones_but_everything_blocks(self):
        """石/铁/铜在网格里分得开，但**只有石矿进 `stones`**。

        小贩 / 武器商店 / 任务点**不是矿，却一样挡路**（任务书 L85）—— 只挑矿会让工人
        一头撞上去，这是"以为能走"的典型。
        """
        grid = self._turn().map
        self.assertEqual(grid.stones, {Pos(4, 24), Pos(14, 3)})
        for pos, what in (
            (Pos(25, 10), "铁"),
            (Pos(22, 26), "铜"),
            (Pos(20, 16), "小贩"),
            (Pos(25, 20), "武器商店"),
            (Pos(14, 14), "挑战者任务点1"),
            (Pos(23, 14), "防守方任务点1"),
        ):
            with self.subTest(what=what):
                self.assertIn(pos, grid.blocked, f"{what} 应当挡路")
                self.assertNotIn(pos, grid.stones, f"{what} 不是矿")


class GridTest(unittest.TestCase):
    """格子矩阵本身 —— `Turn.map` 这一步的主要交付物。

    **`cells` 才是真相，`render()` 只是给人看的**（那张字符表是有损的）。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.grid = model.load(json.loads(SAMPLE.read_text(encoding="utf-8"))).map

    def test_station_is_expanded_to_four_cells(self):
        """基地 2×2，`pos` 给的是**左上角** ⇒ 占 `y` 和 `y-1`。

        接口文档写的是"**双方**基地大小为 2*2"，所以敌我都要展。只标一格，角色会一头
        撞进基地里 —— 那是一条判题器不收的指令。**旧实现只展了我方**，敌方基地只挡了 1 格。
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
        """**这一步的核心回归**：`blocked` 必须恰好等于"非空格子"。

        `expected` 在这里**独立按任务书 L85 重算一遍**（四路来源 + 基地 2×2），完全不走
        `Map` 的代码 —— 迁移中漏掉任何一类挡路物，症状都是"以为能走、其实撞墙"。
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
        """打印出来的图给调试用 —— 错了最坑：**y 翻反了图上照样"像张地图"**。"""
        lines = self.grid.render().splitlines()
        self.assertEqual(len(lines), 32)
        self.assertEqual({len(line) for line in lines}, {41})
        # 行号 = height-1-y（y 向上、终端从上往下印）；**小写 = 我方，大写 = 敌方**
        self.assertEqual(lines[7][10], "s")  # 我方基地左上角 (10,24)
        self.assertEqual(lines[8][11], "s")  # 我方基地右下角 (11,23)
        self.assertEqual(lines[7][9], "g")  # 加特林 (9,24)，与基地同一行
        self.assertEqual(lines[6][10], "r")  # 电磁狙击炮 (10,25)，在基地方上方一行
        self.assertEqual(lines[21][30], "S")  # 敌方基地左上角 (30,10) → 第 31-10=21 行
        self.assertEqual(lines[7][4], "o")  # 石矿 (4,24)
        self.assertEqual(lines[24][28], "%")  # 敌方围墙 (28,7) —— 墙是大小写规则的例外
        self.assertEqual(lines[27][4], "x")  # 机器人 (4,4)
        self.assertEqual(lines[31][0], ".")  # (0,0) 空地 —— 最后一行是最底下的 y=0

    def test_render_degrades_on_a_map_with_no_size(self):
        """尺寸缺失 ⇒ 矩阵为空 ⇒ 不挡路也不动。**降级方向必须是"不动"**，见 `Map.__init__`。"""
        empty = Map((-1, -1), {Pos(0, 0): "wall"})
        self.assertEqual(empty.cells, ())
        self.assertEqual(empty.blocked, frozenset())
        self.assertEqual(empty.render(), "")


class BuildGeometryTest(unittest.TestCase):
    """可建造区与"基地后方"。

    ⚠️ **公式的来源是图不是正文**（`docs/pic/build_map.png`）：任务书没写坐标公式，
    接口文档的 `mapInfo` 里也没有可建造区字段。算错 ⇒ `build` 落点非法 ⇒ 那 25 金币白花。
    """

    BASE = Pos(10, 24)  # 样例里的我方基地

    def test_weapon_cells_are_the_ring_around_the_base(self):
        """12 格 = 基地外圈 4×4 减去基地自己，且**一格都不和基地重叠**。"""
        cells = weapon_cells(self.BASE)
        own = base_cells(self.BASE)
        self.assertEqual(len(cells), 12)
        self.assertEqual(set(cells) & own, set(), "武器格不能落在基地身上")
        self.assertEqual({c.x for c in cells}, {9, 10, 11, 12})
        self.assertEqual({c.y for c in cells}, {22, 23, 24, 25})
        # 样例那三座武器都该落在这个环上 —— 唯一的"外部"交叉验证
        for pos in (Pos(9, 24), Pos(9, 25), Pos(10, 25)):
            self.assertIn(pos, cells)

    def test_back_column_faces_away_from_the_robots(self):
        """基地在左半 ⇒ 取左边那一列；在右半 ⇒ 取右边那一列。

        机器人从基地**面向地图中心**的那一侧来（`docs/pic/大致地图信息.png`）。判反了
        武器就摆在迎着机器人的一侧 —— 不报错、不违规，只是白建三座。
        """
        left = back_weapon_cells(Pos(10, 24), 41)  # 左半 ⇒ 后方 x = 10-1
        self.assertEqual({c.x for c in left}, {9})
        right = back_weapon_cells(Pos(30, 10), 41)  # 右半 ⇒ 后方 x = 30+2
        self.assertEqual({c.x for c in right}, {32})
        self.assertEqual(len(left), len(right), "两侧都该是整整齐齐一列 4 格")

    def test_back_column_builds_beside_the_base_first(self):
        """同列 4 格里，和基地纵向跨度齐平的两格排前面 —— 先用基地的身体挡着。"""
        self.assertEqual(
            back_weapon_cells(Pos(10, 24), 41),
            (Pos(9, 23), Pos(9, 24), Pos(9, 22), Pos(9, 25)),
        )


class DayNightTest(unittest.TestCase):
    """日历：`build` 仅白天，判反了就会在夜里发 `build`（一次执行失败）。"""

    def _turn(self, round_no: int) -> Turn:
        return Turn(round_no=round_no, map=Map((41, 32), {}), roles=(), gold=0)

    def test_is_day_boundaries(self):
        """130 回合 1 天 = 白 70 + 夜 60（任务书 L90），`within % 130` 从 1 起数。"""
        for round_no, day in ((1, True), (70, True), (71, False), (130, False), (131, True)):
            with self.subTest(round_no=round_no):
                self.assertIs(self._turn(round_no).is_day, day)
        self.assertFalse(self._turn(-1).is_day, "roundNo 缺失 ⇒ 判成夜里 ⇒ 不建造")


class MineApproachTest(unittest.TestCase):
    """合成局面：工人真的能走到矿边、开始采，而且不来回抖。**

    **必须有基地**：矿只是砌墙的原料，基地没了就没有围墙环 ⇒ 采了也没用，工人干脆不动
    （`_ring` 返回空）。这个降级方向是有意的，所以 `_turn` 里摆了一个基地。
    """

    BASE = Pos(10, 24)
    MINE = Pos(4, 24)

    def _turn(self, worker_pos: Pos, mine_kind: str = "stone", stone: int = 0) -> Turn:
        return Turn(
            round_no=1,
            map=Map((41, 32), {self.BASE: "station", self.MINE: mine_kind}),
            roles=(Worker(1, worker_pos, stone),),
            gold=0,
        )

    def test_worker_walks_to_the_mine_and_then_harvests_in_place(self):
        """把回合串起来跑，看它**收敛**：走得到矿边，到了就原地采，不再挪。

        单帧"目标格算得对"证明不了这件事 —— 走歪、绕圈、贴住后反复抖动都是单帧看不出的。
        """
        turn = self._turn(Pos(20, 20))
        for _ in range(30):
            cmd = plan(turn).get("1")
            self.assertIsNotNone(cmd, "工人不该空着手不动：矿还在，也没到没时间的时候")
            if cmd["action"] == "collect":
                break
            spot = cmd["targetPos"][0]
            turn = turn._replace(roles=(Worker(1, Pos(spot["x"], spot["y"])),))
        else:
            self.fail("30 回合还没走到矿边，说明在原地绕圈")

        self.assertEqual(
            (cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"]),
            self.MINE,
            "`collect` 的 targetPos 是**矿的坐标**，不是自己的站位（接口文档 §2.3）",
        )
        self.assertEqual(turn.roles[0].pos.dist(self.MINE), 1, "应该停在贴着矿的那一格")
        # 贴住之后就该一直采：采一下、挪一下地反复抖 = 白白浪费白天
        for _ in range(3):
            self.assertEqual(plan(turn)["1"]["action"], "collect", "贴住矿之后不该再挪")

    def test_no_stone_mine_means_no_action(self):
        """场上只有铁矿 → 工人原地不动，而不是随便找个矿走过去（墙只吃石头）。"""
        self.assertEqual(plan(self._turn(Pos(20, 20), "iron")), {})


class BuildWeaponTest(unittest.TestCase):
    """合成开局：白天、0 武器、基地在左半 —— 工人真的会建满三座、建在该建的地方、然后收手。

    **把回合串起来跑**：单帧"目标格算得对"证明不了收不收得住（`MineApproachTest` 同理）。
    结算照判题器的口径来 —— 一回合一步，建起来的武器下一回合就挡路、也占掉那个格子。
    """

    BASE = Pos(10, 24)  # 后方那一列 = x=9，y ∈ 22..25
    MINE = Pos(4, 24)

    def setUp(self) -> None:
        self.gold = 75  # 开局：恰好买满三座（25×3）
        self.entries: dict[Pos, str] = {self.BASE: "station", self.MINE: "stone"}
        self.roles: dict[int, BaseRole] = {
            1: Pioneer(1, Pos(20, 20)),
            # 两个工人都**贴着**后方那一列（各差一格），第 1 回合就能动手
            2: Worker(2, Pos(8, 23)),
            3: Worker(3, Pos(8, 24)),
        }
        self.builds: list[tuple[str, Pos]] = []

    def _turn(self, round_no: int = 1) -> Turn:
        return Turn(
            round_no=round_no,
            map=Map((41, 32), self.entries),
            roles=tuple(self.roles.values()),
            gold=self.gold,
        )

    def _weapons(self) -> list[str]:
        return [name for name, _ in self.builds if name != WALL]

    def _settle(self, round_no: int = 1, limit: int = 20) -> None:
        """跑到**武器建满**为止。墙不归这个类管（见 `BuildWallTest`），但会顺路砌起来 ——
        所以 `builds` 里混着墙，统计武器时要滤掉，金币也只按武器扣。"""
        for _ in range(limit):
            if len(self._weapons()) >= len(WEAPON_ORDER):
                return
            cmds = plan(self._turn(round_no))
            if not cmds:
                return
            for key, cmd in cmds.items():
                role_id = int(key)
                target = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
                if cmd["action"] == "build":
                    self.builds.append((cmd["name"], target))
                    self.entries[target] = cmd["name"]
                    if cmd["name"] != WALL:
                        self.gold -= 25
                else:
                    self.roles[role_id] = type(self.roles[role_id])(role_id, target)
        self.fail(f"{limit} 回合还没把三座武器建完，说明在原地绕圈")

    def _builds(self, cmds: dict) -> list[dict]:
        return [c for c in cmds.values() if c["action"] == "build"]

    def test_builds_three_weapons_in_the_chosen_order(self):
        """加特林 → 电磁狙击炮 → 火箭（用户选定），三座都落在基地后方那一列。"""
        self._settle()
        self.assertEqual(self._weapons(), list(WEAPON_ORDER))
        self.assertEqual({cell.x for _, cell in self.builds}, {9}, "都该在基地后方")
        self.assertEqual(self.gold, 0)

    def test_stops_at_three_even_with_lots_of_gold(self):
        """停手是因为**份额**（角色数），不是因为钱花光了 —— "建立多了没有意义"。"""
        self.gold = 200
        self._settle()
        self.assertEqual(len(self._weapons()), 3)
        self.assertGreater(self.gold, 0, "这次不是钱见底才停的")

    def test_a_short_budget_builds_only_one(self):
        """只有 25 金：**只建一座**，另一个工人转去采矿。

        金币按**递减预算**扣。写成 `gold >= 25 * 待建数`（25 < 75 ⇒ 一座都不建）就全错了。
        """
        self.gold = 25
        cmds = plan(self._turn())
        builds = self._builds(cmds)
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0]["name"], "gatling")
        self.assertEqual({c["action"] for c in cmds.values()}, {"build", "move"})

    def test_never_builds_on_an_occupied_cell(self):
        """目标格已有武器就**换个格子**：覆盖会把原武器打成 level1（§4.5.1 补充说明），
        25 金币打水漂还倒亏一座。"""
        self.entries[Pos(9, 23)] = "gatling"
        self.entries[Pos(9, 24)] = "railgun"
        builds = self._builds(plan(self._turn()))
        self.assertEqual(len(builds), 1, "还差一座火箭")
        self.assertEqual(builds[0]["name"], "rocket")
        self.assertEqual(
            Pos(builds[0]["targetPos"][0]["x"], builds[0]["targetPos"][0]["y"]),
            Pos(9, 22),
            "该躲开 (9,23)/(9,24)，取剩下的首选",
        )

    def test_night_builds_nothing(self):
        """夜里 `build` 不可用（任务书 §4.4）—— 0 武器、75 金也一座都不许建。"""
        self.assertNotIn("build", {c["action"] for c in plan(self._turn(round_no=85)).values()})

    def test_no_base_means_no_build(self):
        """基地没了就没有可建造区（坐标全由它推）⇒ 不建，而不是瞎猜一个坐标。"""
        del self.entries[self.BASE]
        self.assertNotIn("build", {c["action"] for c in plan(self._turn()).values()})


class WallRingTest(unittest.TestCase):
    """围墙环的几何与建造顺序。

    ⚠️ **公式的来源是图不是正文**（`docs/pic/build_map.png`）：任务书只说了"蓝色区域只能建造武器、
    黄色区域只能建造围墙"，没有任何坐标。算错 ⇒ `build` 落点非法。
    """

    BASE = Pos(10, 24)

    def test_ring_is_sixteen_cells_free_of_the_base_and_the_weapons(self):
        cells = wall_cells(self.BASE, 41)
        self.assertEqual(len(cells), 16, "6×6 边框 20 格减去缺口的 4 格")
        self.assertEqual(len(set(cells)), 16, "不该有重复格")
        self.assertEqual(set(cells) & base_cells(self.BASE), set(), "不能落在基地身上")
        self.assertEqual(set(cells) & set(weapon_cells(self.BASE)), set(), "不能占武器环")

    def test_the_gap_is_the_back_columns_middle_four(self):
        """缺口 = **背面那一列**（背离机器人的一侧）的中间 4 格，上下两角仍要砌。

        用户选定：环一旦闭合，工人就进出不得了 —— 既采不了矿，也回不到环内操炮。
        """
        cells = set(wall_cells(self.BASE, 41))
        back = {Pos(8, y) for y in range(21, 27)}  # 基地在左半 ⇒ 背面是 x = bx-2
        self.assertEqual(back - cells, {Pos(8, y) for y in range(22, 26)}, "缺口是中间四格")
        self.assertEqual(cells & back, {Pos(8, 21), Pos(8, 26)}, "背面只留上下两角")

    def test_the_first_six_cells_face_the_robots(self):
        """前 6 格 = **迎着机器人**的那一列，且正对基地纵深中心的两格最先。

        策略指导：「墙建立在**面向机器人进攻的方向**（保护基地）」。判反了墙就砌在机器人
        不来的一侧 —— 不报错、不违规，只是整段白砌，而且石头是工人一块块背回来的。
        """
        first = wall_cells(self.BASE, 41)[:6]
        self.assertEqual(set(first), {Pos(13, y) for y in range(21, 27)}, "左半 ⇒ 正面是 bx+3")
        self.assertEqual(first[:2], (Pos(13, 23), Pos(13, 24)), "正对基地纵深的两格先砌")

    def test_the_ring_mirrors_for_a_right_half_base(self):
        """基地在右半 ⇒ 正面是 `bx-2`、背面是 `bx+3`（换边后自动跟着翻）。

        按**基地坐标**判而不用 `teamOur.type` —— 下半场换边后队伍身份不变、基地会挪。
        """
        ring = wall_cells(Pos(30, 10), 41)
        self.assertEqual({c.x for c in ring[:6]}, {28}, "右半 ⇒ 正面是 bx-2")
        self.assertEqual(ring[-2:], (Pos(33, 7), Pos(33, 12)), "背面 = bx+3 那一列的上下角")


class BuildWallTest(unittest.TestCase):
    """合成开局：白天、武器已建满 → 工人去采石、回来砌墙，落点严格按优先级。

    **把白天串起来跑**：单帧看不出"采够了没有、砌到哪一格、会不会来回抖"。
    每回合重算预算，所以必须真的过一遍时间。
    """

    BASE = Pos(10, 24)
    MINE = Pos(4, 24)  # 基地左侧的石矿
    #: 三座武器先摆好 —— 否则金币/名额会先把工人抽去建武器（那是 `BuildWeaponTest` 的事）
    WEAPONS = {Pos(9, 23): "gatling", Pos(9, 24): "railgun", Pos(9, 22): "rocket"}

    def setUp(self) -> None:
        self.entries: dict[Pos, str] = {self.BASE: "station", self.MINE: "stone", **self.WEAPONS}
        self.worker = Worker(1, Pos(6, 24))

    def _turn(self, round_no: int = 1, stone: int = 0, pos: Pos | None = None) -> Turn:
        self.worker = Worker(1, pos or self.worker.pos, stone)
        return Turn(
            round_no=round_no,
            map=Map((41, 32), self.entries),
            roles=(self.worker,),
            gold=0,
        )

    def test_day_one_mines_then_walls_the_whole_ring_in_order(self):
        """把白天串起来跑到砌满：**16 格全砌上，且顺序与 `wall_cells` 逐格一致**。

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
                # 石头是**攒够了才回来砌**的（这正是回合预算要的效果：少跑冤枉路），
                # 所以这里只要求"有得扣"，扣多少由下面每砌一座减 1 来核。
                self.assertGreaterEqual(stone, 1, "砌一座墙的代价是**石头×1**，背包里得有")
                self.entries[cell] = WALL
                built.append(cell)
                stone -= 1
                self.worker = Worker(1, self.worker.pos, stone)
            else:
                self.worker = Worker(1, cell, stone)

        self.assertEqual(built, list(wall_cells(self.BASE, 41)), "顺序必须与优先级表一致")
        self.assertEqual(stone, 0, "别多采 —— 白天总共就 16 格墙可砌")

    def test_a_late_start_stops_mining_and_goes_to_build(self):
        """白天快过完了（roundNo=60 ⇒ 只剩 11 回合）⇒ **不再采矿**，拿着手里的石头直接去工地。

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

    def test_a_finished_ring_stops_the_mining(self):
        """16 格都砌满了 ⇒ 不再采石（多采的石头只会压在背包里），也不再发任何指令。"""
        self.entries.update({c: WALL for c in wall_cells(self.BASE, 41)})
        self.assertEqual(plan(self._turn(stone=0)), {})

    def test_no_base_means_no_wall_and_no_mining(self):
        """基地没了就没有围墙环（坐标全由它推）⇒ 连矿都不去采，而不是瞎找一个坐标。"""
        del self.entries[self.BASE]
        self.assertEqual(plan(self._turn(stone=0)), {})


class TwoWallBuildersTest(unittest.TestCase):
    """两个工人同时在环上：**不能对着改目标来回踱步**。

    实测过的死循环（第 8 步）：`_ring` 原本把"工人脚下那一格"也当成挡路的格划掉，
    于是另一个工人的 `free[0]` 整体后移一格、掉头去砌更靠后的墙；等前一个工人一挪窝，
    目标又变回来 —— 两人在两格之间一直转到天黑，**一格墙都没砌**（端到端局面上 26 回合 0 座）。
    单帧断言看不出来，必须把回合串起来跑。

    ⚠️ **角色必须自己铺进地图**：真实路径里 `protocol.model._entries` 会把 `teamOur.roles`
    写进网格，于是"谁站在哪"进 `blocked`。手搓 `Map` 时不铺角色就复现不出这个循环 ——
    本用例第一版正是这么写的，于是它连旧代码都放过去了。
    """

    BASE = Pos(10, 24)
    MINE = Pos(4, 24)
    WEAPONS = {Pos(9, 23): "gatling", Pos(9, 24): "railgun", Pos(9, 22): "rocket"}
    #: 死循环当时的局面：正面列已砌三格，两个工人卡在基地与正面列之间的夹缝里
    FRONT_BUILT = {Pos(13, 22): WALL, Pos(13, 23): WALL, Pos(13, 24): WALL}

    def test_two_workers_keep_building_in_the_pocket(self):
        entries = {self.BASE: "station", self.MINE: "stone", **self.WEAPONS, **self.FRONT_BUILT}
        roles = {
            10010: Worker(10010, Pos(13, 21), 14),
            10012: Worker(10012, Pos(11, 22), 13),
        }
        built: list[Pos] = []
        for rnd in range(1, 7):  # 6 回合足够砌完剩下的正面列（死循环下一次都砌不上）
            # 角色压在静态层之上 —— 与 `model._entries` 的写入顺序一致（单位盖过地形）
            grid = {**entries, **{r.pos: "worker" for r in roles.values()}}
            turn = Turn(round_no=rnd, map=Map((41, 32), grid), roles=tuple(roles.values()), gold=0)
            for key, cmd in plan(turn).items():
                role_id = int(key)
                cell = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
                role = roles[role_id]
                if cmd["action"] == "build":
                    self.assertEqual(cmd["name"], WALL)
                    entries[cell] = WALL
                    built.append(cell)
                    roles[role_id] = Worker(role_id, role.pos, role.stone - 1)
                elif cmd["action"] == "collect":
                    roles[role_id] = Worker(role_id, role.pos, role.stone + 1)
                else:
                    roles[role_id] = Worker(role_id, cell, role.stone)

        self.assertGreaterEqual(len(built), 2, "两个工人在原地打转 ⇒ 一座墙都砌不上")
        self.assertEqual({c.x for c in built}, {13}, "先砌的必须是**正面**那一列")
        # 只钉"这两格必须补上"：再往后铺到 (13,26) 也是对的（同属正面列），不必锁死集合
        self.assertTrue({Pos(13, 21), Pos(13, 25)} <= set(built), "夹缝里那两格得砌上")


class NightWeaponTest(unittest.TestCase):
    """夜里：工人回基地、走到武器旁边待命。`attack`（真的开火）是**下一步**。"""

    BASE = Pos(10, 24)
    NIGHT = 85
    NEAR, FAR = Pos(12, 25), Pos(9, 22)

    def _turn(self, *workers: Worker, weapons: tuple[Pos, ...] | None = None) -> Turn:
        # `weapons=None` 才是"用默认的两座"；**不能用 `or`** —— 空元组也是假值，
        # 那样 `test_no_weapons_means_nothing_to_do` 会静默拿到两座武器、白测一场。
        weapons = (self.NEAR, self.FAR) if weapons is None else weapons
        return Turn(
            round_no=self.NIGHT,
            map=Map((41, 32), {self.BASE: "station", **{p: "gatling" for p in weapons}}),
            roles=workers,
            gold=0,
        )

    def test_walks_toward_the_nearest_weapon_and_never_builds(self):
        """夜里 `build` 不可用（任务书 §4.4）—— 一条 `build`/`collect` 都不该有。"""
        cmds = plan(self._turn(Worker(1, Pos(14, 26))))
        self.assertEqual(set(cmds), {"1"})
        self.assertEqual(cmds["1"]["action"], "move")
        cell = Pos(cmds["1"]["targetPos"][0]["x"], cmds["1"]["targetPos"][0]["y"])
        self.assertEqual(cell, Pos(13, 25), "朝最近的 (12,25) 迈一步，停在它旁边")

    def test_two_workers_do_not_share_a_weapon(self):
        """一人只能操一座武器 —— 两个工人都挤同一座，等于白白少一座火力。"""
        cmds = plan(self._turn(Worker(1, Pos(14, 26)), Worker(2, Pos(14, 24))))
        second = Pos(cmds["2"]["targetPos"][0]["x"], cmds["2"]["targetPos"][0]["y"])
        # 两个工人到 (12,25) 都是 2 格，最近的**都是它**；没有去重的话 2 号也会奔它去
        self.assertGreater(second.dist(self.NEAR), 1, "2 号必须换一座，不能也去挤 (12,25)")
        self.assertLess(second.dist(self.FAR), 5, "换的那座该是下一近的 (9,22)")

    def test_no_weapons_means_nothing_to_do(self):
        """武器全没了 ⇒ 不动（空指令合法），而不是瞎走。"""
        self.assertEqual(plan(self._turn(Worker(1, Pos(14, 26)), weapons=())), {})


class PathTest(unittest.TestCase):
    """寻路：BFS 最短路。**这两个局面上贪心版都会挂** —— 换掉它的理由就在这。"""

    #: 合成局面里也得摆个基地，否则 `_ring` 是空的、工人压根不动（矿只服务于砌墙）
    BASE = Pos(20, 20)

    def _walk(self, turn: Turn, limit: int) -> tuple[int, Turn]:
        """把回合串起来走，返回 `(实际走了几步, 走完的局面)`。

        **发 `collect` 就算走到了** —— 那正是"贴着矿"的唯一标志。
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

        贪心会在 `(4,4)` 停死：那里**没有任何一格更近**（更近的三格全在 y=3 的墙上），
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
        """地图边界**不在任务书 L85 的阻挡清单里**，得自己挡。

        整列 `x=1` 封死，工人被关在 `x=0` 这一列；能到达的格子没有一个贴着目标。
        **去掉边界检查这里会返回 `(-1,4)`** —— 一条走出地图的指令。
        """
        goal = Pos(0, 1)
        blocked = {Pos(1, y) for y in range(10)}  # 整列封死
        blocked |= {Pos(0, y) for y in (2, 3, 4)}  # 再堵掉本列的直路
        blocked.add(goal)

        self.assertIsNone(step_toward(Pos(0, 5), goal, frozenset(blocked), (10, 10)))


if __name__ == "__main__":
    unittest.main()
