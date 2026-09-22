"""跨测试文件共用的夹具。discover 的 pattern 是 `test*.py`，本文件不会被当成用例收集；
只放被 ≥2 个文件用的东西，单文件用的留在各自文件里。

跑法：`PYTHONUTF8=1 py tests/<本文件>`（全量：`py -m unittest discover -s tests -v`）。
必须用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import sys
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用、src/ 给 coregeek 用（discover 跑时前者不一定在 sys.path 里）
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coregeek.game import core  # noqa: E402
from coregeek.game.grid import Pos  # noqa: E402
from coregeek.game.world import Weapon  # noqa: E402
from coregeek.protocol import model  # noqa: E402


SAMPLE = Path(__file__).resolve().parents[1] / "docs" / "request.txt"


def _records(kinds: dict[Pos, str], attack_range: int = 4) -> tuple[Weapon, ...]:
    """`{落点: 类别}` → 武器名册。夹具的唯一真相是记录，地形由它推。

    方向与 `protocol.model` 一致（单位 → 网格）：不要从网格里的类别串反向造记录 —— 夹具与
    真实路径不同形的话，坏的实现也能过用例。
    """
    return tuple(
        Weapon(id=100 + i, kind=kind, pos=pos, attack_range=attack_range, cooldown=0)
        for i, (pos, kind) in enumerate(kinds.items())
    )


def _terrain(weapons: tuple[Weapon, ...], *layers: dict[Pos, str]) -> dict[Pos, str]:
    """名册 → 网格里那几格（武器一样挡路）。与 `_records` 配套，方向只有这一个。"""
    grid: dict[Pos, str] = {w.pos: w.kind for w in weapons}
    for layer in layers:
        grid.update(layer)
    return grid


if __name__ == "__main__":
    unittest.main()


def _reset_ledgers() -> None:
    """把模块级的跨回合本地账清零（`core._collected`：我们采过每座矿几次；`core._prices` /
    `core._blocked`：新闻钉住的未来 k 天）。

    用例隔离用 —— 单实例/模块级状态在同一个测试进程里会跨用例串味（与 `night._fired` 同一条
    规矩）。加了新的模块级账就补在这里，别撒到各个用例类里去。
    """
    core._collected.clear()
    for pinned in core._prices:
        pinned.clear()
    for stopped in core._blocked:
        stopped.clear()
