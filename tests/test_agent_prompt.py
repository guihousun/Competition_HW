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

from coregeek.agent import Agent, cmd_explore  # noqa: E402
from coregeek.agent.chat import answer_of  # noqa: E402
from coregeek.agent.tools import sop  # noqa: E402

#: system 的段头，按 `prompt.gen_system_prompt` 那份列表的顺序 —— 段名只在这里写一次，
#: 切段一律走 `_section`。
#: 段头改名/换序时改这一处，用例不会退化成 IndexError。
#: 末段（COT 触发语）**没有段头** ⇒ 不在这张表里，切最后一段时它会跟着前一段一起出来。
SECTIONS = (
    "# 【背景】",
    "# 【ROLE定位】",
    "# 【工作原则】",
    "# 【工具描述】",
    "# 【沉淀的SOP】",
    "# 【输出约定】",
)


def _sop_tool_block(system: str) -> str:
    """`SOP2Prompt` 那个工具块的正文。

    沉淀的全部细则（什么值得存、怎么泛化、去重与更新、以实测为准）在这里 —— 第 107 步起
    不再单独占 system 的一段（旧【沉淀规则】段随用户重写的 prompt 删除）。
    """
    return _section(system, "# 【工具描述】").split("## ToolName - SOP2Prompt", 1)[1]


def _section(system: str, header: str) -> str:
    """切出一段：段头之后、下一个段头之前。段名写错/那一段没出现时当场点名是哪一个。"""
    if header not in system:
        raise AssertionError(f"system 里没有这一段：{header}")
    start = system.index(header) + len(header)
    rest = min(
        (i for h in SECTIONS if h != header and (i := system.find(h)) > start),
        default=len(system),
    )
    return system[start:rest]


