"""Agent 的"说什么 / 怎么读回复"：prompt 的四段模板（system 消息的内容）+ 三个谓词。

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

#: 发给判题器 LLM 的 **system 消息内容**。四段（**定位** → **工具** → **输出格式** →
#: **沉淀的 SOP**），由 `Context.render` 装进 `[{"role": "system", "content": …}]`
#: 的头一条、后面跟这道题的全部往来（第 28 步起整份 prompt 是标准 messages JSON）。
#:
#: ⚠️ **「沉淀的 SOP」那一段的头永远都在**，哪怕还没沉淀过任何东西：那个槽是 LLM 自己写的
#: 目标，看不见槽就不会去用它。两个占位符由 `Agent.chat` 填，`str.format` **只做一次**
#: —— 替换值里若含 `{}`（python 片段里太常见了）不会被二次扫描成占位符。
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
{sop}"""

#: 工具调用的**开标签前缀**。用前缀（而不是整段 `<tool>`）是有意的：`looks_like_tool`
#: 要判**宽**（`<tool>`、`<tool >`、`<tool_name>` 都算"它想调工具"），否则畸形回复会被当成答案。
_TOOL_OPEN = "<tool"
_TOOL_CLOSE = "</tool>"


def tool_of(reply: str) -> tuple[str, str] | None:
    """解析工具调用 ⇒ `(工具名, 参数)`；不是工具调用、或形状不完整 ⇒ `None`。

    判据**严格**（与 `looks_like_tool` 故意相反）：

    1. 取**第一对** `<tool>` … `</tool>`（按开标签 `>` 的位置定位，所以 `<tool >` 也认）；不成对 ⇒ `None`。
    2. 块里有 `<tool_name>` 与 `<tool_param>` ⇒ **两个都要成对**才给值，只有一个 ⇒ `None`
       （**不猜半个调用**）。⚠️ 参数的内部换行原样保留，只去首尾空白。
    3. **块里两个标签都没有 ⇒ 整块正文就是一条 `executeCmd` 命令**（旧形状 `<tool>ls -la</tool>`，
       用户拍板的兼容层）。这里不加"含 `<` 就当畸形"的守卫：那会误杀 `cat < input.txt`。
    4. 块是空的 ⇒ `None`。

    只取**第一条**：`executeCmd` 只有一个字段、判题器一回合只跑一条。
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

    三级判据：**出现 `<answer` 标记 ⇒ 只认成对块的内容**（配对不上或为空 ⇒ `""`，不回落成原文）；
    **否则像是工具调用 ⇒ `""`**；**否则原文即答案**（判题器的 LLM 是黑盒，这是唯一的退路）。
    """
    if "<answer" in reply:
        block = _block(reply, "<answer", "</answer>")
        return block.strip() if block is not None else ""
    if looks_like_tool(reply):
        return ""
    return reply.strip()


def _block(text: str, open_tag: str, close_tag: str) -> str | None:
    """取**第一对**标签之间的正文；不成对 ⇒ `None`。

    开标签按 **`>` 的位置**定位而不是逐字匹配整段标签，所以 `<tool >` 也认（LLM 多敲个空格
    不该让整条回复作废）。**"成对"是所有标签判据的共同要求**，所以四个调用点共用这一份：
    有开无闭的那半截绝不能当内容用（要么被当答案交上去，要么被当命令发进沙盒）。
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
