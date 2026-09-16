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
from coregeek.game.grid import Pos  # noqa: E402
from coregeek.game.map import Map  # noqa: E402
from coregeek.game.planner import plan  # noqa: E402
from coregeek.game.roles import BaseRole, Pioneer, Worker  # noqa: E402
from coregeek.game.world import Robot, Turn, Weapon  # noqa: E402
from coregeek.protocol import model  # noqa: E402


class NightWeaponTest(unittest.TestCase):
    """夜里：所有角色（含开拓者）回炮位、开火打最大伤害落点（方针：打死所有机器人，
    不挑残血补刀）。

    `attack` 仅黑夜可用、`build`/`collect` 仅工人（任务书 §4.4），所以夜里
    除了 `move` 就只该有 `attack`。
    """

    BASE = Pos(10, 24)
    NIGHT = 85
    NEAR, FAR = Pos(12, 25), Pos(9, 22)  # 两座加特林
    REACH = 4  # 加特林 L1 的射程，取自样例 payload（任务书表格写的是 3）
    GUN = 10020  # `_manned` 那座炮的 id —— `attack` 的 key 就是它

    def _turn(
        self,
        *roles: BaseRole,
        weapons: tuple[Weapon, ...] | None = None,
        robots: tuple[Robot, ...] | None = None,
        round_no: int | None = None,
    ) -> Turn:
        # `weapons=None` 才是"用默认的两座"；不能用 `or` —— 空元组也是假值，
        # 那样 `test_no_weapons_means_nothing_to_do` 会静默拿到两座武器、白测一场。
        weapons = self._guns(self.NEAR, self.FAR) if weapons is None else weapons
        # `robots=None`（默认）给一台在场机器人：怪清完的夜里工人会走经济线 ——
        # 本类测的是防守线，得让防守分支真的命中（`robots=()` 可显式给空）。
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
        """一名工人已经贴着那座炮（切比雪夫 1）—— 开火与否只看目标与冷却。"""
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
        # 两个角色到 (12,25) 都是 2 格，最近的都是它；没有去重的话 2 号也会奔它去
        self.assertGreater(second.dist(self.NEAR), 1, "2 号必须换一座，不能也去挤 (12,25)")
        self.assertLess(second.dist(self.FAR), 5, "换的那座该是下一近的 (9,22)")

    def test_no_weapons_means_nothing_to_do(self):
        """武器全没了 ⇒ 不动（空指令合法），而不是瞎走。"""
        self.assertEqual(plan(self._turn(Worker(1, Pos(14, 26)), weapons=())), {})

    def test_the_pioneer_also_mans_a_weapon(self):
        """开拓者也在炮位上（§4.4 里 `attack` 的可用角色是"全部"）。

        漏掉的话本地全绿，只是整夜少一门火力 —— 而它恰恰是离炮最远的那个。
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
        """火箭的最大伤害落点：中心 20 + 周围 8 格溅射 10（任务书 §4.5.4）
        ⇒ 落进机器人簇里、落在最肥的那台身上 —— 而不是挑残血的。

        簇：(13,25) 40 血与 (14,25) 8 血相邻。落 (13,25) = 20+8=28 分；落 (14,25) =
        8+10=18 分 ⇒ 落在 40 血那台身上。"""
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
        """同回合记账：先开火的炮把伤害记在账上（`assigned`），后开的按剩余血挑
        —— 两座炮不挤同一个将死的目标（方针是打死所有，不集火补刀）。

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

    def test_the_rocket_pair_alternates_without_cooldown_data(self):
        """双火箭组的交替开火不依赖 payload 的 `cooldown` 字段（样例不带 ⇒ -1）：
        就绪的炮按回合号轮转。只按 id 挑的话，两座都"就绪"时永远只发 id 小的那座，
        第二座整晚哑火。"""
        rockets = (
            Weapon(id=200, kind="rocket", pos=Pos(12, 24), attack_range=10, cooldown=-1),
            Weapon(id=201, kind="rocket", pos=Pos(12, 25), attack_range=10, cooldown=-1),
        )
        fired = []
        for round_no in (85, 86):
            turn = self._turn(
                Worker(1, Pos(11, 25)),  # 双火箭的操作位：同时贴着两座
                weapons=rockets,
                robots=(Robot(Pos(15, 24), 40),),
                round_no=round_no,
            )
            cmds = plan(turn)
            self.assertEqual(len(cmds), 1, cmds)
            fired.append(int(next(iter(cmds))))
        self.assertEqual(fired, [201, 200], "两回合各发一座（round 85 奇 ⇒ 先发 id 大的，偶 ⇒ 先发 id 小的）")

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


