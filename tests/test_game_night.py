"""game/night.py 夜间线的用例：回炮位、认领与最大伤害落点开火。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import sys
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用（discover 不一定把它放进 sys.path）；src/ 给 coregeek 用
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _fixtures import _reset_ledgers  # noqa: E402
from _fixtures import _terrain  # noqa: E402
from coregeek.game import core, night  # noqa: E402
from coregeek.game.grid import Pos, wall_cells, weapon_sites  # noqa: E402
from coregeek.game.map import Map  # noqa: E402
from coregeek.game.planner import plan  # noqa: E402
from coregeek.game.core import WALL, WALL_FIXER  # noqa: E402
from coregeek.game.roles import BaseRole, Pioneer, Worker  # noqa: E402
from coregeek.game.world import ROUNDS_PER_DAY, Robot, Turn, Wall, Weapon  # noqa: E402
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
        _reset_ledgers()
        #: `_fired` 是 planner 的跨回合开火账（模块级），不清会跨用例串味
        #: （上一条用例打出去的那发，会把这一条的同一座炮判成冷却中）。
        night._fired.clear()

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

    def test_only_the_gunner_mans_the_guns(self):
        """**只有炮手上炮**（`utils._night_gunner` = 名册第一个工人）：第二个工人出门挖矿，
        不再"换一座去操"—— 三座火箭一组、一人按冷却轮换就够，多余的人手全部产矿。

        夹具没有矿 ⇒ 挖矿那位这一回合什么都不发（空指令合法）。
        """
        cmds = plan(self._turn(Worker(1, Pos(14, 26)), Worker(2, Pos(14, 24))))
        self.assertEqual(set(cmds), {"1"}, f"只有炮手有指令：{cmds}")
        self.assertEqual(cmds["1"]["action"], "move", "炮手朝炮位走")
        cell = Pos(cmds["1"]["targetPos"][0]["x"], cmds["1"]["targetPos"][0]["y"])
        self.assertLess(cell.dist(self.NEAR), Pos(14, 26).dist(self.NEAR), "这一步朝炮位去")

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

    def test_the_pioneer_mans_the_guns_and_the_workers_mine(self):
        """**开拓者就是炮手**（用户口径"晚上开拓者操三个火箭筒"）：它上炮位，工人出门挖矿。

        旧的"工人够多 ⇒ 开拓者一组都不认领"（第 74 步补位炮手）随新阵形作废 —— 三座火箭
        共用一个操作位、一人全操，人手不再紧张。夹具没有矿 ⇒ 工人这一回合空指令。
        """
        cmds = plan(
            self._turn(
                Pioneer(1, Pos(14, 26)),  # 离 NEAR 更近
                Worker(2, Pos(12, 24)),
                Worker(3, Pos(9, 19)),
            )
        )
        self.assertEqual(set(cmds), {"1"}, f"只有开拓者有指令：{cmds}")
        self.assertEqual(cmds["1"]["action"], "move", "开拓者朝炮位走")

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

    def test_a_level_two_gun_splits_its_two_shots(self):
        """等级放的是**子弹数**（每颗 10 点，不是"一发 20 点"）⇒ L2 把两颗分给两台。

        例：(13,25) 15 血（距 1）与 (14,26) 40 血（距 2、**不在同一条弹道上**）都够得着 ——
        L1 一颗、各值 10 点、并列取近 ⇒ 打残血的；L2 第一颗仍打残血的（打成 5 血），第二颗
        它只吸收得下 5 点、满血那台吸收得下 10 点 ⇒ 转打满血的。
        ⚠️ 同一直线上的两台不能这么算：子弹被前面那台吃掉（`_first_on_line`）。
        """
        near, far = Robot(Pos(13, 25), 15), Robot(Pos(14, 26), 40)
        self.assertEqual(
            self._only_cmd(self._manned(near, far))["targetPos"], [{"x": 13, "y": 25}], "L1：并列取近"
        )
        self.assertEqual(
            self._only_cmd(self._manned(near, far, level=2))["targetPos"],
            [{"x": 13, "y": 25}, {"x": 14, "y": 26}],
            "L2：第一颗补残血、第二颗转满血那台",
        )

    def test_a_bullet_is_eaten_by_the_nearest_robot_on_its_line(self):
        """子弹沿弹道飞、命中**最近**那台即消耗（任务书 L250）⇒ 同一直线上，后面那台打不到。

        (13,25) 与 (15,25) 都在炮位 (12,25) 的横线上：两颗子弹都只能落在那条线上，
        挨着的 (13,25) 先吃 —— 第二颗也只值 5 分（它只剩 5 血），不会去够后面那台。
        """
        near, far = Robot(Pos(13, 25), 15), Robot(Pos(15, 25), 40)
        self.assertEqual(
            self._only_cmd(self._manned(near, far, level=2))["targetPos"],
            [{"x": 13, "y": 25}, {"x": 13, "y": 25}],
            "挡在前面的那台没死之前，后面那台一颗都吃不到",
        )

    def test_a_level_two_rocket_finishes_the_cluster_then_moves_on(self):
        """火箭的等级放的是**导弹枚数**（每枚中心 20 / 溅射 10）⇒ 逐枚重算，不把两枚砸在同一个点上。

        例：(13,25) 15 血与 (13,26) 15 血挨在一起（一枚 L1 吃 15+10=25 分），另一台 40 血在 (9,22)
        （只值 20 分）⇒ L1 打那簇残血的；L2 第一枚把 (13,25) 打死、(13,26) 只剩 5 血，第二枚
        打它只值 5 分、打满血那台值 20 分 ⇒ 第二枚转打满血的。
        """
        cluster = (Robot(Pos(13, 25), 15), Robot(Pos(13, 26), 15))
        lone = Robot(Pos(9, 22), 40)  # 簇离它切比雪夫 4 ⇒ 没有落点能同时吃到两边
        self.assertEqual(
            self._only_cmd(self._manned(*cluster, lone, kind="rocket"))["targetPos"],
            [{"x": 13, "y": 25}],
            "L1：一簇残血比一台满血值钱",
        )
        # 火箭打出去的那发会记进本地开火账（`_fired`）⇒ 不擦干净，下面那次就是"冷却中"、一炮不发
        night._fired.clear()
        self.assertEqual(
            self._only_cmd(self._manned(*cluster, lone, kind="rocket", level=2))["targetPos"],
            [{"x": 13, "y": 25}, {"x": 9, "y": 22}],
            "L2：第一枚收掉那簇，第二枚别再砸在只剩 5 血的那台上",
        )

    def test_the_lowest_tier_is_worth_the_most(self):
        """低级优先（用户口径"优先攻击最低级的机器人"）：有效伤害并列时挑最低级的 —— 即使它更远。

        (13,25) 是 BOSS（更近、距 1），(14,26) 是小型（距 2）：一颗子弹各值 10 点，权重 4 : 1
        ⇒ 打小型。没有权重表时这里会按"并列取近"打 BOSS。
        """
        boss = Robot(Pos(13, 25), 800, kind="bossRobot")
        small = Robot(Pos(14, 26), 40, kind="smallRobot")
        cmd = self._only_cmd(self._manned(boss, small))
        self.assertEqual(cmd["targetPos"], [{"x": 14, "y": 26}], "并列时挑最低级的")

    def test_a_robot_without_a_kind_is_still_shot_at(self):
        """`roleType` 缺失 ⇒ 当最低级（照打，不静默少打）：并列时挑那台没种类的。"""
        boss = Robot(Pos(13, 25), 800, kind="bossRobot")
        unknown = Robot(Pos(14, 26), 40)
        cmd = self._only_cmd(self._manned(boss, unknown))
        self.assertEqual(cmd["targetPos"], [{"x": 14, "y": 26}])

    def test_the_three_missiles_spread_over_three_finishable_targets(self):
        """一枚一枚地挑：三只 20 血小型各挨一枚就死 ⇒ 三枚分点 = 60 点有效伤害；
        同点砸一只只有 20 点（另两枚打空）。这就是 `targetPos` 多格的价值。"""
        gun = Weapon(
            id=self.GUN, kind="rocket", pos=Pos(13, 23), attack_range=99, cooldown=-1, level=3
        )
        robots = tuple(Robot(Pos(20, y), 20, kind="smallRobot") for y in (15, 23, 31))
        cmd = self._only_cmd(self._turn(Worker(1, Pos(13, 22)), weapons=(gun,), robots=robots))
        self.assertEqual(cmd["action"], "attack")
        self.assertEqual(
            cmd["targetPos"], [{"x": 20, "y": 15}, {"x": 20, "y": 23}, {"x": 20, "y": 31}]
        )

    def test_the_two_missiles_focus_when_that_damages_more(self):
        """该集火时集火：两只 40 血小型挨得近（溅射互相吃到）⇒ 两枚砸同一格（20+20 中心 +
        溅射）比各打一枚更值。逐枚贪心两种都出得来，不是"无脑摊开"。"""
        gun = Weapon(
            id=self.GUN, kind="rocket", pos=Pos(13, 23), attack_range=99, cooldown=-1, level=2
        )
        robots = (Robot(Pos(20, 23), 40, kind="smallRobot"), Robot(Pos(20, 24), 40, kind="smallRobot"))
        cmd = self._only_cmd(self._turn(Worker(1, Pos(13, 22)), weapons=(gun,), robots=robots))
        self.assertEqual(cmd["targetPos"], [{"x": 20, "y": 23}, {"x": 20, "y": 23}], "簇里叠加")

    def test_opposite_targets_collapse_to_one_cell(self):
        """两台在炮位两侧（夹角 180°）⇒ 第二颗**不许**另开一格：整次攻击非法比少打一发严重得多。

        （任务书 L250：任意两个目标相对加特林的夹角 > 90° ⇒ 整次攻击非法。）
        第二颗本来更想打右边那台满血的（10 分 > 左边只剩 5 分的 5 分），但那样就出了锥形
        ⇒ 退回同格（`targetPos` 的个数仍等于等级）。
        """
        gun = Weapon(
            id=self.GUN, kind="gatling", pos=Pos(12, 25), attack_range=9, cooldown=-1, level=2
        )
        robots = (Robot(Pos(11, 25), 15, kind="smallRobot"), Robot(Pos(14, 25), 40, kind="smallRobot"))
        cmd = self._only_cmd(self._turn(Worker(1, Pos(12, 24)), weapons=(gun,), robots=robots))
        self.assertEqual(cmd["targetPos"], [{"x": 11, "y": 25}, {"x": 11, "y": 25}], "退回同格补齐")

    def test_every_volley_stays_inside_the_cone(self):
        """产出的任何一波都过锥形自查（≤45°，比任务书的 90° 更严）—— 这是红线边上那一条。"""
        boards = [
            (Pos(12, 25), (Robot(Pos(15, 24), 40), Robot(Pos(15, 26), 40))),
            (Pos(12, 25), (Robot(Pos(11, 25), 40), Robot(Pos(13, 25), 40))),
            (
                Pos(12, 25),
                (Robot(Pos(14, 26), 10), Robot(Pos(15, 25), 10), Robot(Pos(13, 27), 10)),
            ),
        ]
        for pos, robots in boards:
            gun = Weapon(id=self.GUN, kind="gatling", pos=pos, attack_range=9, cooldown=-1, level=3)
            targets = night._volley(gun, robots, (41, 32))
            self.assertTrue(night._in_cone(pos, targets), f"{pos} {robots} ⇒ {targets} 出了锥形")
        self.assertTrue(night._in_cone(Pos(12, 25), (Pos(15, 24), Pos(15, 26))), "同侧约 37°")
        self.assertFalse(night._in_cone(Pos(12, 25), (Pos(13, 25), Pos(11, 25))), "两侧 180°")
        self.assertFalse(night._in_cone(Pos(12, 25), (Pos(13, 25), Pos(12, 26))), "正好 90° 也不赌")

    def test_the_railgun_aims_through_a_line_of_targets(self):
        """电磁沿弹道穿透（任务书 L252）：能量够时打**穿**一串比只打前排值 ⇒ 瞄线的远端。

        三台里两台 10 血小型在横线上、一台 BOSS 在竖线上（L3 能量 30）：瞄 (16,25) 吃到
        10+10 两台的伤害（80 分），瞄 (15,25) 只吃到它自己（40 分），瞄 BOSS 是 30 点 ×1（30 分）。
        """
        gun = Weapon(
            id=self.GUN, kind="railgun", pos=Pos(12, 25), attack_range=10, cooldown=-1, level=3
        )
        robots = (
            Robot(Pos(15, 25), 10, kind="smallRobot"),
            Robot(Pos(16, 25), 10, kind="smallRobot"),
            Robot(Pos(12, 20), 800, kind="bossRobot"),
        )
        cmd = self._only_cmd(self._turn(Worker(1, Pos(12, 24)), weapons=(gun,), robots=robots))
        self.assertEqual(cmd["action"], "attack")
        self.assertEqual(cmd["targetPos"], [{"x": 16, "y": 25}], "瞄线的远端，把两台都穿上")

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
        _reset_ledgers()
        night._fired.clear()  # 跨回合开火账，不清会串味（见 `NightWeaponTest.setUp`）

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
    """夜里机器人清完 ⇒ 工人出门**采最值钱的矿**（囤到第二天由白天的经济线卖掉）。

    这条线是第 119 步从白天的经济线里**独立出来**的：白天那条按"走得到 **且 回得来**"筛
    （回程参照 = 最近的武器位），夜里清场之后**没有回炮位的义务** —— 照它筛会把整段夜里筛成
    "一座矿都不可行"，工人整夜空指令。夜里这一条**不卖、不买、不回炮位**：只问"走得到吗"，
    排最值钱的那座（并列取近的、再取坐标序）。

    "清完"的判据是**没有打我方的活机器人**（`night.is_cleared`）：一夜一波（任务书 L352）但
    全图可见（L95）⇒ 打对方那一波也在 `turn.robots` 里，我们从不打它；已毁的照旧留在表里
    （`model._robots`）。这一支只发 `collect` / `sell` / `buy` / `use` / `acceptTask` / `move` ——
    **`build` / `remove` 夜里非法**（§4.4），一条都不会发。
    """

    BASE = Pos(10, 24)
    WEAPONS = (Weapon(200, "gatling", Pos(9, 23), 4, 0),)
    NEAR = Pos(5, 24)     # 门侧近矿（穿后方通道出盒）
    FAR = Pos(30, 4)      # 远矿：来回跑不完，但夜里**不要求**回得来
    COPPER = Pos(4, 26)   # 更值钱的矿（价 5 > 石 1）
    VENDOR = Pos(20, 16)  # 样例的小贩位
    SHOP = Pos(25, 20)    # 样例的武器商店位
    PRICES = {"stone": 1, "iron": 4, "copper": 5}
    SHOP_PRICES = {"WeaponUpgradeVoucher1": 100}

    def setUp(self) -> None:
        _reset_ledgers()

    def _turn(
        self,
        role: BaseRole,
        *,
        ores: dict[Pos, str] | None = None,
        vendor: bool = False,
        shop: bool = False,
        gold: int = 0,
        robots=(),
        walls: tuple[Wall, ...] = (),
        task_points: tuple[Pos, ...] = (),
        cooling_tasks: tuple[tuple[Pos, int], ...] = (),
    ) -> Turn:
        grid = _terrain(
            self.WEAPONS,
            {self.BASE: "station"},
            {c: "wall" for c in self.ring()},
            {self.NEAR: "stone"} if ores is None else ores,
            {self.VENDOR: "vendor"} if vendor else {},
            {self.SHOP: "weaponShop"} if shop else {},
            {p: "challengerTaskPoint1" for p in task_points},
        )
        grid[role.pos] = role.type_name
        return Turn(
            round_no=85,  # 夜里（within = 85，夜里剩 45 回合）
            map=Map((41, 32), grid),
            roles=(role,),
            gold=gold,
            weapons=self.WEAPONS,
            robots=robots,
            walls=walls,
            task_points=task_points,
            cooling_tasks=cooling_tasks,
            vendor_prices=self.PRICES,
            shop_prices=self.SHOP_PRICES,
            our_team="challenger",
        )

    def ring(self):
        from coregeek.game.grid import wall_cells

        return wall_cells(self.BASE, 41)

    def test_a_cleared_night_mines_the_priciest_ore(self):
        """几种矿都在 ⇒ 按**收购价**挑最贵的那座，近处那块石头不占便宜（价 5 > 价 1）。

        工人摆在盒外的空地（盒内起步受围墙与通道影响，切比雪夫距离会先增后减，拿它当
        "朝哪儿走"的判据会判反）：一步就该往铜矿那边去，而不是采脚边这块石头。
        """
        worker = Worker(10010, Pos(4, 23), {})  # 贴着石矿（切比雪夫 1）、铜矿在 3 格外
        turn = self._turn(worker, ores={self.NEAR: "stone", self.COPPER: "copper"})
        cmd = plan(turn).get("10010")
        self.assertEqual(cmd["action"], "move", cmd)
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(self.COPPER), Pos(4, 23).dist(self.COPPER), "朝铜矿走")

    def test_a_cleared_night_mines_when_there_is_nothing_else_to_do(self):
        """清完的夜里没有别的差事 ⇒ 出门采矿（夜里 collect 合法，§4.4 没给它写昼夜）。"""
        worker = Worker(10010, Pos(12, 24), {})
        cmd = plan(self._turn(worker)).get("10010")
        self.assertIsNotNone(cmd, "清完的夜里工人不该干等")
        self.assertIn(cmd["action"], ("move", "collect"))

    def test_the_far_mine_is_mined_now_that_no_return_trip_is_required(self):
        """远矿照去 —— 夜里**不问回不回得来**（第 119 步）。

        第 85 回合（夜里还剩 45 回合）下，去 (30,4) 再回炮位的来回远超 `rounds_left −
        TIME_MARGIN`，白天的筛法会判它不可行 ⇒ 整座矿被剔掉 ⇒ 工人**整夜空指令**（用户报的
        症状就是这一条）。夜里不要求回来：天亮前赶不回炮位没有代价，白天还有 70 个回合走回来。
        """
        worker = Worker(10010, Pos(12, 24), {})
        cmd = plan(self._turn(worker, ores={self.FAR: "iron"})).get("10010")
        self.assertIsNotNone(cmd, "远矿也是矿：夜里不该因为'回不来'就干等")
        self.assertIn(cmd["action"], ("move", "collect"))

    def test_a_cleared_night_follows_the_day_line(self):
        """清场后**走白天那条线**（用户口径"机器人死完了直接走白天逻辑"）：贴着小贩、背包有货
        ⇒ 当场卖掉 —— 夜里 `sell` 合法（§4.4 没给它写昼夜门）。

        旧的"夜里只采矿、不卖不买"（第 119 步）随这条口径作废。
        """
        worker = Worker(10010, Pos(20, 15), {"copper": 1})  # 切比雪夫 1 ⇒ "贴着小贩"
        cmd = plan(self._turn(worker, vendor=True, shop=True, gold=100)).get("10010")
        self.assertEqual(cmd, {"action": "sell", "name": "copper", "num": 1}, cmd)

    def test_a_cleared_night_sends_the_idle_pioneer_to_the_task_point(self):
        """清场后开拓者**提前进入白天状态**（用户口径）：有可接的任务点就走过去接 ——
        `acceptTask` §4.4 没写昼夜限制（写"仅白天"的只有 `build` / `remove`）。

        旧口径这一支只跑"买券 / 去等刷新"，`take_task` 只挂在白天那条链上 ⇒ 夜里刷出来的点
        一个都接不到，开拓者白站一整夜。
        """
        pioneer = Pioneer(10011, Pos(20, 20))
        point = Pos(14, 14)
        cmd = plan(self._turn(pioneer, task_points=(point,))).get("10011")
        self.assertIsNotNone(cmd, "有任务点 ⇒ 夜里也该去接")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(point), pioneer.pos.dist(point), f"朝任务点走：{cmd}")

    def test_a_cleared_night_sends_the_penniless_pioneer_to_the_waiting_spot(self):
        """没任务点、券又买不起 ⇒ 开拓者去**最该等的地方**站着（刷新最快那个任务点 / 商店）——
        与白天同一支。"""
        pioneer = Pioneer(10011, Pos(20, 20))
        point = Pos(14, 14)
        cmd = plan(self._turn(pioneer, cooling_tasks=((point, 5),))).get("10011")
        self.assertIsNotNone(cmd, "清场后开拓者不该干等")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(point), pioneer.pos.dist(point), f"朝等着的那格走：{cmd}")

    def test_night_economy_never_builds_or_removes(self):
        """夜里这条线绝不发 build / remove（§4.4：两条都仅白天）—— 环上留多少缺口、包里有没有
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
            all(v["action"] in ("collect", "move") for v in cmds.values()), cmds
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
        """只剩尸体的夜晚按"清完"算 ⇒ 工人出门采矿，不去炮位。

        `model._robots` 不丢 `health == 0` 的（与 `_walls` / `_character` 相反）⇒ 判空如果直接读
        `turn.robots`，一台打死的机器人会把工人整夜钉在炮位上、永远进不了这一支。
        """
        worker = Worker(10010, Pos(20, 15), {})
        turn = self._turn(worker, robots=(Robot(pos=Pos(20, 14), health=0),))
        cmd = plan(turn).get("10010")
        self.assertIn(cmd["action"], ("move", "collect"), cmd)

    def test_a_robot_that_attacks_the_other_team_does_not_hold_the_worker_either(self):
        """场上只剩打**对方**阵营的活机器人 ⇒ 按"清完"算，工人照样出门采矿。

        `robots` 全图可见（L95）⇒ 对方那一波的 `targetTeam` 指向对面。我们从不打它（`_fire` 也
        过 `_foe_robots`）⇒ 照"场上没有活机器人"判的话它整夜都在，工人被钉在炮位上、这一支
        永远进不去（这就是"清完机器人后不出门干活"）。
        """
        worker = Worker(10010, Pos(20, 15), {})
        turn = self._turn(
            worker,
            robots=(Robot(pos=Pos(20, 14), health=800, target_team="defender"),),
        )
        cmd = plan(turn).get("10010")
        self.assertIn(cmd["action"], ("move", "collect"), cmd)


