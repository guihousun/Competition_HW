"""agent/prompt.py 的用例：system 段模板的组装（占位符填满、两个输出形状逐字在、
流程表渲染成 `## SopName - 名`）。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。⚠️ 用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import json
import sys
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用（discover 不一定把它放进 sys.path）；src/ 给 coregeek 用
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coregeek.agent import Agent  # noqa: E402
from coregeek.agent.chat import answer_of  # noqa: E402
from coregeek.agent.tools import sop  # noqa: E402


class ChatPromptTest(unittest.TestCase):
    """prompt 的组装 —— `agent/prompt.py` 的段模板（system）+ **累积的**会话记录（第 25 步起）。

    断言**逐字**钉住段头与两个形状的示例：它们是"LLM 照不照抄"的唯一杠杆，
    而 `str.format` 漏填一个占位符会让整段变成 `{tool_desc}` 这种字面量出现在 prompt 里
    —— 那种错在实盘上表现为"LLM 完全不按格式回"，本地却什么都看不出来。
    第 37 步起模板搬进 `prompt.py`（六段：role定位 / 工具描述 / 输出格式 / 示例（占位未启用）/
    沉淀的SOP / 注意事项），`Agent.chat` 每轮用它现刷 system。
    """

    def setUp(self) -> None:
        self.agent = Agent()

    def test_the_placeholders_are_all_filled(self):
        prompt = self.agent.chat("题目")
        for header in (
            "# 【ROLE定位】",
            "# 【工具描述】",
            "# 【输出约定】",
            "# 【沉淀的SOP】",
            "# 【输出示例】",
            "# 【注意事项】",
        ):
            self.assertIn(header, prompt)
        #: 会话记录是**拼接**出来的（不走 `str.format`），会漏的只有模板自己那两个槽
        for leftover in ("{tool_desc}", "{sop}"):
            self.assertNotIn(leftover, prompt)

    def test_every_tool_appears_in_the_prompt(self):
        """prompt 里的工具清单由**实例的工具表**生成 ⇒ 每个注册的工具都得在。"""
        prompt = self.agent.chat("题目")
        for name in self.agent._tools:
            self.assertIn(name, prompt)

    def test_the_role_section_teaches_depositing_environment_knowledge(self):
        """第 46 步（用户口径）：**探索到的环境知识（接口描述等）也要沉淀成 SOP**。

        会话窗口只留最近两轮、摘要是 best-effort ⇒ SOP 是跨回合**唯一保证还在**的
        记忆——不沉淀的发现过了窗口就丢。这条提示钉在 ROLE 段（沉淀的两个动词
        "流程 / 环境知识"都在），措辞就是产品，别改成同义词。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        role = system.split("# 【工具描述】")[0]
        self.assertIn("环境知识", role)
        self.assertIn("接口", role)
        self.assertIn("SOP2Prompt", role)

    def test_both_output_shapes_are_shown_verbatim(self):
        """两个形状（工具调用 / `<answer>`）**逐字**出现在模板里。

        这是唯一能提高"LLM 照抄概率"的杠杆：描述得含糊一点，它就自己发明第三种形状，
        而那种失败**本地测不出来**（我们的解析自洽，判题器认不认只有实盘知道）。
        第 37 步起工具形状是**嵌套式**（参数是 `<tool_param>` 里的具名元素）。
        """
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        self.assertIn(
            "<tool>\n    <tool_name>工具名</tool_name>\n    <tool_param>\n"
            "        <param1_name>工具参数1值</param1_name>\n"
            "        <param2_name>工具参数2值</param2_name>\n"
            "        ……\n    </tool_param>\n</tool>",
            system,
        )
        self.assertIn(
            "<tool>\n    <tool_name>executeCmd</tool_name>\n    <tool_param>\n"
            "        <cmd> cat /tmp/a.txt </cmd>\n    </tool_param>\n</tool>",
            system,
        )
        self.assertIn("<answer>答案本身</answer>", system)

    def test_the_sop_round_must_carry_the_answer(self):
        """prompt 里必须有"沉淀 SOP 与作答写在同一条回复里"这条**规则与示例**。

        它是省回合的唯一杠杆：代码侧早就支持同回合（`answer_of` + 判据 ⑤ + `_answer_task`
        三处共用同一个谓词，见
        `TaskChannelTest.test_sinking_the_sop_rides_along_with_the_answer`），
        但 prompt 不写的话，LLM 就按"一回合只输出一样东西"把沉淀单独占一回合 ——
        而那一回合在日志上看起来**完全正常**（有提问、无提交），只有分数会低。

        ⚠️ 示例是**两块**（工具块 + 块外的 `<answer>`）；第 37 步起工具块里是
        `<name>` + `<sop>` 两个参数（多流程口径：`name` 是流程名）。
        ⚠️ `sop` 里不许出现 `<answer>` 这对标签的**规则**也在这段里（用户口径）：
        它是 `chat.strip_answers` 在代码侧兜底的那条规矩，必须让 LLM 先知道。
        """
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        self.assertIn("它只沉淀、不产出命令", system)
        self.assertIn("同时采用sop沉淀工具格式和答案输出格式", system)
        self.assertIn(
            "    <tool>\n        <tool_name>SOP2Prompt</tool_name>\n        <tool_param>\n"
            "            <name> 流程名 </name>\n            <sop> xxx </sop>\n"
            "        </tool_param>\n    </tool>\n    <answer>答案本身</answer>",
            system,
        )
        self.assertIn("不要出现 `<answer>` 与 `</answer>` 这对标签", system)

    def test_the_attention_says_how_to_find_the_file(self):
        """`# 【注意事项】` 那几条 = 第 35 步「工作流」的重排（第 37 步换模板时并入），
        **附一段可以直接照抄的命令范式**。

        要治的病是"任务书一般不是完整路径"（任务信息里给的往往只是一个**文件名**），
        而旧措辞是一句散文式提醒 —— 落到 LLM 手里就是"先 `find`、下一回合再 `cat`"
        （两条命令 = 两回合 = 直接掉分）。范式把"找 + 读"写成一条：`find` 加
        `-maxdepth`/`2>/dev/null` 兜住沙盒 15 秒与 64KB 截断（`maxdepth 6` 是第 38 步
        用户手改的 —— 沙盒目录比范例预想的深）。

        ⚠️ 措辞是**拍的**，效果只能靠实盘（`code-task.md` 悬置表第 2 项）。
        """
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        self.assertIn("# 【注意事项】\n1. ", system)
        self.assertIn("f=$(find / -maxdepth 6 -name '*任务书*' -print -quit 2>/dev/null)", system)

    def test_the_task_text_is_there(self):
        self.assertIn("请查询北京天气", self.agent.chat("请查询北京天气"))

    def test_the_two_injections_only_appear_when_given(self):
        """回灌那两段"没有就不占地方"，而且**在第一次提问里一个都看不到**。"""
        plain = self.agent.chat("题目")
        self.assertNotIn("【上一条命令的执行结果", plain)
        self.assertNotIn("【你上一次提交的答案", plain)

        with_result = json.loads(self.agent.chat("题目", result="[exitCode:0]\nok"))
        self.assertIn("【上一条命令的执行结果（原文）】\n[exitCode:0]\nok", with_result[-1]["content"])

        with_retry = json.loads(self.agent.chat("题目", retry="晴 26 度"))
        self.assertIn("【你上一次提交的答案被判定为不正确】\n晴 26 度", with_retry[-1]["content"])

    def test_a_stored_flow_renders_with_its_name(self):
        """**这就是"自进化"的可观测证据**：存过一条流程之后，后面每一份 prompt 都带着
        `## SopName - 流程名` + 正文。空表打占位 —— 段头**永远都在**，那个槽是
        LLM 自己写的目标，看不见槽就不会去用它（第 18 步起的老规矩）。
        第 19 步起接线是**两个方法之间**的（`SOP2Prompt` 写 `self._sop`，`chat` 读它）
        —— 走的是实例，不是"某个模块变量还在"
        （跨回合那条更强的证据在 `TaskChannelTest.test_the_singleton_carries_the_sop_across_turns`）。
        """
        self.assertIn("（暂无沉淀）", json.loads(self.agent.chat("题目"))[0]["content"])
        self.agent.SOP2Prompt("找任务书", "先看目录再动手")
        system = json.loads(self.agent.chat("另一道题"))[0]["content"]
        self.assertIn("## SopName - 找任务书\n先看目录再动手", system)
        self.assertNotIn("（暂无沉淀）", system)

    def test_the_same_task_accumulates_its_conversation(self):
        """**同一个 task = 同一个上下文**（第 25 步的立身之本）：第二次提问里看得见
        题目、它自己的回复与回灌；第一次提问里则什么回复都还没有。"""
        first = json.loads(self.agent.chat("题"))
        self.agent.hear("<tool>ls</tool>")
        second = json.loads(self.agent.chat("题", result="[exitCode:0]\nok"))
        self.assertEqual([m["role"] for m in first], ["system", "user"])
        self.assertEqual(
            [(m["role"], m["content"]) for m in second][1:],
            [
                ("user", "题"),
                ("assistant", "<tool>ls</tool>"),
                ("tool", "【上一条命令的执行结果（原文）】\n[exitCode:0]\nok"),
            ],
        )

    def test_a_different_task_starts_a_fresh_conversation(self):
        """换题 ⇒ 新会话：旧题的往来一个字都不带过来（身份判据 = **题目原文**）。"""
        self.agent.chat("甲题")
        self.agent.hear("甲题的回复")
        prompt = self.agent.chat("乙题")
        self.assertIn("乙题", prompt)
        self.assertNotIn("甲题", prompt)

    def test_a_reask_round_appends_the_standing_line(self):
        """没有新内容的重问轮（畸形回复 / `SOP2Prompt` 之后）⇒ 追加「请继续。」：
        判题器的 LLM 是黑盒，会话停在它自己的输出上是个含糊指令。首问则不需要。"""
        first = self.agent.chat("题")
        self.assertNotIn("请继续。", first)
        self.agent.hear("<tool ls")
        self.assertIn("请继续。", self.agent.chat("题"))

    def test_reset_clears_the_conversation_too(self):
        """`reset` 是用例隔离的唯一手段（单实例换不掉）⇒ 会话必须一起清，
        否则上一用例的往来会灌进这一用例的 prompt。"""
        self.agent.chat("题")
        self.agent.hear("上一场的回复")
        self.agent.reset()
        self.assertEqual(
            [m["role"] for m in json.loads(self.agent.chat("题"))], ["system", "user"]
        )

    def test_the_system_is_refreshed_every_round(self):
        """system（段模板）**每轮现刷**：同一道题进行中沉淀的流程，下一轮就看得见
        —— 这是 ③′（`SOP2Prompt` 不产出命令）"调用成功"的回执，冻在构造时就没了。"""
        first = self.agent.chat("题")
        self.agent.SOP2Prompt("找文件", "先 ls")
        second = self.agent.chat("题")
        self.assertNotIn("先 ls", first)
        self.assertIn("先 ls", second)

    def test_braces_in_the_values_are_not_scanned_again(self):
        """`str.format` **只做一次** —— 替换值里的 `{}` 不能被当成占位符。

        题目原文与沙盒输出都是**任意文本**，python 代码片段里 `{}` 太常见了；
        二次扫描会在 `chat` 里直接抛 `KeyError`/`IndexError` ⇒ 整回合退化成空指令。
        流程正文是第三个替换值（由 `Agent` 传进 `gen_system_prompt`），三处一起钉；
        会话正文则走 `json.dumps`，与 `format` 无关。"""
        self.agent.SOP2Prompt("带花括号", "SOP 里有 {sop} 和 {0}")
        messages = json.loads(self.agent.chat("题目 {task} {0} {}", result="{'a': 1}"))
        contents = [m["content"] for m in messages]
        self.assertIn("题目 {task} {0} {}", contents)
        self.assertIn("【上一条命令的执行结果（原文）】\n{'a': 1}", contents)
        self.assertIn("SOP 里有 {sop} 和 {0}", messages[0]["content"])


    def test_the_task_prompt_carries_no_compression_teaching(self):
        """第 41 步：任务 prompt 卸掉压缩教学 —— 压缩有自己的 prompt（在命令轮随
        `executeCmd` 同发，那个槽本来空着）。任务 prompt 专注任务，压缩 prompt 专注压缩。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        self.assertNotIn("<summary>", system)
        self.assertNotIn("【总目标】", system)

    def test_the_compression_request_carries_the_instruction_and_material(self):
        """压缩请求 = **独立指令**（四槽 + 只输出摘要块）+ **原始上下文全文**。

        原料永远是原文（用户拍板）：压缩从原文重来、不从旧摘要叠 —— 避免多次压缩的
        失真累积；给任务 LLM 的才是压缩后的。"""
        self.agent.chat("题目")
        self.agent.hear("回复1")
        req = self.agent.compression_request()
        self.assertIn("【上下文压缩】", req)
        self.assertIn("【总目标】", req)
        self.assertIn("【关键数据】", req)
        self.assertIn("回复1", req)
        self.assertIn("题目", req)


if __name__ == "__main__":
    unittest.main()
