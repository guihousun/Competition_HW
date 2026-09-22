"""game/core.py 共用底座的用例：`_priciest_ore` 那条排序（白天第 1/3 级与夜里清场后共用）
与新闻钉住的 10 天矿价表（`record_news` / `_expected_price` / `_mineable`）。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。

底座里别的东西（`_Move` / `_Queue` / `_Ctx` / `_emit` / 岗位几何）由两个时段的用例间接盖着：
`test_game_day.py` 走白天那条链，`test_game_night.py` 走夜里那条。
"""

import sys
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用（discover 不一定把它放进 sys.path）；src/ 给 coregeek 用
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _fixtures import _reset_ledgers  # noqa: E402
from _fixtures import _terrain  # noqa: E402
from coregeek.game import core  # noqa: E402
from coregeek.game.core import _collect, _expected_price, _priciest_ore  # noqa: E402
from coregeek.game.grid import Pos  # noqa: E402
from coregeek.game.map import Map  # noqa: E402
from coregeek.game.roles import Worker  # noqa: E402
from coregeek.game.world import DAYS, ROUNDS_PER_DAY, Turn, Wall  # noqa: E402


class PriciestOreTest(unittest.TestCase):
    """`_priciest_ore`：两个时段共用的那条挖矿排序 —— 按**实际单价**挑。

    实际单价（用户口径）= `单价 × 剩余 / (剩余 + 去 + 回)`：把这座矿采空的平均收益，
    去/回是 BFS 真实步数（回 = 矿 → 最近的武器位）。⇒ 不是"标定单价最贵"，远矿会被回程
    拖下来；**采空的矿（本地账记到 `ORE_CHARGES`）与小贩不收的（价 ≤ 0）一律不为它多走一步**。
    """

    BASE = Pos(10, 24)
    NEAR = Pos(4, 24)
    FAR = Pos(20, 30)
    PRICES = {"stone": 1, "iron": 3, "copper": 5}

    def setUp(self) -> None:
        _reset_ledgers()

    def _turn(self, ores: dict[Pos, str], *, prices: dict[str, int] | None = None) -> Turn:
        grid = _terrain((), {self.BASE: "station"}, ores, {Pos(12, 24): "worker"})
        return Turn(
            round_no=1,
            map=Map((41, 32), grid),
            roles=(Worker(10010, Pos(12, 24)),),
            gold=0,
            vendor_prices=self.PRICES if prices is None else prices,
        )

    def _pick(self, ores, **kwargs) -> Pos | None:
        turn = self._turn(ores, **kwargs)
        role = turn.roles[0]
        return _priciest_ore(role, turn, kwargs.pop("taken", set()))

    def test_the_pricier_ore_wins_over_the_nearer_one(self):
        """铜（5 金）比脚边的石头（1 金）值钱 ⇒ 哪怕远也去铜（价差压得过回程）。"""
        self.assertEqual(self._pick({self.NEAR: "stone", self.FAR: "copper"}), self.FAR)

    def test_a_close_cheap_mine_beats_a_very_far_pricier_one(self):
        """**不是纯按标定单价**（第 122 步的用户口径）：铜（5 金）在 19 格外、铁（3 金）就在
        6 格外 ⇒ 实际单价 铁 1.5 > 铜 1.0 ⇒ 去铁。

        旧口径（只比 `vendor_prices`）在这里会挑铜 —— 这条就是那条口径的反面。
        """
        self.assertEqual(self._pick({Pos(6, 25): "iron", Pos(30, 30): "copper"}), Pos(6, 25))

    def test_a_mine_the_vendor_does_not_buy_is_not_worth_a_step(self):
        """小贩不收的矿（价 0）不为它多走一步 —— 一张空价目表也就自然落成"谁也不采"。"""
        self.assertIsNone(self._pick({self.NEAR: "copper"}, prices={}))
        self.assertEqual(
            self._pick({self.NEAR: "copper", self.FAR: "stone"}, prices={"stone": 1}), self.FAR
        )

    def test_an_unreachable_mine_is_skipped(self):
        """走不到的矿（BFS -1）剔掉 —— 剩下的那座顶上。"""
        grid = {
            self.BASE: "station",
            Pos(12, 24): "worker",
            self.NEAR: "stone",
            self.FAR: "copper",
            **{Pos(20, y): "wall" for y in range(0, 32)},  # 把远矿整列封死
        }
        turn = Turn(
            round_no=1,
            map=Map((41, 32), grid),
            roles=(Worker(10010, Pos(12, 24)),),
            gold=0,
            vendor_prices=self.PRICES,
        )
        self.assertEqual(_priciest_ore(turn.roles[0], turn, set()), self.NEAR)

    def test_a_claimed_mine_is_skipped(self):
        """本回合已被同事认领的矿格跳过（两个工人才不会都奔同一座）。"""
        ores = {self.NEAR: "copper", self.FAR: "copper"}
        turn = self._turn(ores)
        self.assertEqual(_priciest_ore(turn.roles[0], turn, {self.NEAR}), self.FAR)

    def test_a_depleted_mine_stops_attracting(self):
        """采空的矿不再吸引人：本地账记满 `ORE_CHARGES`(10) ⇒ 实际单价归零 ⇒ 换旁边那座。"""
        ores = {self.NEAR: "copper", self.FAR: "copper"}
        core._collected[self.NEAR] = core.ORE_CHARGES
        turn = self._turn(ores)
        self.assertEqual(_priciest_ore(turn.roles[0], turn, set()), self.FAR)

    def test_the_ledger_alone_never_starves_the_line(self):
        """账本把候选全清空了 ⇒ **当没账本再挑一遍**（自我修复），绝不静默关掉整条挖矿线。

        "跨回合状态卡住 ⇒ 整条线静默不动"是本项目踩过的老病（第 11 步的教训）；这本账只该
        影响排序，不该影响"去不去"。
        """
        core._collected[self.NEAR] = core.ORE_CHARGES
        turn = self._turn({self.NEAR: "copper"})
        self.assertEqual(
            _priciest_ore(turn.roles[0], turn, set()), self.NEAR, "退化成不看账本，照挑"
        )

    def test_collecting_records_the_ledger(self):
        """`_collect` 是唯一记账的出口：发一条采集指令就记一笔（那本账是"还剩几块"的来源）。"""
        turn = self._turn({self.NEAR: "copper"})
        cmds: dict = {}
        self.assertTrue(_collect(cmds, turn.roles[0], self.NEAR))
        self.assertEqual(core._collected[self.NEAR], 1)
        self.assertEqual(cmds["10010"]["action"], "collect")

    def test_ties_break_by_distance_then_position(self):
        """实际单价并列时取近的 —— 两座同价同剩余时，近的那座赢。"""
        near, far = Pos(9, 24), Pos(30, 30)
        turn = self._turn({near: "copper", far: "copper"})
        self.assertEqual(_priciest_ore(turn.roles[0], turn, set()), near)


