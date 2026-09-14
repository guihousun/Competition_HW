"""Context —— **一个任务**的完整会话上下文（第 25 步）。

判题器的 LLM 每回合只看到我们发出的 `prompt` 一段字符串：所谓"会话"，落地形态就是
**每回合把整个会话渲染进 prompt**。`Context` 只管存储与渲染；什么时候建、每轮往里放
什么是 `Agent` 的事（这里收发的全是字符串，不认识游戏）。

**全量、不压缩**（用户拍板"先不压缩"）：题目、回复、沙盒回执逐字进表、逐字渲染。
代价是长任务的 prompt 无上界增长（极端 = 多轮 64KB 沙盒回执）—— 已记进
`code-task.md` 的"仍生效的不确定性"，压缩是既定的下一步。

**`system` 不在消息表里**：它是 Agent **每次发送前刷新**的一整段（模板 + 工具清单 +
沉淀的 SOP）。不冻在构造时的原因是 SOP 是活的 —— 任务进行中沉淀的 SOP，下一轮就
得看得见，那是 `SOP2Prompt` "调用成功"的唯一回执（它不产出命令）。

**零包内 import**：与 `chat.py` 同为这个包的叶子（措辞在这里，状态在 `Agent`）。
"""

from typing import NamedTuple


class Message(NamedTuple):
    """一条会话消息。`role` 只有一个读者：`Context.hear` 的粘住去重。"""

    role: str
    text: str


_USER = "user"
_ASSISTANT = "assistant"

#: 无新内容的重问轮（畸形回复 / `SOP2Prompt` 之后）追加的固定收尾。判题器的 LLM
#: 是黑盒：会话停在它自己的输出上是个含糊指令，一句"请继续"把"该你了"说清楚。
#: 措辞是拍的，实盘可调。
NUDGE = "请继续。"


class Context:
    """同一道题的会话上下文。**建一个用一道题**（`Agent.chat` 换题即换新）。"""

    def __init__(self, task: str) -> None:
        #: 身份 = **题目原文**（`phaseTask`）。换题即换会话；冷却后同文再现则续上 ——
        #: 它还记得自己试过什么。任务结束不清（死会话，下场换题时自然被替）。
        self.task = task
        #: 四段 header（定位/工具/格式/SOP）。**Agent 每次发送前刷新**，见模块 docstring。
        self.system = ""
        #: **构造即问**：首条 user 消息就是题目。
        self._messages: list[Message] = [Message(_USER, f"【题目】\n{task}")]

    def feed(self, result: str = "", retry: str = "") -> None:
        """回灌轮的新 user 消息：沙盒结果与（或）纠错，标题逐字沿用旧模板。"""
        blocks = []
        if result:
            blocks.append(f"【上一条命令的执行结果（原文）】\n{result}")
        if retry:
            blocks.append(f"【你上一次提交的答案被判定为不正确】\n{retry}\n请重新作答。")
        if blocks:
            self._messages.append(Message(_USER, "\n\n".join(blocks)))

    def nudge(self) -> None:
        """无新内容的重问轮：追加一句固定收尾（见 `NUDGE`）。"""
        self._messages.append(Message(_USER, NUDGE))

    def hear(self, reply: str) -> None:
        """记一条 LLM 的回复（**原文** —— 会话记的是它真说过的话）。

        **粘住去重**：`llmResp` 按可能粘住设计（接口文档对它一个字没写、对
        `lastCmdResult` 却写明"未发命令时为空"）⇒ 与**最后一条消息**（且得是
        assistant）相同就不进表。中间隔了别的消息再来同文 ⇒ 记：那不是粘住，
        是真的又说了。
        """
        if not reply:
            return
        text = f"【你的回复】\n{reply}"
        if self._messages and self._messages[-1] == Message(_ASSISTANT, text):
            return
        self._messages.append(Message(_ASSISTANT, text))

    def render(self) -> str:
        """整份 prompt：system + 「# 对话记录」+ 全量消息。

        **拼接**而非 `str.format`：消息正文里的 `{}` 是任意文本（python 片段里太常见），
        一次都不能被当成占位符。
        """
        turns = "\n\n".join(m.text for m in self._messages)
        return f"{self.system}\n\n# 对话记录\n\n{turns}"
