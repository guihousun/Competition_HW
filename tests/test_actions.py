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
from coregeek.game.grid import Pos, step_toward  # noqa: E402
from coregeek.game.planner import plan  # noqa: E402
from coregeek.game.roles import Worker  # noqa: E402
from coregeek.game.world import Turn  # noqa: E402
from coregeek.protocol import actions, model  # noqa: E402

SAMPLE = Path(__file__).resolve().parents[1] / "docs" / "request.txt"

#: 官方样例（roundNo=85）里工人朝石矿走一格的落点。
#: 石矿在 (4,24) / (14,3)：10010 已经在 (5,23) —— **贴着 (4,24)，所以它不动作**；
#: 10012 从 (10,16) 朝最近的 (4,24) 走一格。开拓者 10011 这一步不发指令。
EXPECTED_MOVES = {"10012": [9, 17]}


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

    def test_typo_role_type_is_rejected(self):
        """角色类型拼错 → 造不出动作，而不是发一条判题器认不出的指令。"""
        with self.assertRaises(PermissionError):
            actions.Move("workre", Pos(0, 0))


class HandleTest(unittest.TestCase):
    """端到端：`app.handle` 是红线所在，改坏了要立刻知道。"""

    def _handle(self, raw: bytes) -> dict:
        return json.loads(handle(raw).decode("utf-8"))

    def test_sample_payload_moves_only_the_far_worker(self):
        body = self._handle(SAMPLE.read_bytes())
        self.assertEqual(set(body), {"roleCommandMap", "prompt", "executeCmd"})
        cmds = body["roleCommandMap"]
        self.assertEqual(
            {k: [v["targetPos"][0]["x"], v["targetPos"][0]["y"]] for k, v in cmds.items()},
            EXPECTED_MOVES,
        )
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

    def test_turn_holds_characters_only_and_still_finds_station(self):
        turn = self._turn()
        self.assertEqual({r.type_name for r in turn.roles}, {"worker", "pioneer"})
        self.assertEqual(len(turn.roles), 3)
        self.assertEqual(turn.station, Pos(10, 24))  # 基地拿的是左上角

    def test_map_size_is_read(self):
        """寻路靠它挡界外，读错了不会有任何症状——只会静悄悄地一步不动或走出去。"""
        self.assertEqual(self._turn().size, (41, 32))

    def test_mines_are_parsed_by_kind(self):
        """石/铁/铜分开；小贩、武器商店、任务点**不是**矿，别混进来。"""
        mines = self._turn().mines
        self.assertEqual({p for p, k in mines.items() if k == "stone"}, {Pos(4, 24), Pos(14, 3)})
        self.assertEqual(len(mines), 6)  # 样例里三种矿各 2 座

    def test_mines_block_movement(self):
        """矿格挡路（任务书 L85）——工人只能站在旁边，不能站上去。"""
        self.assertIn(Pos(4, 24), self._turn().blocked)


class MineApproachTest(unittest.TestCase):
    """合成局面：工人真的能走到矿边并停下。单帧看着对，不代表走得过去停得住。"""

    MINE = Pos(4, 24)

    def _turn(self, worker_pos: Pos, mines: dict[Pos, str] | None = None) -> Turn:
        mines = {self.MINE: "stone"} if mines is None else mines
        return Turn(
            round_no=1,
            size=(41, 32),
            roles=(Worker(1, worker_pos),),
            blocked=frozenset(mines),  # 矿格挡路，正如真实 payload
            station=None,
            mines=mines,
        )

    def test_worker_walks_to_the_mine_and_then_stops(self):
        """把回合串起来跑，看它**收敛**：走得到，且到了就不再动。

        单帧"目标格算得对"证明不了这件事 —— 走歪、绕圈、贴住后反复抖动都是单帧看不出的。
        """
        turn = self._turn(Pos(20, 20))
        for _ in range(40):
            cmds = plan(turn)
            if not cmds:
                break
            target = cmds["1"]["targetPos"][0]
            turn = turn._replace(roles=(Worker(1, Pos(target["x"], target["y"])),))
        else:
            self.fail("40 回合还没走到矿边，说明在原地绕圈")

        self.assertEqual(turn.roles[0].pos.dist(self.MINE), 1, "应该停在贴着矿的那一格")
        self.assertEqual(plan(turn), {}, "贴着矿之后不该再动")

    def test_no_stone_mine_means_no_action(self):
        """场上只有铁矿 → 工人原地不动，而不是随便找个矿走过去。"""
        self.assertEqual(plan(self._turn(Pos(20, 20), {self.MINE: "iron"})), {})


class PathTest(unittest.TestCase):
    """寻路：BFS 最短路。**这两个局面上贪心版都会挂** —— 换掉它的理由就在这。"""

    def _walk(self, turn: Turn, limit: int) -> tuple[int, Turn]:
        """把回合串起来走，返回 `(实际走了几步, 走完的局面)`。"""
        for moves in range(limit + 1):
            cmds = plan(turn)
            if not cmds:
                return moves, turn
            t = cmds["1"]["targetPos"][0]
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
            size=(12, 12),
            roles=(Worker(1, Pos(5, 5)),),
            blocked=frozenset(wall | {mine}),
            station=None,
            mines={mine: "stone"},
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
