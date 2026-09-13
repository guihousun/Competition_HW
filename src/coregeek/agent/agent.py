"""`Agent` —— 单实例的解题智能体：一个进程一个，跨回合活着；开拓者每次来用的都是它。

**它是全项目唯一一处跨回合状态**（`self._sop`，PromptSOP 的沉淀）。`handle` 从第 11 步起
一直是无状态纯函数，第 18 步为本能力破例、第 19 步把状态**收敛到这个对象上** ——
不再是"某个模块里藏着一个变量"，而是"这一个实例身上带着记忆"。三条代价与缓解：

- **它坏了会怎样**：SOP 读出来是空串 ⇒ prompt 里那一段是空的，判题器那一侧看不出区别 ——
  **不碰红线**。SOP 只影响"答得好不好"，不影响"报文字节合不合法"。
- **不加锁**：`web/server.py` 是 `ThreadingHTTPServer`，但判题器是**逐回合同步请求**
  （不会有两条同时进来改 SOP）；即便真有并发，GIL 下 `str` 的赋值与读取不会撕裂，
  最坏结果是"某一条 prompt 带着上一版 SOP"。**不碰红线**，所以不付锁的代价。
- **SOP 不按任务分区**：任务 A 沉淀的会灌进任务 B。用户拍板的取舍，记录、不修。

**"注入"在这里是什么**：`planner.task_channel` 每次都用包根那个 `AGENT`
（`from ..agent import AGENT`），而不是每次新建一个 —— 单实例是"SOP 能跨回合长出来"的前提。
"""

from collections.abc import Callable

from .chat import chat
from .tools.cmd import executeCmd
from .tools.sop import store


class Agent:
    """会用工具解题的智能体。**建一个就够**（`coregeek.agent.AGENT`）。"""

    def __init__(self) -> None:
        #: 「沉淀的 SOP」—— **全项目唯一一处跨回合状态**。整场存活、重启清空。
        self._sop = ""
        #: 工具名 → (实现, 给 LLM 看的描述)。**描述是 prompt 的一部分**，措辞直接决定调用
        #: 正确率（要写清楚"参数是什么"与"结果怎么回来"，否则 LLM 会猜）。顺序即 prompt 里
        #: 的顺序（`dict` 保序）。
        #:
        #: ⚠️ **表必须由实例构造**：`SOP2Prompt` 写的是 `self._sop`，不是某个模块的全局变量
        #: ⇒ 它只能是绑定方法（第 19 步从 `tools/__init__.py` 的模块级常量搬来这里的原因）。
        #: 两个工具都**只依赖标准库、且除了 `SOP2Prompt` 都没有副作用**。
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
        """组装一份上下文发给判题器的 LLM。`request` = 题目原文（payload 里的 `phaseTask`）。

        `sop` 与 `tool_desc` 是**本实例自己的**（状态与它注册的工具表），
        所以 `chat.py` 那边一个包内 import 都不需要 —— 它是纯函数模块。
        """
        return chat(request, sop=self._sop, tool_desc=self.tool_desc(), result=result, retry=retry)

    def tool_call(self, tool_name: str, tool_param: str) -> str:
        """**顶层调度入口**：按名字调工具，返回要放进响应顶层 `executeCmd` 的那条命令。

        ⚠️ **"返回值即命令"是一条铁律**：`""` = 这个工具不产出命令（`SOP2Prompt`），
        下游不需要区分"不产出命令"与"调用不成立"（未知工具 / 空参数），两者都落到判据 ⑥ 重问。
        引出"工具种类"（命令 / 观察）那类判别器会给这条铁律开一个洞，而收益只是把
        "LLM 看得见 SOP 段"换成一句明写的回执（第 18 步评审提过、否决）。

        ⚠️ **绝不抛异常**：它跑在 `app.handle` 的 `try` 里，抛出去会把**整回合所有角色的指令**
        一起带走（不只任务线），退化成空指令 —— 虽然合法、不计异常，但白白丢一个回合。
        所以两道闸门**一律返回 `""`**，而不是抛：

        - **未知工具名**（LLM 编了一个，或者它根本没照我们的形状回）；
        - **参数不是字符串 / 参数是空的**（`strip()` 后为空）—— 空参数调一个工具没有任何意义。

        ⚠️ **为什么只在这里挡、不在每个工具里再挡一遍**：这是**唯一一个由外部字符串驱动**的
        入口（工具名与参数都是 LLM 的原文），边界校验只应有一处。顺带的推论是：`SOP2Prompt`
        走正常路径时**永远不会收到空串**，所以它那条"空串 = 清空"的语义只对直接调用者开放
        （有用例钉着）。
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

        存储规则（上限、截断留痕、内容没变就静默）在 `tools/sop.py`，这里只管一件事：
        **把新值记在自己身上**。方法名保持用户给的接口名（它同时是注册表里的工具名）。
        """
        self._sop = store(self._sop, sop)
        return ""

    @property
    def sop(self) -> str:
        """现在的 SOP —— 「沉淀的 SOP」段的填充值。没沉淀过 ⇒ `""`。"""
        return self._sop

    def reset(self) -> None:
        """清空。**只给用例用** —— 单实例是模块级的，同一个测试进程里会跨用例串味。

        不复用 `SOP2Prompt("")`：那个会打日志，而用例的 `assertLogs` 正盯着日志，
        多出来的"清空"那一行会让断言变成"看运气"（取决于上一个用例往 SOP 里存过什么）。
        """
        self._sop = ""
