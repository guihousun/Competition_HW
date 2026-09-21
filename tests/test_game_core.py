"""game/core.py 共用底座的用例：`_priciest_ore` 那条排序（白天第 1/3 级与夜里清场后共用）。

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
from coregeek.game.core import _collect, _priciest_ore  # noqa: E402
from coregeek.game.grid import Pos  # noqa: E402
from coregeek.game.map import Map  # noqa: E402
from coregeek.game.roles import Worker  # noqa: E402
from coregeek.game.world import Turn  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
