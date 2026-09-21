"""Agent 的"怎么读回复"：四个谓词 + 一处清洗。模板不在这里（在 `prompt.py`）。

收发全是字符串、不收 `Turn`，零包内 import（`strip_answers` 同住：它认的就是
`_ANSWER_RE` 那一个模式，分家等于把标签语法抄成两份）。协议形状是我们自己定的
（任务书只规定了沙盒能跑什么）⇒ 严格只认嵌套形状：prompt 教什么、这里就只认什么；
其他形状靠 `looks_like_tool` 判宽接住、落重问（丢一回合、不碰红线）。
"""

import re

# ── 标签的语法 ────────────────────────────────────────────────────────
#: 所有 `<>` 的解析都在下面这几个正则里；`DOTALL` 一律要（正文里的换行原样保留）。
#: 成对才作数（半截绝不能当内容用），非贪婪停在第一对上（一回合只跑得了一条命令）。
#: 开标签按 `>` 定位（多敲一个空格、加个属性不该让整条回复作废）；`\b` 挡住
#: `<tool_name>`／`<tool_param>` 被当成工具块的开头。
_TOOL_RE = re.compile(r"<tool\b[^>]*>(.*?)</tool>", re.DOTALL)
_NAME_RE = re.compile(r"<tool_name\b[^>]*>(.*?)</tool_name>", re.DOTALL)
#: 嵌套形状：参数写在 `<tool_param>` 里再包一层的具名标签。闭标签用反向引用 `\1` 认同名
#: 那对；`\w+` 是 Unicode 感知的 ⇒ 中文参数名一样认。
_PARAM_RE = re.compile(r"<tool_param\b[^>]*>(.*?)</tool_param>", re.DOTALL)
_INNER_RE = re.compile(r"<(\w+)\b[^>]*>(.*?)</\1>", re.DOTALL)
#: `<answer>` 的两下：成对的取内容，只判标记在不在用 `_ANSWER_MARK_RE`（前缀判据 ——
#: `<answer>`、`<answer >`、`<answer 乱写>` 都算"它想作答"）。
_ANSWER_RE = re.compile(r"<answer[^>]*>(.*?)</answer>", re.DOTALL)
_ANSWER_MARK_RE = re.compile(r"<answer")
#: 执行摘要：`<summary>` 块。成对才作数，`summary_of` 只取第一对。
_SUMMARY_RE = re.compile(r"<summary\b[^>]*>(.*?)</summary>", re.DOTALL)
#: `looks_like_tool` 的宽判据（见那里）。与上面几个相反，它故意只认前缀。
_TOOL_MARK_RE = re.compile(r"<tool")
#: 裸 `<prices>` 回复（新闻查价的产物）—— 判别与 `<summary>` 同族。
_PRICES_RE = re.compile(r"<prices\b[^>]*>(.*?)</prices>", re.DOTALL)
_PRICE_LINE_RE = re.compile(r"(stone|iron|copper)\s*[：:\s]\s*(up|down|flat)", re.IGNORECASE)
#: 反转义表：prompt 教了 LLM 转义 ⇒ 参数值里的五个预定义实体要还原。`&amp;` 必须最后换
#: （`&amp;lt;` 只该还原一层）。LLM 没转义时这条是空操作 —— 裸 `<` / `>` / `&` 一个都不许改写。
_ENTITIES = (
    ("&lt;", "<"),
    ("&gt;", ">"),
    ("&apos;", "'"),
    ("&quot;", '"'),
    ("&amp;", "&"),
)


def strip_answers(text: str) -> tuple[str, int]:
    """把 `text` 里所有成对的 `<answer>…</answer>` 整段挖掉 ⇒ `(挖过的正文, 挖掉几处)`。

    唯一的调用者是 `Agent.SOP2Prompt`：SOP 正文讲的往往正是"答案要用标签包"，与其要求
    LLM 每次绕开，不如在入库这唯一一道口子上保证没有。挖掉、不是作废整次调用；只认成对，
    半截标记原样留着（`answer_of` 认的也是成对块）。"""
    return _ANSWER_RE.subn("", text)


