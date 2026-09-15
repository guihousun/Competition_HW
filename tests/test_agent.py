"""agent/agent.py 的用例：工具注册表（一表两用：描述与调度）与 `tool_call` 顶层调度。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。⚠️ 用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

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


# ── 第 18 步：Agent 系统（`agent/` + `tools/`）；第 19 步：单实例 + 状态在实例上 ──
class AgentToolCallTest(unittest.TestCase):
    """工具注册表与顶层调度 `Agent.tool_call`。

    ⚠️ **这一类的 `setUp` 造的是 `AGENT` 之外的实例**（用户拍板：Agent 是单实例，
    状态住在实例上 ⇒ 新实例天然干净）。所以这里的用例**不需要**复位任何东西 ——
    复位是给"必须走包根单例"的那些用例准备的（`HandleTest` / `TaskChannelTest`）。

    注册表给每个工具声明 `((参数名, 用途), …)`；`tool_call` 收 `[(参数名, 原文), …]`
    —— **第 37 步起只收具名参数**（严格解析不再产出无名参数，位置填充机制删除）：
    认不出的名字忽略，声明的参数**一个不少且非空**才放行，`impl(**resolved)`。
    描述的生成在 `prompt.gen_all_tool_prompt`（第 37 步从 `Agent.tool_desc` 搬走）。
    """

    def setUp(self) -> None:
        self.agent = Agent()

    def test_an_unnamed_param_is_ignored(self):
        """**只收具名参数**（第 37 步）：`(None, 值)` 按"认不出的名字"忽略 ⇒
        声明的参数没给上 ⇒ 不成立。解析侧（`tool_of`）从此产不出无名参数 ——
        这条钉的是 `tool_call` 这一半的契约。"""
        self.assertEqual(self.agent.tool_call("executeCmd", [(None, "ls -la")]), "")

    def test_a_named_param_is_matched_by_its_name(self):
        self.assertEqual(self.agent.tool_call("executeCmd", [("cmd", "ls -la")]), "ls -la")

    def test_a_fabricated_param_name_means_no_call(self):
        """LLM 编的参数名**忽略**（不为它作废整次调用），但声明的参数没给上 ⇒ 不成立。"""
        self.assertEqual(self.agent.tool_call("executeCmd", [("命令", "ls")]), "")

    def test_a_declared_param_left_out_means_no_call(self):
        self.assertEqual(self.agent.tool_call("executeCmd", []), "")

    def test_a_blank_or_non_string_value_never_reaches_the_tool(self):
        """值不是字符串 / 空白 ⇒ `""`，**绝不抛** —— 这条闸门在 `SOP2Prompt` 之前，
        空参数调用不会把整场攒下来的流程表抹掉。"""
        for value in ("", "   ", "\n", None, 42):
            with self.subTest(value=value):
                self.assertEqual(self.agent.tool_call("executeCmd", [("cmd", value)]), "")

    def test_malformed_params_are_not_a_call(self):
        """`params` 整个不是 `[(名|None, 文本), …]` 的形状 ⇒ `""`，**绝不抛**
        （它跑在 `app.handle` 的 `try` 里，抛出去 = 整回合空指令）。"""
        for params in ("ls", None, 42, ["ls"], [("cmd",)], [None]):
            with self.subTest(params=params):
                self.assertEqual(self.agent.tool_call("executeCmd", params), "")

    def test_a_zero_param_tool_needs_no_params(self):
        """**无参数工具**：参数表为空 ⇒ 空参数表就能调起来；多余的具名参数按
        "认不出的名字"**忽略**（宽容那一侧）—— 零参工具照常跑，它又不收输入。"""
        self.agent._tools["查询状态"] = (lambda: "状态正常", "测试用：查个状态", ())
        self.assertEqual(self.agent.tool_call("查询状态", []), "状态正常")
        self.assertEqual(self.agent.tool_call("查询状态", [("多余", "x")]), "状态正常")

    def test_a_two_param_tool_takes_named_params(self):
        """**多参数工具**：按名收；缺一个 ⇒ 不成立。"""
        def echo(**kw: str) -> str:
            return f"{kw['甲']}+{kw['乙']}"

        self.agent._tools["双参"] = (echo, "测试用：两个参数", (("甲", "第一个"), ("乙", "第二个")))
        self.assertEqual(self.agent.tool_call("双参", [("甲", "一"), ("乙", "二")]), "一+二")
        self.assertEqual(self.agent.tool_call("双参", [("甲", "一")]), "")

    def test_execute_cmd_returns_the_command_verbatim(self):
        """`executeCmd` 就是"把命令搬进响应字段"这一步 —— **本地一个字都不执行**。

        换行、引号、重定向、中文原样过（沙盒那边才解释它）。所以这里**只能**断言原文返回：
        断言里出现任何"执行"的痕迹（`subprocess` / `os.system`）都说明这一步走错了。
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
        """`SOP2Prompt` 存下一条流程、**返回空串**（它不产出命令）。

        返回值直接进响应顶层的 `executeCmd` ⇒ 返回非空就是往沙盒里丢一条命令
        （而这条命令根本不存在，只会白烧一次沙盒执行）。
        ⚠️ 顺带钉**闸门的位置**：空白参数在 `tool_call` 就被挡下 ⇒ 清不掉已存的流程
        （`sop` 传空白串的语义是"删掉那条"，校验下沉到工具里就会一次误调用动到流程表）。
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
        #: 不认识的参数名（第 35 步那个 `answer` 通道已作废）**不参与闸门**：name/sop 都在就放行
        self.assertEqual(
            self.agent.tool_call("SOP2Prompt", [("name", "读题"), ("sop", "第二步"), ("答案", "x")]),
            "",
        )
        self.assertEqual(self.agent.sop, {"找任务书": "第一步：先 ls", "读题": "第二步"})
        #: 声明的参数缺一个（这里是 `name`）⇒ 整次调用作废，**流程表一个字节都别动**
        self.assertEqual(self.agent.tool_call("SOP2Prompt", [("sop", "第三步")]), "")
        self.assertEqual(self.agent.tool_call("SOP2Prompt", []), "")
        self.assertEqual(self.agent.sop, {"找任务书": "第一步：先 ls", "读题": "第二步"})

    def test_a_sop_containing_the_answer_tags_is_scrubbed_on_the_way_in(self):
        """⚠️ 第 36 步（用户口径）：**`sop` 里成对的 `<answer>…</answer>` 入库前挖掉**。

        那段正文的用处正是讲"答案怎么写" ⇒ 它几乎必然带上这对标签；不挖掉，存下来的 SOP
        就会带着这对串进后续每一份 prompt。与 `answer_of` 的"先挖工具块"叠在一起，
        才是"答案不会被 SOP 污染"的完整保证（端到端那条见
        `TaskChannelTest.test_a_literal_in_the_sop_does_not_poison_the_submitted_answer`）。
        ⚠️ **挖掉、不是作废整次调用**（用户选的是前者）：沉淀是这个工具的全部价值，
        不该因为它多写了一句示例就整段丢掉；挖了几处进日志 —— 那是"LLM 又把答案格式
        写进 SOP 了"的唯一信号。
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
        #: 多处 / 空块都算"这对串"，一次挖干净
        self.agent.tool_call(
            "SOP2Prompt", [("name", "答题格式"), ("sop", "<answer></answer>先 ls<answer>x</answer>")]
        )
        self.assertEqual(self.agent.sop, {"答题格式": "先 ls"})
        #: 半截的标记（有开无闭）**不挖** —— `answer_of` 认的也是成对块，
        #: 半截标记在正文里只是普通文字（挖它等于替 LLM 改正文）
        self.agent.tool_call("SOP2Prompt", [("name", "答题格式"), ("sop", "写 <answer> 但没有闭标签")])
        self.assertEqual(self.agent.sop, {"答题格式": "写 <answer> 但没有闭标签"})

    def test_an_unknown_tool_yields_no_command_and_no_exception(self):
        """未知工具 ⇒ 空串，**绝不抛**。

        它跑在 `app.handle` 的 `try` 里，抛出去会把**整回合所有角色的指令**一起带走
        （不只任务线）—— 合法、不计异常，但白白丢一个回合。LLM 编工具名是常态。
        """
        for name in ("nope", "", "execute_cmd", None):
            with self.subTest(name=name):
                self.assertEqual(self.agent.tool_call(name, [(None, "ls")]), "")

    def test_every_registered_tool_is_described_and_callable(self):
        """`gen_all_tool_prompt` 覆盖工具表里的每一个工具（含参数行），且每个都能真的调起来。
        注册了却没进描述（LLM 永远不知道它存在），或者描述里有、注册表里没有
        （LLM 一调就落空）—— 两种都是**只有在实盘上才会暴露**的不一致。
        """
        desc = gen_all_tool_prompt(self.agent._tools)
        for name, (impl, _, params) in self.agent._tools.items():
            with self.subTest(name=name):
                self.assertIn(f"## ToolName - {name}", desc)
                self.assertTrue(callable(impl))
                if params:
                    self.assertIn(f"    - {params[0][0]}: ", desc)

    def test_the_tool_section_documents_the_param_table(self):
        """参数说明由注册表**生成**（第 37 步起的块格式），参数一行一个 `- 名: 用途`；
        无参数打 `- Params: （无参数）` —— LLM 照着表写调用，不靠描述正文里的散文。
        第 37 步起 `SOP2Prompt` 声明 `name` + `sop` 两个参数（多流程口径）。"""
        desc = gen_all_tool_prompt(self.agent._tools)
        self.assertIn("- Params:\n    - cmd: 命令原文", desc)
        self.assertIn("- Params:\n    - name: ", desc)
        self.assertIn("    - sop: 该流程的做法总结", desc)
        self.assertNotIn("- answer:", desc)
        self.agent._tools["查询状态"] = (lambda: "s", "测试用", ())
        self.assertIn(
            "## ToolName - 查询状态\n- Description: 测试用\n- Params: （无参数）",
            gen_all_tool_prompt(self.agent._tools),
        )

    def test_a_newly_registered_tool_shows_up_everywhere(self):
        """**加一个工具只改一处**（`Agent.__init__` 里那张表）—— 描述与调度同时跟上。

        注入一个假工具来钉这条性质（把描述写死成字面量的实现会在这里露馅）：
        漏掉的症状是"**LLM 永远不知道它存在**"，本地全绿、实盘上只是"少用了一个工具"。

        ⚠️ **注入的是本类 setUp 里那个新实例的表**（第 19 步起工具表是实例属性）
        ⇒ 不需要 `finally: del` —— 实例是这条用例私有的，跑完就没人再看得见它。
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


if __name__ == "__main__":
    unittest.main()
