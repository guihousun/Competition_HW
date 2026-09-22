"""`SOP2Prompt` 的存储规则：同名覆盖、异名追加、条数上限、单条截断、内容没变就静默。

状态不在这里（两张流程表住在 `Agent` 上：`_pre_sop` 暂存 / `_sop` 正式）：`store` 是纯的，
给旧表返回新表。`LOGGER` 留在这里是有意的 —— logger 名 `coregeek.agent.tools.sop` 是日志侧
认的名字。
"""

import logging

LOGGER = logging.getLogger(__name__)

#: 单条流程的字数上限，超了保头截断（prompt 每回合都发，没上限的流程会一路长成
#: "每回合多发几千字"）。1000 是拍的、待实盘校准。条数另由 `SOP_FLOWS_MAX` 管。
SOP_MAX = 1000

#: 流程条数上限，超了丢最旧的（新经验优先）—— 拍的、待实盘校准。流程表最坏
#: `SOP_MAX × SOP_FLOWS_MAX` 字。
SOP_FLOWS_MAX = 5


def store(current: dict[str, str], name: str, sop: str, *, where: str) -> dict[str, str]:
    """存/改一条流程，返回新表（入参不动）。空文本 = 删掉那条；内容没变 ⇒ 原样返回旧表、
    一个字都不打。

    `where` = 落哪个表（暂存 / 正式），只进日志：同一段正文会先落暂存、后来转正时再落一次
    （`Agent._promote`），日志不带落点就分不出"沉淀了"与"转正了"。

    同名覆盖（位置不动）＝ LLM 用重写同名流程表达"上一版不对"；异名追加；超
    `SOP_FLOWS_MAX` 条丢最旧、丢了谁进日志 —— 静默丢流程会让"怎么少了一条"无从查起。
    内容没变就不吭声：`llm_resp` 粘住时 LLM 会把同一段反复喂进来，每回合打一行是白花
    stdout 预算，而"又存了一遍同样的东西"不算"有事"。
    """
    key = name.strip()
    raw = sop if isinstance(sop, str) else ""
    text = raw[:SOP_MAX]
    if not text:
        if key in current:
            LOGGER.info("【SOP 更新】：从%s表删流程「%s」", where, key)
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
    message = describe(key, raw, text, where)
    if evicted:
        message += f"（超 {SOP_FLOWS_MAX} 条，丢最旧：「{'」、「'.join(evicted)}」）"
    LOGGER.info("【SOP 更新】：%s", message)
    return fresh


def describe(name: str, received: str, stored: str, where: str) -> str:
    """一条流程更新的日志正文 —— 一行，且截断必须留痕。

    超上限时同报"收到多少 / 存了多少"；`\r` 与 `\n` 都转义成字面量（Windows 上模型回
    的多半是 `\r\n`）—— 一条记录恒为一行，时间戳前缀只加在第一条物理行上。
    """
    head = stored[:80].replace("\r", "\\r").replace("\n", "\\n")
    notes = []
    if len(received) > len(stored):
        notes.append(f"收到 {len(received)} 字，超上限截断")
    note = f"（{'；'.join(notes)}）" if notes else ""
    return f"流程「{name}」进{where}表 存 {len(stored)} 字{note}｜ 前 80 字：{head}"
