"""Agent 的"怎么读回复"：四个谓词 + 一处清洗。**模板不在这里**（第 37 步起在 `prompt.py`）。

**这个包不认识游戏**：收字符串、吐字符串，只依赖标准库。切分判据是"什么时候跟 LLM 说话"
属策略（在 `planner.task_channel`），"说什么、怎么解析回复"与战场规则无关。
机械保证就是 `Context` 与模板收发的全是字符串、不收 `Turn`。

**本模块是纯的**：不 import 包里的任何东西。会话的存储与渲染第 25 步起在 `context.py`
（`Context`）、编排在 `Agent.chat`；第 28 步起整份 prompt 是**标准 messages JSON**
（`[{role, content}]`，自造的文本版式用户实测效果非常差、已弃）。
第 37 步起模板搬去 `prompt.py`（用户建的文件）—— 旧版"模板与谓词同住、拆开就是改一处
翻两个文件"的取舍就此作废，分家的代价（协议的两半各住一个文件）记在 `code-task.md`。
`strip_answers` 同住的理由不变：它认的就是 `_ANSWER_RE` 那一个模式，分家等于把标签语法
抄成两份。

⚠️ **协议形状是我们自己定的**（任务书只说了沙盒能跑什么，没规定 LLM 该怎么要命令）
⇒ 第 37 步起**只认嵌套形状**（用户拍板"严格只认新形状"）：`prompt.py` 教什么、这里就
只认什么；旧形状落重问（`looks_like_tool` 判宽接住），丢一回合、不碰红线。
"""

import re

# ── 标签的语法 ────────────────────────────────────────────────────────
#: ⚠️ **所有 `<>` 的解析都在下面这几个正则里**（第 36 步从 `str.find` + 下标算术换成正则）：
#: 判据写在模式里、不再散在切片里；`DOTALL` 一律要（正文里的换行原样保留）。
#:
#: **"成对才作数"是它们的共同要求**（有开无闭的那半截绝不能当内容用 —— 要么被当答案交上去、
#: 要么被当命令发进沙盒），所以每个模式都自带 `</…>`，天然只认成对块。非贪婪 `.*?` 保证
#: 停在**第一对**上：一回合只跑得了一条命令（接口文档 L210）。
#:
#: 开标签一律写成 `<tool\b[^>]*>` —— **按 `>` 的位置定位，不是逐字匹配整段标签**：LLM 多敲
#: 一个空格（`<tool >`）、给标签加个属性都不该让整条回复作废。
#: `\b` 挡的是 `<tool_name>`／`<tool_param>` 这两个**前缀相同**的标签被当成工具块的开头。
_TOOL_RE = re.compile(r"<tool\b[^>]*>(.*?)</tool>", re.DOTALL)
_NAME_RE = re.compile(r"<tool_name\b[^>]*>(.*?)</tool_name>", re.DOTALL)
#: **第 37 步的嵌套形状**：参数不再写在 `<tool_param>` 的属性里、也不直接当它的正文，
#: 而是里面再包一层具名标签 —— `<tool_param><cmd>ls</cmd></tool_param>`。
#: 开标签按 `>` 定位（`<cmd >` 也认），闭标签用反向引用 `\1` 认**同名**的那一对；
#: `\w+` 是 Unicode 感知的 ⇒ 中文参数名（`<参数>`）一样认。
_PARAM_RE = re.compile(r"<tool_param\b[^>]*>(.*?)</tool_param>", re.DOTALL)
_INNER_RE = re.compile(r"<(\w+)\b[^>]*>(.*?)</\1>", re.DOTALL)
#: `<answer>` 的两下：成对的取内容，**只判标记在不在**用 `_ANSWER_MARK_RE`（`<answer`
#: 是个**前缀**判据，`<answer>`、`<answer >`、`<answer 乱写>` 都算"它想作答"）。
_ANSWER_RE = re.compile(r"<answer[^>]*>(.*?)</answer>", re.DOTALL)
_ANSWER_MARK_RE = re.compile(r"<answer")
#: **执行摘要**（第 39 步压缩机制的原料）：LLM 每条回复搭车的 `<summary>` 块。
#: 成对才作数（与 `_ANSWER_RE` 同一条规矩），`summary_of` 只取**第一对**。
_SUMMARY_RE = re.compile(r"<summary\b[^>]*>(.*?)</summary>", re.DOTALL)
#: `looks_like_tool` 那个**宽**判据（见那里）。与上面几个相反，它**故意只认前缀**。
_TOOL_MARK_RE = re.compile(r"<tool")
#: **反转义表**（第 37 步）：`prompt.TOOL_PROMPT` 教了 LLM 对 XML 特殊字符转义 ⇒
#: 参数值里的五个预定义实体要还原。⚠️ `&amp;` **必须最后换**：`&amp;lt;` 只该还原一层
#: （`&lt;`），先换 `&amp;` 就把它变成了 `<`（两层）。LLM 没转义时这条是空操作 ——
#: 裸 `<` / `>` / `&` 在 shell 命令里太常见了，一个都不许被改写。
_ENTITIES = (
    ("&lt;", "<"),
    ("&gt;", ">"),
    ("&apos;", "'"),
    ("&quot;", '"'),
    ("&amp;", "&"),
)


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


