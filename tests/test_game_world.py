"""game/world.py 的用例：`Turn.summary()` 的四块版面与上界、昼夜判定。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import sys
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用（discover 不一定把它放进 sys.path）；src/ 给 coregeek 用
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coregeek.app import handle  # noqa: E402
from coregeek.game.grid import Pos  # noqa: E402
from coregeek.game.map import Map  # noqa: E402
from coregeek.game.roles import Pioneer, Worker  # noqa: E402
from coregeek.game.world import Robot, Turn, Weapon  # noqa: E402


def _blocks(summary: str) -> list[str]:
    """`Turn.summary()` → 非空行。

    摘要按块拼接（`【回合】 / 【我方】 / 【机器】 / 【可接任务点】` 各占一块），
    块数固定但物理行数是数据相关的（一块里放不下会换行）。
    断言块内容就够 —— 空行只是给人眼看的，钉住它反而会把"某块变长了"误报成格式错。
    """
    return [line for line in summary.splitlines() if line]


class TurnSummaryTest(unittest.TestCase):
    """`Turn.summary()` 的四块摘要。

    它跑在 `app.handle` 的 `try` 里 —— 抛异常 = 整回合退化成空指令，
    所以"空局面不炸"与"长度有上界"和内容一样重要。
    """

    def _turn(self, **kw) -> Turn:
        base = dict(
            round_no=85,  # 夜里
            map=Map((41, 32), {Pos(0, 0): "stone"}),
            roles=(),
            gold=20,
        )
        base.update(kw)
        return Turn(**base)

    def test_a_full_turn_reports_every_fact(self):
        turn = self._turn(
            roles=(
                Worker(10010, Pos(5, 23), {"stone": 1}),
                Worker(10012, Pos(10, 16), {"stone": 1}),
                Pioneer(10011, Pos(10, 12)),
            ),
            gold=20,
            weapons=(
                Weapon(10020, "gatling", Pos(9, 24), 4, 0),
                Weapon(10030, "railgun", Pos(10, 25), 7, 0),
                # 火箭刚打完一发，样例里没有 `cooldown` 字段 ⇒ 这一条只能合成
                Weapon(10040, "rocket", Pos(9, 25), 2**31 - 1, 3),
            ),
            robots=(Robot(Pos(4, 4), 40), Robot(Pos(5, 5), 800)),
            task_points=(Pos(14, 14), Pos(17, 17)),
        )
        lines = _blocks(turn.summary())
        self.assertEqual(len(lines), 4)
        self.assertEqual(
            lines[0],
            "【回合】 85（夜里） ｜ 【金币】 20 | 【武器】 3/3："
            "10020 gatling(9,24)L1r4 10030 railgun(10,25)L1r7 10040 rocket(9,25)L1r∞c3",
        )
        self.assertEqual(
            lines[1],
            "【我方】 10010 worker(5,23)石1铁0铜0 ｜ 10012 worker(10,16)石1铁0铜0"
            " ｜ 10011 pioneer(10,12)石0铁0铜0",
        )
        self.assertEqual(lines[2], "【机器】 2 台：(4,4)h40 (5,5)h800")
        self.assertEqual(lines[3], "【可接任务点】 (14,14) (17,17)")

    def test_an_empty_turn_still_prints_every_block(self):
        """空局面：一条事实都没有，但每一块都得有字（`无` / `0 台`），不能是空行。"""
        lines = _blocks(self._turn(roles=(Worker(10010, Pos(5, 23)),)).summary())
        self.assertEqual(len(lines), 4)
        self.assertIn("【武器】 0/1：无", lines[0])
        self.assertIn("【机器】 0 台：无", lines[2])
        self.assertIn("【可接任务点】 无", lines[3])

    def test_missing_fields_are_not_reported_as_zero(self):
        """`-1` 是"字段缺失"，不是 0 金 / 第 -1 回合 —— 打成 `?`，别让它看着像个事实。"""
        line = _blocks(self._turn(gold=-1, round_no=-1).summary())[0]
        self.assertIn("【回合】 ?（夜里）", line)
        self.assertIn("【金币】 ?", line)

    def test_the_unknown_range_is_not_printed_as_a_negative_number(self):
        """射程 -1 = 字段缺失 ⇒ 够不着 ⇒ `?`；0 也照原样打 0（不做特殊处理）。"""
        turn = self._turn(
            roles=(Worker(10010, Pos(5, 23)),),
            weapons=(
                Weapon(1, "gatling", Pos(9, 24), -1, -1),
                Weapon(2, "gatling", Pos(9, 25), 0, 0),
            ),
        )
        line = _blocks(turn.summary())[0]
        self.assertIn("1 gatling(9,24)L1r?", line)
        self.assertIn("2 gatling(9,25)L1r0", line)
        # 冷却 -1 与 0 都是"没有冷却"，都不该出现 `c`
        self.assertNotIn("c-1", line)
        self.assertNotIn("r0c0", line)

    def test_long_lists_are_capped(self):
        """机器人是逐回合全量推送的 ⇒ 摘要长度必须有上界，超出的只报个数。"""
        turn = self._turn(
            roles=(Worker(10010, Pos(5, 23)),),
            robots=tuple(Robot(Pos(i, 5), 10 * i) for i in range(11)),
        )
        line = _blocks(turn.summary())[2]
        self.assertIn("【机器】 11 台：", line)
        self.assertIn("…+3", line)  # 11 - SUMMARY_MAX_ITEMS(8) = 3
        self.assertNotIn("(10,5)", line)  # 第 11 台没打出来


class DayNightTest(unittest.TestCase):
    """日历：`build` 仅白天，判反了就会在夜里发 `build`（一次执行失败）。

    `within = (roundNo-1) % 130 + 1`，缺失的回合号是 -1 ⇒ 判成夜里 ⇒ 不建造。"""

    def _turn(self, round_no: int) -> Turn:
        return Turn(round_no=round_no, map=Map((41, 32), {}), roles=(), gold=0)

    def test_is_day_boundaries(self):
        """130 回合 1 天 = 白 70 + 夜 60（任务书 L90），`within % 130` 从 1 起数。"""
        for round_no, day in ((1, True), (70, True), (71, False), (130, False), (131, True)):
            with self.subTest(round_no=round_no):
                self.assertIs(self._turn(round_no).is_day, day)
        self.assertFalse(self._turn(-1).is_day, "roundNo 缺失 ⇒ 判成夜里 ⇒ 不建造")


if __name__ == "__main__":
    unittest.main()
