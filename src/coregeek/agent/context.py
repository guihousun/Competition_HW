"""Context —— **一个任务**的完整会话上下文（第 25 步；第 28 步起渲染成标准 messages JSON）。

判题器的 LLM 每回合只看到我们发出的 `prompt` 一段字符串（接口文档标它为 String）
⇒ 第 28 步起这段字符串是一个 **JSON 数组**：`[{"role": "system"/"user"/"assistant",
"content": ...}]` —— 标准 chat 格式。第 25 步那版自造的文本版式（`# 对话记录` +
标题块）用户实测**效果非常差**，已弃：结构由 role 表达，正文就是原文。
`Context` 只管存储与渲染；什么时候建、每轮往里放什么 是 `Agent` 的事
（这里收发的全是字符串，不认识游戏）。

**存储全量、渲染有窗**（第 39 步起，"先不压缩"的旧口径就此兑现退役）：消息表仍是
**逐字全量**（`json.dumps`/`loads` 负责转义与还原），但 `render()` 只输出
**题目 + 执行摘要 + 最近 `_WINDOW` 轮** —— prompt 从 O(全部历史) 变成 O(窗口)，
长任务的 64KB 沙盒回执堆不出来了（旧账：无上界增长会撞"响应超时"这条红线）。
摘要由 `Agent.hear` 从每条回复的 `<summary>` 块被动提取（best-effort），渲染成
题目后面的一条 user 消息，替被窗口掐掉的旧往来记账。

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
#: 工具结果用 `tool` 角色（标准 chat 格式的第三个 role）：沙盒回执是**工具的产出**，
#: 不是人类的指令 —— 标成 `user` 会让判题器的 LLM 把命令输出当成"用户说了什么"，
#: 影响它对"任务是否已完成"的判断（上一轮加了决策句之后这点更关键：决策句是
#: 系统的话、归 `user`；结果本身是工具的话、归 `tool`，两者不该混在一条消息里）。
_TOOL = "tool"

#: 无新内容的重问轮（畸形回复 / `SOP2Prompt` 之后）追加的固定收尾。判题器的 LLM
#: 是黑盒：会话停在它自己的输出上是个含糊指令，一句"请继续"把"该你了"说清楚。
#: 措辞是拍的，实盘可调。
NUDGE = "请继续。"

#: 渲染窗口（第 39 步，**拍的**）：原始往来最多保留最近几"轮"——一条 assistant 及
#: 其后跟着的 tool 结果 / 纠错 / nudge 算一轮。更早的只有摘要替它记着。2 的依据：
#: 短任务（≤3 轮）在这个窗口下与不压缩**逐字节同形**（掐不着），长任务的 prompt
#: 才真正被压住。要调就改这一个常量。
_WINDOW = 2


class Context:
    """同一道题的会话上下文。**建一个用一道题**（`Agent.chat` 换题即换新）。"""

    def __init__(self, task: str) -> None:
        #: 身份 = **题目原文**（`phaseTask`）。换题即换会话；冷却后同文再现则续上 ——
        #: 它还记得自己试过什么。任务结束不清（死会话，下场换题时自然被替）。
        self.task = task
        #: system（`prompt.py` 的段模板）。**Agent 每次发送前刷新**，见模块 docstring。
        self.system = ""
        #: **执行摘要**（第 39 步压缩的另一半）：LLM 每条回复搭车的 `<summary>` 内容，
        #: 由 `Agent.hear` 被动提取（**best-effort**：这条回复没带就保留旧值）。
        #: 渲染成题目后面的一条 user 消息，替被窗口掐掉的旧往来记账 —— 所以它必须
        #: 装得下"要交什么 + 关键数据原文"（prompt 的四槽结构管这个）。
        self.summary = ""
        #: **构造即问**：首条 user 消息就是题目原文（不加包装 —— 结构由 role 表达）。
        self._messages: list[Message] = [Message(_USER, task)]

    def feed(self, result: str = "", retry: str = "") -> None:
        """回灌轮的新消息（**按 role 分条**，不再拼成一条 user）：沙盒结果 = `tool`，
        决策句与纠错 = `user` —— 标题留在 content 里当**内容标签**
        （沙盒输出是任意文本，没标签分不清哪段是什么）。

        ⚠️ **结果单独占一条 `tool` 消息**：命令输出是**工具的产出**、不是人类指令，
        标成 `user` 会让判题器的 LLM 把输出当成"用户说了什么"（见 `_TOOL` 的注释）。

        ⚠️ **结果后紧跟一句 user 决策句**：结果回灌是"任务有没有做完"的关键判据轮
        —— LLM 只有这一轮能看到输出并决定下一步。不提示，它容易在答案已经在输出里
        时继续执行多余命令（一轮命令 = 一轮掉分）；提示把它拽回"先判断、再决定"。
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
        """整份 prompt：**system + 题目 + 摘要 + 最近 `_WINDOW` 轮**（第 39 步压缩）。

        存储全量、渲染有窗（见模块 docstring）：题目永远完整（那是"要交什么"的
        权威来源，掐什么也不能掐它）；摘要在题目后面、原始往来前面 —— 它讲的是
        旧账，最近的往来才是现状。`json.dumps`（`ensure_ascii=False`、紧凑分隔符）
        —— 正文里的 `{}`、换行、标签全部逐字保留。
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
        """最近 `_WINDOW` 轮的原始往来：从消息表第 1 条起（第 0 条是题目，
        render 单独放），最后 `_WINDOW` 条 assistant 里**最早**的那条起、其后全部
        保留 —— tool 结果 / 纠错 / nudge 都跟着它们前面那条 assistant 走。
        assistant 不足 `_WINDOW` 条 ⇒ 全量（短任务掐不着）。"""
        body = self._messages[1:]
        kept = [i for i, m in enumerate(body) if m.role == _ASSISTANT]
        if len(kept) <= _WINDOW:
            return body
        return body[kept[-_WINDOW]:]
