"""game/planner.py **夜间线**的用例：回炮位、认领与最大伤害落点开火。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。⚠️ 用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import sys
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用（discover 不一定把它放进 sys.path）；src/ 给 coregeek 用
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _fixtures import _terrain  # noqa: E402
from coregeek.game.grid import Pos  # noqa: E402
from coregeek.game.map import Map  # noqa: E402
from coregeek.game.planner import plan  # noqa: E402
from coregeek.game.roles import BaseRole, Pioneer, Worker  # noqa: E402
from coregeek.game.world import Robot, Turn, Weapon  # noqa: E402
from coregeek.protocol import model  # noqa: E402


class NightWeaponTest(unittest.TestCase):
    """夜里：**所有角色**（含开拓者）回炮位、开火打**最大伤害落点**（第 27 步起，
    用户改的方针：目标是**打死所有机器人**，不再是"补刀残血"）。

    `attack` **仅黑夜**可用、`build`/`collect` 仅工人（任务书 §4.4），所以夜里
    除了 `move` 就只该有 `attack`。
    """

    BASE = Pos(10, 24)
    NIGHT = 85
    NEAR, FAR = Pos(12, 25), Pos(9, 22)  # 两座加特林
    REACH = 4  # 加特林 L1 的射程，**取自样例 payload**（任务书表格写的是 3）
    GUN = 10020  # `_manned` 那座炮的 id —— `attack` 的 key 就是它

    def _turn(
        self,
        *roles: BaseRole,
        weapons: tuple[Weapon, ...] | None = None,
        robots: tuple[Robot, ...] = (),
        round_no: int | None = None,
    ) -> Turn:
        # `weapons=None` 才是"用默认的两座"；**不能用 `or`** —— 空元组也是假值，
        # 那样 `test_no_weapons_means_nothing_to_do` 会静默拿到两座武器、白测一场。
        weapons = self._guns(self.NEAR, self.FAR) if weapons is None else weapons
        return Turn(
            round_no=self.NIGHT if round_no is None else round_no,
            map=Map((41, 32), _terrain(weapons, {self.BASE: "station"})),
            roles=roles,
            gold=0,
            weapons=weapons,
            robots=robots,
        )

    def _guns(self, *cells: Pos) -> tuple[Weapon, ...]:
        """按样例的 L1 加特林造记录（射程 4、无冷却），id 从 `GUN` 起编号 ——
        断言里要能一眼看出"开火的是哪一座"，所以第一座就取 `GUN`。"""
        return tuple(
            Weapon(id=self.GUN + i, kind="gatling", pos=cell, attack_range=self.REACH, cooldown=0)
            for i, cell in enumerate(cells)
        )

    def _manned(
        self,
        *robots: Robot,
        reach: int | None = None,
        cooldown: int = 0,
        round_no: int | None = None,
        kind: str = "gatling",
    ) -> Turn:
        """一名工人**已经贴着**那座炮（切比雪夫 1）—— 开火与否只看目标与冷却。"""
        gun = Weapon(
            id=self.GUN,
            kind=kind,
            pos=self.NEAR,
            attack_range=self.REACH if reach is None else reach,
            cooldown=cooldown,
        )
        return self._turn(
            Worker(1, Pos(12, 24)), weapons=(gun,), robots=robots, round_no=round_no
        )

    def _only_cmd(self, turn: Turn) -> dict:
        cmds = plan(turn)
        self.assertEqual(len(cmds), 1, f"应当恰好一条指令，实际 {cmds}")
        return next(iter(cmds.values()))

    def test_walks_toward_the_nearest_weapon_and_never_builds(self):
        """夜里 `build` 不可用（任务书 §4.4）—— 一条 `build`/`collect` 都不该有。"""
        cmds = plan(self._turn(Worker(1, Pos(14, 26))))
        self.assertEqual(set(cmds), {"1"})
        self.assertEqual(cmds["1"]["action"], "move")
        cell = Pos(cmds["1"]["targetPos"][0]["x"], cmds["1"]["targetPos"][0]["y"])
        self.assertEqual(cell, Pos(13, 25), "朝最近的 (12,25) 迈一步，停在它旁边")

    def test_two_roles_do_not_share_a_weapon(self):
        """一人只能操一座武器 —— 两个角色都挤同一座，等于白白少一门火力。"""
        cmds = plan(self._turn(Worker(1, Pos(14, 26)), Worker(2, Pos(14, 24))))
        second = Pos(cmds["2"]["targetPos"][0]["x"], cmds["2"]["targetPos"][0]["y"])
        # 两个角色到 (12,25) 都是 2 格，最近的**都是它**；没有去重的话 2 号也会奔它去
        self.assertGreater(second.dist(self.NEAR), 1, "2 号必须换一座，不能也去挤 (12,25)")
        self.assertLess(second.dist(self.FAR), 5, "换的那座该是下一近的 (9,22)")

    def test_no_weapons_means_nothing_to_do(self):
        """武器全没了 ⇒ 不动（空指令合法），而不是瞎走。"""
        self.assertEqual(plan(self._turn(Worker(1, Pos(14, 26)), weapons=())), {})

    def test_the_pioneer_also_mans_a_weapon(self):
        """**开拓者也在炮位上**（§4.4 里 `attack` 的可用角色是"全部"）。

        用户这一步的原话是"**所有**角色（上限三个）回来操作武器"。开拓者漏掉的话
        本地全绿，只是整夜少一门火力 —— 而它恰恰是离炮最远的那个。
        """
        cmds = plan(
            self._turn(
                Pioneer(1, Pos(12, 24)),  # 贴着 NEAR
                Worker(2, Pos(9, 21)),  # 贴着 FAR（射程内没有敌人 ⇒ 它不开火）
                robots=(Robot(Pos(12, 27), 40),),
            )
        )
        self.assertEqual(set(cmds), {str(self.GUN)}, "开拓者开的那一炮在不在？")
        self.assertEqual(cmds[str(self.GUN)]["controllerId"], "1", "操控者是开拓者")

    def test_the_command_hangs_on_the_weapon_id(self):
        """⚠️ **`attack` 的 key 是武器 id**，操控角色在 `controllerId` 里（`docs/response.txt`）。

        写成 `{"1": …}`（角色 id）本地照样绿，判题器看到的却是"角色 1 在操炮" —— 一次"指令非法"。
        """
        cmd = self._only_cmd(self._manned(Robot(Pos(12, 27), 40)))
        self.assertEqual(cmd["action"], "attack")
        self.assertEqual(cmd["controllerId"], "1", "操控者是角色 id（字符串）")
        self.assertEqual(cmd["targetPos"], [{"x": 12, "y": 27}])

    def test_beams_tie_break_to_the_nearest(self):
        """第 27 步改方针：**不再挑血最少的**。L1 的 10 点伤害对满血机器人（≥40 血）
        等额 ⇒ 并列时打**近**的；"补刀优先"（第 10 步的旧方针）就此作废。"""
        cmd = self._only_cmd(
            self._manned(Robot(Pos(12, 26), 900), Robot(Pos(14, 26), 40))
        )
        self.assertEqual(cmd["targetPos"], [{"x": 12, "y": 26}], "伤害等额 ⇒ 打近的，不看血量")

    def test_a_dying_robot_is_not_worth_a_full_shot(self):
        """**有效伤害** = min(伤害, 剩余血)：将死者吸收不完一整发 ⇒ 打吸收得完的那台
        —— 目标是打死**所有**机器人，把整发浪费在只剩 4 血的人身上就是少打死一个。"""
        cmd = self._only_cmd(
            self._manned(Robot(Pos(12, 26), 4), Robot(Pos(14, 26), 900))
        )
        self.assertEqual(cmd["targetPos"], [{"x": 14, "y": 26}], "4 血的只值 4 点，900 血的值满 10 点")

    def test_distance_breaks_the_effective_tie(self):
        """有效伤害并列时打**近**的（同伤害下先打完近的，远处的下一回合再补）。"""
        cmd = self._only_cmd(
            self._manned(Robot(Pos(15, 25), 40), Robot(Pos(13, 26), 40))
        )
        self.assertEqual(cmd["targetPos"], [{"x": 13, "y": 26}])

    def test_ignores_the_best_cell_when_it_is_out_of_range(self):
        """**先按射程过滤、再算最大伤害** —— 顺序写反就成了"拿最优落点、但够不着的当目标"。"""
        weak = Robot(Pos(12, 31), 10)  # 距 (12,25) 是 6，够不着
        cmd = self._only_cmd(self._manned(weak, Robot(Pos(13, 25), 900)))
        self.assertEqual(cmd["targetPos"], [{"x": 13, "y": 25}], "10 血那只够不着，只能打 900 的")

    def test_the_rocket_lands_for_maximum_splash(self):
        """**火箭的最大伤害落点**（第 27 步的核心变化）：中心 20 + 周围 8 格溅射 10
        （任务书 §4.5.4）⇒ 落进机器人**簇**里、落在最肥的那台身上 —— 而不是挑残血的。

        簇：(13,25) 40 血与 (14,25) 8 血相邻。落 (13,25) = 20+8=28 分；落 (14,25) =
        8+10=18 分 ⇒ 落在 40 血那台身上（旧方针会去打 8 血的残血）。"""
        cmd = self._only_cmd(
            self._manned(
                Robot(Pos(13, 25), 40),
                Robot(Pos(14, 25), 8),
                kind="rocket",
            )
        )
        self.assertEqual(cmd["action"], "attack")
        self.assertEqual(cmd["targetPos"], [{"x": 13, "y": 25}], "落点该吃满中心+溅射")

    def test_the_rocket_prefers_a_cluster_over_a_lone_target(self):
        """同样的射程里，**簇**（两台相邻 = 30 分）优先于孤零零一台（20 分）——
        机器人成群来，溅射才是火箭的本职。"""
        cmd = self._only_cmd(
            self._manned(
                Robot(Pos(13, 25), 40),
                Robot(Pos(13, 26), 40),
                Robot(Pos(15, 25), 800),  # 孤台、血厚也一样：20 分 < 30 分
                kind="rocket",
            )
        )
        self.assertIn(
            cmd["targetPos"][0],
            [{"x": 13, "y": 25}, {"x": 13, "y": 26}, {"x": 14, "y": 25}],
            "该落进簇里（中心+溅射都吃得到），不打孤台",
        )

    def test_two_guns_do_not_pile_onto_a_dying_robot(self):
        """**同回合记账**：先开火的炮把伤害记在账上（`assigned`），后开的按**剩余血**挑
        —— 两座炮不挤同一个将死的目标（第 27 步撤掉了"集火补刀"，方针是打死**所有**）。

        两座加特林（射程 4）都能打到 (11,24) 12 血与 (9,26) 40 血；1 号炮先开（打近的
        (11,24)，记 10 点）⇒ 2 号炮看到它只剩 2 血（有效 2 < 10）⇒ 转打 40 血那只。
        没有记账的话两座都会去打 12 血的"残血"。"""
        guns = self._guns(Pos(12, 25), Pos(9, 22))
        turn = self._turn(
            Worker(1, Pos(12, 24)),  # 贴着 1 号炮
            Worker(2, Pos(9, 23)),  # 贴着 2 号炮
            weapons=guns,
            robots=(Robot(Pos(11, 24), 12), Robot(Pos(9, 26), 40)),
        )
        cmds = plan(turn)
        self.assertEqual(cmds[str(self.GUN)]["targetPos"], [{"x": 11, "y": 24}], "1 号先开，打近的")
        self.assertEqual(
            cmds[str(self.GUN + 1)]["targetPos"],
            [{"x": 9, "y": 26}],
            "2 号按剩余血算：(11,24) 只剩 2 血，转打 40 血的",
        )

    def test_range_boundary_is_chebyshev(self):
        """射程用**切比雪夫**（任务书 L230），边界取 `<=`：对角 4 格打得到，5 格打不到。

        写成欧氏/曼哈顿，或把 `<=` 写成 `<`，都会在边界上静静地少打一发。
        """
        edge = self._manned(Robot(Pos(16, 29), 40))  # 对角 (+4,+4) ⇒ 切比雪夫 4
        self.assertEqual(self._only_cmd(edge)["targetPos"], [{"x": 16, "y": 29}])
        self.assertEqual(plan(self._manned(Robot(Pos(17, 30), 40))), {}, "5 格超出射程")

    def test_attack_range_comes_from_the_payload(self):
        """射程取自记录（payload 的 `attackRange`），**不是代码里的常数**。

        同一个目标：射程 4 够不着、射程 7 够得着 —— 任务书 §4.5.1 的表格写 3/6/10，
        与样例 payload 的 4/7/INT_MAX 矛盾，**以 payload 为准**。
        """
        far = Robot(Pos(17, 30), 40)  # 切比雪夫 5
        self.assertEqual(plan(self._manned(far, reach=4)), {})
        self.assertEqual(self._only_cmd(self._manned(far, reach=7))["targetPos"], [{"x": 17, "y": 30}])

    def test_a_cooling_rocket_holds_fire(self):
        """火箭发射台发射后有 3 回合空窗（`cooldown`）⇒ 冷却中**一炮不发**，
        而且**不换炮**（换炮会让角色在炮位之间周期性来回走）。"""
        robot = Robot(Pos(12, 26), 40)
        self.assertEqual(plan(self._manned(robot, cooldown=2)), {})
        self.assertEqual(self._only_cmd(self._manned(robot, cooldown=0))["action"], "attack")

    def test_a_missing_cooldown_means_no_cooldown(self):
        """`cooldown` 缺失时 `model` 给 -1 ⇒ **不算冷却中**。

        样例三座炮都没有这个字段 —— 把缺省值写成"冷却中"的话，火箭整晚一炮不开，
        而日志上什么都看不出来。
        """
        self.assertEqual(self._only_cmd(self._manned(Robot(Pos(12, 26), 40), cooldown=-1))["action"], "attack")

    def test_a_dead_robot_is_not_worth_a_shot(self):
        """`health == 0` 的机器人（尸体）不占目标 —— 打空处白耗一次冷却。"""
        self.assertEqual(plan(self._manned(Robot(Pos(12, 26), 0))), {})

    def test_no_robot_in_range_means_hold_fire(self):
        """用户选定：机器人还没走进射程时**站在炮位待命**（不发指令，空指令合法）。"""
        self.assertEqual(plan(self._manned()), {}, "没敌人 ⇒ 什么都不发")
        self.assertEqual(
            plan(self._manned(Robot(Pos(12, 31), 40))), {}, "敌人还在射程外 ⇒ 也不发"
        )

    def test_missing_round_no_never_fires(self):
        """`roundNo` 缺失 ⇒ `within = 129` ⇒ `is_day` 判成**夜里**。

        那个降级方向对"白天不许建造"是安全的，对 `attack` 就反了：**白天开火是非法的**。
        所以 `round_no < 0` 时一条都不发 —— 代价只是这一个回合不动。
        """
        turn = self._manned(Robot(Pos(12, 26), 40), round_no=-1)
        self.assertEqual(plan(turn), {})


if __name__ == "__main__":
    unittest.main()
