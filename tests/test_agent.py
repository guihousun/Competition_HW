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

from coregeek.agent import AGENT, Agent, cmd_explore  # noqa: E402
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
        self.assertIn("- Params:\n    - cmd: 需要在沙盒中执行的完整命令原文", desc)
        self.assertIn("- Params:\n    - name: ", desc)
        self.assertIn("    - sop: ", desc)  # 用途是措辞、会改；钉的是"第二个参数叫 sop"
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

    def test_the_sop_tool_teaches_reuse_and_a_generic_name(self):
        """描述里要有两层：① 沉淀是为了下次同类任务**少做探索**；
        ② `name` 要泛化到"一类问题"（「订去某地的机票的流程」），不能写死成本次的目标。

        第 107 步起"什么时候存、存成什么名"整套细则都在这条描述里（旧的【沉淀规则】段
        随用户重写的 prompt 删除），这一段就是沉淀规则的唯一出口。"""
        desc = gen_all_tool_prompt(self.agent._tools)
        block = desc.split("## ToolName - SOP2Prompt", 1)[1].split("## ToolName", 1)[0]
        self.assertIn("能够少做探索", block)
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
    """`readSandboxFile`：`path` 是**枚举值** —— 只有探明过的那几份（现挂在工具描述里的
    那份清单）允许调用，写整条全路径、或只写它的文件名都行（文件名对上不止一份 ⇒ 不成立，
    第 105 步）；命中就当回合把正文送进会话（省一回合），其余一律"调用不成立"、不发命令。

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

    def test_an_unprobed_path_is_not_a_call(self):
        """清单以外的路径 ⇒ 调用不成立（不产命令）＋回一条说明。说明**不重抄清单**（第 104
        步）：清单就挂在 `readSandboxFile` 的描述里，与它会话里这条说明同处一份 prompt。

        ⚠️ 别退回"拼一条 `cat` 交给沙盒"（第 101 步删掉的旧支）：LLM 编出来的路径（比如
        题目里只给了文件名、它自己拼了个目录）那趟必然报错，白烧一个沙盒往返还引它接着猜。
        """
        cmd_explore._files["/opt/task/one.md"] = "正文"
        self.agent.hear("<tool><tool_name>readSandboxFile</tool_name></tool>")
        self.assertEqual(
            self.agent.tool_call("readSandboxFile", [("path", "/opt/task/none.md")]), ""
        )
        note = [c for c in (m["content"] for m in json.loads(self.agent.chat("题")))
                if "不在可选清单里" in c]
        self.assertEqual(len(note), 1, "未命中要在会话里留一条说明")
        self.assertIn("/opt/task/none.md", note[0])    # 点明是哪一次调用
        self.assertIn("描述里列出的那些", note[0])      # 指回枚举值的唯一出处
        self.assertNotIn("- /opt/task/one.md", note[0])  # 清单不在这里重抄一份

    def test_an_empty_inventory_points_at_execcmd(self):
        """一份都没探明（探查还没跑完）⇒ 说明里让它自己用 executeCmd 读。

        "我们还没摸过"不等于"沙盒里没有"（与清单空着时连工具块一起缺席同一条）：这里回一句
        "沙盒里就这些"，LLM 就不会再去读它本来需要的那份文件了。
        """
        self.agent.hear("<tool><tool_name>readSandboxFile</tool_name></tool>")
        self.agent.tool_call("readSandboxFile", [("path", "/opt/task/none.md")])
        note = [c for c in (m["content"] for m in json.loads(self.agent.chat("题")))
                if "不在可选清单里" in c]
        self.assertEqual(len(note), 1)
        self.assertIn("executeCmd", note[0])

    def test_a_path_never_becomes_a_command(self):
        """LLM 给的字符串不再进任何命令（第 101 步）：带引号/分号的路径照样只是"不成立"。

        旧支那句 `shlex.quote` 连同拼命令一起删了 ⇒ 命令注入面随之消失，留它一条守门。
        """
        path = "/opt/task/a b'; rm -rf /.md"
        self.agent.hear("<tool><tool_name>readSandboxFile</tool_name></tool>")
        self.assertEqual(self.agent.tool_call("readSandboxFile", [("path", path)]), "")
        contents = [m["content"] for m in json.loads(self.agent.chat("题"))]
        self.assertTrue(any(path in c for c in contents), contents)  # 原样带回去、没被转义

    def test_without_a_session_the_body_is_dropped(self):
        """还没开过会话（这道题一次都没问过）⇒ 只丢产出，绝不抛。"""
        cmd_explore._files["/opt/task/one.md"] = "正文"
        self.assertEqual(Agent().tool_call("readSandboxFile", [("path", "/opt/task/one.md")]), "")

    def test_a_missing_or_blank_path_means_no_call(self):
        """声明的参数没给上 / 编的名字 ⇒ 不成立（既有闸门，这条钉的是真工具那一条）。"""
        self.assertEqual(self.agent.tool_call("readSandboxFile", []), "")
        self.assertEqual(self.agent.tool_call("readSandboxFile", [("文件", "/opt/task/one.md")]), "")

    def test_both_branches_say_so_in_the_log(self):
        """两条分支都留痕 —— 实盘上"LLM 用没用这个工具、命中过几次"只有这两行能回答。"""
        with self.assertLogs(level="INFO") as caught:
            cmd_explore._files["/opt/task/one.md"] = "正文"
            self.agent.tool_call("readSandboxFile", [("path", "/opt/task/one.md")])
            self.agent.tool_call("readSandboxFile", [("path", "/opt/task/none.md")])
        hits = [line for line in caught.output if "【沙盒文件】" in line]
        self.assertEqual(len(hits), 2)
        self.assertTrue(any("/opt/task/one.md" in line for line in hits))
        self.assertTrue(any("/opt/task/none.md" in line for line in hits))


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
