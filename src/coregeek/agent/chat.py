"""Agent 的"说什么 / 怎么读回复"：prompt 的五段模板（system 消息的内容）+ 三个谓词。

**这个包不认识游戏**：收字符串、吐字符串，只依赖标准库。切分判据是"什么时候跟 LLM 说话"
属策略（在 `planner.task_channel`），"说什么、怎么解析回复"与战场规则无关。
机械保证就是模板与 `Context` 收发的全是字符串、不收 `Turn`。

**本模块是纯的**：不 import 包里的任何东西。会话的存储与渲染第 25 步起在 `context.py`
（`Context`）、编排在 `Agent.chat`；第 28 步起整份 prompt 是**标准 messages JSON**
（`[{role, content}]`，自造的文本版式用户实测效果非常差、已弃），这里的模板只当
**system 消息的 content**。模板与谓词不拆成两个模块：它们是同一份约定的两面，
拆开就是"改一处要翻两个文件"。

⚠️ **协议形状是我们自己定的**（任务书只说了沙盒能跑什么，没规定 LLM 该怎么要命令）
⇒ `# 输出格式` 那段必须把两个形状逐字给全，并保留旧形状兼容与"原文即答案"的兜底。
"""

#: 发给判题器 LLM 的 **system 消息内容**。五段（**定位** → **工具** → **输出格式** →
#: **沉淀的 SOP** → **工作流**），由 `Context.render` 装进 `[{"role": "system", "content": …}]`
#: 的头一条、后面跟这道题的全部往来（第 28 步起整份 prompt 是标准 messages JSON）。
#:
#: ⚠️ **「沉淀的 SOP」那一段的头永远都在**，哪怕还没沉淀过任何东西：那个槽是 LLM 自己写的
#: 目标，看不见槽就不会去用它。两个占位符由 `Agent.chat` 填，`str.format` **只做一次**
#: —— 替换值里若含 `{}`（python 片段里太常见了）不会被二次扫描成占位符。
#: ⚠️ **模板自身的正文里也不许出现裸 `{}`**（第 35 步加「工作流」时定的规矩）：`format` 会把
#: 它当占位符 ⇒ 运行期 `KeyError` ⇒ 整回合退化成空指令。命令范式用 `$(...)`，安全。
PROMPT = """
# Agent定位
你是一个自主任务执行Agent，能根据用户的任务基于现有的工具了解任务并理解任务，理解任务后严格按照任务要求完成任务；当认为解题流程值得沉淀时，用 SOP2Prompt 沉淀下来，并与最终答案写在同一条回复里。

当手上的信息不足时，就调用工具去取；认定完成任务后，就直接作答。
一回合只输出一样东西：一次工具调用，或者一个答案 —— 唯一的例外是 SOP2Prompt：它不产出命令，单独占一回合就是白花一回合，所以调用它时必须把答案一起写上。不要解释、不要前言、不要 Markdown 代码块标记。

# 可使用的工具
{tool_desc}

# 输出格式
要调工具时，用 `<tool>` 包住，里面写工具名与参数：

<tool>
    <tool_name>
        工具名
    </tool_name>
    <tool_param>
        参数原文
    </tool_param>
</tool>

例：<tool><tool_name>executeCmd</tool_name><tool_param>cat /tmp/a.txt</tool_param></tool>

无参数的工具不用写 `<tool_param>`；有多个参数的工具，每块 `<tool_param name="参数名">` 各写一个、都带上 `name`。

一次只调用一个工具。不用再调工具、可以直接作答时，把**答案本身**放进 `<answer>`：

<answer>答案本身</answer>

**沉淀 SOP 是唯一的例外**：它不产出命令，所以那一回合必须把答案**当成它的参数**一起给出（参数名就叫 `answer`；漏了答案，这次调用整个作废，SOP 也存不下来）：

<tool><tool_name>SOP2Prompt</tool_name><tool_param name="sop">沉淀的方法</tool_param><tool_param name="answer">答案本身</tool_param></tool>

# 沉淀的 SOP
{sop}

# 工作流
1. 任务信息里给的往往只是一个**文件名**、不是完整路径。先用**一条**命令把它找出来并读完 ——
   每条命令要花一个回合，不要拆成两回合。例如把「找文件在哪」和「读文件内容」合成一条：
   f=$(find / -maxdepth 4 -name '*任务书*' -print -quit 2>/dev/null); echo "FILE=$f"; cat "$f"
2. 读完任务书后，**先把它要求的「要交什么、什么格式」抄进回复里**，再动手去做；规格没看清楚就不要猜。
3. 提交答案前，逐条对照任务书核对一遍，不允许跳过任务书里的任何一条要求。
"""

