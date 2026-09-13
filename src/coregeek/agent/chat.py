"""Agent 的"说什么 / 怎么读回复"：prompt 模板、上下文组装、三个谓词。

**这个包不认识游戏。** 它收字符串、吐字符串，只依赖标准库 —— 不 import `game` / `protocol` /
`web` 任何东西。切分的判据是：**"什么时候跟 LLM 说话"是策略**（判据链、纠错、开拓者在不在，
全在 `planner.task_channel`），**"说什么、怎么解析回复"**与战场规则无关，搬到这里。
机械保证就是 `chat(request: str, ...)` 的签名 —— **收字符串，不收 `Turn`**。

**本模块是纯的**（第 19 步）：它**不 import 包里的任何东西**，`sop` 与 `tool_desc`
由调用方（`Agent.chat`）**当参数传进来** —— 状态只有 `Agent` 那一个归处这件事，
因此从注释变成了结构。想读"现在的 SOP 是什么"，只有一个地方可去。

模板与三个谓词**不拆成两个模块**（`prompt.py` / `parse.py`）：它们必须一起读
（严谨的 `tool_of`、宽的 `looks_like_tool`、带兜底的 `answer_of` 是同一份约定的三面），
拆开就是"改一处要翻两个文件"。

⚠️ **协议形状是我们自己定的。** 任务书只说沙盒"能执行基础的 shell 指令与 python 指令、
无法访问外部网络"（L439），**一个字都没规定 LLM 该怎么要一条命令**。所以：

- `# 输出格式` 那段必须把两个形状**逐字**给全（唯一能提高"LLM 照抄概率"的杠杆）；
- `tool_of` 留着第 16 步的**旧形状兼容**（`<tool>整条命令</tool>` ⇒ 当 `executeCmd`），
  `answer_of` 留着**原文即答案**的兜底 —— 两者都是"新形状不被认账"时的退路（用户拍板全覆盖兼容）。
"""

#: 发给判题器 LLM 的模板。四段（用户定的形状）：
#: **定位** → **工具**（由 `Agent.tool_desc()` 生成）→ **输出格式** → **沉淀的 SOP**，
#: 后面再挂第 16 步就有的两段回灌与题目原文。
#:
#: ⚠️ **「沉淀的 SOP」那一段的头永远都在**，哪怕还没沉淀过任何东西：那个槽是 LLM 自己写的
#: 目标（`SOP2Prompt` 的用途就写在上面「可使用的工具」里），看不见槽就不会去用它。
#: 而且形状稳定 ⇔ prompt 的字节量可预估（硬约束 5）。
#:
#: 五个占位符由 `chat` 填，`str.format` **只做一次** —— SOP 原文、沙盒结果、题目原文都是
#: **替换值**，里面若含 `{}`（python 代码片段里太常见了）不会被二次扫描成占位符。
PROMPT = """# Agent定位
你是一个会用工具解题的智能体。手上的信息不够，就调用工具去取；取够了，就直接作答。
一回合**只输出一样东西**：一次工具调用，或者一个答案。不要解释、不要前言、不要 Markdown 代码块标记。

# 可使用的工具
{tool_desc}

# 输出格式
要调工具时，用 `<tool>` 包住，里面写工具名与参数：

<tool><tool_name>工具名</tool_name><tool_param>参数原文</tool_param></tool>

例：<tool><tool_name>executeCmd</tool_name><tool_param>cat /tmp/a.txt</tool_param></tool>

一次只调用一个工具。不用再调工具、可以直接作答时，把**答案本身**放进 `<answer>`：

<answer>答案本身</answer>

# 沉淀的 SOP
{sop}
{result}{retry}
题目：
{task}"""

#: 工具调用的**开标签前缀**。`tool_of` 按它取块，`looks_like_tool` 按它判形状。
#: 用**前缀**（而不是整段 `<tool>`）是有意的：`looks_like_tool` 要判**宽**（`<tool>`、`<tool >`、
#: `<tool_name>` 都算"它想调工具"），否则畸形回复会被当成答案交上去（见 `answer_of` 判据 2）。
_TOOL_OPEN = "<tool"
_TOOL_CLOSE = "</tool>"


def chat(
    request: str, *, sop: str, tool_desc: str, result: str = "", retry: str = ""
) -> str:
    """组装一份上下文：四段模板 + （可选）沙盒结果 + （可选）纠错 + 题目原文。

    `request` = 题目原文（payload 里的 `phaseTask`）。**收字符串不收 `Turn`** 是有意的：
    收了 `Turn` 这个包就认识游戏了，"什么时候说话"与"说什么"也会跟着缠在一起。

    `sop` 与 `tool_desc` 是**必填**的两个关键字（第 19 步从"模块级状态 + 模块级注册表"
    改成参数）：填哪一段 SOP、列出哪些工具，是**调用方**（`Agent`）的事实，
    本模块不该去别处取。它们没有默认值，正是为了让"忘了传"在调用点就炸，
    而不是静默地发一份空槽的 prompt 出去。

    `result` / `retry` 没有就不占地方，但**各自带标题**：沙盒输出是**任意文本**
    （可能是 JSON、可能是报错、可能带换行），没有标题档着，LLM 分不清哪一段是题目、
    哪一段是它要的输出。两段的措辞与第 16 步**逐字一致**（用例拿它们当判据）。
    """
    return PROMPT.format(
        tool_desc=tool_desc,
        sop=sop,
        result=f"\n【上一条命令的执行结果（原文）】\n{result}\n" if result else "",
        retry=(
            f"\n【你上一次提交的答案被判定为不正确】\n{retry}\n请重新作答。\n" if retry else ""
        ),
        task=request,
    )


