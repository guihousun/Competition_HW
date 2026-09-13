"""`SOP2Prompt` —— PromptSOP 的自进化：把 LLM 总结出的解题方法**整段替换**进后续 prompt。

**`_current` 是全项目唯一一处跨回合状态。** 第 11 步起 `app.handle` 一直是无状态纯函数，
本步用户明确拍板破例（"SOP 存在进程内、跨回合、整场存活、重启清空"）。三条代价与缓解：

- **它坏了会怎样**：读出来是空串 ⇒ prompt 里那一段是空的，判题器那一侧看不出区别 ——
  **不碰红线**。SOP 只影响"答得好不好"，不影响"报文字节合不合法"。
- **不加锁**：`web/server.py` 是 `ThreadingHTTPServer`，但判题器是**逐回合同步请求**
  （不会有两条同时进来改 SOP）；即便真有并发，GIL 下 `str` 的赋值与读取不会撕裂，
  最坏结果是"某一条 prompt 带着上一版 SOP"。**不碰红线**，所以不付锁的代价。
- **SOP 不按任务分区**：任务 A 沉淀的会灌进任务 B。用户拍板的取舍，记录、不修。
"""

import logging

LOGGER = logging.getLogger(__name__)

#: SOP 的**字数**上限，超了**保头截断**。理由与硬约束 5 同源：prompt 是**每回合都发**的，
#: 没有上限的 SOP 会在几百回合里一路长成"每回合多发几千字"。1000 是拍的（**待实盘校准**），
#: 比 `app.LOG_TEXT_MAX`(400) 宽 —— 那个是人眼看的日志摘要，这个是**要喂给 LLM 读的正文**。
SOP_MAX = 1000

#: **本模块唯一的状态**，也是全项目唯一一处跨回合状态。整场存活、重启清空。
_current = ""


def SOP2Prompt(sop: str) -> str:
    """把 `sop` **整段替换**进「沉淀的 SOP」段。返回 `""` —— **它不产出命令**。

    **是替换不是追加**：追加没有遗忘机制，几百回合下来 prompt 会被旧套路撑爆；
    而"上一版不对/不完整"这件事，LLM 自己重写一遍就能表达。

    **空串 = 清空**（整段替换语义的必然推论，顺带就是那个"重置"动作）。
    ⚠️ 但走 `tool_call` 时空参数在**上游**就被挡掉了（`tool_call` 里那道 `strip()` 闸门），
    所以这条路径只对直接调用者开放 —— 两者各有用例钉着，不是冗余。

    **内容没变就不吭声**：LLM 若把同一段 SOP 反复喂进来（`llm_resp` 粘住的典型症状），
    每回合打一行日志是白花 stdout 预算（硬约束 5），而"又存了一遍同样的东西"不算"有事"。
    """
    global _current
    raw = sop if isinstance(sop, str) else ""
    text = raw[:SOP_MAX]
    if text == _current:
        return ""
    _current = text
    LOGGER.info("SOP 更新：%s", _describe(raw, text))
    return ""


def current() -> str:
    """现在的 SOP —— 「沉淀的 SOP」段的填充值。没沉淀过 ⇒ `""`。"""
    return _current


def reset() -> None:
    """清空。**只给用例用** —— 模块级状态在同一个测试进程里会跨用例串味。

    不复用 `SOP2Prompt("")`：那个会打日志，而用例的 `assertLogs` 正盯着日志，
    多出来的"清空"那一行会让断言变成"看运气"（取决于上一个用例往 SOP 里存过什么）。
    """
    global _current
    _current = ""


def _describe(received: str, stored: str) -> str:
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
