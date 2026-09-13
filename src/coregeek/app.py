"""组装点：线上报文 → 决策 → 线上报文。

唯一不可妥协的约束：**永远返回合法 JSON，永不抛异常**。
判题器把"响应格式错误"计为一次异常，累计 5 次该队就不再被调度 ——
所以下面那个 `except` 不是防御性编程，它就是红线本身。

响应的三个顶层字段永远都在（接口文档 §2.1）。官方 demo 只发了 `roleCommandMap`。
"""

import json
import logging
from typing import Any

from . import planner

LOGGER = logging.getLogger(__name__)

#: 空指令集是**合法**的，且不计异常。任何失败路径都退到这里。
EMPTY_BODY = b'{"roleCommandMap":{},"prompt":"","executeCmd":""}'


def handle(raw: bytes) -> bytes:
    """处理一个回合。**不抛异常**，返回的字节永远是合法响应。"""
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
        if not isinstance(payload, dict):
            raise ValueError("body 不是 JSON 对象")
        commands = planner.plan(payload)
        body = json.dumps(
            {"roleCommandMap": commands, "prompt": "", "executeCmd": ""},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except Exception as exc:  # noqa: BLE001 —— 任何异常都不能冒泡成一次异常计数
        LOGGER.warning("fallback 空指令：%s", exc)
        return EMPTY_BODY

    LOGGER.info("round %s → %d 条指令", payload.get("roundNo"), len(commands))
    return body
