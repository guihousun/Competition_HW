"""工具注册表（名 → 实现）与顶层调度入口 `tool_call`。

Prompt 里那段「可使用的工具」（`{{tool_desc}}`）就是这张表**生成**的 —— 加一个工具只需在
`TOOLS` 里加一行，prompt 与调度同时跟上。手写第二份迟早会出现"prompt 里写了、代码里没有"
（或反过来），而那种不一致**只有实盘上 LLM 报错才看得出来**（本地怎么测都是绿的）。

⚠️ **"返回值即命令"是一条铁律**：工具函数的返回值直接进响应顶层的 `executeCmd`，
`""` = 这个工具不产出命令（`SOP2Prompt`）。整条编排因此只有一个概念 ——
"LLM 点了工具 → 我们给出命令"。引出"工具种类"（命令 / 观察）那类判别器会给这条铁律
开一个洞，而收益只是把"LLM 看得见 SOP 段"换成一句明写的回执（不划算，第 18 步评审提过、否决）。
"""

from collections.abc import Callable

from .cmd import executeCmd
from .sop import SOP2Prompt

#: 工具名 → (实现, 给 LLM 看的描述)。**描述是 prompt 的一部分**，措辞直接决定调用正确率
#: （要写清楚"参数是什么"与"结果怎么回来"，否则 LLM 会猜）。顺序即 prompt 里的顺序（`dict` 保序）。
TOOLS: dict[str, tuple[Callable[[str], str], str]] = {
    "executeCmd": (
        executeCmd,
        "在判题器的沙盒里执行一条命令（能跑基础 shell 与 python 指令，不能访问外网）。"
        "参数 = 命令原文；执行结果下一回合原文发给你。",
    ),
    "SOP2Prompt": (
        SOP2Prompt,
        "把你总结出的解题方法**整段替换**进后续每一份 prompt 的「沉淀的 SOP」段，参数 = SOP 全文。"
        "它不产出命令、当回合也没有回执，但从此每道题都会看到它。",
    ),
}


def tool_desc() -> str:
    """「可使用的工具」那一段的正文 —— 由 `TOOLS` **生成**，不手写第二份。"""
    return "\n".join(f"- {name}：{desc}" for name, (_, desc) in TOOLS.items())


def tool_call(tool_name: str, tool_param: str) -> str:
    """**顶层调度入口**：按名字调工具，返回要放进 `executeCmd` 的那条命令（`""` = 没有命令）。

    ⚠️ **绝不抛异常**：它跑在 `app.handle` 的 `try` 里，抛出去会把**整回合所有角色的指令**
    一起带走（不只任务线），退化成空指令 —— 虽然合法、不计异常，但白白丢一个回合。
    所以两道闸门**一律返回 `""`**，而不是抛：

    - **未知工具名**（LLM 编了一个，或者它根本没照我们的形状回）；
    - **参数不是字符串 / 参数是空的**（`strip()` 后为空）—— 空参数调一个工具没有任何意义。

    ⚠️ **为什么只在这里挡、不在每个工具里再挡一遍**：这是**唯一一个由外部字符串驱动**的入口
    （工具名与参数都是 LLM 的原文），边界校验只应有一处。顺带的推论是：`SOP2Prompt` 走正常
    路径时**永远不会收到空串**，所以它那条"空串 = 清空"的语义只对直接调用者开放（有用例钉着）。
    """
    entry = TOOLS.get(tool_name)
    if entry is None or not isinstance(tool_param, str) or not tool_param.strip():
        return ""
    return entry[0](tool_param)
