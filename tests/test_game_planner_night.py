"""game/planner.py 夜间线的用例：回炮位、认领与最大伤害落点开火。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import sys
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用（discover 不一定把它放进 sys.path）；src/ 给 coregeek 用
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _fixtures import _terrain  # noqa: E402
from coregeek.game import planner  # noqa: E402
from coregeek.game.grid import Pos, wall_cells  # noqa: E402
from coregeek.game.map import Map  # noqa: E402
from coregeek.game.planner import WALL, plan  # noqa: E402
from coregeek.game.roles import BaseRole, Pioneer, Worker  # noqa: E402
from coregeek.game.world import Robot, Turn, Wall, Weapon  # noqa: E402
from coregeek.protocol import model  # noqa: E402


class NightWeaponTest(unittest.TestCase):
    """夜里：所有角色（含开拓者）回炮位、开火打最大伤害落点（方针是打死所有机器人）。

    `attack` 仅黑夜可用、`build`/`collect` 仅工人（任务书 §4.4）⇒ 夜里除了 `move`
    就只该有 `attack`。
    """

    BASE = Pos(10, 24)
    NIGHT = 85
    NEAR, FAR = Pos(12, 25), Pos(9, 22)  # 两座加特林
    REACH = 4  # 加特林 L1 的射程，取自样例 payload（任务书表格写的是 3）
    GUN = 10020  # `_manned` 那座炮的 id —— `attack` 的 key 就是它

    def setUp(self) -> None:
        #: `_fired` 是 planner 的跨回合开火账（模块级），不清会跨用例串味
        #: （上一条用例打出去的那发，会把这一条的同一座炮判成冷却中）。
        planner._fired.clear()

    def _turn(
        self,
        *roles: BaseRole,
        weapons: tuple[Weapon, ...] | None = None,
        robots: tuple[Robot, ...] | None = None,
        round_no: int | None = None,
    ) -> Turn:
        # `weapons=None` 才是"用默认的两座"；用 `or` 的话空元组是假值，那条"没武器"的
        # 用例会静默拿到两座武器、白测一场。
        weapons = self._guns(self.NEAR, self.FAR) if weapons is None else weapons
        # `robots=None` 给一台在场机器人：清完的夜里工人会走经济线，而本类测的是防守线，
        # 得让防守分支真的命中（`robots=()` 可显式给空）。
        robots = (Robot(pos=Pos(16, 26), health=40),) if robots is None else robots
        return Turn(
            round_no=self.NIGHT if round_no is None else round_no,
            map=Map((41, 32), _terrain(weapons, {self.BASE: "station"})),
            roles=roles,
            gold=0,
            weapons=weapons,
            robots=robots,
        )

    def _guns(self, *cells: Pos) -> tuple[Weapon, ...]:
        """按样例的 L1 加特林造记录（射程 4、无冷却），id 从 `GUN` 起编号 —— 断言里要能
        一眼看出"开火的是哪一座"，所以第一座就取 `GUN`。"""
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
        level: int = 1,
    ) -> Turn:
        """一名工人已经贴着那座炮（切比雪夫 1）—— 开火与否只看目标、冷却与等级。"""
        gun = Weapon(
            id=self.GUN,
            kind=kind,
            pos=self.NEAR,
            attack_range=self.REACH if reach is None else reach,
            cooldown=cooldown,
            level=level,
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
        # 两个角色到 (12,25) 都是 2 格，最近的都是它；没有去重的话 2 号也会奔它去
        self.assertGreater(second.dist(self.NEAR), 1, "2 号必须换一座，不能也去挤 (12,25)")
        self.assertLess(second.dist(self.FAR), 5, "换的那座该是下一近的 (9,22)")

    def test_no_weapons_means_nothing_to_do(self):
        """武器全没了 ⇒ 不动（空指令合法），而不是瞎走。"""
        self.assertEqual(plan(self._turn(Worker(1, Pos(14, 26)), weapons=())), {})

    def test_the_pioneer_fills_in_when_the_workers_are_short(self):
        """工人不够覆盖全部武器组 ⇒ 开拓者补位（§4.4 里 `attack` 的可用角色是"全部"）。

        一个工人、两组炮：它不去的话整夜少一门火力。
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

    def test_the_pioneer_stays_off_the_guns_while_both_workers_are_alive(self):
        """两个工人活着 ⇒ 炮位全归工人，开拓者一组都不认领（用户口径）。

        开拓者排在 payload 最前面：不先把工人挑完，它会把最近的那一组抢走，被挤掉的那个工人
        整夜站着不动 —— 火力没多，任务线的主力还被拴在炮位上。
        """
        cmds = plan(
            self._turn(
                Pioneer(1, Pos(14, 26)),  # 离 NEAR 更近，不拦的话它先抢
                Worker(2, Pos(12, 24)),  # 贴着 NEAR
                Worker(3, Pos(9, 19)),  # 还差三格到 FAR
            )
        )
        self.assertNotIn("1", cmds, f"开拓者不该占炮位：{cmds}")
        self.assertEqual(cmds[str(self.GUN)]["controllerId"], "2", "NEAR 归工人")
        self.assertEqual(cmds["3"]["action"], "move", "另一个工人去 FAR，不是干等")
        cell = Pos(cmds["3"]["targetPos"][0]["x"], cmds["3"]["targetPos"][0]["y"])
        self.assertLess(cell.dist(self.FAR), Pos(9, 19).dist(self.FAR), "这一格得真的离 FAR 更近")

    def test_the_command_hangs_on_the_weapon_id(self):
        """`attack` 的 key 是武器 id，操控角色在 `controllerId` 里（`docs/response.txt`）。

        写成 `{"1": …}`（角色 id）本地照样绿，判题器看到的却是"角色 1 在操炮" —— 一次"指令非法"。
        """
        cmd = self._only_cmd(self._manned(Robot(Pos(12, 27), 40)))
        self.assertEqual(cmd["action"], "attack")
        self.assertEqual(cmd["controllerId"], "1", "操控者是角色 id（字符串）")
        self.assertEqual(cmd["targetPos"], [{"x": 12, "y": 27}])

    def test_beams_tie_break_to_the_nearest(self):
        """并列时打近的，不挑血最少的。L1 的 10 点伤害对满血机器人（≥40 血）等额
        ⇒ 有效伤害并列 ⇒ 取近者。"""
        cmd = self._only_cmd(
            self._manned(Robot(Pos(12, 26), 900), Robot(Pos(14, 26), 40))
        )
        self.assertEqual(cmd["targetPos"], [{"x": 12, "y": 26}], "伤害等额 ⇒ 打近的，不看血量")

    def test_a_dying_robot_is_not_worth_a_full_shot(self):
        """有效伤害 = min(伤害, 剩余血)：将死者吸收不完一整发 ⇒ 打吸收得完的那台
        —— 目标是打死所有机器人，把整发浪费在只剩 4 血的人身上就是少打死一个。"""
        cmd = self._only_cmd(
            self._manned(Robot(Pos(12, 26), 4), Robot(Pos(14, 26), 900))
        )
        self.assertEqual(cmd["targetPos"], [{"x": 14, "y": 26}], "4 血的只值 4 点，900 血的值满 10 点")

    def test_distance_breaks_the_effective_tie(self):
        """有效伤害并列时打近的（同伤害下先打完近的，远处的下一回合再补）。"""
        cmd = self._only_cmd(
            self._manned(Robot(Pos(15, 25), 40), Robot(Pos(13, 26), 40))
        )
        self.assertEqual(cmd["targetPos"], [{"x": 13, "y": 26}])

    def test_ignores_the_best_cell_when_it_is_out_of_range(self):
        """先按射程过滤、再算最大伤害 —— 顺序写反就成了"拿最优落点、但够不着的当目标"。"""
        weak = Robot(Pos(12, 31), 10)  # 距 (12,25) 是 6，够不着
        cmd = self._only_cmd(self._manned(weak, Robot(Pos(13, 25), 900)))
        self.assertEqual(cmd["targetPos"], [{"x": 13, "y": 25}], "10 血那只够不着，只能打 900 的")

    def test_the_rocket_lands_for_maximum_splash(self):
        """火箭的最大伤害落点：中心 20 + 周围 8 格溅射 10（任务书 §4.5.4）⇒ 落进簇里、
        落在最肥的那台身上，而不是挑残血的。

        例：(13,25) 40 血与 (14,25) 8 血相邻 —— 落 (13,25) 得 28 分、落 (14,25) 得 18 分。
        """
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
        """同样的射程里，簇（两台相邻 = 30 分）优先于孤零零一台（20 分）——
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
        """同回合记账（`assigned`）：先开火的把伤害记在账上，后开的按剩余血挑 ⇒ 两座炮不挤
        同一个将死的目标（方针是打死所有，不集火补刀）。

        例：两座射程 4 的加特林都能打到 (11,24)（12 血）与 (9,26)（40 血），1 号先开记 10 点
        ⇒ 2 号看到前者只剩 2 血，转打 40 血那只；没有记账的话两座都去打 12 血那只。
        """
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
        """射程用切比雪夫（任务书 L230），边界取 `<=`：对角 4 格打得到，5 格打不到。

        写成欧氏/曼哈顿，或把 `<=` 写成 `<`，都会在边界上静静地少打一发。
        """
        edge = self._manned(Robot(Pos(16, 29), 40))  # 对角 (+4,+4) ⇒ 切比雪夫 4
        self.assertEqual(self._only_cmd(edge)["targetPos"], [{"x": 16, "y": 29}])
        self.assertEqual(plan(self._manned(Robot(Pos(17, 30), 40))), {}, "5 格超出射程")

    def test_attack_range_comes_from_the_payload(self):
        """射程取自记录（payload 的 `attackRange`），不是代码里的常数。

        同一个目标：射程 4 够不着、射程 7 够得着 —— 任务书 §4.5.1 的表格写 3/6/10，
        与样例 payload 的 4/7/INT_MAX 矛盾，以 payload 为准。
        """
        far = Robot(Pos(17, 30), 40)  # 切比雪夫 5
        self.assertEqual(plan(self._manned(far, reach=4)), {})
        self.assertEqual(self._only_cmd(self._manned(far, reach=7))["targetPos"], [{"x": 17, "y": 30}])

    def test_a_level_two_gun_fires_two_shots_at_the_same_cell(self):
        """`targetPos` 的**个数必须等于武器等级**（接口文档 L218）：升到 L2 还只发一格就是
        一次"指令非法"（红线），多发一格也一样。多发同点 —— 火箭同点叠加、加特林同弹道连着
        吃掉最近那台。"""
        cmd = self._only_cmd(self._manned(Robot(Pos(12, 27), 40), level=2))
        self.assertEqual(cmd["action"], "attack")
        self.assertEqual(cmd["targetPos"], [{"x": 12, "y": 27}, {"x": 12, "y": 27}])

    def test_a_level_three_gun_fires_three_shots(self):
        """L3 发三格（个数 = 等级，不是"最多两格"）。"""
        cmd = self._only_cmd(self._manned(Robot(Pos(12, 27), 40), level=3))
        self.assertEqual(len(cmd["targetPos"]), 3)

    def test_a_level_two_rocket_fires_two_missiles(self):
        """火箭的等级放的是**导弹枚数** ⇒ L2 的 `targetPos` 也是两格。"""
        cmd = self._only_cmd(
            self._manned(Robot(Pos(13, 25), 40), Robot(Pos(14, 25), 8), kind="rocket", level=2)
        )
        self.assertEqual(cmd["targetPos"], [{"x": 13, "y": 25}, {"x": 13, "y": 25}])

    def test_the_railgun_never_fires_more_than_one_shot(self):
        """电磁狙击炮是单目标武器（接口文档 L218 的"恒为 1"）：等级只翻倍能量、不加目标位。

        照"加特林/火箭 = 等级"一刀切，L2 的电磁就会发出 2 格 ⇒ 指令非法。
        """
        cmd = self._only_cmd(self._manned(Robot(Pos(12, 27), 40), kind="railgun", level=3))
        self.assertEqual(cmd["targetPos"], [{"x": 12, "y": 27}])

    def test_a_level_two_gun_is_worth_twice_the_damage(self):
        """升级后伤害翻倍（每级 +10），挑目标要按新伤害算。

        例：(13,25) 15 血（距 1）与 (15,25) 40 血（距 3）都够得着 —— L1 各值 10 点、并列取近，
        打残血的；L2 前者仍只值 15 点、后者值满 20 点，转打满血的。
        """
        near, far = Robot(Pos(13, 25), 15), Robot(Pos(15, 25), 40)
        self.assertEqual(
            self._only_cmd(self._manned(near, far))["targetPos"], [{"x": 13, "y": 25}], "L1：并列取近"
        )
        self.assertEqual(
            self._only_cmd(self._manned(near, far, level=2))["targetPos"],
            [{"x": 15, "y": 25}, {"x": 15, "y": 25}],
            "L2：15 血那只吸收不完 20 点伤害",
        )

    def test_a_level_two_rocket_is_worth_twice_the_splash(self):
        """火箭的导弹数 = 等级 ⇒ 中心/溅射都按等级翻倍（L2 两枚 = 40/20），评分跟着变。

        例：(13,25) 15 血与 (13,26) 15 血挨在一起（L1 吃 15+10=25 分），另一台 40 血在 (9,22)
        （L1 只值 20 分）⇒ L1 打那簇残血的；L2 时那簇被血量封顶只值 30 分、满血那台值满 40 分
        ⇒ 转打满血的（两枚都砸它 = 一炮带走）。
        """
        cluster = (Robot(Pos(13, 25), 15), Robot(Pos(13, 26), 15))
        lone = Robot(Pos(9, 22), 40)  # 簇离它切比雪夫 4 ⇒ 没有落点能同时吃到两边
        self.assertEqual(
            self._only_cmd(self._manned(*cluster, lone, kind="rocket"))["targetPos"],
            [{"x": 13, "y": 25}],
            "L1：一簇残血比一台满血值钱",
        )
        # 火箭打出去的那发会记进本地开火账（`_fired`）⇒ 不擦干净，下面那次就是"冷却中"、一炮不发
        planner._fired.clear()
        self.assertEqual(
            self._only_cmd(self._manned(*cluster, lone, kind="rocket", level=2))["targetPos"],
            [{"x": 9, "y": 22}, {"x": 9, "y": 22}],
            "L2：满血那台吸收得完 40 点",
        )

    def test_a_cooling_rocket_holds_fire(self):
        """火箭发射台发射后有 3 回合空窗（`cooldown`）⇒ 冷却中一炮不发，
        而且不换炮（换炮会让角色在炮位之间周期性来回走）。"""
        robot = Robot(Pos(12, 26), 40)
        self.assertEqual(plan(self._manned(robot, cooldown=2)), {})
        self.assertEqual(self._only_cmd(self._manned(robot, cooldown=0))["action"], "attack")

    def test_a_missing_cooldown_means_no_cooldown(self):
        """`cooldown` 缺失时 `model` 给 -1 ⇒ 不算冷却中。

        样例三座炮都没有这个字段 —— 把缺省值写成"冷却中"的话，火箭整晚一炮不开，
        而日志上什么都看不出来。
        """
        self.assertEqual(self._only_cmd(self._manned(Robot(Pos(12, 26), 40), cooldown=-1))["action"], "attack")

    def test_the_rocket_pair_alternates_by_local_record(self):
        """双火箭交替靠**本地开火账**（用户方案）：发出 `attack` 那回合记下武器 id，
        之后 3 回合不选它 —— payload 不带 `cooldown` 也能精确轮换，且不打冷却中的炮
        （那是指令执行失败，白丢一回合火力）。序列：85 发一座、86 发另一座、
        87~88 两座都在冷却 ⇒ 一发不发（待命）、89 第一座期满再发。"""
        rockets = (
            Weapon(id=200, kind="rocket", pos=Pos(12, 24), attack_range=10, cooldown=-1),
            Weapon(id=201, kind="rocket", pos=Pos(12, 25), attack_range=10, cooldown=-1),
        )
        worker = Worker(1, Pos(11, 25))  # 双火箭的操作位：同时贴着两座
        expected = {85: {201}, 86: {200}, 87: set(), 88: set(), 89: {201}}
        for round_no, want in expected.items():
            with self.subTest(round_no=round_no):
                cmds = plan(
                    self._turn(
                        worker, weapons=rockets,
                        robots=(Robot(Pos(15, 24), 40),), round_no=round_no,
                    )
                )
                self.assertEqual(
                    {int(k) for k in cmds}, want,
                    f"R{round_no} 该打的武器 id：{cmds}",
                )

    def test_a_cooling_rocket_defers_to_its_partner(self):
        """组内一座冷却中 ⇒ 发另一座（cooldown 字段在场时，它优先于轮转）。"""
        rockets = (
            Weapon(id=200, kind="rocket", pos=Pos(12, 24), attack_range=10, cooldown=3),
            Weapon(id=201, kind="rocket", pos=Pos(12, 25), attack_range=10, cooldown=0),
        )
        turn = self._turn(
            Worker(1, Pos(11, 25)),
            weapons=rockets,
            robots=(Robot(Pos(15, 24), 40),),
        )
        cmds = plan(turn)
        self.assertEqual(list(cmds), ["201"], "冷却中的那座让位给就绪的")

    def test_a_dead_robot_is_not_worth_a_shot(self):
        """`health == 0` 的机器人（尸体）不占目标 —— 打空处白耗一次冷却。"""
        self.assertEqual(plan(self._manned(Robot(Pos(12, 26), 0))), {})

    def test_no_robot_in_range_means_hold_fire(self):
        """机器人还没走进射程时站在炮位待命（不发指令，空指令合法）。"""
        self.assertEqual(plan(self._manned()), {}, "没敌人 ⇒ 什么都不发")
        self.assertEqual(
            plan(self._manned(Robot(Pos(12, 31), 40))), {}, "敌人还在射程外 ⇒ 也不发"
        )

    def test_missing_round_no_never_fires(self):
        """`roundNo` 缺失 ⇒ `within = 129` ⇒ `is_day` 判成夜里。

        那个降级方向对"白天不许建造"是安全的，对 `attack` 就反了：白天开火是非法的。
        所以 `round_no < 0` 时一条都不发 —— 代价只是这一个回合不动。
        """
        turn = self._manned(Robot(Pos(12, 26), 40), round_no=-1)
        self.assertEqual(plan(turn), {})


class NightPostTest(unittest.TestCase):
    """真几何下的岗位：两座火箭的共用操作位 `(11,25)` 得**站上去**，站上去才交替得起来。

    这一组必须把角色铺进 `entries`：只有 `protocol.model._entries` 会把我方角色写进网格，
    手搭地形漏掉这一步的话 `Map.blocked` 里就没有"自己挡自己"这回事 —— 用例连旧代码都放得
    过去（`NightWeaponTest.test_the_rocket_pair_alternates_by_local_record` 就是这么写的）。
    """

    BASE = Pos(10, 24)
    SIZE = (41, 32)
    NIGHT = 85
    GUN, ROCKET1, ROCKET2 = 202, 200, 201  # 加特林与两座火箭（`attack` 的 key 就是这些 id）
    SPOT = Pos(11, 25)  # 两座火箭的共用操作位：站上去才同时贴着两座
    ROBOT = Pos(15, 24)  # 射程 10 内的一台机器人

    def setUp(self) -> None:
        planner._fired.clear()  # 跨回合开火账，不清会串味（见 `NightWeaponTest.setUp`）

    def _turn(
        self,
        *roles: BaseRole,
        gatling: bool = False,
        round_no: int = NIGHT,
        extra_robots: tuple[Robot, ...] = (),
    ) -> Turn:
        """基地 2×2 + 14 格围墙砌满 + 两座火箭（可带加特林），角色与机器人都按 payload 的写法进网格。

        `extra_robots` 是**踩着格子**的机器人（堵 `(11,25)` 那种）；默认那台在盒外只当靶子。
        """
        weapons = tuple(
            Weapon(id=i, kind=kind, pos=pos, attack_range=10, cooldown=-1)
            for i, kind, pos in (
                (self.ROCKET1, "rocket", Pos(12, 24)),
                (self.ROCKET2, "rocket", Pos(12, 25)),
                (self.GUN, "gatling", Pos(12, 22)),
            )
            if kind == "rocket" or gatling
        )
        robots = (Robot(self.ROBOT, 40),) + extra_robots
        walls = {c: WALL for c in wall_cells(self.BASE, self.SIZE[0])}
        return Turn(
            round_no=round_no,
            map=Map(
                self.SIZE,
                _terrain(
                    weapons,
                    {self.BASE: "station"},
                    walls,
                    {r.pos: "worker" for r in roles},
                    {r.pos: "robot:small" for r in extra_robots},
                ),
            ),
            roles=roles,
            gold=0,
            weapons=weapons,
            robots=robots,
        )

    def test_the_operator_on_the_shared_spot_alternates_rounds(self):
        """站在共用操作位上的操作者：两座火箭按本地开火账交替，一夜不断火。

        序列与 `test_the_rocket_pair_alternates_by_local_record` 同一个口径（85 发一座、
        86 发另一座、87~88 都在冷却 ⇒ 待命、89 第一座期满再发）。
        """
        worker = Worker(1, self.SPOT)
        expected = {85: {"201"}, 86: {"200"}, 87: set(), 88: set(), 89: {"201"}}
        for round_no, want in expected.items():
            with self.subTest(round_no=round_no):
                cmds = plan(self._turn(worker, round_no=round_no))
                self.assertEqual(set(cmds), want, f"R{round_no} 该打的武器 id：{cmds}")

    def test_the_operator_beside_the_spot_walks_onto_it(self):
        """`(10,25)` 是进那个口袋的必经格：这一回合就该踩上 `(11,25)`，不是原地不动。

        旧的"贴着 goal 即到"口径在这格上只会返回 None（目标每回合重算 ⇒ 永远走不到），
        而站不上去就开不了第二座炮 —— `grid.step_onto` 就是为这一格加的。
        """
        cmds = plan(self._turn(Worker(1, Pos(10, 25))))
        self.assertEqual(cmds["1"]["action"], "move", f"该踩上操作位：{cmds}")
        cell = Pos(cmds["1"]["targetPos"][0]["x"], cmds["1"]["targetPos"][0]["y"])
        self.assertEqual(cell, self.SPOT)

    def test_a_half_manned_pair_fires_first_then_reposts(self):
        """只贴着 `200`（`(12,23)`）：这一回合先打它，它进冷却了才往操作位挪。

        先开火再挪岗：打得了的那一回合不白丢火力，只有都打不了才走那 5~6 步去站共用操作位。
        """
        half = Worker(1, Pos(12, 23))
        self.assertEqual(set(plan(self._turn(half, round_no=85))), {"200"}, "这一回合该打贴着的 200")
        cmds = plan(self._turn(half, round_no=86))
        self.assertEqual(set(cmds), {"1"}, f"200 冷却了 ⇒ 该挪岗：{cmds}")
        self.assertEqual(cmds["1"]["action"], "move")
        cell = Pos(cmds["1"]["targetPos"][0]["x"], cmds["1"]["targetPos"][0]["y"])
        self.assertEqual(cell, Pos(11, 22), "绕开基地那一角、朝操作位去")

    def test_a_colleague_on_the_spot_never_freezes_the_pair(self):
        """同事站在操作位上 ⇒ 他照旧开火，另一位换一组去（岗位被占了，不是两人一起卡住）。"""
        cmds = plan(self._turn(Worker(1, Pos(10, 25)), Worker(2, self.SPOT), gatling=True))
        self.assertEqual(set(cmds), {"201", "1"}, f"火箭归站在岗位上的那位：{cmds}")
        self.assertEqual(cmds["201"]["controllerId"], "2")
        self.assertEqual(cmds["1"]["action"], "move", "另一位该去加特林，不是干等")

    def test_a_gunner_beside_a_rocket_fires_it_though_the_spot_is_taken(self):
        """贴着 `200` 的那位该把它打出去 —— 岗位格被占不能让这一组变成无人可打。

        `_post_spots` 返回空说的是"这回合没地方站"，不是"这组没人打得了"：先看贴没贴着、
        再谈挪岗。旧判据把岗位格当成了这一组的总开关，站在火箭旁边的人跟着一发不发。
        """
        cmds = plan(self._turn(Worker(1, Pos(12, 23)), Worker(2, self.SPOT), gatling=True))
        self.assertEqual(cmds["200"]["controllerId"], "1", f"贴着的 200 该打出去：{cmds}")

    def test_the_pioneer_fires_the_pair_it_is_standing_on(self):
        """开拓者蹲在共用操作位上 ⇒ 它操这一组。

        它不认领就真没人操得了：那一格被它占着、工人站不上去（岗位只有一格），而它自己
        又一发不打 —— 两个火箭整夜沉默。"工人够操满 ⇒ 开拓者不认领"是名册口径，"谁真站得上
        岗位"才是事实。
        """
        cmds = plan(
            self._turn(Worker(1, Pos(9, 22)), Worker(2, Pos(10, 22)), Pioneer(3, self.SPOT))
        )
        self.assertEqual(cmds["201"]["controllerId"], "3", f"蹲在岗位上的开拓者该开火：{cmds}")

    def test_a_robot_on_the_shared_spot_still_leaves_a_rocket_to_fire(self):
        """共用操作位被机器人踩死 ⇒ 退到贴着 `200` 的 `(12,23)`。

        没有退路时整组被跳过：两个火箭整夜一发不打，而这不是没人手 —— 是判据把"岗位格用不了"
        当成了"这一组不用管"。退路只守得了一座，但那一座照打。
        """
        cmds = plan(self._turn(Worker(1, Pos(11, 22)), extra_robots=(Robot(self.SPOT, 40),)))
        self.assertEqual(cmds["1"]["action"], "move", f"该朝 (12,23) 挪：{cmds}")
        step = Pos(cmds["1"]["targetPos"][0]["x"], cmds["1"]["targetPos"][0]["y"])
        self.assertLess(step.dist(Pos(12, 23)), Pos(11, 22).dist(Pos(12, 23)), "朝贴着 200 的那格走")


class NightEconomyTest(unittest.TestCase):
    """夜里机器人全被打光 ⇒ 工人跑**整套**经济兜底：卖矿 → 升级券 → 采闲矿。

    与白天最后一级同一套（`_economy`），只把时间预算换成 `Turn.rounds_left` —— 夜里那段也是
    "还剩多少回合"，而 `day_rounds_left` 在夜里恒为 0，用它的话什么差事都发不出去。"全打光"是
    真清完：机器人全图可见（任务书 L95）、一夜一波（L352）⇒ 判据过 `_alive`（已毁的照旧留在
    `turn.robots` 里，见 `model._robots`）。"赶不回来就不去"由 `_sell_ore` 的第四道门与
    `_mine_spare_ore` 的可行性筛选各自兜住：夜里的活动半径等于"这一段剩下多少回合"。
    夜间经济只发 collect / move / sell / buy / use —— **build / remove 夜里非法**（§4.4）。
    """

    BASE = Pos(10, 24)
    WEAPONS = (Weapon(200, "gatling", Pos(9, 23), 4, 0),)
    NEAR = Pos(5, 24)    # 门侧近矿（穿后方通道出盒）
    FAR = Pos(30, 4)     # 远矿（夜里这一段跑不完来回 ⇒ 不去）
    VENDOR = Pos(20, 16)  # 样例的小贩位
    SHOP = Pos(25, 20)    # 样例的武器商店位
    PRICES = {"stone": 1, "iron": 4, "copper": 5}
    SHOP_PRICES = {"WeaponUpgradeVoucher1": 100}

    def _turn(
        self,
        worker: Worker,
        *,
        ores: dict[Pos, str] | None = None,
        vendor: bool = False,
        shop: bool = False,
        gold: int = 0,
        robots=(),
        walls: tuple[Wall, ...] = (),
    ) -> Turn:
        grid = _terrain(
            self.WEAPONS,
            {self.BASE: "station"},
            {c: "wall" for c in self.ring()},
            {self.NEAR: "stone"} if ores is None else ores,
            {self.VENDOR: "vendor"} if vendor else {},
            {self.SHOP: "weaponShop"} if shop else {},
        )
        grid[worker.pos] = "worker"
        return Turn(
            round_no=85,  # 夜里（within = 85，夜里剩 46 回合）
            map=Map((41, 32), grid),
            roles=(worker,),
            gold=gold,
            weapons=self.WEAPONS,
            robots=robots,
            walls=walls,
            vendor_prices=self.PRICES,
            shop_prices=self.SHOP_PRICES,
        )

    def ring(self):
        from coregeek.game.grid import wall_cells

        return wall_cells(self.BASE, 41)

    def test_a_cleared_night_sells_the_load_before_mining(self):
        """有货、小贩就在旁边 ⇒ 先卖掉（夜里 `sell` 合法：§4.4 没给它写昼夜）。

        第 62 步之前这一支只认矿（`_mine_spare_ore`），背着铜的工人会转身去采那块石头。
        """
        worker = Worker(10010, Pos(20, 15), {"copper": 1})  # 切比雪夫 1 ⇒ "贴着小贩"
        cmd = plan(self._turn(worker, vendor=True)).get("10010")
        self.assertEqual(cmd, {"action": "sell", "name": "copper", "num": 1}, cmd)

    def test_a_cleared_night_walks_to_the_shop_when_the_voucher_is_affordable(self):
        """钱够买券 ⇒ 朝商店走（夜里的差事与白天同一套，只是预算换成 `rounds_left`）。

        工人摆在盒外空地：盒内起步就往西（后方通道在背面），切比雪夫距离在绕墙时先增后减 ——
        拿它当"朝哪儿走"的判据会在盒里判反，这条只钉"朝商店 vs 朝矿"这一件事。
        """
        worker = Worker(10010, Pos(20, 24), {})
        cmd = plan(self._turn(worker, shop=True, gold=100)).get("10010")
        self.assertEqual(cmd["action"], "move", cmd)
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(self.SHOP), Pos(20, 24).dist(self.SHOP), "朝商店走")

    def test_a_cleared_night_mines_when_there_is_nothing_else_to_do(self):
        """没货、买不起券 ⇒ 照旧出门采矿（夜里 collect 合法，§4.4 没给它写昼夜）。"""
        worker = Worker(10010, Pos(12, 24), {})
        cmd = plan(self._turn(worker)).get("10010")
        self.assertIsNotNone(cmd, "清完的夜里工人不该干等")
        self.assertIn(cmd["action"], ("move", "collect"))

    def test_the_far_mine_is_still_out_of_reach_at_night(self):
        """远矿连夜里这一段也跑不完来回 ⇒ 不去（出得了事回不了家的活不干）。

        第 62 步把夜里的紧半径（`NIGHT_WANDER=8`）换成了"这一段还剩多少回合"，宽了很多 ——
        这条钉的是它仍有上界。
        """
        worker = Worker(10010, Pos(12, 24), {})
        self.assertNotIn("10010", plan(self._turn(worker, ores={self.FAR: "iron"})))

    def test_night_economy_never_builds_or_removes(self):
        """夜里经济线绝不发 build / remove（§4.4：两条都仅白天）—— 环上留多少缺口、包里有没有
        石头、墙残不残血都一样。"""
        worker = Worker(10010, Pos(12, 24), {"stone": 5})
        grid = _terrain(self.WEAPONS, {self.BASE: "station", self.NEAR: "stone"})
        grid[worker.pos] = "worker"
        turn = Turn(
            round_no=85,
            map=Map((41, 32), grid),
            roles=(worker,),
            gold=0,
            weapons=self.WEAPONS,
            walls=(Wall(40001, Pos(13, 24), 100, 2),),  # L2 残血墙：白天会被修，夜里不许动
            vendor_prices=self.PRICES,
        )
        cmds = plan(turn)
        self.assertTrue(
            all(v["action"] not in ("build", "remove") for v in cmds.values()), cmds
        )

    def test_robots_present_at_night_still_defend(self):
        """场上还有活机器人 ⇒ 照旧回炮位开火，不出门采矿。"""
        worker = Worker(10010, Pos(12, 24), {})
        turn = self._turn(worker, robots=(Robot(pos=Pos(13, 24), health=40),))
        cmds = plan(turn)
        self.assertTrue(
            all(v["action"] in ("attack", "move") for v in cmds.values()),
            f"夜里只该有 attack/move：{cmds}",
        )

    def test_a_dead_robot_alone_does_not_hold_the_worker_on_defense(self):
        """只剩尸体的夜晚按"清完"算 ⇒ 工人跑经济线（卖货），不去炮位。

        `model._robots` 不丢 `health == 0` 的（与 `_walls` / `_character` 相反）⇒ 判空如果直接读
        `turn.robots`，一台打死的机器人会把工人整夜钉在炮位上、永远进不了经济线。
        """
        worker = Worker(10010, Pos(20, 15), {"copper": 1})  # 贴着小贩
        turn = self._turn(worker, vendor=True, robots=(Robot(pos=Pos(20, 14), health=0),))
        self.assertEqual(
            plan(turn).get("10010"),
            {"action": "sell", "name": "copper", "num": 1},
        )


class StationUpgradeTest(unittest.TestCase):
    """夜里的基地升级：持基地券 + 基地血量 < 满血 1/4 ⇒ 贴基地 `use`（升级 + 回满血一次到位，
    1500/3000/4500 是任务书表格实证）。就算有机器人在场也升 —— 基地要塌了这是救命的
    （升级当回合放弃开火）。"""

    BASE = Pos(10, 24)
    WEAPONS = (Weapon(200, "gatling", Pos(9, 23), 4, 0),)

    def _turn(self, worker: Worker, health: int, robots=()):
        grid = _terrain(self.WEAPONS, {self.BASE: "station"})
        grid[worker.pos] = "worker"
        return Turn(
            round_no=85, map=Map((41, 32), grid), roles=(worker,), gold=0,
            weapons=self.WEAPONS, robots=robots,
            station_health=health, station_level=1,
        )

    def test_a_low_base_with_a_voucher_gets_upgraded(self):
        worker = Worker(10010, Pos(12, 24), {"StationUpgradeVoucher1": 1})
        cmd = plan(self._turn(worker, 300)).get("10010")
        self.assertEqual(cmd["action"], "use")
        self.assertEqual(cmd["name"], "StationUpgradeVoucher1")
        self.assertEqual(Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"]), self.BASE)

    def test_a_healthy_base_is_never_upgraded(self):
        worker = Worker(10010, Pos(12, 24), {"StationUpgradeVoucher1": 1})
        cmds = plan(self._turn(worker, 1500))
        self.assertNotEqual(cmds.get("10010", {}).get("action"), "use")

    def test_upgrade_even_when_robots_are_at_the_gate(self):
        """基地要塌了就算机器人在门口也先升 —— 贴着基地一格内直接用，不挪窝开炮。"""
        worker = Worker(10010, Pos(12, 24), {"StationUpgradeVoucher1": 1})
        cmd = plan(
            self._turn(worker, 300, robots=(Robot(pos=Pos(14, 24), health=60),))
        ).get("10010")
        self.assertEqual(cmd["action"], "use")


if __name__ == "__main__":
    unittest.main()