def tool_of(reply: str) -> tuple[str, str] | None:
    """解析工具调用 ⇒ `(工具名, 参数)`；不是工具调用、或形状不完整 ⇒ `None`。

    判据**严格**（与 `looks_like_tool` 故意相反）：

    1. 取**第一对** `<tool>` … `</tool>`（开标签的 `>` 位置定位，所以 `<tool >` 也认）。
       不成对 ⇒ `None`。
    2. 块里有 `<tool_name>` 与 `<tool_param>` ⇒ **两个都要成对**才给 `(名字.strip(), 参数)`。
       只有名字没参数（或反过来）⇒ `None` —— **不猜半个调用**，让调用方落到"重问"那一支。
       ⚠️ **参数的内部换行原样保留**，只去首尾空白：多行命令、带缩进的 python 片段都合法。
    3. **块里两个标签都没有 ⇒ 整块正文就是一条 `executeCmd` 命令**（第 16 步的旧形状
       `<tool>ls -la</tool>`，用户拍板的兼容层）。⚠️ 这里**不加"含 `<` 就当畸形"的守卫**：
       那会误杀 `cat < input.txt` 这种带重定向的合法命令，而两种失败都会自纠
       （发进沙盒的命令沙盒会报错 → 结果原文回灌给 LLM 下一轮改口）。
    4. 块是空的（`<tool></tool>`）⇒ `None`。空调用没有任何含义，别拿它去调空参数的工具。

    只取**第一条**：`executeCmd` 只有一个字段、判题器一回合只跑一条（接口文档 L210），
    多要几条也只会丢掉。
    """
    body = _block(reply, _TOOL_OPEN, _TOOL_CLOSE)
    if body is None:
        return None
    name = _block(body, "<tool_name>", "</tool_name>")
    param = _block(body, "<tool_param>", "</tool_param>")
    if name is None and param is None:
        text = body.strip()
        return ("executeCmd", text) if text else None
    if name is None or param is None:
        return None
    return name.strip(), param.strip()


def looks_like_tool(reply: str) -> bool:
    """这条回复**像是**工具调用吗？判据只认开标签前缀出现 —— 与第 16 步**逐字相同**（故意判宽）。

    判得比 `tool_of` 宽是有意的：`<tool ls`、`<tool >ls</tool>`、只有 `<tool_name>` 而没外层
    `<tool>` 的回复，都该被认成"它想调工具、但格式没凑对" —— 落到**重问**，而不是被当成
    答案交上去。代价是答案里若含字面量 `<tool` 会被误判（拒绝提交、改问），概率极低，
    而且降级方向是"少交一次"而不是"发非法指令"。

    ⚠️ **`answer_of` 判据 2 必须用这个宽谓词**：用 `tool_of` 就漏掉畸形工具回复，
    而那正是"两条通道同时哑火、永久空转、日志上什么都看不出来"的成因（第 16 步踩过）。
    """
    return _TOOL_OPEN in reply


def answer_of(reply: str) -> str:
    """**该提交什么** —— `planner` 的 `_answer_task` 与 `task_channel` 判据 ④/⑤**共用同一个谓词**。

    **「该提交什么」与「该骂什么」是同一件事**：判题器说"上次答案不对"时，我们要回灌给 LLM 的
    正是**上一回合真正交上去的那一份**。两处各判一次（一个取原文、一个取解包后的内容），
    就会出现"拿着 `<answer>晴</answer>` 去骂'你上次答的 `晴` 不对'" —— 骂的和交的不是一份。

    三级判据，从上往下：

    1. **出现 `<answer` 标记 ⇒ 只认成对块的内容**；配对不上、或内容为空 ⇒ `""`。
       空块**不回落成原文**：那一回合宁可不提交（判题器按"通过率最高的一份"算分，
       少交一次不扣分），也不要把半个标签或者一句废话交上去。
    2. **否则像是工具调用 ⇒ `""`**。工具回复要的是"去沙盒跑一下"，交上去等于把
       `<tool>ls</tool>` 当成答案。
    3. **否则原文即答案**（第 16 步的行为，逐字不变）。判题器的 LLM 是黑盒，它认不认
       `<answer>` 我们没得选 —— 这是不被认账时唯一的退路（用户拍板全覆盖兼容）。
    """
    if "<answer" in reply:
        block = _block(reply, "<answer", "</answer>")
        return block.strip() if block is not None else ""
    if looks_like_tool(reply):
        return ""
    return reply.strip()


def _block(text: str, open_tag: str, close_tag: str) -> str | None:
    """取**第一对**标签之间的正文；不成对 ⇒ `None`。

    开标签按 **`>` 的位置**定位而不是逐字匹配整段标签，所以 `<tool >` / `<answer >` 也认
    （第 16 步 `_tool_command` 就是这么做的，理由一致：LLM 多敲个空格不该让整条回复作废）。

    **"成对"是所有标签判据的共同要求**，所以只写一遍 —— 四个调用点（`<tool>` / `<tool_name>` /
    `<tool_param>` / `<answer>`）共用它。有开无闭的那半截**绝不能**当成内容用：
    要么被当答案交上去（判题器收到一段半截标签），要么被当命令发进沙盒（跑一个残缺命令）。
    """
    start = text.find(open_tag)
    if start < 0:
        return None
    head_end = text.find(">", start)
    if head_end < 0:
        return None
    end = text.find(close_tag, head_end)
    if end < 0:
        return None
    return text[head_end + 1 : end]
