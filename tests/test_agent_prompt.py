"""agent/prompt.py 的用例：system 段模板的组装（占位符填满、两个输出形状逐字在、
流程表渲染成 `## SopName - 名`）。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
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
    """prompt 的组装 —— `agent/prompt.py` 的段模板（system）+ 累积的会话记录。

    断言逐字钉住段头与两个形状的示例：它们是"LLM 照不照抄"的唯一杠杆；`str.format`
    漏填一个占位符会让 `{tool_desc}` 这种字面量出现在 prompt 里 —— 实盘上表现为
    "LLM 完全不按格式回"，本地却什么都看不出来。`Agent.chat` 每轮用模板现刷 system。
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
        # 会话记录是拼接出来的（不走 `str.format`），会漏的只有模板自己那两个槽
        for leftover in ("{tool_desc}", "{sop}"):
            self.assertNotIn(leftover, prompt)

    def test_every_tool_appears_in_the_prompt(self):
        """prompt 里的工具清单由实例的工具表生成 ⇒ 每个注册的工具都得在。"""
        prompt = self.agent.chat("题目")
        for name in self.agent._tools:
            self.assertIn(name, prompt)

    def test_the_role_section_teaches_depositing_environment_knowledge(self):
        """探索到的环境知识（接口描述等）也要沉淀成 SOP。

        会话窗口只留最近两轮、摘要是 best-effort ⇒ SOP 是跨回合唯一保证还在的记忆 ——
        不沉淀的发现过了窗口就丢。措辞就是产品，别改成同义词。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        role = system.split("# 【工具描述】")[0]
        self.assertIn("环境知识", role)
        self.assertIn("接口", role)
        self.assertIn("SOP2Prompt", role)

    def test_the_sop_section_tells_it_to_look_here_first(self):
        """【沉淀的SOP】段要教"先查这里、命中就直接照做、不必重新探索"。

        沉淀的全部回报就在这一条上：第二次遇到同类任务时把整个探索过程省掉。
        只写"可以参考下面的SOP执行"，LLM 会照样从头摸一遍，SOP 白存。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        sop = system.split("# 【沉淀的SOP】")[1].split("# 【输出示例】")[0]
        self.assertIn("动手前先看这里", sop)
        self.assertIn("不必重新探索", sop)

    def test_the_example_shows_a_deposit_then_a_reuse(self):
        """【输出示例】有两例，第二例是 few-shot：同一类任务演两遍 —— 第一次
        「探索 → 调接口 → 沉淀与作答同回合」，第二次「翻 SOP → 跳过探索直接照做」。

        ② 是**步骤形式**（step1. …）并且把每遍花掉几个回合点出来：这是给 LLM 看的，
        要教的是"回合怎么花"，两份流程的回合差（4 → 3）本身就是那条规则。
        每一步还给到**具体命令与它的输出**（示例一 step1 那条 find+cat 同理）—— 摘要式的一句
        "读任务书"教不会它怎么写命令。**`<sop>` 正文收什么、不收什么**（用户手改口径）：
        接口定义与参数定义（接口地址、参数名与含义、调用成功返回什么）**要收** —— 那正是
        第二次能跳过试探的原因；**本次的取值**（这次的目的地、这次拿到的凭证）不收。
        用例两向都钉住，免得再被谁抽象成一句"照文档做"的空话。
        顺带钉示例自身的自洽：里面的 `<sop>` 正文不能出现 `<answer>` 对（否则 LLM 照抄，
        入库时被 `strip_answers` 静默吃掉）。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        example = system.split("# 【输出示例】")[1].split("# 【注意事项】")[0]
        second = example.split("示例二")[1]
        rerun = second.rsplit("第二次：", 1)[1]  # 第二遍（"第二次"在段头标题里也出现过）
        self.assertIn("第一次", second)
        self.assertIn("第二次", second)
        self.assertIn("<tool_name>SOP2Prompt</tool_name>", second)
        self.assertIn("订去某地的机票的流程", second)
        self.assertIn("一共 4 个回合", second)
        self.assertIn("step3.", rerun)
        # 每一步给到具体命令与输出（不是"读任务书"这种摘要），答案用当次那个 token
        self.assertIn("f=$(find / -maxdepth 6 -name 'task.md' -print -quit); cat \"$f\"", second)
        self.assertIn("<answer>tk_9f3a7c</answer>", second)
        self.assertIn("tk_7a2b1c", rerun)
        # 示例一那条 find+cat 也点出来了
        self.assertIn("f=$(find / -maxdepth 6 -name 'problem.txt' -print -quit)", example)
        stored = second.split("<sop>")[1].split("</sop>")[0]
        self.assertNotIn("<answer>", stored)
        # 要收：接口定义与参数定义（第二次照它直接调，省掉试探那一趟）
        for kept in ("xxxx:xxx/xxx/yyy", "zzzz", "token"):
            self.assertIn(kept, stored, f"SOP 正文该带上接口定义：{kept}")
        # 不收：只对本次成立的取值
        for specific in ("北京", "上海", "tk_9f3a7c", "api.md"):
            self.assertNotIn(specific, stored, f"SOP 正文夹带了本次的取值：{specific}")

    def test_the_role_section_pins_the_name_to_a_class_of_tasks(self):
        """`name` 要凝练到"一类问题"上（「订去某地的机票的流程」，不是「订去上海的机票」）。

        名字写死成这一次的目标，下次同类任务就撞不上它 —— SOP 等于白存，而这条错了
        本地一点异常都看不出来（存是存进去了，只是永远复用不到）。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        role = system.split("# 【工具描述】")[0]
        self.assertIn("一类问题", role)
        self.assertIn("订去某地", role)

    def test_the_role_section_pins_the_body_to_a_generic_flow(self):
        """SOP **正文**也要通用：写"这一类任务怎么做"，不夹带只对**本次**成立的东西。

        名字泛化只挡住一半，正文照样能把"这次的目标值、这次拿到的凭证"带进去 —— 条目是
        整场存活、跨任务复用的，下一次同类任务会照着一条过期的取值去做，**而它看不出
        那条已经过期**（用户手改口径：接口定义/参数定义要收，本次的取值不收）。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        role = system.split("# 【工具描述】")[0]
        self.assertIn("正文也必须是通用的", role)
        self.assertIn("不要夹带只对本次成立的东西", role)

    def test_the_role_section_pins_the_deposit_timing(self):
        """沉淀的时机 = 【沉淀的SOP】段里还没有这条经验 —— "值不值得"不再是门槛，
        只要没沉淀过就存；存过的不要重复存（拖到完成任务才存，任务超时经验就丢了）。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        self.assertIn("还没出现在下面的【沉淀的SOP】段", system)
        self.assertIn("已经沉淀过的不要重复存", system)
        self.assertNotIn("当认为解题流程值得沉淀时", system, "旧措辞把判断权丢给'值不值得'")
        self.assertIn("当要沉淀且同回合要交答案时", system)
        self.assertNotIn("当完成任务且认为流程可沉淀时", system)

    def test_both_output_shapes_are_shown_verbatim(self):
        """两个形状（工具调用 / `<answer>`）逐字出现在模板里。

        这是唯一能提高"LLM 照抄概率"的杠杆：描述得含糊一点，它就自己发明第三种形状，
        而那种失败本地测不出来（我们的解析自洽，判题器认不认只有实盘知道）。
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
        """prompt 里必须有"沉淀 SOP 与作答写在同一条回复里"这条规则与示例 ——
        不写的话 LLM 就按"一回合只输出一样东西"把沉淀单独占一回合（日志上看着完全
        正常，只有分数会低）。`sop` 里不许出现 `<answer>` 标签的规则也在这段里：
        它是 `chat.strip_answers` 在代码侧兜底那条规矩，必须让 LLM 先知道。
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
        """`# 【注意事项】` 从第 1 条起编号、并附一段可以直接照抄的命令范式。

        任务信息里给的往往只是一个文件名 ⇒ 散文式提醒落到 LLM 手里就是"先 `find`、
        下一回合再 `cat`"（两条命令 = 两回合 = 直接掉分）。范式把"找 + 读"写成一条，
        现在挂在第 2 条的**成本模型**后面当例子（成本模型本身见
        `test_the_attention_prices_a_round_and_packs_the_command`），措辞是拍的。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        self.assertIn("# 【注意事项】\n1. ", system)
        self.assertIn("f=$(find / -maxdepth 6 -name '*任务书*' -print -quit 2>/dev/null)", system)

    def test_the_attention_prices_a_round_and_packs_the_command(self):
        """回合是有价的、而且能被"一条胖命令"省下来 —— 措辞必须说透这件事。

        协议的成本是死的：一次沙盒往返之后，LLM 的下一步动作要等**两个回合**（发命令那轮
        拿不到回执、回执要下一轮才进 prompt）。所以"一个参数一个参数地试"是最贵的做法：
        5 个候选 = 10 个回合，而压进一条命令 = 2 个回合。判据是资源约束（"一次调用 = 一个
        回合"），不是"遇到 A 就做 B"的流程 —— 后者才是过拟合。这条错了本地一点异常都没有，
        只是分数低（日志上数 `executeCmd` 的条数才看得出来）。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        attention = system.split("# 【注意事项】")[1]
        self.assertIn("一次工具调用就是一个回合", attention)
        self.assertIn("都属于同一条命令", attention)
        self.assertIn("试完 5 个候选", attention)
        self.assertIn("拆成 5 条就是 10 个回合", attention)
        # 产出是写给下一轮的自己读的：不带标签就分不清哪条结果对哪次尝试
        self.assertIn("自己带标签", attention)

    def test_the_attention_says_a_failure_is_a_clue(self):
        """失败的回执要**读**：报错里的字段名 / 缺什么 / 合法取值，直接指向下一次该试什么。

        第一次尝试偏掉之后的默认行为是"换一个参数再来一遍"，一换就是两个回合 —— 而报错里
        往往已经把答案写着了（"未知参数 x" ⇒ 参数名错；"值非法" ⇒ 值错）。不点破这一点，
        LLM 会把每条回执当成二元的"成 / 不成"，然后一个接一个地穷举。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        attention = system.split("# 【注意事项】")[1]
        self.assertIn("失败的回执是线索，不是噪音", attention)
        self.assertIn("换下一个候选之前先把它读懂", attention)

    def test_the_attention_says_to_see_the_environment_before_guessing(self):
        """信息不足时先看清环境：一条命令问清"有哪些文件、哪份是接口文档、有没有验证脚本"。

        这是"第一次尝试就偏"的正面对策 —— 偏的成因多半是**信息不足就动手**（照着一份可能
        写错的文档猜参数）。成本账：看清环境 = 一条命令，猜错一次 = 两个回合才拿回反馈。
        与 ROLE 段那句"信息不足就调用工具去取"是一件事的两面：那边讲该不该取，这边讲
        **第一趟就把要用的都取齐**。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        attention = system.split("# 【注意事项】")[1]
        self.assertIn("动手之前先看清环境", attention)
        self.assertIn("一条命令就能把这些一次问清", attention)

    def test_the_attention_says_to_follow_the_task_book_hints(self):
        """任务书点到的文件 / 接口 / 脚本就是探索的路线，不许绕开自己另找路子。

        这是用户报的"第一次尝试会偏"的原话：偏的是**探索方向** —— 任务书写着"需求在 spec.md、
        用 check.sh 验证"，它却绕开这两样自己猜要做什么、自己另定一套验证标准。与第 5 条
        （看清环境）分工：那条问"手边还有什么"（任务书之外的），这条管"它点到的一律走完"。
        代价同样是回合：绕一圈回来，那两个回合的反馈照样得付。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        attention = system.split("# 【注意事项】")[1]
        self.assertIn("任务书给的线索就是这一趟的路线", attention)
        self.assertIn("一项不落地读完、跑到", attention)
        self.assertIn("别自己另定一套标准", attention)

    def test_the_compression_keeps_the_failed_tries(self):
        """压缩请求要明说"试过并失败的也列上"。

        渲染窗口只留最近两轮（`Context._WINDOW`），试错一长，早先的尝试就掉出窗口 ——
        摘要（压缩轮的产物）是唯一还记得"哪些路已经走死"的地方。不点破这一点，压缩器会
        只留成功经验，"反复试同一条死路"就是它漏记的直接后果。"""
        self.agent.chat("题目")
        req = self.agent.compression_request()
        self.assertIn("试过并且失败", req)
        self.assertIn("反复试同一条死路", req)

    def test_the_role_section_pins_the_deposit_to_what_actually_worked(self):
        """沉淀以**实测**为准：文档可能过时或写错，存的是跑通的那一版。

        "文档写的是某个参数、实际要的是另一个"正是试错任务最值钱的一条 —— 而 LLM 天然只
        记成功经验、不记"文档错了"这件事。不写这一句，第一个任务白试、后面每个同类任务
        再白试一遍（SOP 是整场跨任务的，这条结论对它才是资产）。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        role = system.split("# 【工具描述】")[0]
        self.assertIn("以实测为准", role)
        self.assertIn("跑通的那一版", role)

    def test_the_task_text_is_there(self):
        self.assertIn("请查询北京天气", self.agent.chat("请查询北京天气"))

    def test_the_two_injections_only_appear_when_given(self):
        """回灌那两段"没有就不占地方"，而且在第一次提问里一个都看不到。"""
        plain = self.agent.chat("题目")
        self.assertNotIn("【上一条命令的执行结果", plain)
        self.assertNotIn("【你上一次提交的答案", plain)

        with_result = json.loads(self.agent.chat("题目", result="[exitCode:0]\nok"))
        self.assertIn("【上一条命令的执行结果（原文）】\n[exitCode:0]\nok", with_result[-1]["content"])

        with_retry = json.loads(self.agent.chat("题目", retry="晴 26 度"))
        self.assertIn("【你上一次提交的答案被判定为不正确】\n晴 26 度", with_retry[-1]["content"])

    def test_a_stored_flow_renders_with_its_name(self):
        """存过一条流程之后，后面每一份 prompt 都带着 `## SopName - 流程名` + 正文；
        空表打占位 —— 段头永远都在，那个槽是 LLM 自己写的目标，看不见槽就不会去用它。
        跨回合那条更强的证据在
        `TaskChannelTest.test_the_singleton_carries_the_sop_across_turns`。
        """
        self.assertIn("（暂无沉淀）", json.loads(self.agent.chat("题目"))[0]["content"])
        self.agent.SOP2Prompt("找任务书", "先看目录再动手")
        system = json.loads(self.agent.chat("另一道题"))[0]["content"]
        self.assertIn("## SopName - 找任务书\n先看目录再动手", system)
        self.assertNotIn("（暂无沉淀）", system)

    def test_the_same_task_accumulates_its_conversation(self):
        """同一个 task = 同一个上下文：第二次提问里看得见题目、它自己的回复与回灌；
        第一次提问里则什么回复都还没有。"""
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
        """换题 ⇒ 新会话：旧题的往来一个字都不带过来（身份判据 = 题目原文）。"""
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
        """system（段模板）每轮现刷：同一道题进行中沉淀的流程，下一轮就看得见
        —— 这是 ③′（`SOP2Prompt` 不产出命令）"调用成功"的回执，冻在构造时就没了。"""
        first = self.agent.chat("题")
        self.agent.SOP2Prompt("找文件", "先 ls")
        second = self.agent.chat("题")
        self.assertNotIn("先 ls", first)
        self.assertIn("先 ls", second)

    def test_braces_in_the_values_are_not_scanned_again(self):
        """`str.format` 只做一次 —— 替换值里的 `{}` 不能被当成占位符。

        题目原文、沙盒输出与流程正文都是任意文本，`{}` 太常见；二次扫描会在 `chat` 里
        直接抛 `KeyError`/`IndexError` ⇒ 整回合退化成空指令。三处一起钉。"""
        self.agent.SOP2Prompt("带花括号", "SOP 里有 {sop} 和 {0}")
        messages = json.loads(self.agent.chat("题目 {task} {0} {}", result="{'a': 1}"))
        contents = [m["content"] for m in messages]
        self.assertIn("题目 {task} {0} {}", contents)
        self.assertIn("【上一条命令的执行结果（原文）】\n{'a': 1}", contents)
        self.assertIn("SOP 里有 {sop} 和 {0}", messages[0]["content"])


    def test_the_task_prompt_carries_no_compression_teaching(self):
        """任务 prompt 不带压缩教学 —— 压缩有自己的 prompt（在命令轮随
        `executeCmd` 同发）。任务 prompt 专注任务，压缩 prompt 专注压缩。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        self.assertNotIn("<summary>", system)
        self.assertNotIn("【总目标】", system)

    def test_the_compression_request_carries_the_instruction_and_material(self):
        """压缩请求 = 独立指令（四槽 + 只输出摘要块）+ 原始上下文全文。

        原料永远是原文：压缩从原文重来、不从旧摘要叠 —— 避免多次压缩的失真累积；
        给任务 LLM 的才是压缩后的。"""
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
