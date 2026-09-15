"""Context —— **一个任务**的完整会话上下文（第 25 步；第 28 步起渲染成标准 messages JSON）。

判题器的 LLM 每回合只看到我们发出的 `prompt` 一段字符串（接口文档标它为 String）
⇒ 第 28 步起这段字符串是一个 **JSON 数组**：`[{"role": "system"/"user"/"assistant",
"content": ...}]` —— 标准 chat 格式。第 25 步那版自造的文本版式（`# 对话记录` +
标题块）用户实测**效果非常差**，已弃：结构由 role 表达，正文就是原文。
`Context` 只管存储与渲染；什么时候建、每轮往里放什么 是 `Agent` 的事
（这里收发的全是字符串，不认识游戏）。

**全量、不压缩**（用户拍板"先不压缩"）：题目、回复、沙盒回执逐字进表、逐字渲染
（`json.dumps`/`loads` 负责转义与还原，`{}` 天然无害）。代价是长任务的 prompt
无上界增长（极端 = 多轮 64KB 沙盒回执）—— 已记进 `code-task.md` 的
"仍生效的不确定性"，压缩是既定的下一步。

**`system` 不在消息表里**：它是 Agent **每次发送前刷新**的一整段（模板 + 工具清单 +
沉淀的 SOP）。不冻在构造时的原因是 SOP 是活的 —— 任务进行中沉淀的 SOP，下一轮就
得看得见，那是 `SOP2Prompt` "调用成功"的唯一回执（它不产出命令）。

**只依赖标准库**（`json`）：与 `chat.py` 同为这个包的叶子（措辞在这里，状态在 `Agent`）。
"""

import json
from typing import NamedTuple


class Message(NamedTuple):
    """一条会话消息（标准 chat 格式的一格）。`role` 进 JSON，`text` 即 `content`。"""

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
        #: system（`prompt.py` 的段模板）。**Agent 每次发送前刷新**，见模块 docstring。
        self.system = ""
        #: **构造即问**：首条 user 消息就是题目原文（不加包装 —— 结构由 role 表达）。
        self._messages: list[Message] = [Message(_USER, task)]

    def feed(self, result: str = "", retry: str = "") -> None:
        """回灌轮的新 user 消息：沙盒结果与（或）纠错 —— 标题留在 content 里当
        **内容标签**（沙盒输出是任意文本，没标签分不清哪段是什么）。"""
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
        """记一条 LLM 的回复（**原文** —— assistant 消息收它真说过的话）。

        **粘住去重**：`llmResp` 按可能粘住设计（接口文档对它一个字没写、对
        `lastCmdResult` 却写明"未发命令时为空"）⇒ 与**最后一条消息**（且得是
        assistant）相同就不进表。中间隔了别的消息再来同文 ⇒ 记：那不是粘住，
        是真的又说了。
        """
        if not reply:
            return
        if self._messages and self._messages[-1] == Message(_ASSISTANT, reply):
            return
        self._messages.append(Message(_ASSISTANT, reply))

    def render(self) -> str:
        """整份 prompt：**标准 messages JSON** —— system 头 + 这道题的全部往来。

        `json.dumps`（`ensure_ascii=False`、紧凑分隔符）—— 正文里的 `{}`、换行、
        标签全部逐字保留（`loads` 一转回来就是原文），也永远不经过 `str.format`。
        """
        messages = [{"role": "system", "content": self.system}]
        messages += [{"role": m.role, "content": m.text} for m in self._messages]
        return json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