class NoTaskNightTest(unittest.TestCase):
    """无任务模式的夜班**与平常夜班是同一套**（第 130 步）：炮手上炮、其余挖矿；清场走白天线。

    第 126 步那套"火箭对 → 开拓者、加特林 → 工人、其余挖矿"的固定分工随新阵形作废 ——
    三座火箭一组、一个炮手全操，分工只剩"谁上炮、谁挖矿"一件事（`utils._night_gunner`）。
    """

    BASE = Pos(10, 24)
    NIGHT = 85
    COPPER, IRON = Pos(20, 30), Pos(4, 30)  # 两座矿（两个工人各领一座）

    def setUp(self) -> None:
        _reset_ledgers()
        night._fired.clear()  # 跨回合开火账，不清会串味（见 `NightWeaponTest.setUp`）

    def _turn(
        self,
        *roles: BaseRole,
        tasks_exhausted: bool = True,
        robots=(Robot(pos=Pos(14, 26), health=40),),
    ) -> Turn:
        weapons = tuple(
            Weapon(10020 + i, "rocket", pos, 10, 0)
            for i, pos in enumerate(weapon_sites(self.BASE, 41))
        )
        grid = _terrain(weapons, {self.BASE: "station"}, {self.COPPER: "copper", self.IRON: "iron"})
        grid |= {r.pos: r.type_name for r in roles}
        return Turn(
            round_no=self.NIGHT,
            map=Map((41, 32), grid),
            roles=roles,
            gold=0,
            weapons=weapons,
            robots=robots,
            tasks_exhausted=tasks_exhausted,
            vendor_prices={"copper": 5, "iron": 3},
        )

    def test_the_night_shift_is_the_same_with_or_without_tasks(self):
        """任务耗不耗尽，夜班的指令**一条不变** —— 分工只看"谁上炮、谁挖矿"，与任务无关。"""
        roles = (Pioneer(1, Pos(9, 24)), Worker(2, Pos(5, 24)), Worker(3, Pos(6, 24)))
        with_tasks = plan(self._turn(*roles, tasks_exhausted=False))
        night._fired.clear()  # 两次跑要从同一本账起步，否则第二次看到的是"刚打过的冷却"
        without = plan(self._turn(*roles, tasks_exhausted=True))
        self.assertEqual(with_tasks, without)
        # 开拓者站在共用操作位上 ⇒ 它开火（attack 的 key 是武器 id、controllerId 才是角色）
        attacks = {c["controllerId"] for c in with_tasks.values() if c["action"] == "attack"}
        self.assertEqual(attacks, {"1"}, f"开拓者操炮：{with_tasks}")
        for rid in ("2", "3"):
            self.assertIn(
                with_tasks[rid]["action"], ("move", "collect"), f"工人 {rid} 该在挖矿：{with_tasks}"
            )

    def test_without_a_pioneer_the_first_worker_mans_the_guns(self):
        """名册里没有开拓者 ⇒ **第一个工人顶上操炮**（火力不断），其余照旧挖矿。"""
        cmds = plan(self._turn(Worker(2, Pos(9, 24)), Worker(3, Pos(5, 24))))
        attacks = {c["controllerId"] for c in cmds.values() if c["action"] == "attack"}
        self.assertEqual(attacks, {"2"}, f"第一个工人操炮：{cmds}")
        self.assertIn(cmds["3"]["action"], ("move", "collect"), f"另一个工人挖矿：{cmds}")

    def test_the_miners_keep_clear_of_the_robots(self):
        """挖矿的工人离机器人**至少 `night.DANGER`(2) 格**（用户口径"保证安全"）：最贵的铜矿
        就在机器人旁边 ⇒ 两个工人都去够得着的铁矿，不去送死。

        对照：机器人一死（清场）⇒ 铜矿立刻有人去 —— 危险半径只挡活着的机器人。
        """
        cmds = plan(
            self._turn(
                Pioneer(1, Pos(9, 24)),
                Worker(2, Pos(12, 28)),
                # 铜矿 (20,30) 离机器人 (14,26) 切比雪夫 6 —— 太远，测不出半径；
                # 把机器人挪到铜矿边上：(19,29) 离 (20,30) 只有 1 格
                robots=(Robot(pos=Pos(19, 29), health=40),),
            )
        )
        for rid in ("2", "3"):
            cmd = cmds.get(rid)
            if cmd is None:
                continue
            step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
            self.assertEqual(
                cmd["action"], "move", f"工人 {rid}：夹具里两座矿都在走得到的范围外才对"
            )
            self.assertGreaterEqual(
                step.dist(Pos(19, 29)),
                1,
                f"工人 {rid} 这一步不许踩进机器人周围 2 格内：{cmd}",
            )
        # 机器人死了 ⇒ 铜矿（最贵）立刻成为目标
        cleared = plan(
            self._turn(Pioneer(1, Pos(9, 24)), Worker(2, Pos(12, 28)), robots=())
        )
        cmd = cleared["2"]
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(
            step.dist(self.COPPER), Pos(12, 28).dist(self.COPPER), "清场 ⇒ 朝最贵的铜矿走"
        )


