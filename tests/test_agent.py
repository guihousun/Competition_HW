"""agent/agent.py 的用例：工具注册表（一表两用：描述与调度）与 `tool_call` 顶层调度。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import json
import shlex
import sys
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用（discover 不一定把它放进 sys.path）；src/ 给 coregeek 用
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coregeek.agent import AGENT, Agent, cmd_explore  # noqa: E402
from coregeek.agent.chat import tool_of  # noqa: E402
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
        """`SOP2Prompt` 存下一条流程（落暂存表）、返回空串（它不产出命令）。

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
        self.assertEqual(self.agent.pre_sop, {"找任务书": "第一步：先 ls"})
        self.assertEqual(
            self.agent.tool_call("SOP2Prompt", [("name", "找任务书"), ("sop", "  ")]), ""
        )
        self.assertEqual(self.agent.pre_sop, {"找任务书": "第一步：先 ls"}, "空白参数清不掉流程 —— 闸门在工具之前")
        # 不认识的参数名不参与闸门：name/sop 都在就放行
        self.assertEqual(
            self.agent.tool_call("SOP2Prompt", [("name", "读题"), ("sop", "第二步"), ("答案", "x")]),
            "",
        )
        self.assertEqual(self.agent.pre_sop, {"找任务书": "第一步：先 ls", "读题": "第二步"})
        # 声明的参数缺一个（这里是 `name`）⇒ 整次调用作废，流程表一个字节都别动
        self.assertEqual(self.agent.tool_call("SOP2Prompt", [("sop", "第三步")]), "")
        self.assertEqual(self.agent.tool_call("SOP2Prompt", []), "")
        self.assertEqual(self.agent.pre_sop, {"找任务书": "第一步：先 ls", "读题": "第二步"})

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
        `### Params: （无参数）` —— LLM 照着表写调用，不靠描述正文里的散文。"""
        desc = gen_all_tool_prompt(self.agent._tools)
        self.assertIn("### Params:\n    - cmd: 需要在沙盒中执行的完整命令原文", desc)
        self.assertIn("### Params:\n    - name: ", desc)
        self.assertIn("    - sop: ", desc)  # 用途是措辞、会改；钉的是"第二个参数叫 sop"
        self.assertIn("    - answer: ", desc)  # 交卷工具的那一个参数
        self.agent._tools["查询状态"] = (lambda: "s", "测试用", ())
        self.assertIn(
            "## ToolName - 查询状态\n### Description: 测试用\n### Params: （无参数）",
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

    def test_the_sop_tool_teaches_reuse_and_a_generic_name(self):
        """描述里要有两层：① 沉淀是为了下次同类任务**复用**；
        ② `name` 要泛化到"一类问题"（「订去某地的机票的流程」），不能写死成本次的目标。

        第 107 步起"什么时候存、存成什么名"整套细则都在这条描述里（旧的【沉淀规则】段
        随用户重写的 prompt 删除），这一段就是沉淀规则的唯一出口。① 的措辞改过两次
        （"能够少做探索" → "具有复用价值" → 第 137 步的"能直接复用"），判据跟着走。"""
        desc = gen_all_tool_prompt(self.agent._tools)
        block = desc.split("## ToolName - SOP2Prompt", 1)[1].split("## ToolName", 1)[0]
        self.assertIn("能直接复用", block)
        self.assertIn("泛化", block)
        self.assertIn("订去某地", block)

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


class SubmitAnswerTest(unittest.TestCase):
    """交答案走工具 + 答卷变量：`submitAnswer` 写、`Agent.answer` 读、`take_answer` 取走。

    答案不再从回复原文里解一遍（`tool_of` 认出什么、`submitAnswer` 就写进什么是同一个值）
    —— 没调这个工具的回复**交不出任何东西**（"整段推演被当成答案交上去"这类错误在结构上
    不可能发生）。
    """

    def setUp(self) -> None:
        self.agent = Agent()

    @staticmethod
    def _reply(answer: str) -> str:
        return (
            "<tool><tool_name>submitAnswer</tool_name>"
            f"<tool_param><answer>{answer}</answer></tool_param></tool>"
        )

    def _dispatch(self, reply: str) -> str:
        """派一遍这条回复里的工具调用（答卷变量就在这一行被写），返回要发的命令。"""
        return self.agent.tool_calls(tool_of(reply) or [])

    def test_the_answer_is_the_param_value(self):
        """答卷 = `answer` 参数的值（过反转义、只去首尾空白），不是整条回复。"""
        for reply, expected in (
            (self._reply("晴 26 度"), "晴 26 度"),
            (self._reply("a &lt; b"), "a < b"),
        ):
            with self.subTest(reply=reply):
                self._dispatch(reply)
                self.assertEqual(self.agent.answer, expected)

    def test_the_tool_itself_yields_no_command(self):
        """交卷不产出命令：返回值直接进响应顶层 `executeCmd` ⇒ 非空就是往沙盒丢一条
        不存在的命令。真正发指令的是 `game.task.answer_task`（开拓者）。"""
        self.assertEqual(self._dispatch(self._reply("晴")), "")
        self.assertEqual(self.agent.answer, "晴", "不产命令 ≠ 没交上来")

    def test_the_declared_param_lands_in_the_variable(self):
        """注册表声明的那一个参数名 = `submitAnswer` 的形参名。两处一旦分家，写对了名字的
        调用会在 `impl(**resolved)` 上抛 `TypeError`（那一行在 `try` 之外，而它跑在
        `app.handle` 的 `try` 里 ⇒ **整回合退化成空指令**）：本地全绿，只有实盘看得见。"""
        self.assertEqual(
            [pname for pname, _ in self.agent._tools["submitAnswer"][2]], ["answer"]
        )
        self._dispatch(self._reply("晴"))
        self.assertEqual(self.agent.answer, "晴")

    def test_nothing_else_writes_it(self):
        """没调 `submitAnswer` 的回复一概不写答卷变量：纯文本、空回复、别的工具块、旧的
        `<answer>` 标签全都算。"""
        for reply in (
            "晴 26 度",
            "",
            "<answer>晴 26 度</answer>",
            "<tool><tool_name>executeCmd</tool_name>"
            "<tool_param><cmd>ls</cmd></tool_param></tool>",
        ):
            with self.subTest(reply=reply):
                self._dispatch(reply)
                self.assertEqual(self.agent.answer, "")

    def test_a_parallel_call_still_submits(self):
        """沉淀 + 交卷同回合：两块并列 ⇒ 答案照写，谁在前谁在后都一样。"""
        sop = (
            "<tool><tool_name>SOP2Prompt</tool_name>"
            "<tool_param><name>方法</name><sop>先找文件</sop></tool_param></tool>"
        )
        for reply in (sop + self._reply("晴"), self._reply("晴") + sop):
            with self.subTest(reply=reply[:40]):
                self._dispatch(reply)
                self.assertEqual(self.agent.answer, "晴")

    def test_taking_it_clears_it(self):
        """取走即清（`answer_task` 用它交卷）：同一份答卷只交一次，重交靠 `llmResp` 粘住时
        把同一条工具调用再派一遍。"""
        self._dispatch(self._reply("晴"))
        self.assertEqual(self.agent.take_answer(), "晴")
        self.assertEqual(self.agent.answer, "", "取走之后不再是答案")
        self.assertEqual(self.agent.take_answer(), "", "空手再取还是空")


class ParallelToolTest(unittest.TestCase):
    """并列调用只放行白名单里那两个（都不产出命令、当回合也没有回执）。

    白名单外的并列**整轮作废**：一条都不派 —— 派一半出去，"哪条跑了"日志上都答不出来。
    """

    def setUp(self) -> None:
        self.agent = Agent()
        self.agent.chat("题")  # 作废的说明得有地方落

    def _notes(self) -> list[str]:
        """render 里标着「这次调用不成立」的那几条 tool 消息。"""
        return [
            c
            for c in (m["content"] for m in json.loads(self.agent.chat("题")))
            if "【工具调用：这次调用不成立】" in c
        ]

    def test_the_whitelisted_pair_goes_out(self):
        """`SOP2Prompt` + `submitAnswer`：逐条派出去，各自的出口各留一行。"""
        reply = (
            "<tool><tool_name>SOP2Prompt</tool_name>"
            "<tool_param><name>方法</name><sop>先找文件</sop></tool_param></tool>"
            "<tool><tool_name>submitAnswer</tool_name>"
            "<tool_param><answer>晴</answer></tool_param></tool>"
        )
        with self.assertLogs("coregeek.agent.agent", level="INFO") as caught:
            self.assertEqual(self.agent.tool_calls(tool_of(reply)), "")
        self.assertEqual(self.agent.pre_sop, {"方法": "先找文件"}, "沉淀照落库")
        self.assertEqual(len(caught.records), 2, "两条调用各留一行")

    def test_a_second_command_tool_voids_the_whole_round(self):
        """白名单外的并列 ⇒ 整轮作废：连那条合法的也不派，原因回灌进会话。"""
        reply = (
            "<tool><tool_name>executeCmd</tool_name>"
            "<tool_param><cmd>ls</cmd></tool_param></tool>"
            "<tool><tool_name>SOP2Prompt</tool_name>"
            "<tool_param><name>方法</name><sop>先找文件</sop></tool_param></tool>"
        )
        with self.assertLogs("coregeek.agent.agent", level="INFO") as caught:
            self.assertEqual(self.agent.tool_calls(tool_of(reply)), "")
        self.assertEqual(
            caught.records[0].getMessage(),
            "【工具调用】：executeCmd、SOP2Prompt ⇒ 整轮不成立（这几个不能并列）",
        )
        self.assertEqual(self.agent.pre_sop, {}, "作废那一轮连 SOP 也不落库")
        (note,) = self._notes()
        self.assertIn("并列", note)
        self.assertIn("executeCmd", note)

    def test_a_voided_round_writes_no_answer_either(self):
        """作废那一轮 `submitAnswer` 根本没被派到 ⇒ 答卷变量也没被写 —— "派了没有"与"有没有
        答案"因此是同一件事（第 136 步：判定只看那一个变量，解析侧不再有第二份判据）。"""
        reply = (
            "<tool><tool_name>executeCmd</tool_name>"
            "<tool_param><cmd>ls</cmd></tool_param></tool>"
            "<tool><tool_name>submitAnswer</tool_name>"
            "<tool_param><answer>晴</answer></tool_param></tool>"
        )
        self.assertEqual(self.agent.tool_calls(tool_of(reply)), "")
        self.assertEqual(self.agent.answer, "")

    def test_a_single_call_is_never_voided(self):
        """一条调用与白名单无关（白名单管的是"并列"）。"""
        self.assertEqual(
            self.agent.tool_calls(tool_of("<tool><tool_name>python_exec</tool_name>"
                                          "<tool_param><code>1+1</code></tool_param></tool>")),
            "",
        )


