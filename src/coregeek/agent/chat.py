"""Agent 的"说什么 / 怎么读回复"：prompt 的五段模板（system 消息的内容）+ 三个谓词 + 一处清洗。

**这个包不认识游戏**：收字符串、吐字符串，只依赖标准库。切分判据是"什么时候跟 LLM 说话"
属策略（在 `planner.task_channel`），"说什么、怎么解析回复"与战场规则无关。
机械保证就是模板与 `Context` 收发的全是字符串、不收 `Turn`。

**本模块是纯的**：不 import 包里的任何东西。会话的存储与渲染第 25 步起在 `context.py`
（`Context`）、编排在 `Agent.chat`；第 28 步起整份 prompt 是**标准 messages JSON**
（`[{role, content}]`，自造的文本版式用户实测效果非常差、已弃），这里的模板只当
**system 消息的 content**。模板与谓词不拆成两个模块：它们是同一份约定的两面，
拆开就是"改一处要翻两个文件"；那个清洗函数（`strip_answers`）同住也是这个理由 ——
它认的就是 `_ANSWER_RE` 那一个模式，分家等于把标签语法抄成两份。

⚠️ **协议形状是我们自己定的**（任务书只说了沙盒能跑什么，没规定 LLM 该怎么要命令）
⇒ `# 输出格式` 那段必须把两个形状逐字给全，并保留旧形状兼容与"原文即答案"的兜底。
"""

import re

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
你是一个自主任务执行Agent，能根据用户的任务基于现有的工具了解任务并理解任务，理解任务后严格按照任务要求完成任务；当认为解题流程值得沉淀时，用 SOP2Prompt 把方法沉淀下来 —— 它只沉淀、不产出命令，所以你照样要在同一条回复里把答案交给 `<answer>`。

当手上的信息不足时，就调用工具去取；认定完成任务后，就直接作答。
一回合只输出一样东西：一次工具调用，或者一个答案。唯一的例外是 SOP2Prompt —— 它不产出命令，所以调用它的那一回合照样是你的作答回合：工具块后面再跟一个 `<answer>`。不要解释、不要前言、不要 Markdown 代码块标记。

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

**沉淀 SOP 与作答写在同一条回复里**（它不产出命令，所以答案得另外给）：SOP2Prompt 只有一个参数 `sop`，答案写在**工具块之外**的 `<answer>` 里；漏了的话沉淀**照样生效**，但这一回合算没作答、下回合还会再问你一遍：

<tool><tool_name>SOP2Prompt</tool_name><tool_param name="sop">沉淀的方法</tool_param></tool>
<answer>答案本身</answer>

`sop` 的正文里**不要出现 `<answer>` 与 `</answer>` 这对标签**（讲答案格式时换个说法，比如"把答案用 answer 标签包起来"）。

# 沉淀的 SOP
{sop}

# 工作流
1. 任务信息里给的往往只是一个**文件名**、不是完整路径。先用**一条**命令把它找出来并读完 ——
   每条命令要花一个回合，不要拆成两回合。例如把「找文件在哪」和「读文件内容」合成一条：
   f=$(find / -maxdepth 4 -name '*任务书*' -print -quit 2>/dev/null); echo "FILE=$f"; cat "$f"
