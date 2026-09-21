"""组装根：接起 HTTP 层与决策，守好那条唯一会出局的红线，并每回合记一份复盘日志。

判题器只认三类异常（连接/响应超时、响应格式错、指令非法），累计 5 次出局。`handle()` 的
`except` 就是红线本身：任何失败都退化成合法空指令（合法且不计异常），宁可丢一个回合，
不赌整队资格。三个顶层字段永远都在：`roleCommandMap` 是动作，`prompt` 与 `executeCmd`
是任务线对外通道（`planner.task_channel` 产出）。
"""

import json
import logging
from typing import Any

from .game import planner
from .game.world import Turn
from .protocol import actions, model
from .utils import LOG_TEXT_MAX, _clip
from .web import server

LOGGER = logging.getLogger(__name__)

#: 空指令集合法且不计异常。任何失败路径都退到这里。
EMPTY_BODY = b'{"roleCommandMap":{},"prompt":"","executeCmd":""}'

#: 提问行的字符上限（观察优先，基本不截；真嫌大就改这一个常量）。
LOG_PROMPT_MAX = 100000


def run(port: int) -> None:
    LOGGER.info("listening on 0.0.0.0:%d", port)
    server.serve(port, handle)


def handle(raw: bytes) -> bytes:
    """处理一个回合。不抛异常，返回的字节永远是合法响应。"""
    try:
        text = raw.decode("utf-8") if raw else ""
        payload = json.loads(text) if text else {}
        turn = model.load(payload)
        if turn is None:
            raise ValueError("payload 不是 JSON 对象")
        LOGGER.info(f"###################################第{turn.round_no}回合###################################")
        # 判题器推来的原文逐字进日志（摘要单条看不见地图与未解析字段）。换行原样保留：
        # 要的就是"整段拷出来能直接 json.loads"—— 带缩进的 payload 会让一条记录跨多行。
        LOGGER.info("【本回合请求】：%s", _clip(text, LOG_TEXT_MAX))
        # 处理任务逻辑
        prompt, execute = planner.task_channel(turn)
        # 处理动作逻辑
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
    """本回合的复盘日志：局面 → 动作 → 判题器回执 → 提问，顺序固定。

    实际排列 = banner → 请求 → 任务 → 沙盒 → 局面 → 动作 → 回执 → 提问；任务行与沙盒行由
    `task_channel` 自己打、banner 与请求由 `handle` 打（数记录数时后两样单独 +2）。每条
    "有事才吭声"：干净的白天回合本函数只打 2 条。摘要那条以 `\\n` 开头 —— 空行是留给
    `logging` 时间戳前缀的。写在 try 里：日志代码逃到 `do_POST` 没人接异常 ⇒ 判题器那边
    是响应超时（红线第一条）。

    上限 `LOG_TEXT_MAX`=40000、`LOG_PROMPT_MAX`=100000 ⇒ 基本不截。体量表（**权威副本**，
    只有带 prompt 的那两行会变）：

    | 局面 | 行 | 字节 |
    |---|---|---|
    | 干净回合（没回执、没任务） | 9 | 6266 |
    | 有回执（判题器报错 + 回执名单） | 11 | 6493 |
    | 提问那一轮（题目 400 字） | 11 | 27339 |
    | 顶格：题目/回复/沙盒各 40000 字（`LOG_TEXT_MAX`） | 12 | 492050 |

    其中【本回合请求】一行 = 判题器推来的 payload 原文（样例紧凑重序列化 5164 字节；
    `request.txt` 那份 12032 是带缩进的 —— 判题器发来的形状未知，缩进越多越大，
    `LOG_TEXT_MAX` 兜底）。命令轮另有一条 prompt 行（压缩请求，随原始历史线性变大），
    上表四格没有这一行。

    量法（`logs/measure_bytes.py`）：① 量真 stdout 的形状（带 asctime 前缀的 handler、取
    utf-8 字节数，别只加 `getMessage()` 的长度）；② 每格先 `AGENT.reset()`；③ 顶格那行的
    llmResp 必须是工具形状（答案形状会被 `_answer_task` 抄进 taskAnswer，动作行凭空多
    120KB）；④ 跨步骤只比同一次测量的增量。⇒ 若判题器不读 stdout，顶格回合一回合写满
    64KB 管道（已知并接受）；结构性守卫 = 干净回合仍小（test_a_clean_round_stays_small）。
    """
    LOGGER.info("%s", turn.summary())
    LOGGER.info("【动作】：%s", actions.describe(cmds, clip=_clip))

    # ── 判题器的回执 ────────────────────────────────────────────────
    if turn.errors:
        # 码的含义见 CLAUDE.md —— 不在代码里建码表（会跟接口文档漂移成第二份真相）
        LOGGER.info("【判题器报错】：%s", "；".join(str(e) for e in turn.errors))
    if turn.action_results:
        # `errors` 答"为什么"、这条答"哪一条"（执行失败不计异常、errors 里一个字没有）。
        # 全都打（含 True）："没发指令"与"发了但没过"在只列未通过名单时长得一样；
        # 按 id 升序 —— 同一种局面打出同一种日志。
        LOGGER.info(
            "【上回合合法性】：%s",
            " | ".join(f"{i}={ok}" for i, ok in sorted(turn.action_results)),
        )

    # ── 任务线 ──────────────────────────────────────────────────────
    if prompt:
        # prompt 是拼出来的（模板 + 会话往来），工具清单/SOP 槽/会话接没接上只有这一行能回答
        LOGGER.info("【本轮提问】：%s", _clip(prompt, LOG_PROMPT_MAX))