class ToolCallLogTest(unittest.TestCase):
    """`Agent.tool_call` 的四条出口各打一条 `【工具调用】`。

    实盘上"这一轮 LLM 到底调没调工具、为什么没调成"只有这几行能回答 —— 回复原文在任务行里，
    但原文看不出它有没有变成命令（`llmResp` 与 `executeCmd` 是两个字段）。
    """

    def setUp(self) -> None:
        self.agent = Agent()

    def test_the_log_names_the_tool_the_params_and_the_outcome(self):
        """派出去的调用记全文（工具名 + `名=值`）与出口：出命令 / 不产命令 / 无参数。"""
        self.agent._tools["查询状态"] = (lambda: "状态正常", "测试用：查个状态", ())
        with self.assertLogs("coregeek.agent.agent", level="INFO") as caught:
            self.agent.tool_call("executeCmd", [("cmd", "ls -la")])
            self.agent.tool_call("SOP2Prompt", [("name", "读题"), ("sop", "先 ls")])
            self.agent.tool_call("查询状态", [])
        self.assertEqual(
            [r.getMessage() for r in caught.records],
            [
                "【工具调用】：executeCmd（cmd=ls -la）⇒ 命令已出",
                "【工具调用】：SOP2Prompt（name=读题，sop=先 ls）⇒ 这个工具不产出命令",
                "【工具调用】：查询状态（无参数）⇒ 命令已出",
            ],
        )

    def test_a_call_that_never_reached_a_tool_says_why(self):
        """三条"调用不成立"各有各的话：那是 LLM 白等一回合的唯一线索。"""
        with self.assertLogs("coregeek.agent.agent", level="INFO") as caught:
            self.agent.tool_call("查不到的工具", [("cmd", "ls")])
            self.agent.tool_call("executeCmd", "ls")
            self.agent.tool_call("executeCmd", [("命令", "ls")])
        lines = "\n".join(r.getMessage() for r in caught.records)
        self.assertIn("未知工具「查不到的工具」⇒ 调用不成立", lines)
        self.assertIn("参数不是「名=值」的形状", lines)
        self.assertIn("executeCmd（命令=ls）⇒ 调用不成立（声明了的参数缺了或值为空白）", lines)

    def test_a_long_value_is_clipped_but_the_command_goes_out_whole(self):
        """日志里的值截到 `ARGS_LOG_MAX`（原文在任务行里）—— 发出去的命令一个字不动。"""
        cmd = "echo " + "x" * 1200
        with self.assertLogs("coregeek.agent.agent", level="INFO") as caught:
            self.assertEqual(self.agent.tool_call("executeCmd", [("cmd", cmd)]), cmd)
        self.assertIn(f"…（共 {len(cmd)} 字）", caught.records[0].getMessage())


