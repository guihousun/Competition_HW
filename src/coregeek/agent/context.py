"""Context —— 一个任务的会话上下文，渲染成标准 messages JSON（判题器的 LLM 每回合只看到
`prompt` 这一段字符串）。这里只管存储与渲染，建会话、每轮放什么由 `Agent` 编排。

存储全量、渲染有窗：消息表逐字全量，`render()` 只输出题目 + 摘要 + 最近 `_WINDOW` 轮 ——
prompt 是 O(窗口)，长任务的 64KB 沙盒回执堆不出来。摘要替被窗口掐掉的旧往来记账。
`system` 不在消息表里，`Agent` 每次发送前刷新（SOP 是活的，任务中沉淀的下一轮就得看得见
—— 那是 `SOP2Prompt` "调用成功"的唯一回执）。只依赖标准库；措辞在这里、状态在 `Agent`。
"""

import json
from typing import NamedTuple


class Message(NamedTuple):
    """一条会话消息（标准 chat 格式的一格）。`role` 进 JSON，`text` 即 `content`。"""

    role: str
    text: str


_USER = "user"
_ASSISTANT = "assistant"
#: 工具结果用 `tool` 角色（标准 chat 格式的第三个 role）：沙盒回执是工具的产出、不是人类的
#: 指令 —— 标成 `user` 会让判题器的 LLM 把命令输出当成"用户说了什么"。指令性内容归
#: `user`、工具产出归 `tool`，不混在一条消息里。
_TOOL = "tool"

#: 无新内容的重问轮（畸形回复 / `SOP2Prompt` 之后）追加的固定收尾。判题器的 LLM 是黑盒：
#: 会话停在它自己的输出上是个含糊指令，一句"请继续"把"该你了"说清楚。措辞是拍的、可调。
NUDGE = "请继续。"

#: 渲染窗口（拍的）：原始往来最多保留最近几"轮" —— 一条 assistant 及其后跟着的 tool 结果 /
#: 纠错 / nudge 算一轮，更早的只有摘要替它记着。短任务（≤3 轮）掐不着。改行为只改这一个常量。
_WINDOW = 2


class Context:
    """同一道题的会话上下文。建一个用一道题（`Agent.chat` 换题即换新）。"""

    def __init__(self, task: str) -> None:
        #: 身份 = 题目原文（`phaseTask`）。换题即换会话；冷却后同文再现则续上 —— 它还记得
        #: 自己试过什么。任务结束不清（死会话，下场换题时自然被替）。
        self.task = task
        #: system（`prompt.py` 的段模板）—— `Agent` 每次发送前刷新，SOP 是活的。
        self.system = ""
        #: 执行摘要：压缩轮的 `<summary>` 内容（`Agent.adopt_summary` 记入），渲染成题目后面
        #: 的一条 user 消息、替被窗口掐掉的旧往来记账 ⇒ 它必须装得下"要交什么 + 关键数据原文"。
        self.summary = ""
        #: 构造即问：首条 user 消息就是题目原文（不加包装 —— 结构由 role 表达）。
        self._messages: list[Message] = [Message(_USER, task)]

    def feed(self, result: str = "", retry: str = "") -> None:
        """回灌轮的新消息（按 role 分条）：沙盒结果 = `tool`、纠错 = `user` —— 标题留在
        content 里当内容标签（沙盒输出是任意文本，没标签分不清哪段是什么）。
        """
        if result:
            self._messages.append(
                Message(_TOOL, f"【上一条命令的执行结果（原文）】\n{result}")
            )
        if retry:
            self._messages.append(
                Message(_USER, f"【你上一次提交的答案被判定为不正确】\n{retry}\n请重新作答。")
            )

    def nudge(self) -> None:
        """无新内容的重问轮：追加一句固定收尾（见 `NUDGE`）。"""
        self._messages.append(Message(_USER, NUDGE))

    def tool_output(self, text: str) -> None:
        """本地工具（`python_exec`）的产出：一条 `tool` 消息，当场进表（跟在 `hear` 记下的那条
        assistant 调用后面，下一份 prompt 窗口里就能看到）。与沙盒回执同一条纪律，标题标明
        来源 —— LLM 才分得清"本地算的"与"判题器沙盒跑的"。"""
        self._messages.append(Message(_TOOL, f"【本地 python 的执行结果（原文）】\n{text}"))

    def hear(self, reply: str) -> None:
        """记一条 LLM 的原始回复（原文 —— assistant 消息收它真说过的话）。

        粘住去重：`llmResp` 按可能粘住设计 ⇒ 与最后一条消息（且得是 assistant）相同就不进表。
        中间隔了别的消息再来同文 ⇒ 记：那不是粘住，是真的又说了。
        """
        if not reply:
            return
        if self._messages and self._messages[-1] == Message(_ASSISTANT, reply):
            return
        self._messages.append(Message(_ASSISTANT, reply))

    def render(self) -> str:
        """整份 prompt：system + 题目 + 摘要 + 最近 `_WINDOW` 轮。

        题目永远完整（"要交什么"的权威来源，掐什么也不能掐它）；摘要在题目后面、原始往来
        前面。`json.dumps`（`ensure_ascii=False`、紧凑分隔符）—— 正文里的 `{}`、换行逐字保留。
        """
        messages = [
            {"role": "system", "content": self.system},
            {"role": _USER, "content": self.task},
        ]
        if self.summary:
            messages.append({"role": _USER, "content": f"【历史摘要】\n{self.summary}"})
        messages += [{"role": m.role, "content": m.text} for m in self._window()]
        return json.dumps(messages, ensure_ascii=False, separators=(",", ":"))

    def _window(self) -> list[Message]:
        """最近 `_WINDOW` 轮的原始往来：从最后 `_WINDOW` 条 assistant 里最早的那条起、其后
        全部保留（tool 结果 / 纠错 / nudge 都跟着它们前面那条 assistant 走）；不足 `_WINDOW`
        条 ⇒ 全量。第 0 条是题目，`render` 单独放，这里从第 1 条起。"""
        body = self._messages[1:]
        kept = [i for i, m in enumerate(body) if m.role == _ASSISTANT]
        if len(kept) <= _WINDOW:
            return body
        return body[kept[-_WINDOW]:]

    def material(self) -> str:
        """压缩原料：原始上下文全文 —— 题目 + 全部往来，逐字。

        原文永久保留、压缩总从原文重来（不从旧摘要叠 —— 避免多次压缩的失真累积）；消息表
        永不截断。与 `render` 的分工：render 给任务 LLM 的是压缩后的（摘要 + 窗口），这里
        给压缩器的是原文。
        """
        return json.dumps(
            [{"role": m.role, "content": m.text} for m in self._messages],
            ensure_ascii=False,
            separators=(",", ":"),
        )
