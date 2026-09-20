"""agent/tools/pyexec.py 的用例：本地 Python 执行器 —— 只算数、不见环境。

白名单只放纯计算模块（判题环境本就只有标准库）；真正的硬约束是超时 —— 它跑在判题器
5 秒响应预算里（红线），任何失败路径都不许抛异常。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import sys
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用（discover 不一定把它放进 sys.path）；src/ 给 coregeek 用
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coregeek.agent import AGENT  # noqa: E402
from coregeek.agent.tools import pyexec  # noqa: E402


class PyExecTest(unittest.TestCase):
    """`run` 的规则：纯计算放行、环境面一律拒、错误/超时绝不冒泡。"""

    def test_a_pure_expression_returns_its_value(self):
        """单表达式走 eval：值就是产出（LLM 最顺手的用法之一）。"""
        self.assertEqual(pyexec.run("2**10"), "1024")

    def test_print_output_is_captured(self):
        self.assertEqual(pyexec.run("print(1+1)"), "2")
        self.assertEqual(pyexec.run("print('a')\nprint('b')"), "a\nb")

    def test_statements_only_print(self):
        """多语句走 exec：只有 print 的输出（单表达式才回值，描述里教过的形状）。"""
        self.assertEqual(pyexec.run("print(2)\n3+4"), "2")

    def test_allowed_imports_compute(self):
        """白名单模块放行 —— 判题环境本就只有标准库，"无三方包"自动成立。"""
        self.assertEqual(pyexec.run("import math\nprint(math.floor(3.7))"), "3")
        self.assertEqual(pyexec.run("import statistics\nprint(statistics.mean([1, 3]))"), "2")
        self.assertEqual(pyexec.run("import json\nprint(json.dumps({'k': 1}))"), '{"k": 1}')

    def test_other_imports_are_rejected(self):
        """环境面模块（os/sys/socket/pathlib/subprocess…）全在白名单外。"""
        for code in ("import os", "import sys", "import socket", "from pathlib import Path"):
            with self.subTest(code=code):
                self.assertTrue(pyexec.run(code).startswith("[拒绝]"), code)

    def test_environment_faces_are_rejected(self):
        """文件/输入/动态执行/逃逸口：AST 层点名拒绝（消息比 NameError 清楚）。"""
        for code in (
            "open('x')",
            "eval('1')",
            "exec('1')",
            "input()",
            "__import__('os')",
            "().__class__",
            "(1).real.__doc__",
            "getattr(int, 'real')",
            "globals()",
        ):
            with self.subTest(code=code):
                self.assertTrue(pyexec.run(code).startswith("[拒绝]"), code)

    def test_a_runtime_error_is_reported_not_raised(self):
        """代码里的异常是产出，不是我们的异常 —— 冒出去就是整回合退化空指令。"""
        out = pyexec.run("1/0")
        self.assertTrue(out.startswith("[错误]"), out)
        self.assertIn("ZeroDivisionError", out)

    def test_a_syntax_error_is_reported(self):
        self.assertTrue(pyexec.run("def :").startswith("[语法错误]"))

    def test_a_runaway_loop_is_killed(self):
        """红线：判题器响应预算 5 秒，死循环必须被掐断、当场返回
        （与判题器沙盒的 `[TIMEOUT]` 同一个词，LLM 认得）。"""
        out = pyexec.run("while True: pass", timeout=0.2)
        self.assertTrue(out.startswith("[TIMEOUT]"), out)

    def test_huge_output_is_clipped(self):
        """产出整段进 prompt（窗口内逐字渲染）：99999 字的 print 必须截断留痕，
        不然后续每一份 prompt 都被它撑爆。"""
        out = pyexec.run("print('x' * 99999)")
        self.assertIn("…（共", out)
        self.assertLess(len(out), pyexec.EXEC_TEXT_MAX + 100)


class AgentPythonExecTest(unittest.TestCase):
    """接线：注册表调度 → 本地执行 → 产出当场进会话（tool 消息）→ 返回 `""`。

    "返回值即命令"是铁律 —— 它的价值全在会话里那条 tool 消息：下一份 prompt 的窗口里
    LLM 看得见自己的调用与产出，下一回合就能作答。
    """

    def setUp(self) -> None:
        AGENT.reset()

    def test_the_tool_yields_no_command_but_records_the_output(self):
        AGENT.chat("题目")  # 首问（开会话；工具回复只会在提问之后到 ⇒ 一定有会话）
        self.assertEqual(AGENT.tool_call("python_exec", [("code", "print(6*7)")]), "")
        prompt = AGENT.chat("题目")  # 无新内容 ⇒ nudge；窗口里该有产出
        self.assertIn("【本地 python 的执行结果", prompt)
        self.assertIn("42", prompt)

    def test_the_description_states_its_cost_advantage(self):
        """描述里要点破"它比沙盒便宜" —— 这是 LLM 唯一能看到的成本信号。

        一次沙盒往返之后下一步动作要等两个回合，本地计算只要一个：解析、拼串、比对、
        构造下一条命令都该走本地。不写清楚，LLM 会把纯计算的活儿也丢进沙盒，白花回合
        （分数按回合算，本地一点异常都看不出来）。描述由注册表生成 ⇒ 这里钉的就是
        `Agent.__init__` 那张表里的一行。"""
        desc = AGENT._tools["python_exec"][1]
        self.assertIn("[成本优势]", desc)
        self.assertIn("仅需一个回合执行", desc)
        self.assertIn("不要占用昂贵的沙盒执行", desc)

    def test_a_call_without_code_does_nothing(self):
        """缺参数 ⇒ 调用不成立（`tool_call` 的闸门），什么都不进会话。"""
        AGENT.chat("题目")
        self.assertEqual(AGENT.tool_call("python_exec", []), "")
        prompt = AGENT.chat("题目")
        self.assertNotIn("【本地 python 的执行结果", prompt)


if __name__ == "__main__":
    unittest.main()
