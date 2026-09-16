"""agent/agent.py 的用例：工具注册表（一表两用：描述与调度）与 `tool_call` 顶层调度。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import json
import sys
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用（discover 不一定把它放进 sys.path）；src/ 给 coregeek 用
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coregeek.agent import AGENT, Agent  # noqa: E402
from coregeek.agent.chat import answer_of, tool_of  # noqa: E402
from coregeek.agent.prompt import gen_all_tool_prompt  # noqa: E402
from coregeek.agent.tools import sop  # noqa: E402
from coregeek.app import handle  # noqa: E402


# ── Agent 系统（`agent/` + `tools/`）：单实例，状态在实例上 ──
class AgentToolCallTest(unittest.TestCase):
    """工具注册表与顶层调度 `Agent.tool_call`。

    注册表给每个工具声明 `((参数名, 用途), …)`；`tool_call` 收 `[(参数名, 原文), …]`，
    只收具名参数：认不出的名字忽略、声明的参数一个不少且非空才放行 `impl(**resolved)`。
    `setUp` 造的是 `AGENT` 之外的新实例（状态住在实例上 ⇒ 天然干净），不需要复位。
    """

    def setUp(self) -> None:
        self.agent = Agent()

    def test_an_unnamed_param_is_ignored(self):
        """只收具名参数：`(None, 值)` 按"认不出的名字"忽略 ⇒ 声明的参数没给上 ⇒
        不成立。（解析侧 `tool_of` 不再产出无名参数，这条钉 `tool_call` 这一半。）"""
        self.assertEqual(self.agent.tool_call("executeCmd", [(None, "ls -la")]), "")

    def test_a_named_param_is_matched_by_its_name(self):
        self.assertEqual(self.agent.tool_call("executeCmd", [("cmd", "ls -la")]), "ls -la")

    def test_a_fabricated_param_name_means_no_call(self):
        """LLM 编的参数名忽略（不为它作废整次调用），但声明的参数没给上 ⇒ 不成立。"""
        self.assertEqual(self.agent.tool_call("executeCmd", [("命令", "ls")]), "")

    def test_a_declared_param_left_out_means_no_call(self):
        self.assertEqual(self.agent.tool_call("executeCmd", []), "")

    def test_a_blank_or_non_string_value_never_reaches_the_tool(self):
        """值不是字符串 / 空白 ⇒ `""`，绝不抛 —— 这条闸门在 `SOP2Prompt` 之前，
        空参数调用不会把整场攒下来的流程表抹掉。"""
        for value in ("", "   ", "\n", None, 42):
            with self.subTest(value=value):
                self.assertEqual(self.agent.tool_call("executeCmd", [("cmd", value)]), "")

    def test_malformed_params_are_not_a_call(self):
        """`params` 整个不是 `[(名|None, 文本), …]` 的形状 ⇒ `""`，绝不抛
        （它跑在 `app.handle` 的 `try` 里，抛出去 = 整回合空指令）。"""
        for params in ("ls", None, 42, ["ls"], [("cmd",)], [None]):
            with self.subTest(params=params):
                self.assertEqual(self.agent.tool_call("executeCmd", params), "")

    def test_a_zero_param_tool_needs_no_params(self):
        """无参数工具：参数表为空 ⇒ 空参数表就能调起来；多余的具名参数按
        "认不出的名字"忽略（宽容那一侧）—— 零参工具照常跑，它又不收输入。"""
        self.agent._tools["查询状态"] = (lambda: "状态正常", "测试用：查个状态", ())
        self.assertEqual(self.agent.tool_call("查询状态", []), "状态正常")
        self.assertEqual(self.agent.tool_call("查询状态", [("多余", "x")]), "状态正常")

    def test_a_two_param_tool_takes_named_params(self):
        """多参数工具：按名收；缺一个 ⇒ 不成立。"""
        def echo(**kw: str) -> str:
            return f"{kw['甲']}+{kw['乙']}"

        self.agent._tools["双参"] = (echo, "测试用：两个参数", (("甲", "第一个"), ("乙", "第二个")))
        self.assertEqual(self.agent.tool_call("双参", [("甲", "一"), ("乙", "二")]), "一+二")
        self.assertEqual(self.agent.tool_call("双参", [("甲", "一")]), "")

    def test_execute_cmd_returns_the_command_verbatim(self):
        """`executeCmd` 就是"把命令搬进响应字段"这一步 —— 本地一个字都不执行。

        换行、引号、重定向、中文原样过（沙盒那边才解释它）⇒ 只能断言原文返回。
        """
        for cmd in (
            "ls -la",
            'python -c "print(1+1)"',
            "cat < input.txt > out.txt",
            "grep -n '中文' a.txt\nwc -l a.txt",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.agent.tool_call("executeCmd", [("cmd", cmd)]), cmd)

    def test_sop2prompt_stores_the_flow_and_yields_no_command(self):
        """`SOP2Prompt` 存下一条流程、返回空串（它不产出命令）。

        返回值直接进响应顶层的 `executeCmd` ⇒ 返回非空就是往沙盒丢一条不存在的命令。
        顺带钉闸门的位置：空白参数在 `tool_call` 就被挡下 ⇒ 清不掉已存的流程
        （`sop` 传空白串的语义是"删掉那条"）。
        """
        self.assertEqual(
            self.agent.tool_call(
                "SOP2Prompt", [("name", "找任务书"), ("sop", "第一步：先 ls")]
            ),
            "",
        )
        self.assertEqual(self.agent.sop, {"找任务书": "第一步：先 ls"})
        self.assertEqual(
            self.agent.tool_call("SOP2Prompt", [("name", "找任务书"), ("sop", "  ")]), ""
        )
        self.assertEqual(self.agent.sop, {"找任务书": "第一步：先 ls"}, "空白参数清不掉流程 —— 闸门在工具之前")
        # 不认识的参数名不参与闸门：name/sop 都在就放行
        self.assertEqual(
            self.agent.tool_call("SOP2Prompt", [("name", "读题"), ("sop", "第二步"), ("答案", "x")]),
            "",
        )
        self.assertEqual(self.agent.sop, {"找任务书": "第一步：先 ls", "读题": "第二步"})
        # 声明的参数缺一个（这里是 `name`）⇒ 整次调用作废，流程表一个字节都别动
        self.assertEqual(self.agent.tool_call("SOP2Prompt", [("sop", "第三步")]), "")
        self.assertEqual(self.agent.tool_call("SOP2Prompt", []), "")
        self.assertEqual(self.agent.sop, {"找任务书": "第一步：先 ls", "读题": "第二步"})

    def test_a_sop_containing_the_answer_tags_is_scrubbed_on_the_way_in(self):
        """`sop` 里成对的 `<answer>…</answer>` 入库前挖掉：沉淀正文讲的正是
        "答案怎么写" ⇒ 几乎必然带这对标签，不挖就会进后续每一份 prompt。
        挖掉、不是作废整次调用，挖了几处进日志（"LLM 又把答案格式写进 SOP 了"的信号）。
        与 `answer_of` 的"先挖工具块"叠起来才是"答案不被 SOP 污染"的完整保证（端到端见
        `TaskChannelTest.test_a_literal_in_the_sop_does_not_poison_the_submitted_answer`）。
        """
        with self.assertLogs(level="INFO") as logs:
            self.assertEqual(
                self.agent.tool_call(
                    "SOP2Prompt",
                    [
                        ("name", "答题格式"),
                        ("sop", "先 ls。答案写成 <answer>示例</answer> 的形状。"),
                    ],
                ),
                "",
            )
        self.assertEqual(self.agent.sop, {"答题格式": "先 ls。答案写成  的形状。"})
        self.assertIn("剔除 1 处 <answer> 段", "\n".join(r.getMessage() for r in logs.records))
        # 多处 / 空块都算"这对串"，一次挖干净
        self.agent.tool_call(
            "SOP2Prompt", [("name", "答题格式"), ("sop", "<answer></answer>先 ls<answer>x</answer>")]
        )
        self.assertEqual(self.agent.sop, {"答题格式": "先 ls"})
        # 半截的标记（有开无闭）不挖 —— `answer_of` 认的也是成对块，
        # 半截标记在正文里只是普通文字（挖它等于替 LLM 改正文）
        self.agent.tool_call("SOP2Prompt", [("name", "答题格式"), ("sop", "写 <answer> 但没有闭标签")])
        self.assertEqual(self.agent.sop, {"答题格式": "写 <answer> 但没有闭标签"})

    def test_an_unknown_tool_yields_no_command_and_no_exception(self):
        """未知工具 ⇒ 空串，绝不抛。

        它跑在 `app.handle` 的 `try` 里，抛出去会把整回合所有角色的指令一起带走
        （合法、不计异常，但白白丢一个回合）。LLM 编工具名是常态。
        """
        for name in ("nope", "", "execute_cmd", None):
            with self.subTest(name=name):
                self.assertEqual(self.agent.tool_call(name, [(None, "ls")]), "")

    def test_every_registered_tool_is_described_and_callable(self):
        """`gen_all_tool_prompt` 覆盖工具表里的每一个工具（含参数行），且每个都能真的调起来。
        注册了却没进描述（LLM 永远不知道它存在），或者描述里有、注册表里没有
        （LLM 一调就落空）—— 两种都是只有实盘才会暴露的不一致。"""
        desc = gen_all_tool_prompt(self.agent._tools)
        for name, (impl, _, params) in self.agent._tools.items():
            with self.subTest(name=name):
                self.assertIn(f"## ToolName - {name}", desc)
                self.assertTrue(callable(impl))
                if params:
                    self.assertIn(f"    - {params[0][0]}: ", desc)

    def test_the_tool_section_documents_the_param_table(self):
        """参数说明由注册表生成，参数一行一个 `- 名: 用途`；无参数打
        `- Params: （无参数）` —— LLM 照着表写调用，不靠描述正文里的散文。"""
        desc = gen_all_tool_prompt(self.agent._tools)
        self.assertIn("- Params:\n    - cmd: 命令原文", desc)
        self.assertIn("- Params:\n    - name: ", desc)
        self.assertIn("    - sop: 做法总结或知识本身", desc)
        self.assertNotIn("- answer:", desc)
        self.agent._tools["查询状态"] = (lambda: "s", "测试用", ())
        self.assertIn(
            "## ToolName - 查询状态\n- Description: 测试用\n- Params: （无参数）",
            gen_all_tool_prompt(self.agent._tools),
        )

    def test_the_sop_tool_describes_knowledge_deposits_too(self):
        """SOP2Prompt 的描述要教 LLM 沉淀环境知识（接口描述等），不只是解题流程
        —— 同一张表、类型由 `name` 约定区分（流程「找任务书」/ 知识「接口-XX」），
        零新机制。"""
        desc = gen_all_tool_prompt(self.agent._tools)
        block = desc.split("## ToolName - SOP2Prompt", 1)[1].split("## ToolName", 1)[0]
        self.assertIn("环境知识", block)
        self.assertIn("接口", block)

    def test_a_newly_registered_tool_shows_up_everywhere(self):
        """加一个工具只改一处（`Agent.__init__` 里那张表）—— 描述与调度同时跟上。

        注入一个假工具来钉这条性质（把描述写死成字面量的实现会在这里露馅）；注入的是
        本类 setUp 那个新实例的表（工具表是实例属性）⇒ 不需要清理。
        """
        self.agent._tools["测试用工具"] = (
            lambda **kw: f"命令:{kw['参数']}",
            "只在这条用例里存在",
            (("参数", "测试参数"),),
        )
        self.assertIn("测试用工具", gen_all_tool_prompt(self.agent._tools))
        self.assertIn("    - 参数: 测试参数", gen_all_tool_prompt(self.agent._tools))
        self.assertEqual(self.agent.tool_call("测试用工具", [("参数", "实参")]), "命令:实参")
        self.assertNotIn("测试用工具", gen_all_tool_prompt(Agent()._tools))


class AdoptSummaryTest(unittest.TestCase):
    """压缩轮摘要的落库：`adopt_summary` 是唯一入口。

    摘要由命令轮同发的压缩请求产出（裸 `<summary>` 回复）；任务回复里零星出现的
    `<summary>`（LLM 的习惯残留）一律忽略 —— 那不是我们请求的东西。
    """

    def setUp(self) -> None:
        self.agent = Agent()

    def test_adopt_summary_lands_in_the_next_prompt(self):
        self.agent.chat("题")
        self.agent.adopt_summary("【总目标】交 token")
        prompt = json.loads(self.agent.chat("题"))
        self.assertIn("【历史摘要】\n【总目标】交 token", [m["content"] for m in prompt])

    def test_hear_no_longer_extracts_summaries(self):
        """任务回复里搭的 `<summary>` 不再被提取 —— 摘要的唯一来源是压缩轮。"""
        self.agent.chat("题")
        self.agent.hear("<summary>不该被提取</summary><tool>ls</tool>")
        prompt = json.loads(self.agent.chat("题"))
        contents = [m["content"] for m in prompt]
        self.assertNotIn("【历史摘要】\n不该被提取", contents)

    def test_adopt_summary_is_a_noop_without_a_context(self):
        """还没开过会话（这道题一次都没问过）⇒ 忽略，绝不抛（它跑在 task_channel 里）。"""
        self.agent.adopt_summary("孤儿摘要")


if __name__ == "__main__":
    unittest.main()