#: 工具调用的**开标签前缀**。用前缀（而不是整段 `<tool>`）是有意的：`looks_like_tool`
#: 要判**宽**（`<tool>`、`<tool >`、`<tool_name>` 都算"它想调工具"），否则畸形回复会被当成答案。
_TOOL_OPEN = "<tool"
_TOOL_CLOSE = "</tool>"
_PARAM_OPEN = "<tool_param"
_PARAM_CLOSE = "</tool_param>"

#: 答案走工具参数通道时用的**参数名**（第 35 步）。⚠️ 必须与注册表里 `SOP2Prompt` 声明的
#: 那个参数名一致（`agent.py` 的 `_tools`）—— 这是本模块唯一一处知道具体名字的地方：
#: 它认的是"参数里有个叫 `answer` 的"，**不是**"哪个工具"（本模块不认识工具，见模块 docstring）。
_ANSWER_PARAM = "answer"


def tool_of(reply: str) -> tuple[str, list[tuple[str | None, str]]] | None:
    """解析工具调用 ⇒ `(工具名, [(参数名|None, 原文), …])`；不是工具调用、或形状不完整 ⇒ `None`。

    判据**严格**（与 `looks_like_tool` 故意相反）。参数**可零个可多个**（第 30 步）：

    1. 取**第一对** `<tool>` … `</tool>`（按开标签 `>` 的位置定位，所以 `<tool >` 也认）；
       不成对 ⇒ `None`。
    2. `<tool_name>` 取第一块；`<tool_param>` **全部收集**（出现顺序即表序）：每块可带
       `name="参数名"`（单双引号都认），带名的按名收、无名的留 `None` —— 位置填充是
       `Agent.tool_call` 的事（那边才有工具的参数声明；这个模块只管语法、不认识工具）。
       ⚠️ 参数的内部换行原样保留，只去首尾空白。
    3. **两个标签都没有 ⇒ 整块正文就是一条 `executeCmd` 命令**（旧形状 `<tool>ls -la</tool>`，
       用户拍板的兼容层），落成 `(None, 正文)` 的位置参数 —— 与无名参数同一个机制。
       这里不加"含 `<` 就当畸形"的守卫：那会误杀 `cat < input.txt`。
    4. **只有参数没有名字 ⇒ `None`**（不猜工具名）。**只有名字没有参数**是合法形状 ——
       那可能是无参数工具的调用（第 30 步起 0 参是常态而非畸形），放不放行由
       `tool_call` 按声明的参数表判。
    5. 块是空的 ⇒ `None`。

    只取**第一条** `<tool>` 块：一回合只跑得了一条（接口文档 L210）。
    """
    body = _block(reply, _TOOL_OPEN, _TOOL_CLOSE)
    if body is None:
        return None
    name = _block(body, "<tool_name", "</tool_name>")
    params = _params(body)
    if name is None and not params:
        text = body.strip()
        return ("executeCmd", [(None, text)]) if text else None
    if name is None:
        return None
    return name.strip(), params


def _params(body: str) -> list[tuple[str | None, str]]:
    """收集**全部** `<tool_param>` 块：`[(参数名|None, 原文), …]`，出现顺序即表序。

    名字取开标签里的 `name` 属性（`<tool_param name="cmd">`，单双引号都认、空名当无名）。
    ⚠️ 值**只去首尾空白**（内部换行原样保留 —— 多行命令、带缩进的 python 都合法）。
    半截的块（有开无闭）**不要** —— 与 `_block` 同一条"成对才作数"：半截参数绝不能
    当内容用（会被当命令发进沙盒）。
    """
    out: list[tuple[str | None, str]] = []
    start = 0
    while True:
        head = body.find(_PARAM_OPEN, start)
        if head < 0:
            return out
        head_end = body.find(">", head)
        if head_end < 0:
            return out
        end = body.find(_PARAM_CLOSE, head_end)
        if end < 0:
            return out
        out.append((_attr(body[head : head_end + 1]), body[head_end + 1 : end].strip()))
        start = end + len(_PARAM_CLOSE)


