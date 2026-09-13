"""指令 → 线上报文。**唯一写线上格式的地方。**

判题器把"指令非法"计为一次异常，累计 5 次该队整场不再被调度 ——
所以**所有**指令都必须在这里生成。以后加动作时合法性闸门也在这一处加，
尤其是任务书 §4.4 表**最右列**的角色权限：

    build / remove / collect                   → 仅**工人**
    acceptTask / submitAnswer / summonTreasure → 仅**开拓者**
    其余动作                                     → 全部角色

当前只有 `move`，它对全部角色可用，所以还不需要权限闸门；等第一个受限动作
（建武器 / 采矿）落地时在这里拦一道。
"""

from typing import Any

from ..game.grid import Pos


def move(target: Pos) -> dict[str, Any]:
    return {"action": "move", "targetPos": [_xy(target)]}


def _xy(pos: Pos) -> dict[str, int]:
    return {"x": pos.x, "y": pos.y}
