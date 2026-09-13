"""决策入口：payload → `roleCommandMap`。

当前是**空实现**，只把链路跑通，不派发任何动作。
后续每一步往这里填真策略；`app.py` 只管"报文 ↔ 决策"的转换，不碰策略。

返回 `{角色ID: 指令}`。key 用**字符串** —— JSON 对象的 key 本来就是字符串。
"""

from typing import Any


def plan(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {}
