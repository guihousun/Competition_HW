"""Context —— 一个任务的会话上下文，渲染成标准 messages JSON（判题器的 LLM 每回合只看到
`prompt` 这一段字符串）。这里只管存储与渲染，建会话、每轮放什么由 `Agent` 编排。

存储全量、渲染**跟着摘要走**：消息表逐字全量；`render()` = 题目 + 【历史摘要】 + **摘要覆盖点
之后的全部往来**。摘要没盖到的一句都不丢 —— 压缩是好几个回合才轮到一次的（命令轮才发压缩请求），
按"最近 N 轮"掐会把这几轮之间的往来丢在摘要之外（用户口径："不能关死 2 回合的逻辑"）。
还没有摘要 ⇒ 全量（首问与短任务逐字节同形）。
`system` 不在消息表里，`Agent` 每次发送前刷新（SOP 与沙盒清单是活的）。只依赖标准库。
"""

import json
from typing import NamedTuple


class Message(NamedTuple):
    """一条会话消息（标准 chat 格式的一格）。`role` 进 JSON，`text` 即 `content`。"""

    role: str
    text: str


_USER = "user"
_ASSISTANT = "assistant"
#: 工具结果用 `tool` 角色：沙盒回执是工具的产出、不是人类的指令 —— 标成 `user` 会让
#: 判题器的 LLM 把命令输出当成"用户说了什么"。
_TOOL = "tool"

#: 无新内容的重问轮追加的固定收尾。判题器的 LLM 是黑盒：会话停在它自己的输出上是个
#: 含糊指令，一句"请继续"把"该你了"说清楚。措辞是拍的、可调。
NUDGE = "请继续。"

class Context:
    """同一道题的会话上下文。建一个用一道题（`Agent.chat` 换题即换新）。"""

    def __init__(self, task: str) -> None:
        #: 身份 = 题目原文（`phaseTask`）。换题即换会话；冷却后同文再现则续上。
        self.task = task
        #: system —— `Agent` 每次发送前刷新，SOP 是活的。
        self.system = ""
        #: 执行摘要：压缩轮的 `<summary>` 内容（`adopt_summary` 记入），渲染成题目后面的
        #: 一条 tool 消息、替它盖住的那段往来记账。
        self.summary = ""
        #: 摘要盖到 `_messages` 的第几条（下标）：`render` 从这一条起往后全量输出。
        #: 首元素是题目（单独渲染）⇒ 起点是 1 = "还没盖住任何往来"。
        self._covered = 1
        #: 上一次压缩请求发出去时的快照（`sent_for_compression`）。摘要回来时按它划覆盖点；
        #: 请求发了而判题器没答 ⇒ 快照留着不动，那些往来照旧全量渲染（**绝不能因为"发过请求"
        #: 就当它们已被摘要盖住** —— 那等于把没进摘要的往来丢掉）。
        self._pending: int | None = None
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
        """无新内容的重问轮：追加一句固定收尾（见 `NUDGE`）。

        尾巴上是 `tool` 产出 ⇒ 什么都不加：会话停在工具产出上不含糊，与沙盒回执轮同形。
        """
        if self._messages[-1].role == _TOOL:
            return
        self._messages.append(Message(_USER, NUDGE))

    def tool_output(self, text: str, label: str) -> None:
        """本地工具（`python_exec` / `readSandboxFile`）的产出：一条 `tool` 消息当场进表。
        `label` 标明来源 —— LLM 才分得清"本地算的"与"沙盒文件的正文"。"""
        self._messages.append(Message(_TOOL, f"{label}\n{text}"))

    def hear(self, reply: str) -> bool:
        """记一条 LLM 的原始回复（原文）。返回"这条是不是新的"（False = 空回复、或与
        最后一条消息且是 assistant 同文 = 粘住）。中间隔了别的消息再来同文 ⇒ 记。
        """
        if not reply:
            return False
        if self._messages and self._messages[-1] == Message(_ASSISTANT, reply):
            return False
        self._messages.append(Message(_ASSISTANT, reply))
        return True

    def render(self) -> str:
        """整份 prompt：system + 题目 + 摘要 + **摘要覆盖点之后的全部往来**（紧凑 JSON，正文逐字）。

        ⚠️ 不是"最近 N 轮"：压缩请求只在命令轮发得出去（回合末尾的闸门），连着几轮没轮到时
        中间那些往来必须全量带着走 —— 它们还没进任何摘要。
        """
        messages = [
            {"role": "system", "content": self.system},
            {"role": _USER, "content": self.task},
        ]
        if self.summary:
            messages.append({"role": _TOOL, "content": f"【历史摘要】\n{self.summary}"})
        messages += [
            {"role": m.role, "content": m.text} for m in self._messages[self._covered:]
        ]
        return json.dumps(messages, ensure_ascii=False, separators=(",", ":"))

    def sent_for_compression(self) -> None:
        """记下"这一趟压缩原料盖到哪"（`Agent.compression_request` 发请求时调）。"""
        self._pending = len(self._messages)

    def adopt_summary(self, text: str) -> None:
        """收下压缩轮的摘要：它盖住的是**上一次压缩请求**发出去时的全部往来。没有在途请求
        （凭空来的摘要）⇒ 只记摘要、覆盖点不动。"""
        self.summary = text
        if self._pending is not None:
            self._covered = self._pending
            self._pending = None

    def material(self) -> str:
        """压缩原料：原始上下文全文（题目 + 全部往来，逐字）。压缩总从原文重来、不从旧摘要
        叠；`render` 给任务 LLM 的是压缩后的，这里给压缩器的是原文。
        """
        return json.dumps(
            [{"role": m.role, "content": m.text} for m in self._messages],
            ensure_ascii=False,
            separators=(",", ":"),
        )
