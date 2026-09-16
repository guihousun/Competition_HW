"""agent/chat.py 的用例：三个谓词（`tool_of` 严格 / `looks_like_tool` 宽 / `answer_of` 三级）
——「该提交什么」与「该骂什么」是同一个谓词。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import sys
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用（discover 不一定把它放进 sys.path）；src/ 给 coregeek 用
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coregeek.agent import Agent  # noqa: E402
from coregeek.agent.chat import answer_of, looks_like_tool, summary_of, tool_of  # noqa: E402
from coregeek.agent.tools import sop  # noqa: E402
from coregeek.game.planner import task_channel  # noqa: E402


class ToolReplyParseTest(unittest.TestCase):
    """`tool_of`：解析工具调用。严格（与 `looks_like_tool` 故意相反）。

    只认嵌套形状（参数是 `<tool_param>` 里的具名元素）：属性式、裸 `<tool_param>值`、
    裸 `<tool>cmd</tool>` 都不再是工具调用 —— 落重问（`looks_like_tool` 判宽接住）。
    返回 `(工具名, [(参数名, 原文), …])`，参数名永远不是 None。
    """

    def test_a_full_call_gives_the_name_and_the_params(self):
        """嵌套主形状：`<tool_param>` 里的 `<cmd>…</cmd>` 就是参数，名字取标签名。"""
        reply = (
            "<tool><tool_name>executeCmd</tool_name>"
            "<tool_param><cmd>cat /tmp/a.txt</cmd></tool_param></tool>"
        )
        self.assertEqual(tool_of(reply), ("executeCmd", [("cmd", "cat /tmp/a.txt")]))

    def test_several_params_share_one_param_block(self):
        """多参数（教的主形状）：全塞在一个 `<tool_param>` 里、各用一对标签。"""
        reply = (
            "<tool><tool_name>假工具</tool_name>"
            "<tool_param><甲>一</甲><乙>二</乙></tool_param></tool>"
        )
        self.assertEqual(tool_of(reply), ("假工具", [("甲", "一"), ("乙", "二")]))

    def test_params_split_across_blocks_are_merged(self):
        """参数拆进多个 `<tool_param>` 块也收（块数不是判据，块里的具名格式才是）。"""
        reply = (
            "<tool><tool_name>假工具</tool_name>"
            "<tool_param><甲>一</甲></tool_param>"
            "<tool_param><乙>二</乙></tool_param></tool>"
        )
        self.assertEqual(tool_of(reply), ("假工具", [("甲", "一"), ("乙", "二")]))

    def test_param_values_are_unescaped(self):
        """prompt 教了 XML 转义 ⇒ 参数值里的五个预定义实体要还原。`&amp;` 最后换：
        `&amp;lt;` 只还原一层（`&lt;`），不是两层（`<`）。LLM 没转义时这条是空操作 ——
        裸 `<` / `>` / `&` 在 shell 命令里太常见了，一个都不许被改写。"""
        cases = {
            "cat &lt;a.txt&gt;": "cat <a.txt>",
            "a &amp;&amp; b": "a && b",
            "grep &quot;x&quot; &apos;y&apos;": "grep \"x\" 'y'",
            "&amp;lt;": "&lt;",
            "cat < a.txt > b.txt": "cat < a.txt > b.txt",
            "a && b": "a && b",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                reply = (
                    "<tool><tool_name>executeCmd</tool_name>"
                    f"<tool_param><cmd>{value}</cmd></tool_param></tool>"
                )
                self.assertEqual(tool_of(reply)[1], [("cmd", expected)])

    def test_a_zero_param_call_has_no_param_block(self):
        """无参数工具：只有 `<tool_name>`、一个 `<tool_param>` 都不写 ⇒ 合法形状
        （参数表为空；这工具存不存在、该不该放行由 `Agent.tool_call` 按声明判）。"""
        self.assertEqual(tool_of("<tool><tool_name>查询状态</tool_name></tool>"), ("查询状态", []))

    def test_the_param_keeps_its_inner_newlines(self):
        """参数内部的换行原样保留（只去首尾空白）—— 多行命令、带缩进的 python 都合法。"""
        reply = (
            "<tool><tool_name>executeCmd</tool_name>"
            "<tool_param><cmd>\nls -la\n  wc -l a.txt\n</cmd></tool_param></tool>"
        )
        self.assertEqual(tool_of(reply)[1], [("cmd", "ls -la\n  wc -l a.txt")])

    def test_only_the_first_call_is_taken(self):
        """只取第一条 `<tool>` 块：一回合只跑得了一条（接口文档 L210）。"""
        reply = (
            "<tool><tool_name>executeCmd</tool_name><tool_param><cmd>first</cmd></tool_param></tool>"
            "<tool><tool_name>executeCmd</tool_name><tool_param><cmd>second</cmd></tool_param></tool>"
        )
        self.assertEqual(tool_of(reply)[1], [("cmd", "first")])

    def test_the_old_shapes_are_no_longer_calls(self):
        """严格模式：旧的三种形状（属性式 / 裸参数 / 裸工具块）全部不再是工具调用。
        `tool_of` 给 `None`、`looks_like_tool` 给真 ⇒ 重问（丢一回合，不碰红线）。"""
        for reply in (
            '<tool><tool_name>executeCmd</tool_name><tool_param name="cmd">ls</tool_param></tool>',
            "<tool><tool_name>executeCmd</tool_name><tool_param>ls</tool_param></tool>",
            "<tool>ls -la</tool>",
            "  <tool>  ls -la  </tool>  ",
        ):
            with self.subTest(reply=reply):
                self.assertIsNone(tool_of(reply))
                self.assertTrue(looks_like_tool(reply), "判宽要接住它 ⇒ 落重问、不被当答案")

    def test_a_param_block_without_named_elements_kills_the_call(self):
        """`<tool_param>` 里一个具名元素都没有（裸值 / 空块 / 内层有开无闭）⇒ 整次调用
        不成立 —— 严格模式没有"值当位置参数"的退路。"""
        for body in ("ls", "", "<cmd>ls"):
            with self.subTest(body=body):
                reply = (
                    "<tool><tool_name>executeCmd</tool_name>"
                    f"<tool_param>{body}</tool_param></tool>"
                )
                self.assertIsNone(tool_of(reply))

    def test_partial_markup_is_not_a_call(self):
        """半截的都不算 —— 不猜半个调用，让调用方落到"重问"那一支。

        裸文本没有 `<tool>` 也不认（那只是普通文本）。"有名字没参数"不在这里
        （那是无参数工具的合法形状，见 `test_a_zero_param_call_has_no_param_block`）。
        """
        cases = (
            "<tool><tool_param><cmd>ls</cmd></tool_param></tool>",  # 有参数没名字
            "<tool></tool>",  # 空块
            "<tool>   </tool>",  # 只有空白
            "<tool>ls",  # 有开无闭
            "<tool><tool_name>executeCmd</tool_name>",  # 外层没闭合
            "<tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param>",  # 没有外层
            "ls -la",  # 裸文本（那不是工具调用，是答案）
        )
        for reply in cases:
            with self.subTest(reply=reply):
                self.assertIsNone(tool_of(reply))

    def test_a_broken_block_still_counts_as_a_tool_reply(self):
        """`looks_like_tool` 宽、`tool_of` 严，这个差是承重的。

        半截的工具回复（与不再解析的旧形状）既要"取不出命令"（`tool_of` 返回 `None`）
        又要"不能被当成答案"（`looks_like_tool` 返回真）—— 两个谓词里任何一个判反，
        都会出现"提问与提交同时哑火、永久空转、日志上什么都看不出来"。
        """
        for reply in (
            "<tool ls -la",
            "<tool>ls",
            "<tool_name>executeCmd</tool_name>",
            "<tool><tool_name>executeCmd</tool_name><tool_param>ls</tool_param></tool>",
        ):
            with self.subTest(reply=reply):
                self.assertTrue(looks_like_tool(reply))
                self.assertIsNone(tool_of(reply))

    def test_a_plain_answer_is_not_a_tool_reply(self):
        for reply in ("晴 26 度", "<answer>晴 26 度</answer>", ""):
            with self.subTest(reply=reply):
                self.assertFalse(looks_like_tool(reply))


class AnswerParseTest(unittest.TestCase):
    """`answer_of`：该提交什么（`_answer_task` 与 `task_channel` 判据 ④/⑤ 共用的谓词）。"""

    def test_a_wrapped_answer_is_unwrapped(self):
        self.assertEqual(answer_of("<answer>晴 26 度</answer>"), "晴 26 度")

    def test_an_empty_block_does_not_fall_back_to_the_raw_text(self):
        """空块 ⇒ `""`（不回落成原文）。

        那一回合宁可不提交（判题器按"通过率最高的一份"算分，少交一次不扣分），
        也不要把 `<answer></answer>` 这串标签当成答案交上去。
        """
        self.assertEqual(answer_of("<answer></answer>"), "")
        self.assertEqual(answer_of("<answer>   </answer>"), "")

    def test_a_half_written_answer_is_not_submitted(self):
        """半截 `<answer>`（有开无闭 / 闭标签写错）⇒ `""` —— 不拿半截标记去凑答案。"""
        for reply in ("<answer>晴", "<answer>晴</answer", "<answer>晴<answer>"):
            with self.subTest(reply=reply):
                self.assertEqual(answer_of(reply), "")


    def test_a_tool_call_is_never_an_answer(self):
        """工具回复（新形状 / 旧形状 / 畸形）一个都不能当答案 —— 判宽（`looks_like_tool`）。

        用 `tool_of` 判就会漏掉畸形那种，然后它既不被提交、又不会被重问。
        """
        for reply in (
            "<tool><tool_name>executeCmd</tool_name><tool_param>ls</tool_param></tool>",
            "<tool>ls -la</tool>",
            "<tool ls -la",
            "<tool_name>executeCmd</tool_name>",
        ):
            with self.subTest(reply=reply):
                self.assertEqual(answer_of(reply), "")

    def test_bare_text_is_the_answer(self):
        """兜底，逐字不变：判题器的 LLM 是黑盒，它认不认 `<answer>` 我们没得选 ——
        这是不被认账时唯一的退路。

        孤立的 `</answer>` 也落在这里（判据认的是开标签）—— 与任何一段普通文本同一条
        路径，代价是被判一次错（零成本），不值得为它加一条判据。
        """
        self.assertEqual(answer_of("晴 26 度"), "晴 26 度")
        self.assertEqual(answer_of("  晴 26 度\n"), "晴 26 度")
        self.assertEqual(answer_of("</answer>"), "</answer>")
        self.assertEqual(answer_of(""), "")

    def test_a_literal_in_the_sop_is_not_the_answer(self):
        """工具块里面的 `<answer>` 一律不算答案 —— 这一条是防 SOP 污染的全部理由。

        沉淀正文讲的往往正是"答案要用 `<answer>` 包" ⇒ 里面几乎必然出现字面量；不先挖掉
        工具块就扫，扫到的正是 SOP 里那一段，错答案被交上去且日志上看不出来。所以先挖。
        """
        reply = (
            "<tool><tool_name>SOP2Prompt</tool_name>"
            "<tool_param><name>答题格式</name>"
            "<sop>答案要写成 <answer>假答案</answer> 的形状</sop></tool_param></tool>"
        )
        self.assertEqual(answer_of(reply), "")
        # 真答案落在块外 ⇒ 照旧认（挖完剩下的正好是它）
        self.assertEqual(answer_of(reply + "\n<answer>真答案</answer>"), "真答案")

    def test_an_answer_outside_the_tool_block_is_the_only_place_it_counts(self):
        """`<answer>` 落在 `<tool>` 块之外是唯一认它的地方。

        沉淀与作答是两件事：工具块沉淀、块外的 `<answer>` 作答。块内那对标签分不清是
        答案还是 SOP 里的示例 ⇒ 一律不算，那一回合不提交、落回重问（丢一回合，不碰红线）。"""
        # 唯一认的形状
        self.assertEqual(
            answer_of(
                "<tool><tool_name>SOP2Prompt</tool_name>"
                "<tool_param><name>方法</name><sop>先找文件</sop></tool_param></tool>"
                "\n<answer>晴 26 度</answer>"
            ),
            "晴 26 度",
        )
        # 答案写进参数块里（各种姿势）：块内一律不算，块外也没有 ⇒ 这一回合不提交
        for reply in (
            "<tool><tool_name>SOP2Prompt</tool_name>"
            "<tool_param><name>方法</name><sop>沉淀</sop><answer>晴 26 度</answer></tool_param></tool>",
            # 半截的参数块（内层有开无闭）⇒ 整次调用不成立，块内那点字够不着
            "<tool><tool_name>SOP2Prompt</tool_name><tool_param><answer>晴 26 度</tool_param></tool>",
            # 值是空白 ⇒ 与"没给"同义
            "<tool><tool_name>SOP2Prompt</tool_name><tool_param><answer>   </answer></tool_param></tool>",
        ):
            with self.subTest(reply=reply):
                self.assertEqual(answer_of(reply), "")

    def test_a_literal_in_the_summary_is_not_the_answer(self):
        """摘要块里的字面量 `<answer>…</answer>` 也不是答案：摘要讲的是任务与执行
        状态、很可能引用答案格式。摘要落在工具块外，不挖掉它 `answer_of` 就会把
        摘要里那段当成答案交上去 —— 而且日志上看不出来。先挖摘要块再扫。
        """
        reply = (
            "<summary>答案要写成 <answer>假答案</answer> 的形状</summary>\n"
            "<answer>真答案</answer>"
        )
        self.assertEqual(answer_of(reply), "真答案")
        # 摘要里塞了假答案、块外没有 ⇒ 不交（宁缺勿错 —— 判题器取通过率最高的一份）
        self.assertEqual(answer_of(reply[: reply.index("\n<answer>")]), "")

    def test_structural_blocks_are_all_stripped_before_the_scan(self):
        """挖的是全部结构块（工具块 + 摘要块），不是"第一个工具块"：
        任何结构块内的 `<answer>` 都分不清是真答案还是示例 —— 宁可不交。
        块外的照旧认。"""
        two_tools = (
            "<tool><tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>"
            "<tool><tool_name>executeCmd</tool_name>"
            "<tool_param><cmd>答案是 <answer>块内答案</answer></cmd></tool_param></tool>"
        )
        self.assertEqual(answer_of(two_tools), "")
        self.assertEqual(answer_of(two_tools + "\n<answer>块外答案</answer>"), "块外答案")


class SummaryParseTest(unittest.TestCase):
    """`summary_of`：提取 `<summary>` 块。

    best-effort 是它的立身规则：摘要取不到 ⇒ `""`，调用方带着旧摘要继续 ——
    绝不因为摘要缺失而重问（重问要花一回合，而任务得分的分母就是回合数）。
    """

    def test_the_first_paired_block_is_taken(self):
        """成对块 ⇒ 内容（首尾空白去掉）。摘要与工具调用/答案并列在一条回复里。"""
        reply = "<summary>【总目标】交 token</summary>\n<tool>ls</tool>"
        self.assertEqual(summary_of(reply), "【总目标】交 token")

    def test_only_the_first_block_is_taken(self):
        """多块只取第一对 —— 与 `tool_of` 只取第一个工具块同一条规矩。"""
        reply = "<summary>第一份</summary>\n<summary>第二份</summary>"
        self.assertEqual(summary_of(reply), "第一份")

    def test_no_summary_or_half_written_yields_nothing(self):
        """没有 / 半截 / 空块 ⇒ `""`，不回落原文 —— 跟 `answer_of` 对空块的
        态度一致：拿半截标记去凑，只会把标签串当内容用。"""
        for reply in (
            "",
            "没有摘要的回复",
            "<summary>半截",
            "<summary></summary>",
            "<summary>   </summary>",
        ):
            with self.subTest(reply=reply):
                self.assertEqual(summary_of(reply), "")


if __name__ == "__main__":
    unittest.main()
