"""`SOP2Prompt` 的**存储规则**：整段替换、上限截断、内容没变就静默、变化时留一行日志。

⚠️ **状态不在这里**（第 19 步搬走了）。「沉淀的 SOP」现在住在 `Agent` 实例上
（`agent.agent.Agent._sop`，包根那个单实例 `coregeek.agent.AGENT`），本模块只留**规则**：
`store(current, sop)` 是纯的 —— 给旧值与新文本、返回新值，状态由调用方持有。

这么切的两个理由：

- **`Agent.SOP2Prompt` 才有状态**，它注册在工具表里，是 `SOP2Prompt(sop)` 这个接口名的主人；
  本模块不该再藏一个"另有其人"的变量（第 18 步那版 `_current` 正是这种"状态躲在模块里"的形状，
  用户第 19 步明确要求改掉：*"这个Agent是一个单实例的"*）。
- **`LOGGER` 留在这里是有意的**：logger 名 `coregeek.agent.tools.sop` 因此**不变**
  ⇒ `app._log` 的字节表、"唯一一条不在 `app` 名下的日志"那条守卫用例、`CLAUDE.md` 硬约束 5
  三处都不用跟着改。搬进 `agent/agent.py` 就会变成 `coregeek.agent.agent`，
  一次没有收益的重命名要牵动三份文档。

**`store` 不是为抽象而抽象**：它有两个使用者 —— `Agent.SOP2Prompt` 与用例（直接测存储语义，
不用造实例）。
"""

import logging

LOGGER = logging.getLogger(__name__)

#: SOP 的**字数**上限，超了**保头截断**。理由与硬约束 5 同源：prompt 是**每回合都发**的，
#: 没有上限的 SOP 会在几百回合里一路长成"每回合多发几千字"。1000 是拍的（**待实盘校准**），
#: 比 `app.LOG_TEXT_MAX`(400) 宽 —— 那个是人眼看的日志摘要，这个是**要喂给 LLM 读的正文**。
SOP_MAX = 1000


def store(current: str, sop: str) -> str:
    """把 `sop` 存成新的 SOP，返回**新值**；内容没变 ⇒ **原样返回旧值、一个字都不打**。

    **是整段替换不是追加**：追加没有遗忘机制，几百回合下来 prompt 会被旧套路撑爆；
    而"上一版不对/不完整"这件事，LLM 自己重写一遍就能表达。
    **空串 = 清空**（整段替换语义的必然推论，顺带就是那个"重置"动作）；
    ⚠️ 但走 `tool_call` 时空参数在**上游**就被挡掉了（`Agent.tool_call` 里那道 `strip()` 闸门），
    所以这条路径只对直接调用者开放 —— 两者各有用例钉着，不是冗余。

    **内容没变就不吭声**：LLM 若把同一段 SOP 反复喂进来（`llm_resp` 粘住的典型症状），
    每回合打一行日志是白花 stdout 预算（硬约束 5），而"又存了一遍同样的东西"不算"有事"。
    """
    raw = sop if isinstance(sop, str) else ""
    text = raw[:SOP_MAX]
    if text == current:
        return current
    LOGGER.info("SOP 更新：%s", describe(raw, text))
    return text


def describe(received: str, stored: str) -> str:
    """一条 SOP 更新的日志正文 —— **一行**，且**截断必须留痕**。

    两个细节各有出处：

    - **超上限时同时报"收到多少 / 存了多少"**（与 `app._clip` 的 `…（共 N 字）` 同源）：
      LLM 灌进来一段 9000 字的 SOP，日志上只看见"存 1000 字"，下一个人会以为是它只写了
      1000 字 —— **静默截断**正是第 14 步被叫醒的那个坑。
    - **换行转义成 `\\n`**：SOP 必然是**多行**的（LLM 总结套路就是一串条目），不转义的话
      一条记录变几十行，而 `logging` 的时间戳前缀只加在**第一条物理行**上
      （与 `app._log` 的"三块拼成一条"同一条理由）。`\\r` 与 `\\n` **两个都要转**：
      Windows 上模型回的多半是 `\\r\\n`，只转一个剩下那个照样断行，这个转义就成了摆设。
    """
    if not stored:
        return "清空"
    head = stored[:80].replace("\r", "\\r").replace("\n", "\\n")
    note = f"（收到 {len(received)} 字，超上限截断）" if len(received) > len(stored) else ""
    return f"存 {len(stored)} 字{note}｜ 前 80 字：{head}"
