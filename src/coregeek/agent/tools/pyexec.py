"""本地 Python 执行器（第 45 步，用户口径：**只允许计算、无三方包、不碰环境的任何东西**——
文件、接口等一律不行）。

与 `cmd.executeCmd` 的分工（两边的工具描述里也这么教 LLM）：那是把命令交给**判题器的
沙盒**跑（一回合往返、限时 15 秒、能看到任务文件）；这里在**我们自己的进程里**即时算
——产出**当回合**就进 prompt（`Agent.python_exec` 记进会话），但**看不见**沙盒里的任何东西。

**护栏不是对抗级沙箱**：AST 白名单 + 内置白名单 + 守卫超时，防的是 LLM **误伤**
（写出 `open` / `import os` / 死循环），不是防恶意逃逸 —— LLM 是我们自己的解题者，
不是攻击者。真正的硬约束是**超时**：`task_channel` 跑在判题器 5 秒响应预算里（红线），
`while True` 必须被掐断、当场返回 `[TIMEOUT]`（与判题器沙盒回执的标记同一个词，LLM 认得）。

**只依赖标准库、零状态**（与 `cmd.py` 同为这个包的叶子）。
"""

import ast
import builtins
import io
import threading
from contextlib import redirect_stdout

#: 允许 import 的标准库模块——**纯计算**的那一小撮（math/json/re/datetime…）；
#: os/sys/socket/pathlib/subprocess 一类环境面全在白名单外。判题环境本就只有标准库
#: ⇒ "无三方包"自动成立，这里管的是"标准库里也不许碰环境"。
ALLOWED_MODULES = frozenset(
    {
        "math", "cmath", "decimal", "fractions", "statistics",
        "itertools", "functools", "collections", "heapq", "bisect", "array",
        "json", "re", "string", "datetime", "random",
    }
)

#: 禁用的内置名（AST 层点名拒绝，消息比运行期 NameError 清楚）：文件 / 输入 /
#: 动态执行 / 逃逸口。`getattr` 一族也在 —— 它们能绕过一切静态检查。
BANNED_NAMES = frozenset(
    {
        "open", "input", "eval", "exec", "compile", "__import__", "breakpoint",
        "exit", "quit", "help", "globals", "locals", "vars",
        "getattr", "setattr", "delattr",
    }
)

#: 给代码用的内置（**白名单**：不在表上 ⇒ NameError）——常用函数、类型与异常，仅此而已。
SAFE_BUILTINS = {
    name: getattr(builtins, name)
    for name in (
        "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes", "callable",
        "chr", "complex", "dict", "divmod", "enumerate", "filter", "float", "format",
        "frozenset", "hash", "hex", "int", "isinstance", "issubclass", "iter", "len",
        "list", "map", "max", "min", "next", "object", "oct", "ord", "pow", "print",
        "range", "repr", "reversed", "round", "set", "slice", "sorted", "str", "sum",
        "tuple", "type", "zip",
        "Exception", "ValueError", "TypeError", "KeyError", "IndexError",
        "ArithmeticError", "ZeroDivisionError", "OverflowError", "StopIteration",
        "RuntimeError", "RecursionError", "AttributeError", "NameError",
        "NotImplementedError",
    )
}

#: 输出上限（**字**不是字节）：产出会整段进 prompt（窗口内逐字渲染），99999 字的
#: print 会把后续每一份 prompt 都撑爆。截断留痕与 `utils._clip` 同形 —— agent 是
#: 叶子包（只依赖标准库），这条规则各存一份。
EXEC_TEXT_MAX = 4000

#: 守卫超时（秒）：判题器响应预算 5 秒，给 plan/网络留足余量。超时的线程杀不掉
#: （Python 没有杀线程的 API），但它是 daemon —— 不阻塞返回，进程退出时一并带走。
EXEC_TIMEOUT = 2.0

#: 真 `__import__`（在限定内置之前抓一份——import 语句在底层全走它）。
_REAL_IMPORT = builtins.__import__

