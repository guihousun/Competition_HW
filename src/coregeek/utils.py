"""日志层的两条规则（不是状态）：字符上限与截断。

叶子模块（零包内 import）。这里不放状态、不放 logger、不放格式化函数，
只放"上限"与"截断"这一件事；`protocol.actions.describe` 那条边走参数注入
而不是 import——它的契约是"不认识日志"。
"""

#: 日志里单个输入字段的字符上限（单位是字不是字节：中文 1 字 = 3 字节）。
#: 40000 是"观察优先"：任务原文/回复/沙盒输出基本不截。代价 = 顶格字段一回合
#: 可写满 64KB 管道（见 `CLAUDE.md` 硬约束 5）——已知并接受。
LOG_TEXT_MAX = 40000


def _clip(text: str, limit: int = LOG_TEXT_MAX) -> str:
    """超长文本截到 `limit` 字，并把截断说出来（`…（共 N 字）`）——静默截断会让日志
    变成没头没尾的一段，看不出后面还有没有内容。截断规则只有这一份，上限由参数给
    （prompt 那一处用 `app.LOG_PROMPT_MAX`）。
    """
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…（共 {len(text)} 字）"
