"""`SOP2Prompt` 的**存储规则**：流程表（第 37 步起）—— 同名覆盖、异名追加、条数上限、
单条截断、内容没变就静默、变化时留一行日志。

⚠️ **状态不在这里**：流程表住在 `Agent` 实例上（`Agent._sop`），本模块只留规则
—— `store(current, name, sop)` 是纯的，给旧表与新流程、返回新表，状态由调用方持有。
`LOGGER` 留在这里是有意的：logger 名 `coregeek.agent.tools.sop` 是日志侧认的名字。
"""

import logging

LOGGER = logging.getLogger(__name__)

#: **单条流程**的字数上限，超了**保头截断**（prompt 是每回合都发的，没有上限的流程会在
#: 几百回合里一路长成"每回合多发几千字"）。1000 是拍的、**待实盘校准**。
#: 第 37 步起语义从"整份 SOP"收窄成"单条流程"—— 条数另由 `SOP_FLOWS_MAX` 管。
SOP_MAX = 1000

#: 流程**条数**上限，超了丢**最旧**的（新经验优先）—— 拍的、**待实盘校准**。
#: 防膨胀的另一半：单条有 `SOP_MAX`、条数有它，流程表最坏 `SOP_MAX × SOP_FLOWS_MAX` 字。
SOP_FLOWS_MAX = 5


def store(
    current: dict[str, str], name: str, sop: str, *, stripped: int = 0
) -> dict[str, str]:
    """存/改**一条**流程，返回**新表**（入参不动）。空文本 = 删掉那条；内容没变 ⇒
    **原样返回旧表、一个字都不打**。

    - **同名覆盖**（dict 赋值，位置不动）："上一版不对/不完整"由 LLM 重写同名流程表达；
    - **异名追加**：不同的经验各存各的（第 37 步多流程口径）；
    - **超 `SOP_FLOWS_MAX` 条丢最旧**：丢了谁进日志 —— 静默丢流程正是第 14 步
      "静默截断"那个坑的同款（"怎么少了一条"无从查起）。

    **内容没变就不吭声**：LLM 把同一段流程反复喂进来时（`llm_resp` 粘住的典型症状），
    每回合打一行日志是白花 stdout 预算，而"又存了一遍同样的东西"不算"有事"。

    `stripped` = 调用方（`Agent.SOP2Prompt`）在存**之前**挖掉了几处 `<answer>…</answer>`
    （`chat.strip_answers`）—— 只进日志。⚠️ 因此 `sop` 是**挖过之后**的正文：
    这里的"收到 N 字"数的是挖完的那一份，两者的差由 `describe` 那句话解释。
    """
    key = name.strip()
    raw = sop if isinstance(sop, str) else ""
    text = raw[:SOP_MAX]
    if not text:
        if key in current:
            LOGGER.info("【SOP 更新】：删流程「%s」", key)
            fresh = dict(current)
            del fresh[key]
            return fresh
        return current
    if current.get(key) == text:
        return current
    fresh = dict(current)
    fresh[key] = text
    evicted: list[str] = []
    while len(fresh) > SOP_FLOWS_MAX:
        oldest = next(iter(fresh))
        evicted.append(oldest)
        del fresh[oldest]
    message = describe(key, raw, text, stripped)
    if evicted:
        message += f"（超 {SOP_FLOWS_MAX} 条，丢最旧：「{'」、「'.join(evicted)}」）"
    LOGGER.info("【SOP 更新】：%s", message)
    return fresh


def describe(name: str, received: str, stored: str, stripped: int = 0) -> str:
    """一条流程更新的日志正文 —— **一行**，且**截断必须留痕**。

    三个细节各有出处：**超上限时同时报"收到多少 / 存了多少"**（静默截断正是第 14 步
    被叫醒的那个坑）；**挖掉过 `<answer>` 段就报几处**（否则"收到 N 字、存 M 字"缺一句
    解释，而这正是"LLM 又把答案格式写进 SOP 了"的唯一信号）；**换行转义成 `\\n`**
    （流程正文必然是多行的，不转义一条记录会变几十行，而时间戳前缀只加在第一条物理行上）
    —— `\\r` 与 `\\n` 两个都要转，Windows 上模型回的多半是 `\\r\\n`。第 37 步起
    报**流程名**（条数多了以后"哪一条变了"只有它能回答）。
    """
    head = stored[:80].replace("\r", "\\r").replace("\n", "\\n")
    notes = [f"剔除 {stripped} 处 <answer> 段"] if stripped else []
    if len(received) > len(stored):
        notes.append(f"收到 {len(received)} 字，超上限截断")
    note = f"（{'；'.join(notes)}）" if notes else ""
    return f"流程「{name}」存 {len(stored)} 字{note}｜ 前 80 字：{head}"
