"""Agent 的"怎么读回复"：四个谓词 + 一处清洗。模板不在这里（在 `prompt.py`）。

这个包不认识游戏：收发全是字符串、不收 `Turn`；"什么时候跟 LLM 说话"是策略
（`planner.task_channel`），"怎么解析回复"与战场规则无关。本模块是纯的：零包内
import。`strip_answers` 同住，因为它认的就是 `_ANSWER_RE` 那一个模式，分家等于把
标签语法抄成两份。协议形状是我们自己定的（任务书只说了沙盒能跑什么，没规定 LLM
该怎么要命令）⇒ 严格只认嵌套形状：`prompt.py` 教什么、这里就只认什么；其他形状
靠 `looks_like_tool` 判宽接住、落重问，丢一回合、不碰红线。
"""

import re

# ── 标签的语法 ────────────────────────────────────────────────────────
#: 所有 `<>` 的解析都在下面这几个正则里；`DOTALL` 一律要（正文里的换行原样保留）。
#:
#: "成对才作数"是它们的共同要求（有开无闭的那半截绝不能当内容用 —— 要么被当答案
#: 交上去、要么被当命令发进沙盒），所以每个模式都自带 `</…>`。非贪婪 `.*?` 停在
#: 第一对上：一回合只跑得了一条命令（接口文档 L210）。
#:
#: 开标签一律写成 `<tool\b[^>]*>` —— 按 `>` 的位置定位，不是逐字匹配整段标签：
#: LLM 多敲一个空格（`<tool >`）、给标签加个属性都不该让整条回复作废。
#: `\b` 挡的是 `<tool_name>`／`<tool_param>` 这两个前缀相同的标签被当成工具块的开头。
_TOOL_RE = re.compile(r"<tool\b[^>]*>(.*?)</tool>", re.DOTALL)
_NAME_RE = re.compile(r"<tool_name\b[^>]*>(.*?)</tool_name>", re.DOTALL)
#: 嵌套形状：参数写在 `<tool_param>` 里面再包一层的具名标签 ——
#: `<tool_param><cmd>ls</cmd></tool_param>`。开标签按 `>` 定位（`<cmd >` 也认），
#: 闭标签用反向引用 `\1` 认同名的那一对；`\w+` 是 Unicode 感知的 ⇒
#: 中文参数名（`<参数>`）一样认。
_PARAM_RE = re.compile(r"<tool_param\b[^>]*>(.*?)</tool_param>", re.DOTALL)
_INNER_RE = re.compile(r"<(\w+)\b[^>]*>(.*?)</\1>", re.DOTALL)
#: `<answer>` 的两下：成对的取内容，只判标记在不在用 `_ANSWER_MARK_RE`（`<answer`
#: 是个前缀判据，`<answer>`、`<answer >`、`<answer 乱写>` 都算"它想作答"）。
_ANSWER_RE = re.compile(r"<answer[^>]*>(.*?)</answer>", re.DOTALL)
_ANSWER_MARK_RE = re.compile(r"<answer")
#: 执行摘要：`<summary>` 块。成对才作数（与 `_ANSWER_RE` 同一条规矩），
#: `summary_of` 只取第一对。
_SUMMARY_RE = re.compile(r"<summary\b[^>]*>(.*?)</summary>", re.DOTALL)
#: `looks_like_tool` 的宽判据（见那里）。与上面几个相反，它故意只认前缀。
_TOOL_MARK_RE = re.compile(r"<tool")
#: 裸 `<prices>` 回复（新闻查价的产物）—— 判别与 `<summary>` 同族。
_PRICES_RE = re.compile(r"<prices\b[^>]*>(.*?)</prices>", re.DOTALL)
_PRICE_LINE_RE = re.compile(r"(stone|iron|copper)\s*[：:\s]\s*(up|down|flat)", re.IGNORECASE)
#: 反转义表：`prompt.TOOL_PROMPT` 教了 LLM 对 XML 特殊字符转义 ⇒ 参数值里的
#: 五个预定义实体要还原。`&amp;` 必须最后换：`&amp;lt;` 只该还原一层（`&lt;`），
#: 先换 `&amp;` 就把它变成了 `<`（两层）。LLM 没转义时这条是空操作 ——
#: 裸 `<` / `>` / `&` 在 shell 命令里太常见，一个都不许被改写。
_ENTITIES = (
    ("&lt;", "<"),
    ("&gt;", ">"),
    ("&apos;", "'"),
    ("&quot;", '"'),
    ("&amp;", "&"),
)


