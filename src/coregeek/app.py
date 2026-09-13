"""组装根：接起 HTTP 层与决策，并守好那条唯一会出局的红线。

判题器只认三类异常（建连/响应超时、响应格式错、**指令非法**），累计 5 次该队整场不再被调度。
所以 `handle()` 里的 `except` 不是防御性编程 —— 它就是红线本身：任何失败都退化成
**合法空指令**（空指令集合法且不计异常），宁可丢掉一个回合，不赌整队资格。

响应的三个顶层字段永远都在（接口文档 §2.1）。官方 demo 只发了 `roleCommandMap`。
`prompt` 是**任务线唯一的对外通道**（发给判题器的 LLM，答案下一回合从 payload 的
`llmResp` 回来），其余时候恒为空串；`executeCmd` 至今没用过。

**每回合记一份复盘日志**（`_log`）：先局面（摘要 + 图例 + 地图）、再本回合的动作、
再**判题器的回执**（`errors` 与 `lastRoundRoleActionResults`）、最后任务线。
判题器是黑盒、不给别的视角，出事故时能看见当时的局面，而不是只看见一条 `move`。

**回执那一块是"我们做对了没有"的唯一来源**，与"我们做了什么"同等重要：
只记自己发了什么、不记判题器认不认，失败就只能靠猜（第 14 步之前正是如此）。
"""

import json
import logging
from typing import Any

from .game import planner
from .game.map import LEGEND as map_legend
from .game.world import Turn
from .protocol import actions, model
from .web import server

LOGGER = logging.getLogger(__name__)

#: 空指令集是**合法**的，且不计异常。任何失败路径都退到这里。
EMPTY_BODY = b'{"roleCommandMap":{},"prompt":"","executeCmd":""}'

#: 任务日志里单个文本字段的**字符**上限，超了截断。**单位是字不是字节**
#: （中文 1 字 = 3 字节，按字节数限制的话同一个数字在中英文题目下差 3 倍）。
#: 上限存在的理由与硬约束 5 同源：日志长度**不能是"数据相关的量"**，
#: 判题器给多长的任务原文不该决定我们写多少字节。
LOG_TEXT_MAX = 400


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
    """本回合的复盘日志：**先局面、再动作、再判题器的回执、最后任务线**，顺序固定
    （看着图才知道动作合不合理，看着回执才知道下一回合该怎么改）。

    **写在 `try` 里面**：日志代码再不起眼也是代码。逃到 `do_POST` 去的话，
    `server.py` 不会接（`web/server.py:22`）—— 连接直接断掉，判题器那边正是
    "响应超时"，红线第一条。宁可日志出错退化成空指令（合法、不计异常），
    也不要一个写坏了的 `render()` 把整队资格赔进去。

    **记录数不固定，但每条的触发条件都是"有事才吭声"**：局面与动作每回合各一条
    （摘要在日志里有定序作用，也方便按行数对账），判题器报错 / 有实体未通过 / 任务线
    三条各自只在有内容时出现。所以样例那种"夜里、没任务、判题器报了假错"的局面
    是 4 条，而一个干净的白天回合只有 2 条。

    三块内容**拼成一条记录**（摘要 / 图例 / 地图），不是三条 —— 用例
    `test_every_round_logs_the_map_then_the_actions` 钉着这一条，
    而且 `logging` 的时间戳前缀只加在**第一条物理行**上，
    拆开之后地图那几十行就没有时间戳了（按时间翻日志时正是这些行要定位）。

    顺序是"**头部 → 状态 → 图例 → 图**"：头部带时间戳，图例紧挨着图。
    这里**一点领域知识都不剩**（谁白天谁夜里、图长什么样、金币几位数全是
    `Turn` / `Map` 自己的事），本函数只做装配与截断。

    ⚠️ **stdout 是会被写满的**（第 14 步实测，四种局面，样例 `request.txt`）：

    | 局面 | 行 | 字节 |
    |---|---|---|
    | 干净回合（没回执、没任务） | 42 | 2294 |
    | 有回执（判题器报错 + 两条未通过） | 44 | 2459 |
    | **任务在身（`phaseTask`/`llmResp` 都顶到 `LOG_TEXT_MAX`）** | 45 | **4832** |

    任务线那一条是**大头**，而且**任务期间每回合都打**（同一个任务原文重复打几十遍）。
    1300 回合 ≈ 3 MB（无任务）～ 6.3 MB（全程有任务），而 Windows 管道缓冲只有 64KB
    —— 判题器若**不读** stdout，约 **26 回合**（无任务）/ **13 回合**（任务在身）后
    这里就阻塞到响应超时。**未实测**（本地没法验证判题器读不读），真出事只能改这一处
    （把 `LOG_TEXT_MAX` 调小最直接）。**数字与 `CLAUDE.md` 硬约束 5 必须一致**
    —— 留着旧数字会让下一个人按错的量级估风险。
    """
    LOGGER.info(
        "%s\n%s\n%s", turn.summary(), map_legend, turn.map.render()
    )
    LOGGER.info("动作：%s", actions.describe(cmds))

    # ── 判题器的回执 ────────────────────────────────────────────────
    # 这两条**在任务线之外也该出现**，所以不放进下面那个 `if`：errorCode 4（指令错误）
    # 与 5（LLM 额度超限）跟任务没有关系，而它们恰恰是最该第一时间看见的东西。
    if turn.errors:
        # 码的含义（2 = 答案错误 / 1 = 任务超时 / 4 = 指令错误 / 5 = LLM 额度超限）
        # 见 `CLAUDE.md` —— **不在代码里建一张码表**，那是会跟接口文档漂移的第二份真相。
        LOGGER.info("判题器报错：%s", "；".join(str(e) for e in turn.errors))
    failed = sorted(i for i, ok in turn.action_results if not ok)
    if failed:
        # `errors` 说的是"**为什么**"，这条说的是"**哪一条**"：格式合法的指令也会
        # 执行失败（撞墙、打空），那类不计异常、`errors` 里一个字都没有。
        LOGGER.info("上回合未通过：%s", " ".join(str(i) for i in failed))

    # ── 任务线 ──────────────────────────────────────────────────────
    if turn.phase_task or turn.llm_resp:
        # **必须记**：判题器是黑盒，题目原文与 LLM 答了什么只存在于本回合的 payload 里
        # —— 不落下来，赛后无从校准时序与格式。触发条件带上 `llm_resp`：任务刚结束的
        # 那一回合 `phase_task` 已经空了，而那是唯一一次能看见"判题器最后答了什么"的机会。
        #
        # `prompt` **不打印原文**：它就是 `TASK_PROMPT.format(task=任务原文)`，
        # 全文等于把任务抄第二遍（白花一倍字节），有意义的只是"这一回合到底问没问"。
        LOGGER.info(
            "任务：%s ｜ 提交：%s ｜ 提问：%s",
            _clip(turn.phase_task) or "无",
            _clip(turn.llm_resp) or "无",
            f"有（{len(prompt)} 字）" if prompt else "无",
        )


def _clip(text: str) -> str:
    """超长文本截到 `LOG_TEXT_MAX` 字 —— **并且把截断这件事说出来**。

    原来那版是 `text[:120]` 的**静默**截断：任务一长，日志里就是一段没头没尾的文字，
    看不出后面还有没有内容。第 14 步正是被这个坑叫醒的（"任务一直失败，
    把任务信息打印出来我看看"——打印了，但打印的是被砍过的）。被砍掉多少必须写在脸上。
    """
    if len(text) <= LOG_TEXT_MAX:
        return text
    return f"{text[:LOG_TEXT_MAX]}…（共 {len(text)} 字）"
