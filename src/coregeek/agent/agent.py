"""`Agent` —— 单实例的解题智能体：一个进程一个，跨回合活着；开拓者每次来用的都是它。

跨回合状态有**两处**，都在实例上：`_sop`（**整场**存活的沉淀）与 `_context`
（**任务内**的会话，第 25 步）。两条共同的账：

- **它坏了会怎样**：都只影响 prompt 的**内容**（答得好不好），**不碰红线** ——
  会话丢了 ⇒ 退化成单轮提问（第 25 步之前的行为）；SOP 空着 ⇒ 那一段是空的。
- **不加锁**：判题器是**逐回合同步请求**；即便真有并发，GIL 下 `str` 的赋值与读取不撕裂。
- **SOP 不按任务分区**：任务 A 沉淀的会灌进任务 B（用户拍板的取舍，记录、不修）。
  会话**按任务分区**（身份 = 题目原文），但**无上界增长**（先不压缩，同样记录、不修）。

`planner.task_channel` 每次都用包根那个 `AGENT`，而不是每次新建一个 —— 单实例是
"SOP 能跨回合长出来、会话能跨回合接上"的共同前提。
"""

from collections.abc import Callable

from .chat import PROMPT
from .context import Context
from .tools.cmd import executeCmd
from .tools.sop import store


class Agent:
    """会用工具解题的智能体。**建一个就够**（`coregeek.agent.AGENT`）。"""

    def __init__(self) -> None:
        #: 「沉淀的 SOP」—— 整场存活的跨回合状态之一。重启清空。
        self._sop = ""
        #: **任务内**的会话上下文 —— 跨回合状态之二（第 25 步）。题目变了即换新；
        #: 任务结束不清（死会话，下场换题时自然被替）。
        self._context: Context | None = None
        #: 工具名 → (实现, 给 LLM 看的描述)。**描述是 prompt 的一部分**，措辞直接决定调用
        #: 正确率（要写清楚"参数是什么"与"结果怎么回来"）。顺序即 prompt 里的顺序。
        #: ⚠️ **表必须由实例构造**：`SOP2Prompt` 写的是 `self._sop` ⇒ 只能是绑定方法。
        self._tools: dict[str, tuple[Callable[[str], str], str]] = {
            "executeCmd": (
                executeCmd,
                "在判题器的沙盒里执行一条命令（能跑基础 shell 与 python 指令，不能访问外网）。"
                "参数 = 命令原文；执行结果下一回合原文发给你。",
            ),
            "SOP2Prompt": (
                self.SOP2Prompt,
                "把你总结出的解题方法**整段替换**进后续每一份 prompt 的「沉淀的 SOP」段，"
                "参数 = SOP 全文。它不产出命令、当回合也没有回执，但从此每道题都会看到它。",
            ),
        }

    def chat(self, request: str, *, result: str = "", retry: str = "") -> str:
        """组装（**累积的**）会话发给判题器的 LLM。`request` = 题目原文。

        同一道题 ⇒ 续上之前的全部往来（`self._context`）；换题 ⇒ 新会话。每轮往里放什么：
        首问 = 题目（构造即问）；回灌轮 = 结果/纠错（`feed`）；无新内容的重问轮 =
        一句「请继续。」（`nudge`）—— 判题器的 LLM 是黑盒，会话停在它自己的输出上
        是个含糊指令。返回值是**标准 messages JSON**（第 28 步：`[{role, content}]`，
        自造文本版式实测效果非常差、已弃）。

        system（四段模板）**每次现刷**：SOP 是活的，任务进行中沉淀的下一轮就得看得见
        —— 那是 `SOP2Prompt` "调用成功"的回执（它不产出命令）。
        """
        fresh = self._context is None or self._context.task != request
        if fresh:
            # 换题 ⇒ 新会话。粘住的回执也照样 feed：与旧的单轮行为一致（照样回灌）。
            self._context = Context(request)
        if result or retry:
            self._context.feed(result, retry)
        elif not fresh:
            self._context.nudge()
        self._context.system = PROMPT.format(tool_desc=self.tool_desc(), sop=self._sop)
        return self._context.render()

    def hear(self, reply: str) -> None:
        """记下判题器 LLM 这回合的回复（`planner.task_channel` 每回合都调 —— 发命令/
        交答案那两轮没有 prompt，回复照样得进会话，否则回灌时它自己的命令凭空消失）。

        还没开过会话（这道题一次都没问过）⇒ 忽略。粘住的重复由 `Context.hear` 去重。
        """
        if self._context is not None:
            self._context.hear(reply)

    def tool_call(self, tool_name: str, tool_param: str) -> str:
        """**顶层调度入口**：按名字调工具，返回要放进响应顶层 `executeCmd` 的那条命令。

        ⚠️ **"返回值即命令"是一条铁律**：`""` = 这个工具不产出命令（`SOP2Prompt`），
        下游不需要区分"不产出命令"与"调用不成立"，两者都落到"重问"。

        ⚠️ **绝不抛异常**（它跑在 `app.handle` 的 `try` 里，抛出去会把整回合所有角色的指令
        一起带走）⇒ 两道闸门一律返回 `""`：**未知工具名**、**参数不是字符串 / 空参数**。
        ⚠️ 只在这里挡、不在每个工具里再挡一遍：这是**唯一一个由外部字符串驱动**的入口，
        边界校验只应有一处（推论：`SOP2Prompt` 走正常路径时永远不会收到空串）。
        """
        entry = self._tools.get(tool_name)
        if entry is None or not isinstance(tool_param, str) or not tool_param.strip():
            return ""
        return entry[0](tool_param)

    def tool_desc(self) -> str:
        """「可使用的工具」那一段的正文 —— 由工具表**生成**，不手写第二份。

        手写第二份迟早会出现"prompt 里写了、代码里没有"（或反过来），而那种不一致
        **只有实盘上 LLM 报错才看得出来**（本地怎么测都是绿的）。
        """
        return "\n".join(f"- {name}：{desc}" for name, (_, desc) in self._tools.items())

    def SOP2Prompt(self, sop: str) -> str:
        """把 `sop` **整段替换**进「沉淀的 SOP」段。返回 `""` —— **它不产出命令**。

        存储规则（上限、截断留痕、内容没变就静默）在 `tools/sop.py`，这里只管
        **把新值记在自己身上**。方法名同时是注册表里的工具名。
        """
        self._sop = store(self._sop, sop)
        return ""

    @property
    def sop(self) -> str:
        """现在的 SOP —— 「沉淀的 SOP」段的填充值。没沉淀过 ⇒ `""`。"""
        return self._sop

    def reset(self) -> None:
        """清空（SOP 与会话**两处**）。**只给用例用** —— 单实例是模块级的，
        同一个测试进程里会跨用例串味。

        不复用 `SOP2Prompt("")`：那个会打日志，而用例的 `assertLogs` 正盯着日志。
        """
        self._sop = ""
        self._context = None
