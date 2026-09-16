"""agent/tools/sop.py 的用例：SOP 流程表的存储规则（同名覆盖 / 条数上限 / 截断留痕 /
内容没变就静默）。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import sys
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用（discover 不一定把它放进 sys.path）；src/ 给 coregeek 用
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coregeek.agent import AGENT, Agent  # noqa: E402
from coregeek.agent.tools import sop  # noqa: E402


class SopStateTest(unittest.TestCase):
    """SOP 流程表 `{流程名: 正文}`（同名覆盖、异名追加）—— 状态住在 `Agent` 实例上。

    用每条用例自己的新实例（不用包根单例）：状态在实例上 ⇒ 天然隔离，顺带把"状态确实
    在实例上而不是某个模块里"钉住（见 `test_a_fresh_agent_starts_with_no_sop`）。
    """

    def setUp(self) -> None:
        self.agent = Agent()

    def test_a_new_flow_is_appended(self):
        """异名 ⇒ 追加一条（不同的经验各存各的）。"""
        self.agent.SOP2Prompt("找文件", "第一步")
        self.agent.SOP2Prompt("读题", "第二步")
        self.assertEqual(self.agent.sop, {"找文件": "第一步", "读题": "第二步"})

    def test_the_same_name_replaces_that_flow(self):
        """同名 ⇒ 只覆盖那一条（"上一版不对"由 LLM 重写同名流程表达），
        别的流程一个字不动。"""
        self.agent.SOP2Prompt("找文件", "第一版")
        self.agent.SOP2Prompt("读题", "留着")
        self.agent.SOP2Prompt("找文件", "第二版")
        self.assertEqual(self.agent.sop, {"找文件": "第二版", "读题": "留着"})

    def test_it_survives_across_calls(self):
        """存下来之后下一个调用者读得到 —— 这就是"跨回合"的全部含义。"""
        self.agent.SOP2Prompt("找文件", "先看 ls 的输出再算")
        self.assertEqual(self.agent.sop, {"找文件": "先看 ls 的输出再算"})
        self.assertEqual(self.agent.sop, {"找文件": "先看 ls 的输出再算"})

    def test_a_fresh_agent_starts_with_no_sop(self):
        """新实例不带任何流程（状态是实例属性，不是模块里的变量）。"""
        self.agent.SOP2Prompt("甲", "甲的方法")
        self.assertEqual(Agent().sop, {})

    def test_the_sop_never_leaks_between_instances(self):
        """两个实例各存各的 —— 反向钉死"状态在模块级"那种退化。"""
        other = Agent()
        self.agent.SOP2Prompt("甲", "x")
        other.SOP2Prompt("乙", "y")
        self.assertEqual(self.agent.sop, {"甲": "x"})
        self.assertEqual(other.sop, {"乙": "y"})

    def test_each_instance_keeps_its_own_tool_table(self):
        """工具表也是实例的：`SOP2Prompt` 那一项是绑定方法，钉在各自的实例上。

        退化写法是共享一张模块级工具表、表里那个函数去写"某个全局 SOP"（症状：两个
        Agent 的 SOP 互相覆盖）—— 这条走工具表路径、不直接调方法，把它挡住。
        """
        other = Agent()
        self.agent.tool_call("SOP2Prompt", [("name", "流程"), ("sop", "甲走工具表")])
        other.tool_call("SOP2Prompt", [("name", "流程"), ("sop", "乙走工具表")])
        self.assertEqual(self.agent.sop, {"流程": "甲走工具表"})
        self.assertEqual(other.sop, {"流程": "乙走工具表"})

    def test_reset_clears_this_instance(self):
        """`reset()` 是隔离机制本身，必须真的清掉自己（`AGENT` 是模块级的，别处的
        `setUp` 全靠它才不跨用例串味）。

        两种退化各挡一半：① 不生效（`return` 掉）⇒ 上个用例的流程灌进下个用例的
        prompt；② 清错了对象（如 `Agent()._sop = {}`）⇒ 看着像清了，自己那份没动。
        """
        self.agent.SOP2Prompt("甲", "要清掉的东西")
        self.agent.reset()
        self.assertEqual(self.agent.sop, {})
        self.assertNotIn("要清掉的东西", self.agent.chat("题目"))
        # 复位之后还能重新存（别把 reset 写成"把实例锁死"）
        self.agent.SOP2Prompt("乙", "第二版")
        self.assertIn("第二版", self.agent.chat("题目"))

    def test_an_overlong_flow_is_truncated_and_says_so(self):
        """超上限 ⇒ 保头截断（上限是单条流程的），日志同时报"收到多少 / 存了多少"、
        带流程名 —— 静默截断会让后来人以为 LLM 只写了 1000 字。上限的理由是硬约束 5。
        """
        with self.assertLogs(sop.__name__, level="INFO") as caught:
            self.agent.SOP2Prompt("长流程", "长" * 9000)
        self.assertEqual(len(self.agent.sop["长流程"]), sop.SOP_MAX)
        line = caught.records[0].getMessage()
        self.assertIn("9000", line)
        self.assertIn(str(sop.SOP_MAX), line)
        self.assertIn("长流程", line)

    def test_the_flow_count_is_capped_and_the_eviction_is_logged(self):
        """条数超 `SOP_FLOWS_MAX` ⇒ 丢最旧的（新经验优先），日志点名丢了谁 ——
        否则"怎么少了一条"无从查起。与 `SOP_MAX` 合起来是流程表的防膨胀机制。"""
        for i in range(sop.SOP_FLOWS_MAX + 1):
            self.agent.SOP2Prompt(f"流程{i}", f"做法{i}")
        self.assertEqual(len(self.agent.sop), sop.SOP_FLOWS_MAX)
        self.assertNotIn("流程0", self.agent.sop, "最旧的被丢")
        with self.assertLogs(sop.__name__, level="INFO") as caught:
            self.agent.SOP2Prompt("更新的", "又一条")
        self.assertIn("流程1", caught.records[0].getMessage(), "这次轮到丢它，要留名")

    def test_storing_the_same_text_again_is_silent(self):
        """同样的内容存第二遍不打日志。

        `llm_resp` 粘住时 LLM 会把同一段 SOP 反复喂进来，每回合打一行是白花 stdout 预算，
        而"又存了一遍同样的东西"不算"有事"。
        """
        self.agent.SOP2Prompt("一样", "一样的内容")
        with self.assertNoLogs(sop.__name__, level="INFO"):
            self.agent.SOP2Prompt("一样", "一样的内容")

    def test_a_multiline_flow_is_logged_as_one_line(self):
        """流程正文必然是多行的 ⇒ 日志必须打成一行（换行转义）。

        不转义的话一条记录变几十行，而 `logging` 的时间戳前缀只加在第一条物理行上。
        """
        with self.assertLogs(sop.__name__, level="INFO") as caught:
            self.agent.SOP2Prompt("多行", "第一步：ls\r\n第二步：cat")
        message = caught.records[0].getMessage()
        self.assertNotIn("\n", message)
        self.assertNotIn("\r", message)
        self.assertIn("\\n", message)

    def test_an_empty_text_deletes_that_flow(self):
        """空文本 = 删掉那一条（"整段替换成空"的语义在流程表上的推论），删谁留一行 ——
        否则"怎么没了"无从查起。删一条不存在的 ⇒ 什么都不发生、也不吭声。"""
        self.agent.SOP2Prompt("甲", "有内容")
        with self.assertLogs(sop.__name__, level="INFO") as caught:
            self.agent.SOP2Prompt("甲", "")
        self.assertEqual(self.agent.sop, {})
        self.assertEqual(len(caught.records), 1)
        with self.assertNoLogs(sop.__name__, level="INFO"):
            self.agent.SOP2Prompt("不存在的", "")
        self.assertEqual(self.agent.sop, {})

    def test_the_logger_name_does_not_depend_on_the_agent(self):
        """SOP 那一行的 logger 名保持 `coregeek.agent.tools.sop`。

        名字变了会连带改三份东西：`app._log` 的字节表、"唯一一条不在 `app` 名下的日志"
        那条守卫、`CLAUDE.md` 硬约束 5 —— 把 `store` 挪进 `agent/agent.py` 就会在这里挂。
        """
        # 断的是 `LOGGER` 的名字（不是模块的 `__name__`，那是同义反复）：它得与 `app._log`
        # 的字节表、`CLAUDE.md` 硬约束 5 里那个字面量一致 —— 把 `LOGGER` 挪走 ⇒ 这里
        # `AttributeError`；只改名字 ⇒ 断言的字符串对不上。两种都挂。
        self.assertEqual(sop.LOGGER.name, "coregeek.agent.tools.sop")


if __name__ == "__main__":
    unittest.main()