class StationUpgradeTest(unittest.TestCase):
    """夜里的基地升级：持基地券 + 基地血量 < 满血 1/4 ⇒ 贴基地 `use`（升级 + 回满血一次到位，
    1500/3000/4500 是任务书表格实证）。就算有机器人在场也升 —— 基地要塌了这是救命的
    （升级当回合放弃开火）。"""

    BASE = Pos(10, 24)
    WEAPONS = (Weapon(200, "gatling", Pos(9, 23), 4, 0),)

    def setUp(self) -> None:
        _reset_ledgers()

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


class WallRepairTest(unittest.TestCase):
    """第 4 夜起的修墙线：血 < `WALL_REPAIR_HP`(200) 的**正面墙**用修复包回满。

    三条口径都出自用户：① 第 3 天起攒包（`day.RepairStock`）、**第 4 夜起**派工；② 没包就
    出门挖矿、有包就守在正面墙后方那一列；③ 阈值与白天"拆了重砌"**共用** `core.WALL_REPAIR_HP`。
    """

    BASE = Pos(10, 24)
    NIGHT4 = 461  # 第 4 天的夜里第一个回合（within = 71）
    NIGHT3 = 331  # 第 3 天的夜里第一个回合
    FRONT = Pos(13, 23)  # 正面列中段那一格
    POST = Pos(12, 23)  # 它的后方待命位（靠基地那一列里"离最远那格最近"的一格）
    FOE = Pos(15, 23)  # 一台够得着正面列的小型机器人（攻击距离 3）

    def setUp(self) -> None:
        _reset_ledgers()
        night._fired.clear()

    def _turn(
        self,
        *roles: BaseRole,
        damaged: dict[Pos, tuple[int, int]] | None = None,
        robots: tuple[Robot, ...] | None = None,
        round_no: int = NIGHT4,
        ores: dict[Pos, str] | None = None,
        weapons: tuple[Weapon, ...] = (),
        gaps: frozenset[Pos] = frozenset(),
    ) -> Turn:
        # 第 3 天起环是 16 格（`utils._sealed_back` 补上背面两个角格）—— 白天那条链按 `_ring`
        # 算缺口，铺少了那两个角格会被当成缺口、把人支去砌墙（与 `RepairStockTest` 同一个坑）
        sealed = round_no > 2 * ROUNDS_PER_DAY
        ring = wall_cells(self.BASE, 41, sealed=sealed)
        grid: dict[Pos, str] = {c: WALL for c in ring if c not in gaps}
        grid[self.BASE] = "station"
        grid.update(ores or {})
        for gun in weapons:
            grid[gun.pos] = gun.kind
        for role in roles:
            grid[role.pos] = role.type_name
        foes = (Robot(pos=self.FOE, health=40),) if robots is None else robots
        for foe in foes:
            grid[foe.pos] = "robot:smallRobot"
        walls = tuple(
            Wall(40000 + i, cell, *(damaged or {}).get(cell, (1000, 1)))
            for i, cell in enumerate(ring)
            if cell not in gaps
        )
        return Turn(
            round_no=round_no,
            map=Map((41, 32), grid),
            roles=roles,
            gold=100,
            weapons=weapons,
            robots=foes,
            walls=walls,
            vendor_prices={"stone": 1, "iron": 3, "copper": 5},
        )

    def _night(self, worker: Worker, **kw) -> Turn:
        """炮手（开拓者 = `_night_gunner` 认的第一个角色）+ 这名工人。"""
        return self._turn(Pioneer(1, Pos(12, 24)), worker, **kw)

    def _actions(self, cmds: dict) -> list[str]:
        return [cmd["action"] for cmd in cmds.values()]

    def test_a_fourth_night_repairs_a_front_wall_below_the_threshold(self):
        """第 4 夜 + 手里有包 + 正面墙 140 血 ⇒ 贴着就 `use WallFixer`，目标是那一格。"""
        worker = Worker(2, self.POST, {WALL_FIXER: 1})
        cmds = plan(self._night(worker, damaged={self.FRONT: (140, 1)}))
        self.assertEqual(cmds["2"]["action"], "use")
        self.assertEqual(cmds["2"]["name"], WALL_FIXER)
        self.assertEqual(
            Pos(cmds["2"]["targetPos"][0]["x"], cmds["2"]["targetPos"][0]["y"]), self.FRONT
        )

    def test_an_earlier_night_never_repairs(self):
        """第 3 夜不派修墙工：同一个局面下一张包都不许花（那个工人照旧出门挖矿）。"""
        worker = Worker(2, self.POST, {WALL_FIXER: 1})
        turn = self._night(worker, damaged={self.FRONT: (140, 1)}, round_no=self.NIGHT3)
        self.assertIsNone(night.repairer(turn, 1), "第 3 夜不该派修墙工")
        self.assertNotIn("use", self._actions(plan(turn)))

    def test_a_wall_above_the_threshold_is_left_alone(self):
        """260 血（> 200）不修：已经守在待命位上 ⇒ 这一回合什么都不发。"""
        worker = Worker(2, self.POST, {WALL_FIXER: 1})
        cmds = plan(self._night(worker, damaged={self.FRONT: (260, 1)}))
        self.assertNotIn("use", self._actions(cmds))
        self.assertNotIn("2", cmds)

    def test_the_threshold_follows_no_level(self):
        """阈值是**绝对值**：L2 墙 150 血（< 200）照样修，L3 墙 260 血照样不动。"""
        worker = Worker(2, self.POST, {WALL_FIXER: 1})
        low = plan(self._night(worker, damaged={self.FRONT: (150, 2)}))
        self.assertEqual(low["2"]["action"], "use", "L2 墙也归修墙线管")
        self.assertEqual(
            Pos(low["2"]["targetPos"][0]["x"], low["2"]["targetPos"][0]["y"]), self.FRONT
        )
        high = plan(self._night(worker, damaged={self.FRONT: (260, 3)}))
        self.assertNotIn("use", self._actions(high))

    def test_a_wall_voucher_repairs_better_than_a_pack(self):
        """手里有打得上的墙券 ⇒ 先打券（升级顺带回满血，比修复包更值）。"""
        worker = Worker(2, self.POST, {WALL_FIXER: 2, "WallUpgradeVoucher1": 1})
        cmds = plan(self._night(worker, damaged={self.FRONT: (140, 1)}))
        self.assertEqual(cmds["2"]["action"], "use")
        self.assertEqual(cmds["2"]["name"], "WallUpgradeVoucher1", f"有券先打券：{cmds}")

    def test_the_pack_is_used_when_the_voucher_does_not_fit(self):
        """券打不上这一档（L1 墙遇上二级券）⇒ 照旧用修复包，别把券白花掉。"""
        worker = Worker(2, self.POST, {WALL_FIXER: 2, "WallUpgradeVoucher2": 1})
        cmds = plan(self._night(worker, damaged={self.FRONT: (140, 1)}))
        self.assertEqual(cmds["2"]["action"], "use")
        self.assertEqual(cmds["2"]["name"], WALL_FIXER, f"券打不上 ⇒ 用包：{cmds}")

    def test_a_level_three_wall_never_spends_a_voucher(self):
        """L3 到顶 ⇒ 没有"再升一级"的券可用，老老实实用包。"""
        worker = Worker(2, self.POST, {WALL_FIXER: 2, "WallUpgradeVoucher2": 1})
        cmds = plan(self._night(worker, damaged={self.FRONT: (150, 3)}))
        self.assertEqual(cmds["2"]["name"], WALL_FIXER, f"到顶了 ⇒ 用包：{cmds}")

    def test_the_lowest_wall_goes_first(self):
        """两格都够格：先修血最少的那格（并列才比坐标）。"""
        worker = Worker(2, self.POST, {WALL_FIXER: 1})
        cmds = plan(
            self._night(
                worker, damaged={Pos(13, 22): (140, 1), Pos(13, 24): (90, 1)}
            )
        )
        self.assertEqual(
            Pos(cmds["2"]["targetPos"][0]["x"], cmds["2"]["targetPos"][0]["y"]), Pos(13, 24)
        )

    def test_only_the_front_column_is_repaired(self):
        """侧面那一列不归它管：那格 100 血也不许花包（白天拆了重砌是它唯一的出路）。"""
        worker = Worker(2, self.POST, {WALL_FIXER: 1})
        cmds = plan(self._night(worker, damaged={Pos(11, 21): (100, 1)}))
        self.assertNotIn("use", self._actions(cmds))

    def test_a_destroyed_wall_is_not_a_repair_target(self):
        """被打穿的墙已经不在 `turn.walls` 里 ⇒ 既不发 `use` 也**绝不发 `build`**（夜里非法）。"""
        worker = Worker(2, self.POST, {WALL_FIXER: 1})
        cmds = plan(self._night(worker, gaps=frozenset({self.FRONT})))
        self.assertNotIn("use", self._actions(cmds))
        self.assertNotIn("build", self._actions(cmds))

    def test_without_a_pack_the_worker_mines_instead(self):
        """手里一个包都没有 ⇒ 不派修墙工，那名工人照旧出门挖矿（用户口径）。"""
        worker = Worker(2, self.POST, {})
        turn = self._night(worker, damaged={self.FRONT: (140, 1)}, ores={Pos(20, 10): "copper"})
        self.assertIsNone(night.repairer(turn, 1), "没包就不该有人去修墙")
        cmds = plan(turn)
        self.assertEqual(cmds["2"]["action"], "move", "朝矿走（挖矿那条线）")
        self.assertNotIn("use", self._actions(cmds))

    def test_the_worker_waits_behind_the_front_wall(self):
        """没有要修的 ⇒ 去正面墙**后方那一列**待命（不是盒外、也不是炮位）。"""
        worker = Worker(2, Pos(20, 20), {WALL_FIXER: 1})
        turn = self._night(worker)
        self.assertEqual(core._wall_post(turn, worker), self.POST, "待命位在盒内那一列")
        cmds = plan(turn)
        cell = Pos(cmds["2"]["targetPos"][0]["x"], cmds["2"]["targetPos"][0]["y"])
        self.assertLess(cell.dist(self.POST), Pos(20, 20).dist(self.POST), "这一步朝待命位去")

    def test_the_gunner_still_mans_the_guns(self):
        """修墙工不是炮手：同一回合里 `attack` 与 `use` 各一条、分属两个角色。"""
        gun = Weapon(id=90001, kind="gatling", pos=Pos(12, 25), attack_range=4, cooldown=0)
        worker = Worker(2, self.POST, {WALL_FIXER: 1})
        cmds = plan(self._night(worker, damaged={self.FRONT: (140, 1)}, weapons=(gun,)))
        self.assertEqual(cmds["90001"]["action"], "attack", f"炮手照旧开火：{cmds}")
        self.assertEqual(cmds["2"]["action"], "use")

    def test_the_repair_worker_walks_back_before_dark(self):
        """天黑前先回待命位（照收工门的思路）：白天只剩 1 回合 + 手里有包 ⇒ 往正面墙后方挪。

        金币给 0 是为了让"这一条 move"只可能来自回待命位那道闸门（不然他会朝商店走）。
        """
        worker = Worker(2, Pos(20, 20), {WALL_FIXER: 3})
        turn = self._turn(Pioneer(1, Pos(12, 24)), worker, round_no=460)  # 第 4 天最后一回合
        cmds = plan(turn._replace(gold=0))
        cell = Pos(cmds["2"]["targetPos"][0]["x"], cmds["2"]["targetPos"][0]["y"])
        self.assertLess(cell.x, worker.pos.x, f"朝盒子那一侧走（不是在朝商店）：{cell}")
        self.assertLess(cell.dist(self.POST), worker.pos.dist(self.POST), "朝待命位去")

    def test_an_early_day_does_not_call_him_back(self):
        """白天还早（剩 70 回合）就不叫他回 —— 他照旧干自己的活。"""
        worker = Worker(2, Pos(20, 20), {WALL_FIXER: 3})
        turn = self._turn(Pioneer(1, Pos(12, 24)), worker, round_no=391)  # 第 4 天第一回合
        turn = turn._replace(gold=0)
        self.assertNotIn("2", plan(turn), "白天还早就该接着干活，不是回待命位站着")

    def test_a_worker_without_a_pack_is_never_called_back(self):
        """没包的那个人不归这条线管（他照旧采矿；这一夜也不会派他修墙）。"""
        worker = Worker(2, Pos(20, 20), {})
        turn = self._turn(Pioneer(1, Pos(12, 24)), worker, round_no=460)
        turn = turn._replace(gold=0)
        self.assertIsNone(night.repairer(turn, 1))
        self.assertNotIn("2", plan(turn))

    def test_before_the_fourth_night_nobody_is_called_back(self):
        """第 4 夜之前不派修墙工 ⇒ 那份包还在背包里，人不往墙边走。"""
        worker = Worker(2, Pos(20, 20), {WALL_FIXER: 3})
        turn = self._turn(Pioneer(1, Pos(12, 24)), worker, round_no=330)  # 第 3 天最后一回合
        turn = turn._replace(gold=0)
        self.assertIsNone(night.repairer(turn, 1))
        self.assertNotIn("2", plan(turn))

    def test_a_pinned_pioneer_hands_the_repair_job_to_the_other_worker(self):
        """开拓者被任务钉死 ⇒ 炮手换成名册第一个工人（没包），持包的那个才是修墙工。"""
        pioneer = Pioneer(1, Pos(12, 24))
        gunner = Worker(2, Pos(12, 22), {})
        carrier = Worker(3, self.POST, {WALL_FIXER: 1})
        turn = self._turn(pioneer, gunner, carrier, damaged={self.FRONT: (140, 1)})
        turn = turn._replace(phase_task="一道题")
        cmds = plan(turn)
        self.assertEqual(cmds["3"]["action"], "use", f"持包的那个才是修墙工：{cmds}")
        self.assertNotIn("2", cmds, "顶炮位的那个工人不去修墙")


if __name__ == "__main__":
    unittest.main()