def tool_of(reply: str) -> tuple[str, list[tuple[str, str]]] | None:
    """解析工具调用 ⇒ `(工具名, [(参数名, 原文), …])`；不是工具调用、或形状不完整 ⇒ `None`。

    严格只认嵌套形状：取第一对 `<tool>…</tool>`；`<tool_name>` 取第一块、`<tool_param>`
    全部收集，每块里必须有一个或多个具名元素（属性式、裸值都不认），任何一块里一个都
    没有 ⇒ 整次调用 `None`。只有参数没名字 ⇒ `None`（不猜）；只有名字没有参数是合法
    形状（放行与否由 `tool_call` 按参数声明判）。参数值只去首尾空白，再过 `_unescape`。"""
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
    r"""收集全部 `<tool_param>` 块里的具名元素 ⇒ `[(参数名, 原文), …]`；任何一块里一个
    具名元素都没有 ⇒ `None`（整次调用不成立）。元素名取标签名（`\w+`，中文也认），值只去
    首尾空白（多行命令、带缩进的 python 都合法）。参数可拆进多个块（块数不是判据）。"""
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

    best-effort：取不到（没写 / 半截 / 空块）⇒ 保旧摘要继续，绝不重问（摘要是搭车品、
    重问要花一回合）。不回落原文：拿半截标记去凑只会把标签串当内容用。"""
    block = _SUMMARY_RE.search(reply)
    return block.group(1).strip() if block else ""


def is_summary_reply(reply: str) -> str | None:
    """裸摘要回复（压缩轮的产物）⇒ 返回摘要文本；否则 `None`。

    判据：`<summary>` 块取得出内容，且挖掉摘要块之后一个字不剩 —— 任务回复（带工具调用 /
    答案 / 正文）不算。任务 prompt 不教摘要 ⇒ "裸摘要"几乎必属压缩回复；粘住同文再判仍幂等。"""
    summary = summary_of(reply)
    if summary and not _SUMMARY_RE.sub("", reply).strip():
        return summary
    return None


def is_prices_reply(reply: str) -> dict[str, str] | None:
    """裸 `<prices>` 回复（新闻查价的产物）⇒ `{矿种: 方向}`；否则 `None`。判据与
    `is_summary_reply` 同构。行格式 `矿种: 方向`（大小写都认）；解析不出的行直接丢。"""
    block = _PRICES_RE.search(reply)
    if block is None or _PRICES_RE.sub("", reply).strip():
        return None
    return {
        m.group(1).lower(): m.group(2).lower()
        for m in _PRICE_LINE_RE.finditer(block.group(1))
    }


def looks_like_tool(reply: str) -> bool:
    """这条回复像工具调用吗？只认开标签前缀出现（故意判宽）。

    只有 `<tool_name>` 的回复、解析不出的形状都该被认成"它想调工具、但格式没凑对" ⇒
    落重问，而不是被当成答案交上去。本模块唯一只认前缀的地方 —— 别顺手改成 `_TOOL_RE`：
    那会把"想调但没凑对"变成"原文即答案"。"""
    return _TOOL_MARK_RE.search(reply) is not None


def answer_of(reply: str) -> str:
    """该提交什么 —— `task` 的 `answer_task` 与 `task_channel` 判据共用同一个谓词
    （"该提交什么"与"该骂什么"必须是同一份，分家会把带标签的原文喂回去纠错）。

    三级判据：① 挖掉全部结构块（`<tool>` 与 `<summary>`）再扫 —— 有 `<answer` 标记 ⇒
    只认成对块的内容（配不上或为空 ⇒ `""`，不回落原文）。先挖结构块防的是污染：SOP 正文
    与执行摘要讲的往往正是"答案要用 `<answer>` 包"，落在结构块内的一律不算。② 否则像
    工具调用 ⇒ `""`。③ 否则原文即答案（判题器的 LLM 是黑盒，这是唯一的退路）。"""
    rest = _SUMMARY_RE.sub("", _TOOL_RE.sub("", reply))
    if _ANSWER_MARK_RE.search(rest):
        block = _ANSWER_RE.search(rest)
        return block.group(1).strip() if block else ""
    if looks_like_tool(reply):
        return ""
    return rest.strip()
