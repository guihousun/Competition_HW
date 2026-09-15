"""跨测试文件共用的夹具。discover 的 pattern 是 `test*.py`，本文件不会被当成用例收集；
只放**被 ≥2 个文件用**的东西，单文件用的留在各自文件里。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。⚠️ 用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import sys
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用（discover 不一定把它放进 sys.path）；src/ 给 coregeek 用
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coregeek.game.grid import Pos  # noqa: E402
from coregeek.game.world import Weapon  # noqa: E402
from coregeek.protocol import model  # noqa: E402


SAMPLE = Path(__file__).resolve().parents[1] / "docs" / "request.txt"


def _records(kinds: dict[Pos, str], attack_range: int = 4) -> tuple[Weapon, ...]:
    """`{落点: 类别}` → 武器名册。**夹具的唯一真相是记录，地形由它推。**

    方向与 `protocol.model` 一致（单位 → 网格），不是反过来从网格里的类别串凭空造记录 ——
    第 8 步踩过"夹具与真实路径不同形、于是连旧代码都放过去"的坑。
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