#: **预热**（第 45 步实测踩出来的）：启动时（进程拉起、判题器第一回合之前）把白名单
#: 模块全部 import 一遍 ⇒ 沙盒里的 `import` 从此只是 `sys.modules` 的字典命中（微秒级）。
#: 没有这一步，冷导入在慢机器/杀毒扫描下能吃掉几秒——本机实测 `import statistics`
#: （连带 decimal/fractions/random）冷加载超过 2 秒守卫超时，而 5 秒响应预算是红线。
#: 代价是启动时一次性 ~百毫秒，不落在任何回合的预算里。
for _name in sorted(ALLOWED_MODULES):
    __import__(_name)


def run(code: str, timeout: float = EXEC_TIMEOUT) -> str:
    """执行一段纯计算的 Python，返回**给 LLM 看的产出文本**；绝不抛异常。

    标记与判题器沙盒回执同一套词根：`[语法错误]` / `[拒绝]`（环境面）/
    `[错误]`（代码自己抛的） / `[TIMEOUT]`。单表达式走 eval、值即产出；
    多语句走 exec、只有 print 的输出 —— 这两个形状都写进了工具描述。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"[语法错误] {exc}"
    problem = _reject(tree)
    if problem:
        return f"[拒绝] {problem}"
    single = len(tree.body) == 1 and isinstance(tree.body[0], ast.Expr)

    buf = io.StringIO()
    outcome: dict[str, object] = {}

    def work() -> None:
        try:
            globs: dict[str, object] = {
                "__builtins__": {**SAFE_BUILTINS, "__import__": _guarded_import}
            }
            with redirect_stdout(buf):
                if single:
                    outcome["value"] = eval(code, globs)  # noqa: S307 —— 安检过才到这
                else:
                    exec(code, globs)  # noqa: S102 —— 同上
        except BaseException as exc:  # noqa: BLE001 —— 代码里的任何异常都是产出，不是我们的
            outcome["error"] = f"{type(exc).__name__}: {exc}"

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return f"[TIMEOUT] 超过 {timeout:g} 秒被终止（本地计算不让等：判题器响应预算只有 5 秒）"
    if "error" in outcome:
        return _clip_out(f"[错误] {outcome['error']}")
    text = buf.getvalue().rstrip("\n")
    if single and outcome.get("value") is not None:
        text = f"{text}\n{outcome['value']!r}" if text else repr(outcome["value"])
    return _clip_out(text or "(无输出)")


def _guarded_import(name: str, *args: object, **kwargs: object) -> object:
    """只放行白名单模块的 `__import__`（import / from … import 底层全走它）。"""
    if name.split(".")[0] not in ALLOWED_MODULES:
        raise ImportError(f"只许 import 这些模块：{'/'.join(sorted(ALLOWED_MODULES))}")
    return _REAL_IMPORT(name, *args, **kwargs)  # type: ignore[return-value]


def _reject(tree: ast.Module) -> str | None:
    """静态安检：返回拒绝理由；`None` = 放行。只看三类东西——
    import 的模块、危险内置名、下划线属性（`__class__`/`__globals__` 一类逃逸口）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ALLOWED_MODULES:
                    return f"不许 import {alias.name}（本地只做纯计算，白名单外的模块一律拒绝）"
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if node.level != 0 or root not in ALLOWED_MODULES:
                return f"不许 from {node.module or '.'} import（白名单外的模块一律拒绝）"
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                return f"不许访问下划线属性 .{node.attr}（逃逸口）"
        elif isinstance(node, ast.Name):
            if node.id in BANNED_NAMES:
                return f"不许使用 {node.id}（本地只做纯计算，不碰文件/环境）"
    return None


def _clip_out(text: str) -> str:
    """产出超长截断留痕（与 `utils._clip` 同形；agent 是叶子包，规则各存一份）。"""
    return text if len(text) <= EXEC_TEXT_MAX else text[:EXEC_TEXT_MAX] + f"…（共 {len(text)} 字）"
