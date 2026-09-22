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
from coregeek.agent.agent import COMPRESS_AFTER_TOOLS  # noqa: E402
from coregeek.agent.tools import sop  # noqa: E402

#: system 的段头，按 `prompt.gen_system_prompt` 那份列表的顺序 —— 段名只在这里写一次，
#: 切段一律走 `_section`。
#: 段头改名/换序时改这一处，用例不会退化成 IndexError。
#: 末段（COT 触发语）**没有段头** ⇒ 不在这张表里，切最后一段时它会跟着前一段一起出来。
SECTIONS = (
    "# 【ROLE定位】",
    "# 【工作原则】",
    "# 【工具描述】",
    "# 【沉淀的SOP】",
    "# 【注意事项】",
)


def _deposit_system(agent) -> str:
    """沉淀阶段的 system（`Agent.sop_request` 那条 prompt 的 system，第 141 步）。

    沉淀的细则随沉淀通道从任务 system 搬到了这儿：任务阶段的工具表里没有它
    （用户口径「一次只专注一件事情」）。**必须在 `chat()` 之后调** —— 没开会话返回 `""`。
    """
    return json.loads(agent.sop_request())[0]["content"]


def _deposit_rules(system: str) -> str:
    """沉淀 system 里的**规则那半**（`# 【沉淀的SOP】` 段之前）。

    沉淀的全部细则（什么值得存、怎么泛化、去重与更新、以实测为准、四栏与输出形状）都在
    `SOP_REQUEST` 里 —— 第 144 步起它是**指令**而不是工具描述（回复是裸 `<sop>` 块，
    由 `chat.sops_of` 解）；段之后是现存的流程表，那是判"这条是不是已经有了"的原料。
    """
    return system.split("# 【沉淀的SOP】", 1)[0]


