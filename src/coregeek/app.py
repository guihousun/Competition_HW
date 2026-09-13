"""组装根：接起 HTTP 层与决策，并守好那条唯一会出局的红线。

判题器只认三类异常（建连/响应超时、响应格式错、**指令非法**），累计 5 次该队整场不再被调度。
所以 `handle()` 里的 `except` 不是防御性编程 —— 它就是红线本身：任何失败都退化成
**合法空指令**（空指令集合法且不计异常），宁可丢掉一个回合，不赌整队资格。

响应的三个顶层字段永远都在（接口文档 §2.1）。官方 demo 只发了 `roleCommandMap`。
`prompt` 是**任务线唯一的对外通道**（发给判题器的 LLM，答案下一回合从 payload 的
`llmResp` 回来），其余时候恒为空串；`executeCmd` 至今没用过。

**每回合记一份复盘日志**（`_log`）：先整张地图、再本回合的动作。判题器是黑盒、不给别的视角，
出事故时能看见当时的局面，而不是只看见一条 `move`。
"""

import json
import logging
from typing import Any

from .game import planner
from .game.world import Turn
from .protocol import actions, model
from .web import server

LOGGER = logging.getLogger(__name__)

#: 空指令集是**合法**的，且不计异常。任何失败路径都退到这里。
EMPTY_BODY = b'{"roleCommandMap":{},"prompt":"","executeCmd":""}'


def run(port: int) -> None:
    LOGGER.info("listening on 0.0.0.0:%d", port)
    server.serve(port, handle)


def handle(raw: bytes) -> bytes:
    """处理一个回合。**不抛异常**，返回的字节永远是合法响应。"""
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
        turn = model.load(payload)
        if turn is None:
            raise ValueError("payload 不是 JSON 对象")
        prompt = planner.prompt_for(turn)
        cmds = planner.plan(turn)
        _log(turn, cmds, prompt)
        body = json.dumps(
            {"roleCommandMap": cmds, "prompt": prompt, "executeCmd": ""},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except Exception as exc:  # noqa: BLE001 —— 任何异常都不能冒泡成一次异常计数
        LOGGER.warning("fallback 空指令：%s", exc)
        return EMPTY_BODY

    return body


def _log(turn: Turn, cmds: dict[str, dict[str, Any]], prompt: str) -> None:
    """本回合的复盘日志：**先地图、后动作**，顺序固定（看着图才知道动作合不合理）。

    **写在 `try` 里面**：日志代码再不起眼也是代码。逃到 `do_POST` 去的话，
    `server.py` 不会接（`web/server.py:22`）—— 连接直接断掉，判题器那边正是
    "响应超时"，红线第一条。宁可日志出错退化成空指令（合法、不计异常），
    也不要一个写坏了的 `render()` 把整队资格赔进去。

    地图 32 行 × 1300 回合 ≈ 四万行是**有意为之**（判题器不给我们别的视角），
    但要注意：判题器若不读 stdout，管道写满后这里会**阻塞到超时**。
    真出事只能改这一处。
    """
    LOGGER.info(
        "回合 %s（%s）\n%s",
        turn.round_no,
        "白天" if turn.is_day else "夜里",
        turn.map.render(),
    )
    LOGGER.info("动作：%s", actions.describe(cmds))
    if turn.phase_task:
        # 任务线唯一的一行。**必须记**：判题器是黑盒，题目原文与 LLM 答了什么
        # 只存在于本回合的 payload 里 —— 不落下来，赛后无从校准时序与格式。
        # 截断到 120 字，理由同硬约束 5（stdout 管道缓冲只有 64KB）。
        LOGGER.info(
            "任务：%s ｜ 提交：%s ｜ 提问：%s",
            turn.phase_task[:120],
            turn.llm_resp[:120],
            prompt[:120],
        )
