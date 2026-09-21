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

from _fixtures import _terrain  # noqa: E402
from coregeek.game.core import _priciest_ore  # noqa: E402
from coregeek.game.grid import Pos  # noqa: E402
from coregeek.game.map import Map  # noqa: E402
from coregeek.game.roles import Worker  # noqa: E402
from coregeek.game.world import Turn  # noqa: E402


class PriciestOreTest(unittest.TestCase):
    """`_priciest_ore`：两个时段共用的那条挖矿排序。

    只看小贩收购价（**不读新闻修正**、也不看路程远近），并列取近的、再取坐标序。
    剔掉三类：本回合已被别人认领的（`ore_taken`）、走不到的（BFS -1）、小贩不收的（价 ≤ 0）。
    """

    BASE = Pos(10, 24)
    NEAR = Pos(4, 24)
    FAR = Pos(20, 30)
    PRICES = {"stone": 1, "iron": 3, "copper": 5}

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
        """铜（5 金）比脚边的石头（1 金）值钱 ⇒ 哪怕远也去铜。

        与旧口径的区别：那时还按"价 ÷ 来回回合"算性价比，近的贱矿可能跑赢远的贵矿 —— 新口径
        只看价（用户口径"挖今天最贵的矿"）。
        """
        self.assertEqual(self._pick({self.NEAR: "stone", self.FAR: "copper"}), self.FAR)

    def test_a_mine_the_vendor_does_not_buy_is_not_worth_a_step(self):
        """小贩不收的矿（价 0）不为它多走一步 —— 一张空价目表也就自然落成"谁也不采"。"""
        self.assertIsNone(self._pick({self.NEAR: "copper"}, prices={}))
        self.assertEqual(self._pick({self.NEAR: "copper", self.FAR: "stone"}, prices={"stone": 1}), self.FAR)

    def test_an_unreachable_mine_is_skipped(self):
        """走不到的矿（BFS -1）剔掉 —— 剩下的最值钱的那座顶上。"""
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

    def test_ties_break_by_distance_then_position(self):
        """同价取近的 —— 两座都是铜时，近的那座赢。"""
        near, far = Pos(9, 24), Pos(30, 30)
        turn = self._turn({near: "copper", far: "copper"})
        self.assertEqual(_priciest_ore(turn.roles[0], turn, set()), near)


if __name__ == "__main__":
    unittest.main()