class ChatPromptTest(unittest.TestCase):
    """prompt 的组装 —— `agent/prompt.py` 的段模板（system）+ 累积的会话记录。

    断言逐字钉住段头与两个形状的示例：它们是"LLM 照不照抄"的唯一杠杆；`str.format`
    漏填一个占位符会让 `{tool_desc}` 这种字面量出现在 prompt 里 —— 实盘上表现为
    "LLM 完全不按格式回"，本地却什么都看不出来。`Agent.chat` 每轮用模板现刷 system。
    """

    def setUp(self) -> None:
        self.agent = Agent()
        # 探查的已知路径表也是跨回合状态（模块级）⇒ 不清会跨用例串味
        cmd_explore.reset()
        self.addCleanup(cmd_explore.reset)

    def test_the_placeholders_are_all_filled(self):
        prompt = self.agent.chat("题目")
        for header in SECTIONS:
            self.assertIn(header, prompt)
        # 会话记录是拼接出来的（不走 `str.format`），会漏的只有模板自己那两个槽
        for leftover in ("{tool_desc}", "{sop}"):
            self.assertNotIn(leftover, prompt)

    def test_the_sections_appear_once_each_and_in_the_declared_order(self):
        """带段头的七段按声明的顺序出现、每段头只出现一次（末段的 COT 触发语没有段头）。

        段序就是四层的落地（决策 → 工具 → 知识 → 输出）；「只一次」是"同一条规则只写
        一处"的机械保证 —— 重复的规则会稀释注意力，而这件事在实盘上测不出来。
        【背景】段还必须是整份 system 的第一个字节（`test_app` 的日志链路按它断言，
        它前面不许有空白 —— 段与段之间靠 `gen_system_prompt` 的 join 排版）。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        self.assertTrue(system.startswith(SECTIONS[0]))
        positions = [system.index(h) for h in SECTIONS]
        self.assertEqual(positions, sorted(positions), "段序与 SECTIONS 声明的不一致")
        for header in SECTIONS:
            self.assertEqual(system.count(header), 1, f"这一段出现了不止一次：{header}")

    def test_the_system_stays_within_its_budget(self):
        """整份 system 的字数有上限 —— 它**每回合都发**。

        守的是"措辞只增不减"的漂移：每次加一句话都看不出什么，几十次之后 prompt 就
        被稀释得没法看了。阈值是拍的：顶格那一档实测 11800，上浮约 7%。
        基线数字会漂，量的时候看是**哪一档**：第 128 步实测干净 system **6753**
        （第 116 步删【输出示例】后是 6718、删段前 7965；中间的差来自 `[路径清单]` 措辞与工具块的
        `###` 小标题）；沙箱清单每多探明一条路径 **+29**（第一条 +49，含清单头），
        满 SOP（5 × `SOP_MAX`(1000)）再 **+5082** ⇒ **顶格 11800**。要加内容先删同等量级的
        旧话，或者改这个阈值并说明理由。
        """
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        self.assertLess(len(system), 12600)

    def test_every_tool_appears_in_the_prompt(self):
        """prompt 里的工具清单由实例的工具表生成 ⇒ 每个注册的工具**每轮都在**。

        工具块恒在（第 107 步改的口径）：`readSandboxFile` 在清单空着时也只是**不挂清单那几行**，
        块本身照列 —— 它自己的描述里点过别的工具的名（"已知文件用 readSandboxFile"），藏起来
        就成了一条悬空指引；描述里也已经交代了清单为空时怎么办（改用 executeCmd 自己找）。
        旧口径（一份都没探明就整块藏起来）随用户重写的 prompt 作废。
        断言按**整份 prompt** 查这个名字（不只是工具块）。"""
        empty = self.agent.chat("题目")
        for name in self.agent._tools:
            self.assertIn(name, empty)
        self.assertNotIn("/opt/task", empty, "还没探明 ⇒ 清单那几行不许凭空出现")

        cmd_explore._files["/opt/task/one.md"] = "正文"
        probed = self.agent.chat("题目")
        for name in self.agent._tools:
            self.assertIn(name, probed)
        self.assertIn("/opt/task/one.md", probed)

    def test_the_deposit_rules_cover_environment_knowledge(self):
        """探索到的环境知识（接口、参数、路径、返回值）也要沉淀成 SOP。

        会话窗口只留最近两轮、摘要是 best-effort ⇒ SOP 是跨回合唯一保证还在的记忆 ——
        不沉淀的发现过了窗口就丢。措辞就是产品，别改成同义词。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        rules = _sop_tool_block(system)
        self.assertIn("环境知识", rules)
        self.assertIn("接口", rules)
        self.assertIn("SOP2Prompt", rules)

    def test_the_sop_section_tells_it_to_look_here_first(self):
        """【沉淀的SOP】段要教"先查这里、命中就直接照做、不必重新探索"。

        沉淀的全部回报就在这一条上：第二次遇到同类任务时把整个探索过程省掉。
        只写"可以参考下面的SOP执行"，LLM 会照样从头摸一遍，SOP 白存。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        sop = _section(system, "# 【沉淀的SOP】")
        self.assertIn("动手前先看这里", sop)
        self.assertIn("不必重新探索", sop)

    def test_the_probed_paths_ride_along_in_the_tools_own_block(self):
        """探明的路径清单挂在 `readSandboxFile` 自己的描述里（第 104 步），不单独占一段。

        那张清单就是这个 `path` 参数的合法取值表 —— 归到参数自己所在的那块是它唯一的家；
        与 SOP 段同一条现刷机制（`Agent.prompt_tools` 每轮跟 `self._sop` 一起进 system）：
        探查是个异步的活儿，摸到的路径必须自己走进 prompt。还没探明 ⇒ **只不挂清单那几行**
        （工具块照列，第 107 步），而且不许写「（暂无）」：**"我们还没摸过"不等于"沙盒里没有"**
        —— 写出去就是让 LLM 干脆不去找那些文件。

        两种写法（整条全路径、或它的文件名）的规则在**工具描述**里，清单逐行把两种写法
        并排给出来 —— 那两条路各自有派发侧的用例（`test_agent.AgentTest`）。
        """
        empty = json.loads(self.agent.chat("题目"))[0]["content"]
        self.assertNotIn("/opt/task", empty)

        cmd_explore.next_command()
        cmd_explore.observe(
            "[exitCode:0]\n"
            "@@@FILE /opt/task/a.md@@@\n甲\n"
            "@@@FILE /opt/task/b.md@@@\n乙\n"
            "@@@MORE 0@@@\n"
        )
        system = json.loads(self.agent.chat("题"))[0]["content"]
        self.assertNotIn("# 【沙盒知识】", system, "清单只有工具描述这一处，不许有第二个家")
        tools = _section(system, "# 【工具描述】")
        self.assertIn("## ToolName - readSandboxFile", tools)
        block = tools.index("## ToolName - readSandboxFile")
        for path in ("/opt/task/a.md", "/opt/task/b.md"):
            self.assertIn(path, tools, f"清单里少了这条路径：{path}")
            self.assertLess(block, tools.index(path), "清单要落在工具块里，不是别处")
        # 清单是一行一条：`- 全路径，文件名`（用户手改的版面：第 116 步逗号连接、第 127 步换回逐行）
        self.assertIn("- /opt/task/a.md，a.md", tools)
        self.assertIn("- /opt/task/b.md，b.md", tools)
        # 两种写法这条**规则**在描述本身里（清单不再复述）⇒ 改版面别把它一起删掉
        self.assertIn("传入[路径清单]中的完整路径", tools)
        self.assertIn("传入[路径清单]中的文件名", tools)

    def test_the_deposit_rules_pin_the_name_to_a_class_of_tasks(self):
        """`name` 要凝练到"一类问题"上（「订去某地的机票的流程」，不是「订去上海的机票」）。

        名字写死成这一次的目标，下次同类任务就撞不上它 —— SOP 等于白存，而这条错了
        本地一点异常都看不出来（存是存进去了，只是永远复用不到）。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        rules = _sop_tool_block(system)
        self.assertIn("一类问题", rules)
        self.assertIn("订去某地", rules)

    def test_the_deposit_rules_pin_the_body_to_a_generic_flow(self):
        """SOP **正文**也要通用：写"这一类任务怎么做"，不夹带只对**本次**成立的东西。

        名字泛化只挡住一半，正文照样能把"这次的目标值、这次拿到的凭证"带进去 —— 条目是
        整场存活、跨任务复用的，下一次同类任务会照着一条过期的取值去做，**而它看不出
        那条已经过期**（口径：接口定义/参数定义要收，本次的取值不收）。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        rules = _sop_tool_block(system)
        self.assertIn("sop 正文也必须泛化", rules)
        self.assertIn("当前任务的 token", rules)
        self.assertIn("仅本次有效的参数值", rules)

    def test_the_deposit_rules_pin_the_timing(self):
        """沉淀的时机 = 那四种情况（有新的可复用知识 / 还没沉淀过 / 需要修正旧条目 / 已确认）。

        第 107 步口径**变回来了**：用户重写的 prompt 把"值不值得"这道门槛放回了
        `SOP2Prompt` 的描述（"[什么时候使用]"+"判断标准"），旧口径「只要还没沉淀过就存、
        值不值得不是门槛」作废 ⇒ 本用例改钉新措辞。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        rules = _sop_tool_block(system)
        self.assertIn("该知识尚未存在于已有 SOP 中", rules)
        self.assertIn("不重复创建", rules)
        self.assertIn("使用相同 name 更新旧 SOP", rules)
        # 沉淀与作答同轮的形状仍在（问答两侧各一处，两处都不许走）
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

    def test_the_answer_round_says_the_tags_are_mandatory(self):
        """作答**一定**包在 `<answer></answer>` 里；解释、推演写在标签外面，不许写进标签里。

        裸文本（"答案是：3"、一段解释后跟个数字）在我们这一侧**会被当答案整段交上去**
        （`chat.answer_of` 第三级：原文即答案）⇒ 判题器按字段算通过率，多写的字直接扣分，
        而本地一切自洽、只有在任务行里看得到交出去的那一段不对劲。措辞就是唯一的杠杆：
        【输出约定】的"只能用 … 包起来、整条回复只写一个这个块"与【工作原则】第 5 条的
        "其他提交方式均被禁止"是同一件事的两处落点（第 107 步，用户把原来那两句"后果"的
        说明删了，杠杆换成禁令本身 —— 见 `code-task.md` 第 107 步）。
        `sop` 那条禁令紧挨着这条，必须写明它**只**管 `sop` 文本 —— 否则 LLM 把
        "不要出现这对标签"读成"作答也别用"，正是它不守格式的一个入口。
        """
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        output = _section(system, "# 【输出约定】")
        self.assertIn("提交答案时只能用 <answer>任务答案</answer> 包起来", output)
        self.assertIn("只写一个这个块", output)
        self.assertIn("其他的任务结果提交方式均被禁止", _section(system, "# 【工作原则】"))
        self.assertIn("这条只管 `sop` 那段文本", output)

    def test_the_background_frames_where_the_two_requirements_come_from(self):
        """【背景】段只写处境：远程沙盒、判题器下达任务并收答案、环境陌生而文档可能过时。

        与【工作原则】/【工具描述】的分工是**机制不重复**：回合怎么算、回执什么时候回来、
        沙盒里能跑什么，各段写各的；背景段回答"我为什么在这儿、这活儿替谁干"。
        第 107 步：旧版那句"两条要求就是这个处境来的"被用户删掉，"以实测为准"现在只在
        【ROLE定位】与 `SOP2Prompt` 的描述里（用例按新落点钉）。
        它必须是整份 system 的第一段（`test_app` 的日志链路按段头断言）。
        """
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        background = _section(system, "# 【背景】")
        self.assertIn("远程沙盒", background)
        self.assertIn("判题器", background)
        self.assertIn("文档可能过时、也可能写错", background)
        # 机制不在这里复述（成本模型与沙盒能力都在【工具描述】）
        self.assertNotIn("一个回合", background)
        self.assertNotIn("15 秒", background)

    def test_the_flow_asks_for_the_reasoning_before_the_blocks(self):
        """先写推演、再给工具块或答案块（用户口径：COT 引导）。

        推演是写给**下一回合的自己**看的：会话窗口只留最近两轮（`Context._WINDOW`），
        不写下来就只剩一个结果、没有"上一步为什么没成"。落点必须在块**前面** —— 写进
        `<answer>` 里会被当成答案的一部分交上去。
        第 116 步删掉【输出示例】后只剩两处落点：末段那句 COT 触发语，与【输出约定】里
        "开头那段推演不算，它是写给你自己看的"。
        """
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        self.assertIn("让我们一步步推理", system)
        self.assertTrue(system.rstrip().endswith("正确无误。"), "COT 触发语收在整份 system 最后")
        self.assertIn("开头那段推演不算，它是写给你自己看的", _section(system, "# 【输出约定】"))

    def test_the_flow_numbered_steps_start_at_one(self):
        """【工作原则】把执行循环与任务理解都编了号，从 (1) / 1. 起。

        散文式提醒落到 LLM 手里会变成"先 `find`、下一回合再 `cat`"这类拆开的动作；编号
        清单是它能逐条对照的东西。第 107 步起这两张清单都住在【工作原则】里（旧的
        【每回合流程】整段删除），措辞是拍的。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        rules = _section(system, "# 【工作原则】")
        self.assertIn("(1)理解任务", rules)
        self.assertIn("(7)分析结果 / 错误", rules)
        self.assertIn("1. 任务目标是什么", rules)
        self.assertIn("2. 最终需要提交什么", rules)

    def test_the_flow_prices_a_round_and_packs_the_command(self):
        """回合是有价的、而且能被"一条胖命令"省下来 —— 措辞必须说透这件事。

        协议的成本是死的：一次沙盒往返之后，LLM 的下一步动作要等**两个回合**（发命令那轮
        拿不到回执、回执要下一轮才进 prompt）。所以"一个参数一个参数地试"是最贵的做法。
        第 107 步起成本模型与"合并命令"的细则都搬进了 `executeCmd` 的工具描述（旧的
        【每回合流程】整段删除）：一次调用 = 两个回合、能合就合、批量试、给每次尝试打标签；
        本地纯计算那条退路（一个回合就回）在 `python_exec` 的描述里。
        判据是资源约束，不是"遇到 A 就做 B"的流程 —— 后者才是过拟合。这条错了本地一点异常
        都没有，只是分数低（日志上数 `executeCmd` 的条数才看得出来）。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        tools = _section(system, "# 【工具描述】")
        cmd, local = tools.split("## ToolName - python_exec", 1)
        self.assertIn("消耗两个回合", cmd)
        self.assertIn("尽可能完成多个连续操作", cmd)
        self.assertIn("优先在一次命令中批量尝试", cmd)
        # 产出是写给下一轮的自己读的：不带标签就分不清哪条结果对哪次尝试
        self.assertIn("给每次尝试输出清晰标签", cmd)
        self.assertIn("仅需一个回合执行", local)

    def test_the_flow_says_a_failure_is_a_clue(self):
        """拿到结果先分析、照着结果调整方案再继续（【工作原则】循环的第 (7) 步那一支）。

        第一次尝试偏掉之后的默认行为是"换一个参数再来一遍"，一换就是两个回合 —— 不点破
        "先读懂回执"，LLM 会把每条回执当成二元的"成 / 不成"，然后一个接一个地穷举。
        第 107 步：旧措辞（"失败的回执是线索，不是噪音 / 报错里往往已经写着下一次该试什么"）
        随【每回合流程】整段删除，新 prompt 里只剩下面这一句 —— 缺口记在 `code-task.md`
        第 107 步的"仍生效的已知不确定性"里。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        rules = _section(system, "# 【工作原则】")
        self.assertIn("(7)分析结果 / 错误", rules)
        self.assertIn("根据结果调整方案并继续执行", rules)

    def test_the_flow_says_to_see_the_environment_before_guessing(self):
        """信息不足时先看清环境：拿环境里现成的东西换掉"猜"。

        这是"第一次尝试就偏"的正面对策 —— 偏的成因多半是**信息不足就动手**（照着一份可能
        写错的文档猜参数）。成本账：看清环境 = 一条命令，猜错一次 = 两个回合才拿回反馈。
        第 107 步起这段改钉它在【工作原则】（循环第 (4) 步）与 `executeCmd` 用途表里的落点
        —— 旧【每回合流程】那句"一条命令就能把这些一次问清"已删除。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        self.assertIn("(4)检查当前环境中已有的资源", _section(system, "# 【工作原则】"))
        self.assertIn("- 探查沙盒环境", _section(system, "# 【工具描述】"))

    def test_the_flow_says_to_follow_the_task_book_hints(self):
        """任务书点到的文件 / 接口 / 脚本就是探索的路线，不许绕开它自己另定一套验证标准。

        这是用户报的"第一次尝试会偏"的原话：偏的是**探索方向** —— 任务书写着"需求在 spec.md、
        用 check.sh 验证"，它却绕开这两样自己猜要做什么、自己另定一套标准。第 107 步起只剩
        两处落点：`executeCmd` 的第 8 条使用原则（指定了脚本就用那一个）与【工作原则】任务
        理解的第 5 条（先看它给了哪些线索）。绕一圈回来，那两个回合的反馈照样得付。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        self.assertIn(
            "优先执行指定脚本，不要自行创造另一套验证方式",
            _section(system, "# 【工具描述】"),
        )
        self.assertIn("任务中明确提供了哪些线索", _section(system, "# 【工作原则】"))

    def test_the_compression_keeps_the_failed_tries(self):
        """压缩请求要明说"试过并失败的也列上"。

        渲染窗口只留最近两轮（`Context._WINDOW`），试错一长，早先的尝试就掉出窗口 ——
        摘要（压缩轮的产物）是唯一还记得"哪些路已经走死"的地方。不点破这一点，压缩器会
        只留成功经验，"反复试同一条死路"就是它漏记的直接后果。"""
        self.agent.chat("题目")
        req = self.agent.compression_request()
        self.assertIn("试过并且失败", req)
        self.assertIn("反复试同一条死路", req)

    def test_the_deposit_rules_pin_it_to_what_actually_worked(self):
        """沉淀以**实测**为准：文档可能过时或写错，存的是跑通的那一版。

        "文档写的是某个参数、实际要的是另一个"正是试错任务最值钱的一条 —— 而 LLM 天然只
        记成功经验、不记"文档错了"这件事。不写这一句，第一个任务白试、后面每个同类任务
        再白试一遍（SOP 是整场跨任务的，这条结论对它才是资产）。
        落点两处：`SOP2Prompt` 的描述（文档与实测冲突时以实测为准）与【ROLE定位】的可信度
        排序（第 107 步，旧【沉淀规则】段删除）。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        rules = _sop_tool_block(system)
        self.assertIn("以实际执行结果为准", rules)
        self.assertIn("文档写参数为 destination", rules)
        self.assertIn("冲突时以实测为准", _section(system, "# 【ROLE定位】"))

    def test_the_deposit_rules_say_how_a_stale_entry_gets_replaced(self):
        """复用条目而实测与它不一致时，用**同名覆盖**更新那一条 —— 而不是机械重复、
        也不是以旧条目为准。

        旧条目错了而没人改，它就会一直被照做；新起一条同样名字的又会把旧的挤掉或并存。
        同名覆盖是 `tools/sop.py` 已有的存储规则，这里只是把它讲给 LLM 听。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        rules = _sop_tool_block(system)
        self.assertIn("使用相同 name 更新旧 SOP", rules)
        self.assertIn("同名会覆盖旧条目", rules)

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