class FailedCallFeedbackTest(unittest.TestCase):
    """「调用不成立」的回灌（第 110 步）：三种失败 + 形状没写对，LLM 都得在下一份
    prompt 里看见"这次调用没有发出去 + 原因"—— 没有它，这一轮它只看得见「请继续。」，
    会以为命令已在跑、等一个不会来的回执，逐字重发同一条失败调用就原地空转。
    """

    def setUp(self) -> None:
        self.agent = Agent()
        self.agent.chat("题")  # 开会话：说明得有地方落

    def _notes(self) -> list[str]:
        """render 里标着「这次调用不成立」的那几条 tool 消息。"""
        return [
            c
            for c in (m["content"] for m in json.loads(self.agent.chat("题")))
            if "【工具调用：这次调用不成立】" in c
        ]

    def test_each_failure_names_the_culprit(self):
        """三条不成立各有各的话：编的工具名 ⇒ 点名它并给出真名单（注册表现拼，不手写
        第二份）；缺参数 ⇒ 点名缺哪个、指回 Params；参数形状不对 ⇒ 指回调用格式。"""
        self.agent.tool_call("查不到的工具", [("cmd", "ls")])
        self.agent.tool_call("executeCmd", [])
        self.agent.tool_call("executeCmd", "ls")
        notes = self._notes()
        self.assertEqual(len(notes), 3, "三次失败三条说明，一条不少")
        self.assertIn("查不到的工具", notes[0])
        for name in ("executeCmd", "readSandboxFile", "python_exec", "SOP2Prompt"):
            self.assertIn(name, notes[0])
        self.assertIn("缺了参数 cmd", notes[1])
        self.assertIn("Params", notes[1])
        self.assertIn("<tool_param>", notes[2])

    def test_the_note_says_no_receipt_will_come(self):
        """共同的那句"没有发出去、不会有执行结果"是回执幻觉的解药，每条都得带上。"""
        self.agent.tool_call("executeCmd", [])
        (note,) = self._notes()
        self.assertIn("没有发出去", note)
        self.assertIn("不会有它的执行结果", note)

    def test_a_broken_shape_is_rejected_with_the_format(self):
        """`reject_shape`：日志一行（原文与第 109 步那条相同）+ 会话里重述调用格式。"""
        with self.assertLogs("coregeek.agent.agent", level="INFO") as caught:
            self.assertEqual(self.agent.reject_shape(), "")
        self.assertIn(
            "形状没写对（取不出工具名）⇒ 这一轮落重问",
            caught.records[0].getMessage(),
        )
        (note,) = self._notes()
        self.assertIn("<tool_name>", note)
        self.assertIn("<tool_param>", note)

    def test_without_a_session_the_note_is_dropped(self):
        """还没开过会话（这道题一次都没问过）⇒ 只留日志、说明丢弃，绝不抛。"""
        with self.assertLogs("coregeek.agent.agent", level="INFO"):
            self.assertEqual(Agent().tool_call("executeCmd", []), "")


