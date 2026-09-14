"""组装根：接起 HTTP 层与决策，并守好那条唯一会出局的红线。

判题器只认三类异常（建连/响应超时、响应格式错、**指令非法**），累计 5 次该队整场不再被调度。
所以 `handle()` 里的 `except` 不是防御性编程 —— 它就是红线本身：任何失败都退化成**合法空指令**
（空指令集合法且不计异常），宁可丢掉一个回合，不赌整队资格。

响应的三个顶层字段永远都在：`roleCommandMap` 是动作，`prompt` 与 `executeCmd` 是**任务线的
对外通道**（发给判题器的 LLM 与它的沙盒），由 `planner.task_channel` 一起产出、**互斥**。

**每回合记一份复盘日志**：局面 → 本回合的动作 → 判题器的回执 → 提问（`_log`），
外加 `planner.task_channel` 自己打的**任务行与沙盒行**（那两样字段只在它手里）。
判题器是黑盒、不给别的视角，出事故时能看见当时的局面，而不是只看见一条 `move`。
"""

import json
import logging
from typing import Any

from .game import planner
from .game.world import Turn
from .protocol import actions, model
from .utils import _clip
from .web import server

LOGGER = logging.getLogger(__name__)

#: 空指令集是**合法**的，且不计异常。任何失败路径都退到这里。
EMPTY_BODY = b'{"roleCommandMap":{},"prompt":"","executeCmd":""}'

#: 提问行的上限 —— 用户两度放宽后（1000 → 100000），这一行也**基本不截**了
#: （与 `LOG_TEXT_MAX`=40000 同一条策略：观察优先）。截断留痕照旧（`_clip`），
#: 真嫌大就改这一个常量。
LOG_PROMPT_MAX = 100000


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
        LOGGER.info(f"###################################第{turn.round_no}回合###################################")
        # 处理Agent逻辑
        prompt, execute = planner.task_channel(turn)
        cmds = planner.plan(turn)
        _log(turn, cmds, prompt)
        body = json.dumps(
            {"roleCommandMap": cmds, "prompt": prompt, "executeCmd": execute},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except Exception as exc:  # noqa: BLE001 —— 任何异常都不能冒泡成一次异常计数
        LOGGER.warning("fallback 空指令：%s", exc)
        return EMPTY_BODY

    return body


def _log(turn: Turn, cmds: dict[str, dict[str, Any]], prompt: str) -> None:
    """本回合的复盘日志：**先局面、再动作、再判题器的回执、最后提问**，顺序固定
    （看着图才知道动作合不合理，看着回执才知道下一回合该怎么改）。

    ⚠️ **任务行与沙盒行不在本函数里** —— 它们由 `planner.task_channel` 自己打
    （那两样字段只在它的作用域里），而 `task_channel` 先于本函数被调用
    ⇒ 日志里它们出现在局面之前。**日志顺序的实际排列是：banner → 任务 → 沙盒 → 局面 → 动作 → 回执 → 提问。**

    **写在 `try` 里面**：日志代码再不起眼也是代码。逃到 `do_POST` 去的话，
    `server.py` 不会接 —— 连接直接断掉，判题器那边正是"响应超时"，红线第一条。

    **记录数不固定，但每条的触发条件都是"有事才吭声"**：局面与动作每回合各一条；
    判题器报错 / 有回执 / 提问三条各自只在有内容时出现（干净的白天回合本函数打 2 条）。
    ⚠️ `handle` 在它之前还多打**一条 banner**（`######第N回合######`），
    那是唯一一条**不受本函数管辖**的日志 ⇒ 一次 `handle` 的总记录数各多 1。
    摘要那一条以 `\n` 开头 —— 那个空行是留给 `logging` 时间戳前缀的。

    ⚠️ **第 26 步起局面 = 摘要单条**（用户手改：**图例与整张地图退出日志**），且两个上限
    一并放宽（用户拍板"观察优先"）：`LOG_TEXT_MAX` 400 → **40000**、`LOG_PROMPT_MAX`
    1000 → **100000** ⇒ **日志基本不截**：

    | 局面 | 行 | 字节 |
    |---|---|---|
    | 干净回合（没回执、没任务） | 7 | 506 |
    | 有回执（判题器报错 + 回执名单） | 9 | 599 |
    | 提问那一轮（题目 400 字） | 34 | 4140 |
    | **顶格：题目/回复/沙盒各 40000 字（`LOG_TEXT_MAX`）** | 39 | **≈605000** |

    ⇒ 若判题器**不读** stdout，顶格回合**一回合**就写满 64KB 管道 ⇒ 阻塞到响应超时
    （红线第一条）—— **已知并接受**（第 26 步用户拍板；"最坏 ≤ 9300"的旧守卫随策略
    一起撤销）。剩下的守卫是结构性的：干净回合必须仍然小（摘要有自己的上界），
    见用例 `test_a_clean_round_stays_small`。**数字与 `CLAUDE.md` 硬约束 5 对齐，别凭记忆写。**

    **截断留痕照旧**：`_clip(text, limit)` 超长时打 `…（共 N 字）`，单位是**字**不是字节
    （提问行只在大到 100000 字时才碰得到）。
    """
    LOGGER.info("%s", turn.summary())
    LOGGER.info("【动作】：%s", actions.describe(cmds, clip=_clip))

    # ── 判题器的回执 ────────────────────────────────────────────────
    # 这两条**在任务线之外也该出现**：errorCode 4（指令错误）与 5（LLM 额度超限）
    # 跟任务没有关系，而它们恰恰是最该第一时间看见的东西。
    if turn.errors:
        # 码的含义（2 = 答案错误 / 1 = 任务超时 / 4 = 指令错误 / 5 = LLM 额度超限）
        # 见 `CLAUDE.md` —— **不在代码里建一张码表**，那是会跟接口文档漂移的第二份真相。
        LOGGER.info("【判题器报错】：%s", "；".join(str(e) for e in turn.errors))
    if turn.action_results:
        # `errors` 说的是"**为什么**"，这条说的是"**哪一条**"：格式合法的指令也会执行失败
        # （撞墙、打空），那类不计异常、`errors` 里一个字都没有。
        #
        # **全都打**（含 `True` 的那些）：判题器只回它**收到**的那几条，所以"这一条压根没发指令"
        # 与"发了但没过"在只列未通过名单时长得一模一样 —— 而这两件事下一步该怎么改完全相反。
        # 按 id 升序（不照 payload 顺序）：同一种局面必须打出同一种日志，翻日志才对得上号。
        LOGGER.info(
            "【上回合合法性】：%s",
            " | ".join(f"{i}={ok}" for i, ok in sorted(turn.action_results)),
        )

    # ── 任务线 ──────────────────────────────────────────────────────
    if prompt:
        # `prompt` **打出来**：它是拼出来的（模板 + 这道题的会话往来），工具清单长什么样、
        # SOP 那个槽填进去没有、会话接没接上，**实盘上只有这一行能回答**。
        # 上限 `LOG_PROMPT_MAX`（第 26 步放宽到 100000，基本不截）。
        # 题目原文与 LLM 回复那两样由 `planner.task_channel` 自己打（见那边的说明）。
        LOGGER.info("【本轮提问】：%s", _clip(prompt, LOG_PROMPT_MAX))

