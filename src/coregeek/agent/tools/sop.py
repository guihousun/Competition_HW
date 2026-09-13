"""`SOP2Prompt` 的**存储规则**：整段替换、上限截断、内容没变就静默、变化时留一行日志。

⚠️ **状态不在这里**：「沉淀的 SOP」住在 `Agent` 实例上（`Agent._sop`），本模块只留规则
—— `store(current, sop)` 是纯的，给旧值与新文本、返回新值，状态由调用方持有。
`LOGGER` 留在这里也是有意的：logger 名 `coregeek.agent.tools.sop` 是日志侧认的名字。
"""

import logging

LOGGER = logging.getLogger(__name__)

#: SOP 的**字数**上限，超了**保头截断**（prompt 是每回合都发的，没有上限的 SOP 会在几百回合里
#: 一路长成"每回合多发几千字"）。1000 是拍的、**待实盘校准**，比 `utils.LOG_TEXT_MAX` 宽 ——
#: 那个人眼看的是日志摘要，这个是**要喂给 LLM 读的正文**。
SOP_MAX = 1000


def store(current: str, sop: str) -> str:
    """把 `sop` 存成新的 SOP，返回**新值**；内容没变 ⇒ **原样返回旧值、一个字都不打**。

    **是整段替换不是追加**：追加没有遗忘机制，几百回合下来 prompt 会被旧套路撑爆；
    而"上一版不对/不完整"这件事，LLM 自己重写一遍就能表达。**空串 = 清空**（整段替换语义
    的必然推论）；⚠️ 走 `tool_call` 时空参数在**上游**就被挡掉了，所以这条路径只对直接调用者开放。

    **内容没变就不吭声**：LLM 把同一段 SOP 反复喂进来时（`llm_resp` 粘住的典型症状），
    每回合打一行日志是白花 stdout 预算，而"又存了一遍同样的东西"不算"有事"。
    """
    raw = sop if isinstance(sop, str) else ""
    text = raw[:SOP_MAX]
    if text == current:
        return current
    LOGGER.info("SOP 更新：%s", describe(raw, text))
    return text


def describe(received: str, stored: str) -> str:
    """一条 SOP 更新的日志正文 —— **一行**，且**截断必须留痕**。

    两个细节各有出处：**超上限时同时报"收到多少 / 存了多少"**（静默截断正是第 14 步
    被叫醒的那个坑）；**换行转义成 `\\n`**（SOP 必然是多行的，不转义一条记录会变几十行，
    而时间戳前缀只加在第一条物理行上）—— `\\r` 与 `\\n` 两个都要转，Windows 上模型回的多半是 `\\r\\n`。
    """
    if not stored:
        return "清空"
    head = stored[:80].replace("\r", "\\r").replace("\n", "\\n")
    note = f"（收到 {len(received)} 字，超上限截断）" if len(received) > len(stored) else ""
    return f"存 {len(stored)} 字{note}｜ 前 80 字：{head}"