class ReadSandboxFileTest(unittest.TestCase):
    """`readSandboxFile` 的三条分支：**本地命中** ⇒ 正文当回合进会话、返 `""`（省一回合）；
    **撞名**（文件名对上不止一份）⇒ 调用不成立、只回一条说明，本地明明有、是 `path` 没写全；
    **本地没有** ⇒ 派一条按文件名去沙盒里找它的命令（正文下一回合随回执回来）。
    整条全路径与只写它的文件名两种写法都收（第 105 步）。

    `_files` 住在 `cmd_explore` 的模块层 ⇒ 每个用例复位（既有先例，见那个文件）。
    """

    def setUp(self) -> None:
        cmd_explore.reset()
        self.addCleanup(cmd_explore.reset)
        self.agent = Agent()
        self.agent.chat("题")  # 开会话：命中的正文得有地方落

    def test_a_probed_file_lands_in_the_next_prompt(self):
        """命中：返回空串（不产命令）＋正文进会话 —— 下一份 prompt 里就看得见。

        真流程的顺序与 `task_channel` 一致：先 `hear` 记下那条调用，再 `tool_call`。
        """
        path = "/opt/task/one.md"
        cmd_explore._files[path] = "# 任务\n正文"
        self.agent.hear(f"<tool><tool_name>readSandboxFile</tool_name></tool>")
        self.assertEqual(self.agent.tool_call("readSandboxFile", [("path", path)]), "")
        contents = [m["content"] for m in json.loads(self.agent.chat("题"))]
        self.assertTrue(any(path in c and "正文" in c for c in contents), contents)

    def test_a_bare_file_name_reaches_the_body_too(self):
        """`path` 的第二种写法：只写文件名（第 105 步，用户口径"枚举值再把独立的文件名加上"）。

        题目里给的往往正是个文件名 —— 逼它先拼出一条全路径纯属白费一回合（实测吃过：
        它拼出来的目录沙盒里根本没有）。两种写法落到同一份正文、都不产命令。
        """
        cmd_explore._files["/opt/task/one.md"] = "正文"
        self.agent.hear("<tool><tool_name>readSandboxFile</tool_name></tool>")
        self.assertEqual(self.agent.tool_call("readSandboxFile", [("path", "one.md")]), "")
        contents = [m["content"] for m in json.loads(self.agent.chat("题"))]
        self.assertTrue(any("正文" in c and "one.md" in c for c in contents), contents)
        self.assertFalse(any("不在可选清单里" in c for c in contents), contents)

    def test_a_name_that_matches_two_files_asks_for_the_full_path(self):
        """文件名撞了 ⇒ 不成立，并**只把撞上的那几份**列出来（"你指的是哪一份"）。

        替它挑一份 = 把错的那份正文交出去而它看不出；指回整份清单也没用（它写字名时正是
        照清单抄的）。列撞上的那几份是能落地的那一句话。
        """
        cmd_explore._files["/opt/task/a.md"] = "甲"
        cmd_explore._files["/home/task/a.md"] = "乙"
        self.agent.hear("<tool><tool_name>readSandboxFile</tool_name></tool>")
        self.assertEqual(self.agent.tool_call("readSandboxFile", [("path", "a.md")]), "")
        contents = [m["content"] for m in json.loads(self.agent.chat("题"))]
        note = [c for c in contents if "对上了不止一份" in c]
        self.assertEqual(len(note), 1, "撞名要在会话里留一条说明")
        self.assertIn("/opt/task/a.md", note[0])
        self.assertIn("/home/task/a.md", note[0])
        self.assertFalse(any("甲" in c or "乙" in c for c in contents), "撞名时一份正文都不许交出去")

    def test_a_local_miss_is_looked_up_in_the_sandbox(self):
        """本地没有这份 ⇒ 派一条按**文件名**去沙盒里找它并打印的命令（正文随回执回来）。

        清单空着（探查还没跑完）与"清单里确实没这份"走同一条 —— "我们还没摸过"不等于
        "沙盒里没有"，两种都值得去问沙盒一趟。说明里明写"已经派了一条查找命令"：不说的话
        它会再自己发一条 executeCmd 找同一个文件（多烧一个往返）。
        """
        for probed in (None, "/opt/task/one.md"):
            with self.subTest(probed=probed):
                cmd_explore.reset()
                if probed:
                    cmd_explore._files[probed] = "正文"
                agent = Agent()
                agent.chat("题")
                agent.hear("<tool><tool_name>readSandboxFile</tool_name></tool>")
                cmd = agent.tool_call("readSandboxFile", [("path", "/opt/task/none.md")])
                self.assertIn("-name none.md", cmd, "按文件名找它")
                self.assertIn("/opt/task/none.md", cmd, "找不到时那行要点名是它")
                notes = [c for c in (m["content"] for m in json.loads(agent.chat("题")))
                         if "本地没有" in c]
                self.assertEqual(len(notes), 1, "派了命令要在会话里说一声")
                self.assertIn("下一回合", notes[0])

    def test_only_the_file_name_enters_the_find(self):
        """兜底命令的两处引号：`-name` 只拿**最后一段文件名**、整条路径进 `printf` 时过
        `shlex.quote` —— LLM 写的那串字一个裸词都不许落地（旧兜底把整条路径塞进 `cat`，
        第 101 步删掉它正是这个理由）。

        ⚠️ 只认文件名也意味着它拼出来的目录不作数（题目里给的往往就是个裸文件名）。
        """
        path = "/opt/task/x'; rm -rf *.md"
        cmd = self.agent.tool_call("readSandboxFile", [("path", path)])
        self.assertIn(f"-name {shlex.quote(cmd_explore.file_name(path))}", cmd)
        self.assertIn(shlex.quote(path), cmd)
        self.assertNotIn(path, cmd, "整条路径只能出现在引号里")

    def test_without_a_session_the_body_is_dropped(self):
        """还没开过会话（这道题一次都没问过）⇒ 只丢产出，绝不抛。"""
        cmd_explore._files["/opt/task/one.md"] = "正文"
        self.assertEqual(Agent().tool_call("readSandboxFile", [("path", "/opt/task/one.md")]), "")
        # 本地没有那条路不依赖会话：说明丢了、命令照返回
        self.assertNotEqual(Agent().tool_call("readSandboxFile", [("path", "/opt/none.md")]), "")

    def test_a_missing_or_blank_path_means_no_call(self):
        """声明的参数没给上 / 编的名字 ⇒ 不成立（既有闸门，这条钉的是真工具那一条）。"""
        self.assertEqual(self.agent.tool_call("readSandboxFile", []), "")
        self.assertEqual(self.agent.tool_call("readSandboxFile", [("文件", "/opt/task/one.md")]), "")

    def test_every_branch_says_so_in_the_log(self):
        """三条分支各留一行（本地命中 / 本地没有去沙盒找 / 撞名不成立）—— 实盘上
        "它用没用这个工具、为它跑过几趟沙盒"只有这三行能回答。"""
        with self.assertLogs(level="INFO") as caught:
            cmd_explore._files["/opt/task/one.md"] = "正文"
            cmd_explore._files["/home/task/one.md"] = "副本"
            self.agent.tool_call("readSandboxFile", [("path", "/opt/task/one.md")])
            self.agent.tool_call("readSandboxFile", [("path", "/opt/task/none.md")])
            self.agent.tool_call("readSandboxFile", [("path", "one.md")])
        hits = [line for line in caught.output if "【沙盒文件】" in line]
        self.assertEqual(len(hits), 3)
        self.assertTrue(any("取回" in line and "/opt/task/one.md" in line for line in hits))
        self.assertTrue(any("本地没有" in line and "/opt/task/none.md" in line for line in hits))
        self.assertTrue(any("不成立" in line for line in hits))


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