from coregeek.game.grid import Pos  # noqa: E402
from coregeek.game.map import Map  # noqa: E402
from coregeek.game.planner import NIGHT_WANDER, plan  # noqa: E402
from coregeek.game.roles import BaseRole, Pioneer, Worker  # noqa: E402
from coregeek.game.world import Robot, Turn, Weapon  # noqa: E402
from coregeek.protocol import model  # noqa: E402


class NightEconomyTest(unittest.TestCase):
    """夜里怪清完（视野内无机器人）⇒ 工人出门近矿经济。

    机器人列表是视野过滤的 —— "清完"只是看不见；近矿限制（回炮位 BFS ≤
    `NIGHT_WANDER`）让工人出得了事也回得了家。build/remove 夜里非法，
    夜间经济只发 collect / move / sell。"""

    BASE = Pos(10, 24)
    WEAPONS = (Weapon(200, "gatling", Pos(9, 23), 4, 0),)
    NEAR = Pos(5, 24)   # 门侧近矿（出盒穿背面那 2 格门，BFS ≤ 8）
    FAR = Pos(30, 4)    # 远矿（超出门槛 ⇒ 不去）

    def _turn(self, worker: Worker, ore: Pos, robots=(), near_ore: bool = True):
        grid = _terrain(
            self.WEAPONS,
            {self.BASE: "station", ore: "iron"}
            | ({self.NEAR: "stone"} if near_ore else {}),
            {c: "wall" for c in self.ring()},
        )
        grid[worker.pos] = "worker"
        return Turn(
            round_no=85, map=Map((41, 32), grid), roles=(worker,), gold=0,
            weapons=self.WEAPONS, robots=robots,
            vendor_prices={"iron": 4, "stone": 1},
        )

    def ring(self):
        from coregeek.game.grid import wall_cells

        return wall_cells(self.BASE, 41)

    def test_a_cleared_night_sends_workers_to_near_ore(self):
        """怪清完 + 近矿 ⇒ 出门走两步（夜里 collect 合法，§4.4 没给它写昼夜）。"""
        worker = Worker(10010, Pos(12, 24), {})
        cmd = plan(self._turn(worker, self.NEAR)).get("10010")
        self.assertIsNotNone(cmd, "清完的夜里工人不该干等")
        self.assertIn(cmd["action"], ("move", "collect"))

    def test_the_near_mine_limit_keeps_workers_close(self):
        """远矿超出门槛 ⇒ 不出门（出得了事回不了家的活不干）。"""
        worker = Worker(10010, Pos(12, 24), {})
        self.assertNotIn("10010", plan(self._turn(worker, self.FAR, near_ore=False)))

    def test_night_economy_never_builds(self):
        """夜里经济线绝不发 build（`build` 仅白天，§4.4）—— 环上留多少缺口都一样。"""
        worker = Worker(10010, Pos(12, 24), {"stone": 5})
        grid = _terrain(
            self.WEAPONS,
            {self.BASE: "station", self.NEAR: "stone"},  # 环一块没砌
        )
        grid[worker.pos] = "worker"
        turn = Turn(
            round_no=85, map=Map((41, 32), grid), roles=(worker,), gold=0,
            weapons=self.WEAPONS,
        )
        cmds = plan(turn)
        self.assertTrue(all(v["action"] != "build" for v in cmds.values()), cmds)

    def test_robots_present_at_night_still_defend(self):
        """视野里有机器人 ⇒ 照旧回炮位开火，不出门采矿。"""
        worker = Worker(10010, Pos(12, 24), {})
        turn = self._turn(
            worker,
            self.NEAR,
            robots=(Robot(pos=Pos(13, 24), health=40),),
        )
        cmds = plan(turn)
        self.assertTrue(
            all(v["action"] in ("attack", "move") for v in cmds.values()),
            f"夜里只该有 attack/move：{cmds}",
        )


class StationUpgradeTest(unittest.TestCase):
    """夜里的基地升级：持基地券 + 基地血量 < 满血 1/4
    ⇒ 贴基地 `use`（升级 + 回满血一次到位，1500/3000/4500 是任务书表格实证）。
    就算视野里有机器人也升 —— 基地要塌了这是救命的（升级当回合放弃开火）。"""

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