def _grow_tools(agent: Agent, tools: int) -> None:
    """攒够工具往来：压缩闸门按"还没被摘要盖住的 `tool` 条数"判（`COMPRESS_AFTER_TOOLS`）。

    走 `python_exec` 这条真口子（本地即时执行、产出当场进会话）。**必须在 `chat()` 之后调**
    —— 没开会话时产出直接丢。
    """
    for i in range(tools):
        agent.tool_call("python_exec", [("code", f"print({i})")])


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
        """带段头的五段按声明的顺序出现、每段头只出现一次（末段的 COT 触发语没有段头）。

        段序就是四层的落地（决策 → 工具 → 知识 → 输出）；「只一次」是"同一条规则只写
        一处"的机械保证 —— 重复的规则会稀释注意力，而这件事在实盘上测不出来。
        首段还必须是整份 system 的第一个字节（`test_app` 的日志链路按段头断言，
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
        被稀释得没法看了。阈值是拍的：顶格那一档实测 8518，上浮约 8%。
        基线数字会漂，量的时候看是**哪一档**：第 141 步实测干净 system **3431**
        （工具表里去掉 `SOP2Prompt` 那块之后；它连同并列教学一起搬进了沉淀请求）；
        满 SOP（5 × `SOP_MAX`(1000)）⇒ **顶格 8518**。要加内容先删同等量级的旧话，
        或者改这个阈值并说明理由。
        """
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        self.assertLess(len(system), 9200)

    def test_every_tool_appears_in_the_prompt(self):
        """prompt 里的工具清单由实例的工具表生成 ⇒ 每个注册的工具**每轮都在**。

        工具块恒在（第 107 步改的口径）：`readSandboxFile` 在清单空着时也只是**不挂清单那几行**，
        块本身照列 —— 它自己的描述里点过别的工具的名（"已知文件用 readSandboxFile"），藏起来
        就成了一条悬空指引；描述里也已经交代了清单为空时怎么办（改用 executeCmd 自己找）。
        旧口径（一份都没探明就整块藏起来）随用户重写的 prompt 作废。
        断言按**整份 prompt** 查这个名字（不只是工具块）。

        查的是 `prompt_tools()` —— 第 144 步起它就是注册表本身（沉淀不再是工具，旧的那层
        过滤随之删除）：工具表里有的、prompt 里就得有。"""
        empty = self.agent.chat("题目")
        self.assertEqual(
            list(self.agent.prompt_tools()),
            list(self.agent._tools),
            "工具表与注册表一个不差（沉淀不在其中）",
        )
        self.assertNotIn("SOP2Prompt", empty, "沉淀不是工具：它的形状写在沉淀请求里")
        for name in self.agent.prompt_tools():
            self.assertIn(name, empty)
        self.assertNotIn("/opt/task", empty, "还没探明 ⇒ 清单那几行不许凭空出现")

        cmd_explore._files["/opt/task/one.md"] = "正文"
        probed = self.agent.chat("题目")
        for name in self.agent.prompt_tools():
            self.assertIn(name, probed)
        self.assertIn("/opt/task/one.md", probed)

    def test_the_deposit_rules_cover_environment_knowledge(self):
        """探索到的环境知识（接口、参数、路径、返回值）也要沉淀成 SOP。

        会话窗口只留最近两轮、摘要是 best-effort ⇒ SOP 是跨回合唯一保证还在的记忆 ——
        不沉淀的发现过了窗口就丢。措辞就是产品，别改成同义词。"""
        self.agent.chat("题目")  # 先开会话（沉淀请求要拿会话原文当原料，没会话是空串）
        deposit = _deposit_system(self.agent)
        rules = _deposit_rules(deposit)
        self.assertIn("环境知识", rules)
        self.assertIn("接口", rules)
        # 沉淀是为了复用：这条理由与"该存什么"同处一份指令（旧工具描述那句话搬进这里）
        self.assertIn("能直接复用", rules)

    def test_the_deposit_rules_carry_the_output_shape(self):
        """输出定义在**指令**里（第 144 步）：`<sop>` 块 + 块里的 `<name>`，其余全是正文。

        形状不进工具表（沉淀不是工具了）⇒ 这一份指令是它唯一的家，与 `chat.sops_of` 的
        解析同源：解析侧认的就是这里教的那两样，多收的（散文包着、多条）全是宽容那一侧。
        """
        self.agent.chat("题目")
        rules = _deposit_rules(_deposit_system(self.agent))
        self.assertIn("<sop>", rules)
        self.assertIn("<name>", rules)
        self.assertIn("只输出一个", rules)
        self.assertIn("块外不写任何别的内容", rules)
        self.assertNotIn("## ToolName", rules, "沉淀不是工具：这半份里不许有工具块")

    def test_the_deposit_body_keeps_its_four_columns(self):
        """正文固定四栏（第 143 步，用户口径"沉淀的SOP应该是结构化的"）：适用场景 / 做法 /
        实测结论 / 注意事项 —— 每栏回答一个问题，一条里有几栏没内容就写「无」。

        四栏是**消费端**的需求，不只是排版：读到这条 SOP 的人先看 [适用场景] 判断这题跟
        自己有没有关系，再照 [做法] 做、[实测结论] 当权威、[注意事项] 避坑。缺了适用场景
        它就不知道该不该用（SOP 白存）；缺了实测结论，知识类的条目没有地方落。
        ⚠️ **"名字"不在这四栏里** —— 它就是块里那个 `<name>`（形状里已经写了），正文再写一遍
        就是第二份会漂移的真相；"同类任务怎么做"与"环境事实"合成一栏会让它把同一句话说两遍。
        四栏**只在这份指令里出现**，而且正好两处：规格一处 + 输出示例一处（第 144 步加的
        few-shot）—— 第三处就是复述（旧参数表那种写法）。
        栏名是**方括号**（用户口径：段头与四栏名都按这个格式走）—— 写成全角书名号就是另一份
        不存在的规格。"""
        self.agent.chat("题目")
        rules = _deposit_rules(_deposit_system(self.agent))
        for column in ("[适用场景]", "[做法]", "[实测结论]", "[注意事项]"):
            self.assertEqual(
                rules.count(column), 2, f"这一栏没写、或有多余的一份：{column}"
            )
        self.assertIn("固定写四栏", rules)
        self.assertIn("写「无」", rules)

    def test_the_deposit_rules_show_a_filled_example(self):
        """输出示例（第 144 步，用户口径"增强 few-shot 理解"）：形状照抄、正文换成自己的。

        示例是**形状**的锚，正文必须是别的领域（机票）——照抄它 = 把一段与本题无关的假经验
        存进整场复用的表里，比不沉淀更坏，所以那句"不要照抄例子里的内容"跟着示例。
        """
        self.agent.chat("题目")
        rules = _deposit_rules(_deposit_system(self.agent))
        self.assertIn("<sop>\n<name>", rules, "示例要逐字给出一整块")
        self.assertIn("</sop>", rules)
        self.assertIn("只照形状", rules)
        self.assertIn("不要照抄例子里的内容", rules)

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
        self.assertIn("全路径或它的文件名", tools)

    def test_the_deposit_rules_pin_the_name_to_a_class_of_tasks(self):
        """`name` 要凝练到"一类问题"上（「订去某地的机票的流程」，不是「订去上海的机票」）。

        名字写死成这一次的目标，下次同类任务就撞不上它 —— SOP 等于白存，而这条错了
        本地一点异常都看不出来（存是存进去了，只是永远复用不到）。

        第 133 步描述压缩成一句之后，"一类问题"这层意思一度由参数表那句"泛化后的问题类型
        名称"承担；第 144 步参数表随工具一起没了，这层意思回到指令正文里（"泛化到「一类
        问题」上"），例子照旧挂着。"""
        self.agent.chat("题目")
        rules = _deposit_rules(_deposit_system(self.agent))
        self.assertIn("泛化到", rules)
        self.assertIn("一类问题", rules)
        self.assertIn("不要写成本次的目标", rules)
        self.assertIn("订去某地", rules)

    def test_the_deposit_rules_pin_the_body_to_a_generic_flow(self):
        """SOP **正文**也要通用：写"这一类任务怎么做"，不夹带只对**本次**成立的东西。

        名字泛化只挡住一半，正文照样能把"这次的目标值、这次拿到的凭证"带进去 —— 条目是
        整场存活、跨任务复用的，下一次同类任务会照着一条过期的取值去做，**而它看不出
        那条已经过期**（口径：接口定义/参数定义要收，本次的取值不收）。

        第 133 步那串黑名单清单（token / 仅本次有效的参数值 / 一次性中间状态）压缩成
        一句"不要记录一次性答案、临时状态、临时文件/路径"；第 137 步把"临时文件/路径"
        收回成"本次任务自己产生的临时文件"（裸的"路径"与【工作原则】§2 打架 —— 接口真实
        路径正是要收的环境知识），排除项改钉"只对本次成立的取值"。泛化要求由 [做法] 那行
        "该类问题的通用解决流程"承担。"""
        self.agent.chat("题目")
        rules = _deposit_rules(_deposit_system(self.agent))
        self.assertIn("该类问题的通用解决流程", rules)
        self.assertIn("不要记录只对本次成立的取值", rules)

    def test_the_deposit_rules_pin_the_timing(self):
        """沉淀的时机：**已实际验证** + **每次都要落到一条上**（新建 / 改写已有那条 / 核对最相关那条）。

        门槛第 137 步翻回正面（表 #66：实盘上一条都不沉淀 ⇒ 太紧）：第 133 步那版只剩
        "已实际验证、且具有复用价值"一道纯闸门，"什么时候该存"没有任何正面触发语。
        第 143 步按用户口径把"无需沉淀"这个出口删掉 —— 每次都要落到一条上；「已实际验证」
        照旧管闸门。三分支（新建 / 重写同类那条 / 真没新东西就核对最相关那条）在正文里
        就是那三行，两条断言各钉一头：第一行钉"还没这类经验"，第三行钉"没有新东西也不许
        什么都不产"。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        deposit = _deposit_system(self.agent)
        self.assertIn("已实际验证", deposit)
        self.assertIn("也要挑最相关的那一条按这次的执行结果核对一遍", deposit)
        self.assertIn("【沉淀的SOP】里还没有这次这类经验 ⇒ 新建一条", deposit)
        self.assertNotIn("无需沉淀", deposit)
        # 时机第 141 步改由系统安排（交卷之后单独问一轮）⇒ 任务 system 里那句"与答案并列"
        # 连同它的形状一起删掉，触发语搬进沉淀请求
        self.assertIn("当前任务已完成", deposit)
        self.assertNotIn("当要沉淀且同回合要交答案时", system)

    def test_the_tool_shape_is_shown_verbatim(self):
        """工具调用的形状逐字出现在模板里（【工具描述】那一份）—— 这是唯一能提高"LLM 照抄
        概率"的杠杆：描述得含糊一点，它就自己发明第三种形状，而那种失败本地测不出来
        （我们的解析自洽，判题器认不认只有实盘知道）。

        第 142 步删掉【输出约定】后，交卷不再单独举例：答案走 `submitAnswer` 这个工具，
        形状与其他工具同源，名字与参数名都在工具表里。
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
        tools = _section(system, "# 【工具描述】")
        self.assertIn("## ToolName - submitAnswer", tools)
        self.assertIn("    - answer: 要提交的最终答案原文", tools)

    def test_the_deposit_round_comes_after_the_answer(self):
        """沉淀不再与交卷抢同一条回复：任务阶段一个字都不提它，交卷之后由系统单独问一轮。

        旧口径（`SOP2Prompt` 与 `submitAnswer` 并列写在同一块回复里，形状叫
        `3. [特殊混合模式]`）实盘上从没被照做过 —— 并列要求在它收尾那一刻既交卷又记账，
        而收尾时的注意力全在答案上。第 141 步拆成两阶段（用户口径「一次只专注一件事情」），
        第 144 步把那个工具本身也换掉了（长物料下模型不照工具格式调用，改成裸 `<sop>` 块）。
        下面几条断言是那两次删除的守门员 —— 别什么时候又顺手把并列教学加回去。
        """
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        self.assertNotIn("SOP2Prompt", system)
        self.assertNotIn("[特殊混合模式]", system)
        self.assertNotIn("允许并列", system)
        deposit = _deposit_system(self.agent)
        self.assertIn("当前任务已完成", deposit)
        self.assertIn("<sop>", deposit)

    def test_the_answer_round_submits_through_the_tool(self):
        """交卷**一定**走 `submitAnswer` 工具；解释、推演写在块外面，不许写进 `answer` 里。

        裸文本（"答案是：3"、一段解释后跟个数字）在新通道下**什么都交不出去**（旧通道的
        "原文即答案"兜底已删）—— 结构性堵死 vs 靠措辞。措辞仍然有用：`answer` 参数里只许放
        最终结果（判题器按字段算通过率，多写的字直接扣分），三个落点各说一遍：【工作原则】
        第 5 条、`submitAnswer` 的描述、【注意事项】第 1 条。第 142 步删掉【输出约定】后
        形状不再单独举例（它与别的工具同源），但"答案不许走别的路"这句闸门一个字没松。
        """
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        self.assertIn("其他的任务结果提交方式均被禁止", _section(system, "# 【工作原则】"))
        self.assertIn("只放最终结果本身", _section(system, "# 【工具描述】"))
        self.assertIn(
            "1. 任务的答案必须用 submitAnswer 工具提交，其他的提交方式都不允许。",
            _section(system, "# 【注意事项】"),
        )

    def test_the_flow_asks_for_the_reasoning_before_the_blocks(self):
        """先写推演、再给工具块或答案块（用户口径：COT 引导）。

        推演是写给**下一回合的自己**看的：渲染只到「摘要盖住的那段」为止（`Context.render`），
        不写下来就只剩一个结果、没有"上一步为什么没成"。落点必须在块**前面** —— 写进
        `<answer>` 里会被当成答案的一部分交上去。
        第 116 步删掉【输出示例】、第 142 步删掉【输出约定】之后，只剩两处落点：末段那句
        COT 触发语，与 `submitAnswer` 描述里的"不要带推导过程"。第 133 步用户把触发语从
        "让我们一步步推理"换成"先判断信息缺口与行动价值"—— 落点与次序不变，仍钉"它收在
        整份 system 最后"。
        """
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        self.assertIn("每次行动前判断信息缺口和行动价值", system)
        self.assertTrue(
            system.rstrip().endswith("只执行能推进任务的最小必要动作。"),
            "COT 触发语收在整份 system 最后",
        )
        self.assertIn("不要带推导过程", _section(system, "# 【工具描述】"))

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
        都没有，只是分数低（日志上数 `executeCmd` 的条数才看得出来）。

        第 133 步用户把两段描述压成散文：条目符号与标题没了，四条性质逐条改钉新措辞。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        tools = _section(system, "# 【工具描述】")
        cmd, local = tools.split("## ToolName - python_exec", 1)
        self.assertIn("一次调用消耗 2 个回合", cmd)
        self.assertIn("连续操作应尽量合并", cmd)
        self.assertIn("批量尝试", cmd)
        # 产出是写给下一轮的自己读的：不带标签就分不清哪条结果对哪次尝试
        self.assertIn("给每次尝试输出清晰标签", cmd)
        self.assertIn("比 executeCmd 省一个回合", local)

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
        —— 旧【每回合流程】那句"一条命令就能把这些一次问清"已删除。第 133 步用途表压成
        一句"用于探索环境" ⇒ 工具描述那一侧的断言改钉它。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        self.assertIn("(4)检查当前环境中已有的资源", _section(system, "# 【工作原则】"))
        self.assertIn("用于探索环境", _section(system, "# 【工具描述】"))

    def test_the_flow_says_to_follow_the_task_book_hints(self):
        """任务书点到的文件 / 接口 / 脚本就是探索的路线，不许绕开它自己另定一套验证标准。

        这是用户报的"第一次尝试会偏"的原话：偏的是**探索方向** —— 任务书写着"需求在 spec.md、
        用 check.sh 验证"，它却绕开这两样自己猜要做什么、自己另定一套标准。第 107 步起只剩
        两处落点：`executeCmd` 的第 8 条使用原则（指定了脚本就用那一个）与【工作原则】任务
        理解的第 5 条（先看它给了哪些线索）。绕一圈回来，那两个回合的反馈照样得付。
        第 133 步压缩时这一条**整句被删过一次**，已按守门员的原意补回：判据仍是
        "不要自行创造另一套验证方式"这半句，别在下次改版面时再删掉。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        self.assertIn(
            "指定了脚本或验证方式时优先执行它，不要自行创造另一套验证方式",
            _section(system, "# 【工具描述】"),
        )
        self.assertIn("任务中明确提供了哪些线索", _section(system, "# 【工作原则】"))

    def test_the_compression_keeps_the_failed_tries(self):
        """压缩请求要明说"试过并失败的也列上"。

        渲染只带「摘要盖住的那段」之后的部分（`Context.render`），试错一长，早先的尝试就只剩摘要 ——
        摘要（压缩轮的产物）是唯一还记得"哪些路已经走死"的地方。不点破这一点，压缩器会
        只留成功经验，"反复试同一条死路"就是它漏记的直接后果。"""
        self.agent.chat("题目")
        _grow_tools(self.agent, COMPRESS_AFTER_TOOLS + 1)  # 上下文够大才有请求（第 145 步）
        req = self.agent.compression_request()
        self.assertIn("试过并且失败", req)
        self.assertIn("反复试同一条死路", req)

    def test_the_deposit_rules_pin_it_to_what_actually_worked(self):
        """沉淀以**实测**为准：文档可能过时或写错，存的是跑通的那一版。

        "文档写的是某个参数、实际要的是另一个"正是试错任务最值钱的一条 —— 而 LLM 天然只
        记成功经验、不记"文档错了"这件事。不写这一句，第一个任务白试、后面每个同类任务
        再白试一遍（SOP 是整场跨任务的，这条结论对它才是资产）。
        落点两处：沉淀指令（文档与实测冲突时以实测为准）与【ROLE定位】的可信度排序
        （第 107 步，旧【沉淀规则】段删除）。第 133 步压缩时描述那一处**整句被删过一次**，
        已补回"文档与实测冲突时以实际执行结果为准"；旧长文里那个 destination/target 例子
        没有回来（例子是解释用的，判据是那句规则本身）。"""
        system = json.loads(self.agent.chat("题目"))[0]["content"]
        rules = _deposit_rules(_deposit_system(self.agent))
        self.assertIn("以实际执行结果为准", rules)
        self.assertIn("存跑通的那一版", rules)
        self.assertIn("冲突时以实测为准", _section(system, "# 【ROLE定位】"))

    def test_the_deposit_rules_say_how_a_stale_entry_gets_replaced(self):
        """复用条目而实测与它不一致时，用**同名覆盖**改那一条 —— 而不是机械重复、
        也不是以旧条目为准。

        旧条目错了而没人改，它就会一直被照做；新起一条同样名字的又会把旧的挤掉或并存。
        同名覆盖是 `tools/sop.py` 已有的存储规则，这里只是把它讲给 LLM 听。
        第 143 步起判据是"改写已有那条"这件事本身：**逐字照抄它原来的 name**（差别一个字
        就是另存了一条），改出来的正文要**写全**而不是只写差异。"""
        self.agent.chat("题目")
        deposit = _deposit_system(self.agent)
        rules = _deposit_rules(deposit)
        self.assertIn("逐字照抄它原来的 name", rules)
        self.assertIn("换个写法就是又存了一条", rules)
        self.assertIn("用原来那个 name 重写它", deposit)
        self.assertIn("不是只写改了哪儿", deposit)

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

    def _promote(self, name: str, text: str) -> None:
        """存一条流程并推它转正（交卷 → 下一轮判题器没报错那两轮，见 `Agent.settle_deposit`）。
        只有正式表进 system ⇒ 钉渲染的用例都得先过那道闸门。"""
        self.agent.deposit(name, text)
        self.agent.settle_deposit(True, True)
        self.agent.settle_deposit(False, True)

    def test_a_stored_flow_renders_with_its_name(self):
        """转正过的流程在**后面每一份** prompt 里都带着 `## SopName - 流程名` + 正文；
        空表打占位 —— 段头永远都在，那个槽是 LLM 自己写的目标，看不见槽就不会去用它。
        跨回合那条更强的证据在
        `TaskChannelTest.test_the_singleton_carries_the_sop_across_turns`；暂存的不进
        system 由 `test_agent_sop` 与 `test_game_task` 那两条钉着。
        """
        self.assertIn("（暂无沉淀）", json.loads(self.agent.chat("题目"))[0]["content"])
        self._promote("找任务书", "先看目录再动手")
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
        """没有新内容的重问轮（畸形回复 / 本地工具轮之后）⇒ 追加「请继续。」：
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
        """system（段模板）每轮现刷：表在这个进程里变了（这里是转正），下一轮就看得见
        —— 冻在构造时就没有（`prompt_tools()` 那份清单同理，两者都在 system 里）。"""
        first = self.agent.chat("题")
        self._promote("找文件", "先 ls")
        second = self.agent.chat("题")
        self.assertNotIn("先 ls", first)
        self.assertIn("先 ls", second)

    def test_braces_in_the_values_are_not_scanned_again(self):
        """`str.format` 只做一次 —— 替换值里的 `{}` 不能被当成占位符。

        题目原文、沙盒输出与流程正文都是任意文本，`{}` 太常见；二次扫描会在 `chat` 里
        直接抛 `KeyError`/`IndexError` ⇒ 整回合退化成空指令。三处一起钉。"""
        self._promote("带花括号", "SOP 里有 {sop} 和 {0}")
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
        _grow_tools(self.agent, COMPRESS_AFTER_TOOLS + 1)  # 上下文够大才有请求（第 145 步）
        req = self.agent.compression_request()
        self.assertIn("【上下文压缩】", req)
        self.assertIn("【总目标】", req)
        self.assertIn("【关键数据】", req)
        self.assertIn("回复1", req)
        self.assertIn("题目", req)

    def test_the_compression_waits_until_the_context_is_big(self):
        """上下文小了不压（第 145 步用户口径：超过 5 次 tool 才压一次）。

        摘要一落地，`render` 就把那些往来整段换成摘要（原始信息当场没了）—— 所以只有
        未覆盖的工具往来多到阈值之上才值得开口。阈值两侧各钉一次。"""
        self.agent.chat("题目")
        _grow_tools(self.agent, COMPRESS_AFTER_TOOLS)
        self.assertEqual(self.agent.compression_request(), "", "刚好到阈值还不压")
        _grow_tools(self.agent, 1)
        self.assertTrue(self.agent.compression_request(), "多一条工具往来才发请求")

    def test_an_unanswered_request_is_not_asked_again_every_round(self):
        """判题器不答 ⇒ 在途请求本身也是基准：再攒到阈值之内不会重问。

        不这么算的话，上下文一大就每个命令轮都在讨摘要 —— 白扔 prompt 槽（而且它一旦
        真答了，那一整段原始往来当场被替掉）。"""
        self.agent.chat("题目")
        _grow_tools(self.agent, COMPRESS_AFTER_TOOLS + 1)
        self.assertTrue(self.agent.compression_request())
        _grow_tools(self.agent, COMPRESS_AFTER_TOOLS)
        self.assertEqual(self.agent.compression_request(), "", "在途请求之后重新起算")

    def test_the_sop_request_carries_the_instruction_the_sop_and_the_material(self):
        """沉淀请求 = 指令（形状 + 该产什么）+ 现在的 SOP + 本题完整记录 + 末尾那道格式。

        原料与压缩请求同源（`Context.material()`）：判题器只看得到这一条 prompt，不给记录它
        不知道这道题发生了什么。带上现在的 SOP 是为了判"这条是不是已经有了"。
        没开会话 ⇒ `""`（与压缩请求同一条退路）。

        末尾那句 `SOP_TAIL` 是第 144 步加的：长物料会把 system 里的格式说明稀释掉，而离
        回复最近的那条指令才是模型最认的 —— 所以"只按格式回块"在物料之后再钉一遍。
        """
        self.assertEqual(self.agent.sop_request(), "", "没开会话 ⇒ 不发")
        self.agent.chat("题目")
        self.agent.hear("回复1")
        req = self.agent.sop_request()
        self.assertIn("当前任务已完成", req)
        self.assertIn("<sop>", req)
        self.assertIn("回复1", req)
        self.assertIn("题目", req)
        user = json.loads(req)[1]["content"]
        self.assertIn("回复1", user)
        self.assertLess(user.index("回复1"), user.index("以上是本次任务的全部记录"), "格式那道令在记录之后")


if __name__ == "__main__":
    unittest.main()
