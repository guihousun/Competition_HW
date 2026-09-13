"""组装根：接起 HTTP 层与决策，并守好那条唯一会出局的红线。

判题器只认三类异常（建连/响应超时、响应格式错、**指令非法**），累计 5 次该队整场不再被调度。
所以 `handle()` 里的 `except` 不是防御性编程 —— 它就是红线本身：任何失败都退化成
**合法空指令**（空指令集合法且不计异常），宁可丢掉一个回合，不赌整队资格。

响应的三个顶层字段永远都在（接口文档 §2.1）。官方 demo 只发了 `roleCommandMap`。
`prompt` 与 `executeCmd` 是**任务线的对外通道**（发给判题器的 LLM 与它的沙盒，
回复与执行结果下一回合分别从 payload 的 `llmResp` / `lastCmdResult` 回来），
两者由 `planner.task_channel` 一起产出、**互斥**，任务之外恒为空串。

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

#: 日志里单个**输入字段**的**字符**上限，超了截断。**单位是字不是字节**
#: （中文 1 字 = 3 字节，按字节数限制的话同一个数字在中英文题目下差 3 倍）。
#: 上限存在的理由与硬约束 5 同源：日志长度**不能是"数据相关的量"**，
#: 判题器给多长的任务原文不该决定我们写多少字节。
LOG_TEXT_MAX = 400

#: 组装出来的 `prompt` 的上限 —— **比 `LOG_TEXT_MAX` 大是有理由的**（第 20 步）：
#: prompt 不是 payload 里的一个字段，而是「模板（591 字，含工具清单）+ 沉淀的 SOP
#: （≤ `sop.SOP_MAX`）+ 沙盒结果 + 纠错 + 题目」拼出来的，**光是模板就已经超过 400**
#: —— 按 400 截，看见的永远只有开头的「Agent定位」几行，槽位填没填上、题目在不在里面
#: 全看不见，而那正是要打它的原因。1000 够覆盖"SOP 还没长到一半"的绝大部分回合。
LOG_PROMPT_MAX = 1000


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
    """本回合的复盘日志：**先局面、再动作、再判题器的回执、最后任务线**，顺序固定
    （看着图才知道动作合不合理，看着回执才知道下一回合该怎么改）。

    **写在 `try` 里面**：日志代码再不起眼也是代码。逃到 `do_POST` 去的话，
    `server.py` 不会接（`web/server.py:22`）—— 连接直接断掉，判题器那边正是
    "响应超时"，红线第一条。宁可日志出错退化成空指令（合法、不计异常），
    也不要一个写坏了的 `render()` 把整队资格赔进去。

    **记录数不固定，但每条的触发条件都是"有事才吭声"**：局面与动作每回合各一条
    （摘要在日志里有定序作用，也方便按行数对账），判题器报错 / 有回执 / 任务线 / 沙盒结果
    四条各自只在有内容时出现。所以样例那种"夜里、没任务、判题器报了假错"的局面
    是 4 条，而一个干净的白天回合只有 2 条。

    三块内容**拼成一条记录**（摘要 / 图例 / 地图），不是三条 —— 用例
    `test_every_round_logs_the_map_then_the_actions` 钉着这一条，
    而且 `logging` 的时间戳前缀只加在**第一条物理行**上，
    拆开之后地图那几十行就没有时间戳了（按时间翻日志时正是这些行要定位）。

    顺序是"**头部 → 状态 → 图例 → 图**"：头部带时间戳，图例紧挨着图。
    这里**一点领域知识都不剩**（谁白天谁夜里、图长什么样、金币几位数全是
    `Turn` / `Map` 自己的事），本函数只做装配与截断。

    ⚠️ **stdout 是会被写满的**（第 20 步重测 —— 日志详细化之后每个数字都变了；
    "行"是**物理行**，样例 `request.txt`，每一格都从"没沉淀过 SOP"起算）：

    | 局面 | 行 | 字节 | 写满 64KB |
    |---|---|---|---|
    | 干净回合（没回执、没任务） | 41 | 2329 | 约 28 回合 |
    | 有回执（判题器报错 + 回执名单） | 43 | 2453 | 约 26 回合 |
    | 任务在身、还没答过（**提问那一轮**） | 65 | 5907 | 约 11 回合 |
    | 任务在身 + 沙盒结果顶格（回灌那一轮） | 66 | 7176 | 约 9 回合 |
    | **最坏：上面全部 + 一回合 SOP 调用顶格** | 66 | **8574** | 约 **7 回合** |

    任务线那几条是**大头**，而且**任务期间每回合都打**（同一个任务原文重复打几十遍）。
    第 20 步把 `prompt` 从"有（N 字）"改成**打全文**之后它成了最大的一块
    （上限 `LOG_PROMPT_MAX` = 1000 字 ≈ 3000 字节），而且**它自带十几个换行**
    ⇒ 提问那一轮比不提问的还多出二十来行物理行（时间戳只加在第一条上，与局面那块同理）。
    SOP 那条（`coregeek.agent.tools.sop`）**只在内容变化时出现**，最长约 321 字节
    （"存 1000 字（收到 N 字，超上限截断）｜ 前 80 字…"）—— 它是第 18 步新加的，
    也是**唯一一条不在本模块名下的日志**（守卫用例为此改用 root logger 收）。
    1300 回合 ≈ 3 MB（无任务）～ 11 MB（全程有任务），而 Windows 管道缓冲只有 64KB
    —— 判题器若**不读** stdout，最坏约 **7 回合**后这里就阻塞到响应超时。**未实测**
    （本地没法验证判题器读不读），真出事只能改这一处（把两个上限调小最直接，
    第 20 步的取舍是**用更早阻塞换更全的现场** —— 用户拍板"接受上升，重新钉上限"）。
    **数字与 `CLAUDE.md` 硬约束 5、以及与用例 `test_the_worst_round_stays_under_the_budget`
    三处必须一致** —— 留着旧数字会让下一个人按错的量级估风险。

    ⚠️ **更早的数字（2294 / 4618 / 6014 / 6180 那一串）都作废**：第 18 步那次是
    "prompt 只报字数"的口径量出来的（那一次改掉了更早一版对不上号的数字）。
    现值同样是**用 `D:/tmp/budget20.py` 这份 fixture 重新跑出来的**，
    最坏那一格与用例里那 9300 的上限同源。
    """
    LOGGER.info(
        "%s\n%s\n%s", turn.summary(), map_legend, turn.map.render()
    )
    LOGGER.info("动作：%s", actions.describe(cmds, clip=_clip))

    # ── 判题器的回执 ────────────────────────────────────────────────
    # 这两条**在任务线之外也该出现**，所以不放进下面那个 `if`：errorCode 4（指令错误）
    # 与 5（LLM 额度超限）跟任务没有关系，而它们恰恰是最该第一时间看见的东西。
    if turn.errors:
        # 码的含义（2 = 答案错误 / 1 = 任务超时 / 4 = 指令错误 / 5 = LLM 额度超限）
        # 见 `CLAUDE.md` —— **不在代码里建一张码表**，那是会跟接口文档漂移的第二份真相。
        LOGGER.info("判题器报错：%s", "；".join(str(e) for e in turn.errors))
    if turn.action_results:
        # `errors` 说的是"**为什么**"，这条说的是"**哪一条**"：格式合法的指令也会
        # 执行失败（撞墙、打空），那类不计异常、`errors` 里一个字都没有。
        #
        # **全都打**（含 `True` 的那些，第 20 步）：判题器只回它**收到**的那几条，
        # 所以"这一条压根没发指令"与"发了但没过"在只列未通过名单时长得一模一样 ——
        # 而这两件事下一步该怎么改完全相反（补发一条 vs 换打法）。
        # 按 id 升序（不照 payload 顺序）：同一种局面必须打出同一种日志，翻日志才对得上号。
        LOGGER.info(
            "上回合合法性：%s",
            " ".join(f"{i}={ok}" for i, ok in sorted(turn.action_results)),
        )

    # ── 任务线 ──────────────────────────────────────────────────────
    if turn.phase_task or turn.llm_resp:
        # **必须记**：判题器是黑盒，题目原文与 LLM 答了什么只存在于本回合的 payload 里
        # —— 不落下来，赛后无从校准时序与格式。触发条件带上 `llm_resp`：任务刚结束的
        # 那一回合 `phase_task` 已经空了，而那是唯一一次能看见"判题器最后答了什么"的机会。
        #
        # `prompt` **打全文**（第 20 步改；原来只报"有（N 字）"）：它是模板套上任务原文
        # （外加沙盒结果 / 纠错 /「沉淀的 SOP」那几段）拼出来的，**别处看不见** ——
        # 模板里的工具清单长什么样、"沉淀的 SOP"那个槽到底填进去没有、题目在不在里面，
        # 实盘上只有这一行能看到（本地 e2e 的"LLM"是我们自己写的，只证明解析自洽）。
        # 上限用 `LOG_PROMPT_MAX` 而不是 `LOG_TEXT_MAX`，理由见那个常量。
        LOGGER.info(
            "任务：%s ｜ 提交：%s ｜ 提问：%s",
            _clip(turn.phase_task) or "无",
            _clip(turn.llm_resp) or "无",
            _clip(prompt, LOG_PROMPT_MAX) or "无",
        )

    if turn.cmd_result:
        # 沙盒回执**必须记**：回灌给 LLM 的就是它，"答案为什么不对"多半得从它里面看。
        #
        # **只记结果、不记发出去的命令** —— 发命令那一回合 `llm_resp` 就是那次工具调用
        # （新旧形状都算），已经印在上面那行的"提交："里了，再抄一遍是**同一回合、
        # 同一条字符串抄第二遍**（与"`prompt` 不打印原文"逐字同源）。
        # 而且这样"沙盒行数 = 实际跑过的命令数"，对账关系更干净。
        #
        # 回灌给 LLM 是**全文**，这里才截断 —— 两个下游要的东西不同：一个要正确性，
        # 一个要人眼看得下。`_clip` 是**保头**截断，而沙盒输出最有诊断价值的恰好是头
        # （`[exitCode:N]` / `[TIMEOUT]` / `[JUDGER_ERROR]` 全在第一行）。
        LOGGER.info("沙盒：回「%s」", _clip(turn.cmd_result))


def _clip(text: str, limit: int = LOG_TEXT_MAX) -> str:
    """超长文本截到 `limit` 字（`LOG_TEXT_MAX`，只有 `prompt` 用 `LOG_PROMPT_MAX`）
    —— **并且把截断这件事说出来**。

    原来那版是 `text[:120]` 的**静默**截断：任务一长，日志里就是一段没头没尾的文字，
    看不出后面还有没有内容。第 14 步正是被这个坑叫醒的（"任务一直失败，
    把任务信息打印出来我看看"——打印了，但打印的是被砍过的）。被砍掉多少必须写在脸上。

    **`limit` 是个参数而不是两个函数**（第 20 步）：截断规则（保头 + 留痕）只有这一份，
    唯一的差别是上限；`protocol.actions.describe` 也拿它当参数用（那边不能 import 本模块）。
    """
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…（共 {len(text)} 字）"