2. 读完任务书后，**先把它要求的「要交什么、什么格式」抄进回复里**，再动手去做；规格没看清楚就不要猜。
3. 提交答案前，逐条对照任务书核对一遍，不允许跳过任务书里的任何一条要求。
"""

# ── 标签的语法 ────────────────────────────────────────────────────────
#: ⚠️ **所有 `<>` 的解析都在下面这几个正则里**（第 36 步从 `str.find` + 下标算术换成正则）：
#: 判据写在模式里、不再散在切片里；`DOTALL` 一律要（正文里的换行原样保留）。
#:
#: **"成对才作数"是它们的共同要求**（有开无闭的那半截绝不能当内容用 —— 要么被当答案交上去、
#: 要么被当命令发进沙盒），所以每个模式都自带 `</…>`，天然只认成对块。非贪婪 `.*?` 保证
#: 停在**第一对**上：一回合只跑得了一条命令（接口文档 L210）。
#:
#: 开标签一律写成 `<tool\b[^>]*>` —— **按 `>` 的位置定位，不是逐字匹配整段标签**：LLM 多敲
#: 一个空格（`<tool >`）、给标签加个属性（`<tool_param name="cmd">`）都不该让整条回复作废。
#: `\b` 挡的是 `<tool_name>`／`<tool_param>` 这两个**前缀相同**的标签被当成工具块的开头。
_TOOL_RE = re.compile(r"<tool\b[^>]*>(.*?)</tool>", re.DOTALL)
_NAME_RE = re.compile(r"<tool_name\b[^>]*>(.*?)</tool_name>", re.DOTALL)
_PARAM_RE = re.compile(r"<tool_param\b([^>]*)>(.*?)</tool_param>", re.DOTALL)
#: `<answer>` 的两下：成对的取内容，**只判标记在不在**用 `_ANSWER_MARK_RE`（`<answer`
#: 是个**前缀**判据，`<answer>`、`<answer >`、`<answer 乱写>` 都算"它想作答"）。
_ANSWER_RE = re.compile(r"<answer[^>]*>(.*?)</answer>", re.DOTALL)
_ANSWER_MARK_RE = re.compile(r"<answer")
#: `looks_like_tool` 那个**宽**判据（见那里）。与上面几个相反，它**故意只认前缀**。
_TOOL_MARK_RE = re.compile(r"<tool")
#: 开标签里的 `name="参数名"`：单双引号都认、`name = "x"`（多敲空格）也认 —— LLM 的标点
#: 风格不该让调用作废。`group(1)` 是引号（反向引用 `\1` 要求首尾同一种）、`group(2)` 是值。
_PARAM_ATTR_RE = re.compile(r"""name\s*=\s*(["'])(.*?)\1""", re.DOTALL)


def strip_answers(text: str) -> tuple[str, int]:
    """把 `text` 里**所有成对**的 `<answer>…</answer>` 整段挖掉 ⇒ `(挖过的正文, 挖掉几处)`。

    唯一的调用者是 `Agent.SOP2Prompt`：**SOP 正文里不许有这对串**（第 36 步的用户口径）——
    沉淀的正文讲的往往正是"答案要用标签包"（第 35 步的污染缺陷就是这么来的），
    与其要求 LLM 每次都绕开它，不如在**入库这唯一一道口子**上保证存下来的那份没有。

    ⚠️ **挖掉、不是作废整次调用**：沉淀是那个工具的全部价值，不该因为它多写了一句示例就整段丢掉
    （用户口径）。挖了几处进日志（`tools/sop.py`）—— 否则"收到 N 字、存了 M 字"就成了没头没尾的账。
    ⚠️ **只认成对**：半截的标记（有开无闭）原样留着 —— `answer_of` 认的也是成对块，
    而半截标记在正文里只是普通文字。**空块**（`<answer></answer>`）照挖：它同样是这对串。
    """
    return _ANSWER_RE.subn("", text)


def tool_of(reply: str) -> tuple[str, list[tuple[str | None, str]]] | None:
    """解析工具调用 ⇒ `(工具名, [(参数名|None, 原文), …])`；不是工具调用、或形状不完整 ⇒ `None`。

    判据**严格**（与 `looks_like_tool` 故意相反）。参数**可零个可多个**（第 30 步）：

    1. 取**第一对** `<tool>` … `</tool>`（`_TOOL_RE`，开标签按 `>` 的位置定位 ⇒ `<tool >` 也认）；
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
    match = _TOOL_RE.search(reply)
    if match is None:
        return None
    body = match.group(1)
    name = _NAME_RE.search(body)
    params = _params(body)
    if name is None and not params:
        text = body.strip()
        return ("executeCmd", [(None, text)]) if text else None
    if name is None:
        return None
    return name.group(1).strip(), params


def _params(body: str) -> list[tuple[str | None, str]]:
    """收集**全部** `<tool_param>` 块：`[(参数名|None, 原文), …]`，`finditer` 的顺序即表序。

    名字取开标签里的 `name` 属性（`<tool_param name="cmd">`），没有/为空 ⇒ `None`
    —— 位置填充是 `Agent.tool_call` 的事（那边才有工具的参数声明；这个模块只管语法、
    不认识工具）。⚠️ 值**只去首尾空白**（内部换行原样保留 —— 多行命令、带缩进的 python
    都合法）。半截的块（有开无闭）**不要**：`_PARAM_RE` 自带 `</tool_param>` ⇒ 天然只认
    成对块，而半截参数**绝不能**当内容用（会被当命令发进沙盒）。
    """
    return [
        (_attr(match.group(1)), match.group(2).strip())
        for match in _PARAM_RE.finditer(body)
    ]


def _attr(attrs: str) -> str | None:
    """开标签的属性串（如 `<tool_param name="cmd">` 里的 ` name="cmd"`）里 `name` 的值。

    只认这一种属性、值须用成对的引号包着（单双都行，`\1` 反向引用保证首尾同一种）；
    `name = "cmd"`（多敲空格）也认。不做完整的属性语法分析：标签里没有第二种属性要认。
    """
    match = _PARAM_ATTR_RE.search(attrs)
    return (match.group(2).strip() or None) if match else None


def looks_like_tool(reply: str) -> bool:
    """这条回复**像是**工具调用吗？只认开标签前缀出现（故意判宽）。

    判得比 `tool_of` 宽是有意的：`<tool ls`、`<tool >ls</tool>`、只有 `<tool_name>` 的回复，
    都该被认成"它想调工具、但格式没凑对" ⇒ 落到**重问**，而不是被当成答案交上去。
    代价是答案里若含字面量 `<tool` 会被误判（拒绝提交、改问），概率极低，且降级方向安全。
    ⚠️ 它是本模块**唯一**只认前缀、不认成对的地方（`_TOOL_MARK_RE`）—— 别顺手改成
    `_TOOL_RE`：那会把"想调但没凑对"的回复从重问变成"原文即答案"。
    """
    return _TOOL_MARK_RE.search(reply) is not None


def answer_of(reply: str) -> str:
    """**该提交什么** —— `planner` 的 `_answer_task` 与 `task_channel` 判据 ④/⑤共用同一个谓词。

    **「该提交什么」与「该骂什么」是同一件事**：判题器说"上次答案不对"时，回灌给 LLM 的正是
    上一回合真正交上去的那一份；两处各判一次就会出现"拿着 `<answer>晴</answer>` 去骂
    '你上次答的 `晴` 不对'"。

    三级判据：

    1. 把**第一个工具块整段挖掉**得到 `rest`，再扫：出现 `<answer` 标记 ⇒ 只认**成对块**的内容
       （配对不上或为空 ⇒ `""`，**不回落成原文**）。
       ⚠️ **"先挖掉工具块"这一条是第 35 步那个污染缺陷的正面修法**：`SOP2Prompt` 沉淀的正文
       讲的往往正是"答案要用 `<answer>` 包" ⇒ 里面会出现**字面量** `<answer>…</answer>`，
       而整块挖走后那些实例压根扫不到。落在块**内**的 `<answer>` 一律**不算答案**
       （分不清那是真答案还是 SOP 里的示例，就不许当成答案交上去）。
    2. **否则像是工具调用 ⇒ `""`**（⚠️ 用**原文**判，不能用挖过的 —— 挖完就不像了，会误放行
       `<tool>ls</tool>` 后面跟着的那句话）。
    3. **否则原文即答案**（判题器的 LLM 是黑盒，这是唯一的退路）。

    没有工具块的回复 ⇒ 与第 34 步之前的实现**逐字一致**。
    """
    match = _TOOL_RE.search(reply)
    rest = reply[: match.start()] + reply[match.end() :] if match else reply
    if _ANSWER_MARK_RE.search(rest):
        block = _ANSWER_RE.search(rest)
        return block.group(1).strip() if block else ""
    if looks_like_tool(reply):
        return ""
    return rest.strip()