class NewsPriceTableTest(unittest.TestCase):
    """新闻钉住的未来 k 天：`core._prices` / `core._blocked` 与那条唯一的读价口径。

    三条不变量：① **当天价永远是载荷里的实测价** —— 预报只在未来天顶用，一到那天就作废
    （判题器真涨价了当天就看得见）；② **没新闻 ⇒ 与"只读载荷"逐字相同**（没钉过的天回落实测价）；
    ③ 表只影响**排序偏好**，不碰报文 ⇒ 最坏是多跑一趟，不碰红线。
    行是 LLM 从新闻原文里读出来的（`chat.is_prices_reply` 给 `(矿种, 方向, 起始天, 天数)`）。
    """

    BASE = Pos(10, 24)
    #: 近处铁 3 金（去 6 / 回 4 ⇒ 实际单价 1.5）与远处铜 5 金（去 18 / 回 20 ⇒ 1.04）
    #: —— 今天铁赢，把明天的铜钉到 10（2.08）就该反过来。
    IRON = Pos(6, 25)
    COPPER = Pos(30, 30)
    PRICES = {"stone": 1, "iron": 3, "copper": 5}

    def setUp(self) -> None:
        _reset_ledgers()

    def _turn(
        self, ores: dict[Pos, str], *, day: int = 1, prices: dict[str, int] | None = None
    ) -> Turn:
        grid = _terrain((), {self.BASE: "station"}, ores, {Pos(12, 24): "worker"})
        return Turn(
            round_no=(day - 1) * ROUNDS_PER_DAY + 1,
            map=Map((41, 32), grid),
            roles=(Worker(10010, Pos(12, 24)),),
            gold=0,
            vendor_prices=self.PRICES if prices is None else prices,
        )

    def _pick(self, turn: Turn, **kwargs) -> Pos | None:
        return _priciest_ore(turn.roles[0], turn, kwargs.pop("taken", set()), **kwargs)

    def _pinned(self) -> list[int]:
        """表里写过东西的格号（1 起算的第几天）—— 断言"哪些天被钉了"用它。"""
        return [day for day, slot in enumerate(core._prices, 1) if slot]

    def test_a_pinned_day_changes_the_ranking_only_when_looking_ahead(self):
        """新闻说"明天铜涨"⇒ **只有 `ahead=1`（夜里那条线）改挑远铜**，当天的排序一个字不动。
        夜里采的货第二天才卖 —— 这是"明天"进得了决策的唯一入口。"""
        ores = {self.IRON: "iron", self.COPPER: "copper"}
        turn = self._turn(ores)
        core.record_news([("copper", "up", 1, 1)], turn)  # 起始天 1 = 明天
        self.assertEqual(core._prices[1]["copper"], 10, "以当天实测价为基准 ×2")
        self.assertEqual(self._pick(turn), self.IRON, "今天照实测价：铁 1.5 > 铜 1.04")
        self.assertEqual(self._pick(turn, ahead=1), self.COPPER, "明天铜 10 ⇒ 2.08 > 1.5")

    def test_the_real_price_wins_once_the_day_comes(self):
        """预报只顶到那一天为止：第 2 天一到，价格就是当天载荷里的实测价（哪怕跌回 1 金）。
        ⇒ 预报错也错不到真值上，错的只是**前一天夜里**怎么挑矿。"""
        core.record_news([("copper", "up", 1, 1)], self._turn({}))
        tomorrow = self._turn({}, day=2, prices={"stone": 1, "iron": 3, "copper": 1})
        self.assertEqual(_expected_price(tomorrow, "copper", 2), 1)
        self.assertEqual(core._prices[1]["copper"], 10, "表里的预报还在，只是不再被读")

    def test_a_stopped_ore_is_mined_today_but_not_tomorrow(self):
        """`stop` 进的是**可采性**表、而且只看"今天"：窗口从明天起 ⇒ **今夜照挖**
        （今天还采得了），第二天那座矿才被剔掉，窗口过完又回来。"""
        ores = {self.IRON: "iron"}
        core.record_news([("iron", "stop", 1, 1)], self._turn(ores))
        self.assertEqual(self._pick(self._turn(ores)), self.IRON, "今天没停工 ⇒ 照挖")
        self.assertIsNone(self._pick(self._turn(ores, day=2)), "第 2 天停工 ⇒ 不为它跑一趟")
        self.assertEqual(self._pick(self._turn(ores, day=3)), self.IRON, "窗口过完 ⇒ 又是它")
        self.assertEqual(core._prices[1], {}, "停工不写价格表")

    def test_flat_and_an_unpriced_ore_are_not_pinned(self):
        """`flat` = 没影响、基准价 ≤ 0（小贩不收）⇒ 什么都不写：回落值本来就是实测价，
        写一遍只是把"照旧"变成一条假预报。"""
        core.record_news([("copper", "flat", 1, 1)], self._turn({}))
        core.record_news([("iron", "up", 1, 1)], self._turn({}, prices={"stone": 1}))
        self.assertEqual(self._pinned(), [])

    def test_the_window_is_clipped_to_the_last_day(self):
        """连着 30 天也钉不出第 11 格（任务书就是 10 天，表就 `DAYS` 格）；
        起点出表（第 99 天）⇒ 整条丢。"""
        core.record_news([("copper", "up", 1, 30)], self._turn({}))
        self.assertEqual(self._pinned(), list(range(2, DAYS + 1)), "今天那格从不写")
        _reset_ledgers()
        core.record_news([("copper", "up", 99, 1)], self._turn({}))
        self.assertEqual(self._pinned(), [])

    def test_the_table_says_so_in_the_log_when_it_changes(self):
        """有实质改动才打**一条**（方向是 LLM 猜的，复盘要看得到它猜了什么）；
        同一份预报再来一遍 ⇒ 静默，不反复刷屏。"""
        turn = self._turn({})
        events = [("iron", "up", 1, 2), ("copper", "stop", 1, 1)]
        with self.assertLogs("coregeek.game.core", level="INFO") as logs:
            core.record_news(events, turn)
        self.assertIn("【价格表】", logs.output[0])
        self.assertIn("iron", logs.output[0])
        with self.assertNoLogs("coregeek.game.core", level="INFO"):
            core.record_news(events, turn)