def _attr(open_tag: str) -> str | None:
    """开标签文本（如 `<tool_param name="cmd">`）里 `name` 属性的值；没有/为空 ⇒ `None`。

    只认这一种属性、值须用成对的引号包着（单双都行）；`name = "cmd"`（多敲空格）也认 ——
    LLM 的标点风格不该让调用作废。不做完整的属性语法分析：标签里没有第二种属性要认。
    """
    at = open_tag.find("name")
    if at < 0:
        return None
    eq = open_tag.find("=", at)
    if eq < 0:
        return None
    for quote in ('"', "'"):
        first = open_tag.find(quote, eq)
        if first >= 0:
            last = open_tag.find(quote, first + 1)
            if last > first:
                return open_tag[first + 1 : last].strip() or None
    return None


def looks_like_tool(reply: str) -> bool:
    """这条回复**像是**工具调用吗？只认开标签前缀出现（故意判宽）。

    判得比 `tool_of` 宽是有意的：`<tool ls`、`<tool >ls</tool>`、只有 `<tool_name>` 的回复，
    都该被认成"它想调工具、但格式没凑对" ⇒ 落到**重问**，而不是被当成答案交上去。
    代价是答案里若含字面量 `<tool` 会被误判（拒绝提交、改问），概率极低，且降级方向安全。
    """
    return _TOOL_OPEN in reply


def answer_of(reply: str) -> str:
    """**该提交什么** —— `planner` 的 `_answer_task` 与 `task_channel` 判据 ④/⑤共用同一个谓词。

    **「该提交什么」与「该骂什么」是同一件事**：判题器说"上次答案不对"时，回灌给 LLM 的正是
    上一回合真正交上去的那一份；两处各判一次就会出现"拿着 `<answer>晴</answer>` 去骂
    '你上次答的 `晴` 不对'"。

    四级判据：

    1. **完整的工具调用、且它的参数里有一个 `name="answer"` 且非空 ⇒ 返回它**（第 35 步）。
       ⚠️ **这一级必须压在 `<answer>` 块扫描之前**：`SOP2Prompt` 沉淀的 SOP 正文里几乎必然
       出现字面量 `<answer>…</answer>`（它讲的就是"答案要用 `<answer>` 包"），先扫块就会把
       **SOP 里那一段**当成答案交上去。参数在工具块内部、只认参数名 ⇒ 这类污染够不着这里。
    2. 否则把**第一个工具块整段挖掉**再扫：出现 `<answer` 标记 ⇒ 只认成对块的内容
       （配对不上或为空 ⇒ `""`，不回落成原文）。挖掉是为了让第 1 条没接住时也扫不到工具块的正文。
    3. **否则像是工具调用 ⇒ `""`**（⚠️ 用**原文**判，不能用挖过的 —— 挖完就不像了，会误放行
       `<tool>ls</tool>` 后面跟着的那句话）。
    4. **否则原文即答案**（判题器的 LLM 是黑盒，这是唯一的退路）。

    没有工具块的回复 ⇒ 第 2~4 条与第 34 步之前的实现**逐字一致**。
    """
    span = _span(reply, _TOOL_OPEN, _TOOL_CLOSE)
    rest = reply
    if span is not None:
        for name, value in _params(reply[span[0] : span[1]]):
            if name == _ANSWER_PARAM:
                answer = value.strip()
                if answer:
                    return answer
        head = reply.find(_TOOL_OPEN)
        rest = reply[:head] + reply[span[1] + len(_TOOL_CLOSE) :]
    if "<answer" in rest:
        block = _block(rest, "<answer", "</answer>")
        return block.strip() if block is not None else ""
    if looks_like_tool(reply):
        return ""
    return rest.strip()


def _span(text: str, open_tag: str, close_tag: str) -> tuple[int, int] | None:
    """**第一对**标签之间的**正文**在 `text` 里的下标区间 `(起, 止)`；不成对 ⇒ `None`。

    开标签按 **`>` 的位置**定位而不是逐字匹配整段标签，所以 `<tool >` 也认（LLM 多敲个空格
    不该让整条回复作废）。**"成对"是所有标签判据的共同要求**，所以四个调用点共用这一份：
    有开无闭的那半截绝不能当内容用（要么被当答案交上去，要么被当命令发进沙盒）。
    `answer_of` 额外要这个区间 —— 它得把工具块**整段挖掉**再去找答案（见那里）。
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
    return head_end + 1, end


def _block(text: str, open_tag: str, close_tag: str) -> str | None:
    """取**第一对**标签之间的正文；不成对 ⇒ `None`。判据全在 `_span` 里。"""
    span = _span(text, open_tag, close_tag)
    return text[span[0] : span[1]] if span is not None else None
