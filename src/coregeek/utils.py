"""日志层的两条规则：字符上限与截断。叶子模块，零包内 import —— 只放"上限"与
"截断"，不放状态、不放 logger、不放格式化函数。
"""

#: 日志里单个输入字段的字符上限（单位是字不是字节：中文 1 字 = 3 字节）。40000 是
#: "观察优先"：任务原文/回复/沙盒输出基本不截。代价 = 顶格字段一回合可写满 64KB 管道
#: （见 `CLAUDE.md` 硬约束 5）——已知并接受。
LOG_TEXT_MAX = 40000


def _clip(text: str, limit: int = LOG_TEXT_MAX) -> str:
    """超长文本截到 `limit` 字，并把原长说出来（`…（共 N 字）`）。截断规则只有这一份。"""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…（共 {len(text)} 字）"