class WallThresholdTest(unittest.TestCase):
    """修墙 / 拆墙共用的那道血量线**随天数抬**（用户口径"机器人的进攻更猛了"）：

    第 1–4 天 `WALL_REPAIR_HP`(200)，第 5 天起每天 +`WALL_REPAIR_HP_STEP`(50) ⇒ 第 5 天 250、
    第 10 天 500。一个阈值两条线（白天 `day._weak_l1` 拆了重砌、夜里 `night.repair_wall` 用包
    回满）⇒ 抬它会同时抬高两边。
    """

    BASE = Pos(10, 24)

    def _turn(self, round_no: int) -> Turn:
        return Turn(
            round_no=round_no,
            map=Map((41, 32), {self.BASE: "station"}),
            roles=(),
            gold=0,
            weapons=(),
            robots=(),
        )

    def test_the_threshold_stands_still_for_the_first_four_days(self):
        for day in (1, 4):
            turn = self._turn((day - 1) * ROUNDS_PER_DAY + 1)
            self.assertEqual(turn.day_no, day, "夹具的回合号该对得上第几天")
            self.assertEqual(core.wall_repair_hp(turn), core.WALL_REPAIR_HP)

    def test_the_threshold_climbs_from_the_fifth_day(self):
        for day in (5, 6, 10):
            turn = self._turn((day - 1) * ROUNDS_PER_DAY + 1)
            self.assertEqual(turn.day_no, day)
            self.assertEqual(
                core.wall_repair_hp(turn),
                core.WALL_REPAIR_HP + core.WALL_REPAIR_HP_STEP * (day - core.WALL_REPAIR_HP_FROM_DAY + 1),
                f"第 {day} 天",
            )

    def test_needs_repair_follows_the_day(self):
        """同一面 240 血的墙：第 4 天不碰、第 6 天该动（240 < 300）。血量未知（-1）永远不碰。"""
        wall = Wall(40000, Pos(13, 23), 240, 1)
        self.assertFalse(core.needs_repair(wall, self._turn(4 * ROUNDS_PER_DAY)), "第 4 天 240 > 200")
        self.assertTrue(core.needs_repair(wall, self._turn(6 * ROUNDS_PER_DAY)), "第 6 天 240 < 300")
        unknown = Wall(40001, Pos(13, 23), -1, 1)
        self.assertFalse(core.needs_repair(unknown, self._turn(6 * ROUNDS_PER_DAY)))

if __name__ == "__main__":
    unittest.main()