def strip_answers(text: str) -> tuple[str, int]:
    """把 `text` 里所有成对的 `<answer>…</answer>` 整段挖掉 ⇒ `(挖过的正文, 挖掉几处)`。

    唯一的调用者是 `Agent.SOP2Prompt`：SOP 正文里不许有这对串 —— 沉淀的正文讲的
    往往正是"答案要用标签包"，与其要求 LLM 每次绕开，不如在入库这唯一一道口子上
    保证存下来的那份没有。挖掉、不是作废整次调用：沉淀是那个工具的全部价值。
    挖了几处进日志（`tools/sop.py`）—— 否则"收到 N 字、存了 M 字"就成了没头没尾的账。
    只认成对：半截标记（有开无闭）原样留着 —— `answer_of` 认的也是成对块，半截
    在正文里只是普通文字。空块（`<answer></answer>`）照挖：它同样是这对串。
    """
    return _ANSWER_RE.subn("", text)


def tool_of(reply: str) -> tuple[str, list[tuple[str, str]]] | None:
    """解析工具调用 ⇒ `(工具名, [(参数名, 原文), …])`；不是工具调用、或形状不完整 ⇒ `None`。

    判据严格（与 `looks_like_tool` 故意相反），只认嵌套形状：

    1. 取第一对 `<tool>` … `</tool>`（开标签按 `>` 定位 ⇒ `<tool >` 也认）；不成对 ⇒ `None`。
    2. `<tool_name>` 取第一块；`<tool_param>` 全部收集，每块里必须是一个或多个具名元素
       （`<参数名>值</参数名>`）—— 任何一块里一个都没有 ⇒ 整次调用 `None`（属性式
       `name="…"`、裸值都不认）。参数值只去首尾空白、内部换行原样保留，再过 `_unescape`。
    3. 只有参数没有名字 ⇒ `None`（不猜工具名）。只有名字没有参数是合法形状 —— 可能是
       无参数工具的调用，放不放行由 `tool_call` 按声明的参数表判。
    4. 块是空的 ⇒ `None`。

    只取第一条 `<tool>` 块：一回合只跑得了一条（接口文档 L210）。
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
    """收集全部 `<tool_param>` 块里的具名元素 ⇒ `[(参数名, 原文), …]`；
    任何一块里一个具名元素都没有 ⇒ `None`（整次调用不成立）。

    元素名取标签名（`\w+`，中文也认），值只去首尾空白（内部换行原样保留 —— 多行命令、
    带缩进的 python 都合法）。参数可拆进多个块（块数不是判据，块里的格式才是）。
    半截的内层标签（有开无闭）配不上 `\1` ⇒ 那块算"没有具名元素" ⇒ 整次调用 `None`
    —— 半截的东西绝不能当内容用（会被当命令发进沙盒）。
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
    """每条回复搭车的执行摘要 ⇒ 第一对 `<summary>` 块的内容；取不到 ⇒ `""`。

    best-effort：摘要取不到（没写 / 半截 / 空块）⇒ `""`，调用方保留旧摘要继续 ——
    绝不因为摘要缺失而重问（重问要花一回合，而摘要是搭车品）。与 `answer_of`
    对空块的态度一致：不回落原文，拿半截标记去凑只会把标签串当内容用。
    """
    block = _SUMMARY_RE.search(reply)
    return block.group(1).strip() if block else ""