def tool_of(reply: str) -> tuple[str, list[tuple[str, str]]] | None:
    """解析工具调用 ⇒ `(工具名, [(参数名, 原文), …])`；不是工具调用、或形状不完整 ⇒ `None`。

    判据**严格**（与 `looks_like_tool` 故意相反），第 37 步起**只认嵌套形状**：

    1. 取**第一对** `<tool>` … `</tool>`（`_TOOL_RE`，开标签按 `>` 的位置定位 ⇒ `<tool >` 也认）；
       不成对 ⇒ `None`。
    2. `<tool_name>` 取第一块；`<tool_param>` **全部收集**，每块里面必须是**一个或多个
       具名元素**（`<参数名>值</参数名>`）—— **任何一块里一个都没有 ⇒ 整次调用 `None`**
       （属性式 `name="…"`、裸值都是旧协议的形状，不再认）。参数值只去首尾空白、
       内部换行原样保留，再过 `_unescape` 还原转义。
    3. **只有参数没有名字 ⇒ `None`**（不猜工具名）。**只有名字没有参数**是合法形状 ——
       那可能是无参数工具的调用（prompt 教的：无参工具不写 `<tool_param>`），放不放行由
       `tool_call` 按声明的参数表判。
    4. 块是空的 ⇒ `None`。

    只取**第一条** `<tool>` 块：一回合只跑得了一条（接口文档 L210）。
    """
    match = _TOOL_RE.search(reply)
    if match is None:
        return None
    body = match.group(1)
    name = _NAME_RE.search(body)
    params = _params(body)
    if name is None or params is None:
        return None
    return name.group(1).strip(), params


def _params(body: str) -> list[tuple[str, str]] | None:
    """收集**全部** `<tool_param>` 块里的具名元素 ⇒ `[(参数名, 原文), …]`；
    **任何一块里一个具名元素都没有 ⇒ `None`**（整次调用不成立）。

    元素名取标签名（`\w+`，中文也认），值只去首尾空白（内部换行原样保留 —— 多行命令、
    带缩进的 python 都合法）。参数可拆进多个块（块数不是判据，**块里的格式**才是）。
    半截的内层标签（有开无闭）配不上 `\1` ⇒ 那块算"没有具名元素" ⇒ 整次调用 `None`
    —— 半截的东西**绝不能**当内容用（会被当命令发进沙盒）。
    """
    params: list[tuple[str, str]] = []
    for match in _PARAM_RE.finditer(body):
        inner = _INNER_RE.findall(match.group(1))
        if not inner:
            return None
        params += [(tag, _unescape(value.strip())) for tag, value in inner]
    return params


def _unescape(text: str) -> str:
    """把五个 XML 预定义实体还原成原字符（`&amp;` 最后换，见 `_ENTITIES` 的注释）。"""
    for entity, char in _ENTITIES:
        text = text.replace(entity, char)
    return text


def summary_of(reply: str) -> str:
    """每条回复搭车的**执行摘要** ⇒ 第一对 `<summary>` 块的内容；取不到 ⇒ `""`。

    ⚠️ **best-effort 是它的立身规则**：摘要取不到（没写 / 半截 / 空块）⇒ `""`，
    调用方（`Agent.hear`）**保留旧摘要继续** —— 绝不因为摘要缺失而重问
    （重问要花一回合，而摘要是纯赚的搭车品）。与 `answer_of` 对空块的态度一致：
    **不回落原文**，拿半截标记去凑只会把标签串当内容用。
    """
    block = _SUMMARY_RE.search(reply)
    return block.group(1).strip() if block else ""


def is_summary_reply(reply: str) -> str | None:
    """**裸摘要回复**（第 41 步：压缩轮的产物）⇒ 返回摘要文本；否则 `None`。

    判据：`<summary>` 块取得出内容，**且挖掉摘要块之后一个字都不剩** —— 任务回复
    （带工具调用 / 答案 / 正文）不算。任务 prompt 不再教摘要 ⇒ "裸摘要"几乎必属
    压缩回复，判别是干净的；粘住的压缩回复同文再判一次 = 幂等。
    """
    summary = summary_of(reply)
    if summary and not _SUMMARY_RE.sub("", reply).strip():
        return summary
    return None


def looks_like_tool(reply: str) -> bool:
    """这条回复**像是**工具调用吗？只认开标签前缀出现（故意判宽）。

    判得比 `tool_of` 宽是有意的：`<tool ls`、`<tool >ls</tool>`、只有 `<tool_name>` 的回复、
    第 37 步起**不再解析的旧形状**（属性式 / 裸参数 / 裸工具块），都该被认成
    "它想调工具、但格式没凑对" ⇒ 落到**重问**，而不是被当成答案交上去。
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

    1. 把**全部结构块**（每一对 `<tool>` 与 `<summary>`，第 39 步起从"第一个工具块"
       放宽成"所有结构块"）整段挖掉得到 `rest`，再扫：出现 `<answer` 标记 ⇒ 只认
       **成对块**的内容（配对不上或为空 ⇒ `""`，**不回落成原文**）。
       ⚠️ **"先挖掉结构块"是污染缺陷的正面修法**：`SOP2Prompt` 沉淀的正文（第 35 步）与
       执行摘要（第 39 步）讲的往往都是"答案要用 `<answer>` 包" ⇒ 里面会出现**字面量**
       `<answer>…</answer>`，而整块挖走后那些实例压根扫不到。落在结构块**内**的
       `<answer>` 一律**不算答案**（分不清那是真答案还是示例，就不许当成答案交上去）。
    2. **否则像是工具调用 ⇒ `""`**（⚠️ 用**原文**判，不能用挖过的 —— 挖完就不像了，会误放行
       `<tool>ls</tool>` 后面跟着的那句话）。
    3. **否则原文即答案**（判题器的 LLM 是黑盒，这是唯一的退路）。

    没有结构块的回复 ⇒ 与第 34 步之前的实现**逐字一致**。
    """
    rest = _SUMMARY_RE.sub("", _TOOL_RE.sub("", reply))
    if _ANSWER_MARK_RE.search(rest):
        block = _ANSWER_RE.search(rest)
        return block.group(1).strip() if block else ""
    if looks_like_tool(reply):
        return ""
    return rest.strip()
