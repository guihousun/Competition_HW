"""Context —— 一个任务的会话上下文，渲染成标准 messages JSON（判题器的 LLM 每回合只看到
`prompt` 这一段字符串）。这里只管存储与渲染，建会话、每轮放什么由 `Agent` 编排。

存储全量、渲染**只走到最近一次工具调用**：消息表逐字全量；`render()` = 题目 + 【历史摘要】 +
**最近一次工具调用与它的结果**（`_tail_start` 起的那一段）。夹在中间那些往来不再逐条渲染 ——
它们的去处是摘要，`middle()` 给出的就是喂给压缩器的**中间段**（题目 + 已有摘要 + 覆盖点之后
的全部往来）。摘要没盖到的一句都不丢：覆盖点只在**摘要真到的那一刻**推进，`render` 取
`min(覆盖点, 尾巴起点)` ⇒ 判题器不答时那些往来照旧全量跟着走。
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
        #: 一条 assistant 消息、替它盖住的那段往来记账（与压缩原料里那一份同文）。
        self.summary = ""
        #: 摘要盖到 `_messages` 的第几条（下标）：这条之前的往来只在摘要里、不再逐条渲染。
        #: 首元素是题目（单独渲染）⇒ 起点是 1 = "还没盖住任何往来"。
        self._covered = 1
        #: 第一份**还没被答复**的压缩请求压到哪（= 那一刻的尾巴起点）。摘要回来时按它划覆盖点；
        #: 请求发了而判题器没答 ⇒ 覆盖点不动，那些往来照旧全量渲染（**绝不能因为"发过请求"
        #: 就当它们已被摘要盖住** —— 那等于把没进摘要的往来丢掉）。在途期间再发的请求不改写它。
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

    def _head(self) -> list[dict]:
        """渲染与压缩原料共用的前半：题目 +（有摘要就带上）【历史摘要】。

        摘要标成 `assistant`：它是替旧往来记账的一段压缩话，与"最近一次工具调用"同侧，
        不是新的一轮用户提问。
        """
        head = [{"role": _USER, "content": self.task}]
        if self.summary:
            head.append({"role": _ASSISTANT, "content": f"【历史摘要】\n{self.summary}"})
        return head

    def _tail_start(self) -> int:
        """保留尾巴的起点 = 最后一条 `assistant` 消息的下标（一条都没说过 ⇒ 表尾）。

        尾巴 = 最近一次工具调用（LLM 那条回复）与它的结果。渲染与压缩原料共用这一条口径：
        尾巴之前的往来才是该进摘要的，尾巴本身照旧原样留着（压它只是白花 token）。
        """
        for i in range(len(self._messages) - 1, -1, -1):
            if self._messages[i].role == _ASSISTANT:
                return i
        return len(self._messages)

    def render(self) -> str:
        """整份 prompt：system + 题目 + 【历史摘要】 + **最近一次工具调用与它的结果**。

        起点取 `min(摘要覆盖点, 尾巴起点)`：摘要新鲜（已盖过尾巴）⇒ 只剩尾巴那一对；
        摘要还没盖上（在途请求没回来 / 压根没压过）⇒ 从覆盖点起，那一段一句都不丢。
        两头的保证因此同时在：**至少**留着最近一次调用与结果，**不多**留更早的散条。
        """
        messages = [{"role": "system", "content": self.system}]
        messages += self._head()
        messages += [
            {"role": m.role, "content": m.text}
            for m in self._messages[min(self._covered, self._tail_start()):]
        ]
        return json.dumps(messages, ensure_ascii=False, separators=(",", ":"))

    def middle(self) -> str:
        """压缩原料 = **中间段**：覆盖点之后的全部往来，头上加题目与已有摘要。

        题目永远带着（没有它，压缩器的【总目标】"照抄任务书原文"没依据）；已有摘要在最前
        —— 新摘要是**在它之上并进这一段**，逐次压缩因此不丢老账。**"这段非空"就是压缩闸门
        的判据**：有了就压，没有（一条往来都还没盖住）⇒ `""`。
        """
        if len(self._messages) <= self._covered:
            return ""
        messages = self._head()
        messages += [
            {"role": m.role, "content": m.text} for m in self._messages[self._covered:]
        ]
        return json.dumps(messages, ensure_ascii=False, separators=(",", ":"))

    def sent_for_compression(self) -> None:
        """记下"这一趟压缩原料盖到哪"（`Agent.compression_request` 发请求时调）：表尾 ——
        原料压的就是此刻的全部往来，摘要回来时覆盖点推到这儿。

        ⚠️ **在途的那一份不被后来的请求改写**：判题器漏答一份时，后一份的原料本来就含着
        前一份那段，覆盖点按**先发**那份推是保守的（少盖一段、照旧渲染）；按后发那份推则会
        把"先发那份盖住、后发那份没盖住"的中间那段凭空丢掉。
        """
        if self._pending is None:
            self._pending = len(self._messages)

    def adopt_summary(self, text: str) -> None:
        """收下压缩轮的摘要：它盖住的恰好是**上一份压缩请求**那一趟的原料（覆盖点推到那儿；
        尾巴那一对本来就不在原料里）。没有在途请求（凭空来的摘要）⇒ 只记摘要、覆盖点不动。
        """
        self.summary = text
        if self._pending is not None:
            self._covered = self._pending
            self._pending = None

    def material(self) -> str:
        """本题的完整记录（题目 + 全部往来逐字）—— 沉淀请求（`Agent.sop_request`）的原料。

        与压缩原料 `middle()` 分工：压缩只要中间段（老账已经在摘要里、尾巴原样留着），
        沉淀要**全部** —— 交卷之后那一条是它唯一一次看记录的机会（"以上是本次任务的全部记录"）。
        """
        return json.dumps(
            [{"role": m.role, "content": m.text} for m in self._messages],
            ensure_ascii=False,
            separators=(",", ":"),
        )
