"""protocol/actions.py 的用例：动作的权限闸门（角色不对根本造不出动作）与线上报文形状。
权限类 bug 的特征是本地全绿——格式完全合法，只有判题器会说"不"，而那条路直通红线
（累计 5 次异常即整场不再被调度）。"格式对"证明不了"这角色有权这么做"，必须单独钉。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import sys
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用（discover 不一定把它放进 sys.path）；src/ 给 coregeek 用
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coregeek.game.grid import Pos  # noqa: E402
from coregeek.protocol import actions  # noqa: E402


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
        """`name` 是建筑名（与 `roleType` 同名），不是动作名；`targetPos` 永远是数组。

        字段名照 `docs/response.txt` 里唯一那条 `build` —— 形状写错就是一次"指令非法"。
        """
        self.assertEqual(
            actions.Build("worker", "gatling", Pos(9, 23)).to_wire(),
            {"action": "build", "name": "gatling", "targetPos": [{"x": 9, "y": 23}]},
        )

    def test_remove_wire_shape(self):
        """逐字对照 `docs/response.txt` 里那条实证报文 —— 里面没有 `name`。

        `remove` 与 `build` 长得最像（都要指定 `targetPos`），照 `build` 抄一份、多打一个
        `name` 就是赌一次"指令非法" —— 所以专门钉住"没有它"。
        """
        wire = actions.Remove("worker", Pos(29, 7)).to_wire()
        self.assertEqual(wire, {"action": "remove", "targetPos": [{"x": 29, "y": 7}]})
        self.assertNotIn("name", wire, "`build` 才有 `name`；多一个字段就是指令非法")

    def test_collect_wire_shape(self):
        """`collect` 的 `targetPos` 是矿石的坐标，形状与 `move` 同（只有 action + targetPos）。

        没有实证报文（`docs/response.txt` 只有 move/build/remove），形状从接口文档
        §2.2/§2.3 推 —— 写错就是一次"指令非法"。
        """
        self.assertEqual(
            actions.Collect("worker", Pos(4, 24)).to_wire(),
            {"action": "collect", "targetPos": [{"x": 4, "y": 24}]},
        )

    def test_attack_wire_shape(self):
        """逐字对照 `docs/response.txt` 里唯一一条 `attack` 实证报文。

        这个对象是"武器 10020 由角色 10010 操控"，`to_wire()` 编的是挂在武器 id 下的
        那条记录（key 由 `planner._emit` 给）。字段名 `controllerId` 写错
        （`controllerID` / `roleId`）就是一次"指令非法"。
        """
        wire = actions.Attack("worker", "10010", Pos(29, 7)).to_wire()
        self.assertEqual(
            wire,
            {"action": "attack", "controllerId": "10010", "targetPos": [{"x": 29, "y": 7}]},
        )
        self.assertIsInstance(wire["controllerId"], str, "接口文档标的是 String")

    def test_attack_target_pos_is_always_one_point(self):
        """`targetPos` 数量 = 武器等级数；`Attack` 恒编一格（L1 的口径）。"""
        for role_type in ("worker", "pioneer"):
            with self.subTest(role_type=role_type):
                self.assertEqual(len(actions.Attack(role_type, "7", Pos(1, 1)).to_wire()["targetPos"]), 1)

    def test_sell_wire_shape(self):
        """`{"action":"sell","name":<矿种>,"num":<件数>}`：`num` 是 Int（§2.2，不填默认 1），
        `name` 取矿种（与 `neutralType` / `vendorShopList.name` / 背包物品名同一套词），
        不是动作名。没有实证报文，形状从接口文档推。`attack` 的 `controllerId` 标 String、
        这里标 Int，两者相反 ⇒ 两处各钉一次类型（抄错方向 = 一次"指令非法"）。
        """
        wire = actions.Sell("worker", "copper", 4).to_wire()
        self.assertEqual(wire, {"action": "sell", "name": "copper", "num": 4})
        self.assertIsInstance(wire["num"], int)
        self.assertNotIsInstance(wire["num"], bool)

    def test_buy_wire_shape(self):
        """`buy` 在武器商店旁用（§4.4：全部角色）：`name` = 商品名
        （`weaponShopList.name` 那套词）、`num` 是 Int（不填默认 1，支持批量）。
        没有实证报文，形状从接口文档推。
        """
        wire = actions.Buy("worker", "WeaponUpgradeVoucher1", 1).to_wire()
        self.assertEqual(wire, {"action": "buy", "name": "WeaponUpgradeVoucher1", "num": 1})
        self.assertIsInstance(wire["num"], int)

    def test_use_wire_shape(self):
        """`use` 用升级券：`name` + `targetPos` —— 券须站在目标建筑周围一格内并指定
        目标位置（任务书 L292）。已 level3 再用不生效、非法使用不消耗（L293-294）
        ⇒ 发错顶多白跑一趟，不碰红线。没有实证报文，形状从接口文档推。"""
        self.assertEqual(
            actions.Use("worker", "WeaponUpgradeVoucher1", Pos(12, 22)).to_wire(),
            {"action": "use", "name": "WeaponUpgradeVoucher1", "targetPos": [{"x": 12, "y": 22}]},
        )

    def test_buy_and_use_are_allowed_for_both_roles(self):
        """§4.4 最右列：buy / use 都是全部角色（开拓者也能用）。"""
        for role_type in ("worker", "pioneer"):
            with self.subTest(role_type=role_type):
                self.assertEqual(
                    actions.Buy(role_type, "WallFixer", 1).to_wire()["action"], "buy"
                )
                self.assertEqual(
                    actions.Use(role_type, "WallFixer", Pos(1, 1)).to_wire()["action"], "use"
                )


class GateTest(unittest.TestCase):
    """`BaseAction` 的校验机制本身：闸门在构造函数里。"""

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
        """`build` 仅在工人那一行（任务书 §4.4 最右列）。

        开拓者误发 `build` 是典型的"本地全绿"bug —— 报文格式挑不出毛病，只有判题器
        会说"不"，而那条路直通红线。
        """
        with self.assertRaises(PermissionError):
            actions.Build("pioneer", "gatling", Pos(9, 23))

    def test_pioneer_cannot_collect(self):
        """`collect` 同样仅在工人那一行（任务书 §4.4 最右列）。

        它的昼夜限制表里没写（`build`/`remove`/`attack` 都写了）⇒ 闸门只拦角色、
        不拦时段，只管"谁"。每加一个受限动作都要在这里补一条。
        """
        with self.assertRaises(PermissionError):
            actions.Collect("pioneer", Pos(4, 24))

    def test_pioneer_cannot_remove(self):
        """`remove` 同样仅在工人那一行（任务书 §4.4 最右列）。

        拆墙真的会由工人发 —— 一旦闸门写成"全部角色"，开拓者某天顺手拆一格
        就是一次异常。
        """
        with self.assertRaises(PermissionError):
            actions.Remove("pioneer", Pos(13, 23))

    def test_typo_role_type_is_rejected(self):
        """角色类型拼错 → 造不出动作，而不是发一条判题器认不出的指令。"""
        with self.assertRaises(PermissionError):
            actions.Move("workre", Pos(0, 0))

    def test_attack_is_allowed_for_both_roles(self):
        """§4.4 里 `attack` 的可用角色是全部 —— 开拓者也上炮位，别误窄成工人
        （反过来的 bug 本地全绿，只是整夜少一门火力）。"""
        for role_type in ("worker", "pioneer"):
            with self.subTest(role_type=role_type):
                self.assertEqual(actions.Attack(role_type, "10010", Pos(1, 1)).to_wire()["action"], "attack")

    def test_worker_cannot_accept_a_task(self):
        """`acceptTask` 只在开拓者那一行（任务书 §4.4 最右列）。

        它的报文只有一个 `action` 字段、格式挑不出毛病 ⇒ "本地全绿"bug 最容易藏在这里。
        每加一个受限动作都要补一条。
        """
        with self.assertRaises(PermissionError):
            actions.AcceptTask("worker")

    def test_worker_cannot_submit_an_answer(self):
        """`submitAnswer` 同样只在开拓者那一行（任务书 §4.4 最右列）。"""
        with self.assertRaises(PermissionError):
            actions.SubmitAnswer("worker", "晴 26 度")

    def test_sell_is_allowed_for_both_roles(self):
        """§4.4 里 `sell` 的可用角色是全部（开拓者一样能卖），与 `build`/`collect` 相反。

        反方向的 bug（把开拓者挡在门外）本地永远测不出来 —— 报文格式完全合法，
        只有判题器会说"不"。与上面那两条成对：`build`/`collect` 钉"窄得对"，
        这里钉"窄不得"。
        """
        for role_type in ("worker", "pioneer"):
            with self.subTest(role_type=role_type):
                self.assertEqual(
                    actions.Sell(role_type, "stone", 1).to_wire()["action"], "sell"
                )


if __name__ == "__main__":
    unittest.main()