def is_summary_reply(reply: str) -> str | None:
    """裸摘要回复（压缩轮的产物）⇒ 返回摘要文本；否则 `None`。

    判据：`<summary>` 块取得出内容，且挖掉摘要块之后一个字都不剩 —— 任务回复
    （带工具调用 / 答案 / 正文）不算。任务 prompt 不教摘要 ⇒ "裸摘要"几乎必属
    压缩回复；粘住的压缩回复同文再判一次 = 幂等。
    """
    summary = summary_of(reply)
    if summary and not _SUMMARY_RE.sub("", reply).strip():
        return summary
    return None


def is_prices_reply(reply: str) -> dict[str, str] | None:
    """裸 `<prices>` 回复（新闻查价的产物）⇒ `{矿种: 方向}`；否则 `None`。

    判据与 `is_summary_reply` 同构：块取得出内容、挖掉之后一个字不剩。行格式
    `矿种: 方向`（大小写都认）；解析不出的行直接丢 —— 宁可少认一条，不猜。
    """
    block = _PRICES_RE.search(reply)
    if block is None or _PRICES_RE.sub("", reply).strip():
        return None
    return {
        m.group(1).lower(): m.group(2).lower()
        for m in _PRICE_LINE_RE.finditer(block.group(1))
    }


def looks_like_tool(reply: str) -> bool:
    """这条回复像工具调用吗？只认开标签前缀出现（故意判宽）。

    判得比 `tool_of` 宽是有意的：`<tool ls`、`<tool >ls</tool>`、只有 `<tool_name>` 的
    回复、解析不出的形状（属性式 / 裸参数 / 裸工具块），都该被认成"它想调工具、但
    格式没凑对" ⇒ 落到重问，而不是被当成答案交上去。代价是答案里含字面量 `<tool`
    会被误判（拒绝提交、改问），概率极低，且降级方向安全。
    它是本模块唯一只认前缀、不认成对的地方 —— 别顺手改成 `_TOOL_RE`：那会把
    "想调但没凑对"的回复从重问变成"原文即答案"。
    """
    return _TOOL_MARK_RE.search(reply) is not None


def answer_of(reply: str) -> str:
    """该提交什么 —— `planner` 的 `_answer_task` 与 `task_channel` 判据共用同一个谓词。

    「该提交什么」与「该骂什么」是同一件事：判题器说"上次答案不对"时，回灌给 LLM 的
    正是上一回合真正交上去的那一份；两处各判一次就会出现"拿着 `<answer>晴</answer>`
    去骂'你上次答的 `晴` 不对'"。

    三级判据：

    1. 把全部结构块（每一对 `<tool>` 与 `<summary>`）整段挖掉得到 `rest`，再扫：
       出现 `<answer` 标记 ⇒ 只认成对块的内容（配对不上或为空 ⇒ `""`，不回落原文）。
       先挖结构块防的是污染：SOP 正文与执行摘要讲的往往正是"答案要用 `<answer>` 包"，
       里面会出现字面量 `<answer>…</answer>`，整块挖走后那些实例压根扫不到。
       落在结构块内的 `<answer>` 一律不算答案（分不清是真是假，就不许当成答案交上去）。
    2. 否则像是工具调用 ⇒ `""`（用原文判，不能用挖过的 —— 挖完就不像了，会误放行
       `<tool>ls</tool>` 后面跟着的那句话）。
    3. 否则原文即答案（判题器的 LLM 是黑盒，这是唯一的退路）。
    """
    rest = _SUMMARY_RE.sub("", _TOOL_RE.sub("", reply))
    if _ANSWER_MARK_RE.search(rest):
        block = _ANSWER_RE.search(rest)
        return block.group(1).strip() if block else ""
    if looks_like_tool(reply):
        return ""
    return rest.strip()
