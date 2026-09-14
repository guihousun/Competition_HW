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
        #: 工具名 → (实现, 给 LLM 看的描述, **参数表** `((参数名, 用途), …)`)。
        #: 描述与参数说明都是 prompt 的一部分，措辞直接决定调用正确率；参数表同时是
        #: `tool_call` 的**调度签名**（实现按关键字收参，见第 30 步）—— 一张表两处用，
        #: 描述与调度不会分家。顺序即 prompt 里的顺序。
        #: ⚠️ **表必须由实例构造**：`SOP2Prompt` 写的是 `self._sop` ⇒ 只能是绑定方法。
        self._tools: dict[
            str, tuple[Callable[..., str], str, tuple[tuple[str, str], ...]]
        ] = {
            "executeCmd": (
                executeCmd,
                "在判题器的沙盒里执行一条命令（能跑基础 shell 与 python 指令，不能访问外网）。"
                "当命令涉及到文件 path 时，若无法判定文件的位置，先找到文件的位置。",
                (("cmd", "命令原文"),),
            ),
            "SOP2Prompt": (
                self.SOP2Prompt,
                "把你总结出的解题方法整段替换进后续每一份 prompt 的「沉淀的 SOP」段。"
                "它不产出命令、当回合也没有回执，但从此每道题都会看到它。"
                "产出的sop应该是任务无关的，而是对方法的总结，且要尽量简短。"
                "所以调用它的那一回合必须把答案一起写上。",
                (("sop", "SOP 全文"),),
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

    def tool_call(self, tool_name: str, params: list[tuple[str | None, str]]) -> str:
        """**顶层调度入口**：按名字调工具，返回要放进响应顶层 `executeCmd` 的那条命令。

        `params` 是 `tool_of` 解析出来的 `[(参数名|None, 原文), …]`（第 30 步）：
        **无名参数按声明的参数表位置填充**（单参数工具的无名形状、旧形状
        `<tool>ls</tool>` 全是它的特例）；**带名的按名对**，认不出的名字忽略
        （不为一个编造的名字作废整次调用）；声明了的参数**一个不少、值是非空字符串**
        才放行，最后 `impl(**resolved)` —— 无参数工具就是 `impl()`。

        ⚠️ **"返回值即命令"是一条铁律**：`""` = 这个工具不产出命令（`SOP2Prompt`）/
        调用不成立（未知工具、形状不对、缺参数、空白值），**下游不需要区分**，全落到"重问"。

        ⚠️ **绝不抛异常**（它跑在 `app.handle` 的 `try` 里，抛出去会把整回合所有角色的指令
        一起带走）⇒ 一切不成立都返回 `""`。⚠️ 边界校验只在**这一处**：这是唯一一个由
        外部字符串驱动的入口（推论：`SOP2Prompt` 走正常路径时永远不会收到空串 ——
        空白参数在进工具之前就被挡下，清不掉已存的 SOP）。
        """
        entry = self._tools.get(tool_name)
        if entry is None:
            return ""
        impl, _, spec = entry
        resolved: dict[str, str] = {}
        position = 0
        try:
            for name, value in params:
                if name is None:
                    if position >= len(spec):
                        return ""  # 位置参数多过声明 —— 多半是 LLM 自己编的
                    resolved[spec[position][0]] = value
                    position += 1
                elif name in dict(spec):
                    resolved[name] = value
            # 认不出的参数名：忽略（宽容那一侧）
        except (TypeError, ValueError):
            return ""  # params 不是 [(名|None, 文本)] 的形状
        if any(
            not isinstance(resolved.get(pname), str) or not resolved[pname].strip()
            for pname, _ in spec
        ):
            return ""
        return impl(**resolved)

    def tool_desc(self) -> str:
        """「可使用的工具」那一段的正文 —— 由工具表**生成**，不手写第二份。

        第 32 步起每个工具是一个**块**（用户指定的格式）：

            ## ToolName: {name}
            Description: 一句话说清它干什么
            Params:
                - 参数名: 用途

        无参数的工具打 `Params: （无参数）` —— 不用 `- （无参数）`，那看起来像
        多了一个叫"（无参数）"的参数。块之间空一行（`##` 标题摆在那里，不空行会黏成一坨）。
        参数说明是**生成**的，LLM 照着表写调用、不靠描述正文里的散文：手写第二份迟早会出现
        "prompt 里写了、代码里没有"（或反过来），而那种不一致**只有实盘上 LLM 报错
        才看得出来**（本地怎么测都是绿的）。
        """
        blocks: list[str] = []
        for name, (_, desc, params) in self._tools.items():
            lines = [f"## ToolName: {name}", f"Description: {desc}"]
            if params:
                lines.append("Params:")
                lines += [f"    - {pname}: {pdesc}" for pname, pdesc in params]
            else:
                lines.append("Params: （无参数）")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

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
