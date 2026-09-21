"""agent/chat.py 的用例：三个谓词（`tool_of` 严格 / `looks_like_tool` 宽 / `summary_of`）。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import sys
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用（discover 不一定把它放进 sys.path）；src/ 给 coregeek 用
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coregeek.agent.chat import looks_like_tool, summary_of, tool_of  # noqa: E402


class ToolReplyParseTest(unittest.TestCase):
    """`tool_of`：解析工具调用。严格（与 `looks_like_tool` 故意相反）。

    只认嵌套形状（参数是 `<tool_param>` 里的具名元素）：属性式、裸 `<tool_param>值`、
    裸 `<tool>cmd</tool>` 都不再是工具调用 —— 落重问（`looks_like_tool` 判宽接住）。
    返回 `[(工具名, [(参数名, 原文), …]), …]`（按出现顺序），参数名永远不是 None。
    """

    def test_a_full_call_gives_the_name_and_the_params(self):
        """嵌套主形状：`<tool_param>` 里的 `<cmd>…</cmd>` 就是参数，名字取标签名。"""
        reply = (
            "<tool><tool_name>executeCmd</tool_name>"
            "<tool_param><cmd>cat /tmp/a.txt</cmd></tool_param></tool>"
        )
        self.assertEqual(tool_of(reply), [("executeCmd", [("cmd", "cat /tmp/a.txt")])])

    def test_several_params_share_one_param_block(self):
        """多参数（教的主形状）：全塞在一个 `<tool_param>` 里、各用一对标签。"""
        reply = (
            "<tool><tool_name>假工具</tool_name>"
            "<tool_param><甲>一</甲><乙>二</乙></tool_param></tool>"
        )
        self.assertEqual(tool_of(reply), [("假工具", [("甲", "一"), ("乙", "二")])])

    def test_params_split_across_blocks_are_merged(self):
        """参数拆进多个 `<tool_param>` 块也收（块数不是判据，块里的具名格式才是）。"""
        reply = (
            "<tool><tool_name>假工具</tool_name>"
            "<tool_param><甲>一</甲></tool_param>"
            "<tool_param><乙>二</乙></tool_param></tool>"
        )
        self.assertEqual(tool_of(reply), [("假工具", [("甲", "一"), ("乙", "二")])])

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
                self.assertEqual(tool_of(reply)[0][1], [("cmd", expected)])

    def test_a_zero_param_call_has_no_param_block(self):
        """无参数工具：只有 `<tool_name>`、一个 `<tool_param>` 都不写 ⇒ 合法形状
        （参数表为空；这工具存不存在、该不该放行由 `Agent.tool_call` 按声明判）。"""
        self.assertEqual(tool_of("<tool><tool_name>查询状态</tool_name></tool>"), [("查询状态", [])])

    def test_the_param_keeps_its_inner_newlines(self):
        """参数内部的换行原样保留（只去首尾空白）—— 多行命令、带缩进的 python 都合法。"""
        reply = (
            "<tool><tool_name>executeCmd</tool_name>"
            "<tool_param><cmd>\nls -la\n  wc -l a.txt\n</cmd></tool_param></tool>"
        )
        self.assertEqual(tool_of(reply)[0][1], [("cmd", "ls -la\n  wc -l a.txt")])

    def test_all_the_calls_are_collected_in_order(self):
        """并列的调用**全部**收集、按出现顺序 —— "沉淀 + 交卷同回合"就靠这一条。

        `submitAnswer` 与 `SOP2Prompt` 都不产出命令、当回合也没有回执，所以并列是合法的
        （放行与否由 `Agent.tool_calls` 按白名单判，这里只负责一条不漏地解出来）。"""
        reply = (
            "<tool><tool_name>SOP2Prompt</tool_name>"
            "<tool_param><name>方法</name><sop>先找文件</sop></tool_param></tool>\n"
            "<tool><tool_name>submitAnswer</tool_name>"
            "<tool_param><answer>晴 26 度</answer></tool_param></tool>"
        )
        self.assertEqual(
            tool_of(reply),
            [
                ("SOP2Prompt", [("name", "方法"), ("sop", "先找文件")]),
                ("submitAnswer", [("answer", "晴 26 度")]),
            ],
        )

    def test_one_broken_block_voids_the_whole_reply(self):
        """全有或全无：**任何**一块取不出工具名、或某块 `<tool_param>` 里一个具名元素都
        没有 ⇒ 整条回复 `None`（前后位置都一样）。

        半条调用比没有更坏：派一半出去，"到底哪条跑了"日志上也答不出来。空值不算不完整
        （值非空的闸门在 `Agent.tool_call` 那一侧）。"""
        good = "<tool><tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>"
        for bad in (
            "<tool><tool_name>executeCmd</tool_name><tool_param>ls</tool_param></tool>",
            "<tool><tool_param><cmd>ls</cmd></tool_param></tool>",
            "<tool><tool_name>executeCmd</tool_name><tool_param></tool_param></tool>",
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(tool_of(good + bad))
                self.assertIsNone(tool_of(bad + good))

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
                self.assertTrue(looks_like_tool(reply), "判宽要接住它 ⇒ 落重问")

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

        裸文本没有 `<tool>` 也不认（那只是普通文本，现在既不产命令也不交卷）。
        "有名字没参数"不在这里（那是无参数工具的合法形状，见
        `test_a_zero_param_call_has_no_param_block`）。
        """
        cases = (
            "<tool><tool_param><cmd>ls</cmd></tool_param></tool>",  # 有参数没名字
            "<tool></tool>",  # 空块
            "<tool>   </tool>",  # 只有空白
            "<tool>ls",  # 有开无闭
            "<tool><tool_name>executeCmd</tool_name>",  # 外层没闭合
            "<tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param>",  # 没有外层
            "ls -la",  # 裸文本
        )
        for reply in cases:
            with self.subTest(reply=reply):
                self.assertIsNone(tool_of(reply))

    def test_a_broken_block_still_counts_as_a_tool_reply(self):
        """`looks_like_tool` 宽、`tool_of` 严，这个差是承重的。

        半截的工具回复（与不再解析的旧形状）既要"取不出命令"（`tool_of` 返回 `None`）
        又要"被认成想调工具"（`looks_like_tool` 返回真 ⇒ `reject_shape` 把原因回灌进会话）
        —— 两个谓词里任何一个判反，都会出现"提问与提交同时哑火、永久空转、日志上什么都
        看不出来"。
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

    def test_plain_text_is_not_a_tool_reply(self):
        for reply in ("晴 26 度", "<answer>晴 26 度</answer>", ""):
            with self.subTest(reply=reply):
                self.assertFalse(looks_like_tool(reply))


class SummaryParseTest(unittest.TestCase):
    """`summary_of`：提取 `<summary>` 块。

    best-effort 是它的立身规则：摘要取不到 ⇒ `""`，调用方带着旧摘要继续 ——
    绝不因为摘要缺失而重问（重问要花一回合，而任务得分的分母就是回合数）。
    """

    def test_the_first_paired_block_is_taken(self):
        """成对块 ⇒ 内容（首尾空白去掉）。摘要与工具调用并列在一条回复里。"""
        reply = "<summary>【总目标】交 token</summary>\n<tool>ls</tool>"
        self.assertEqual(summary_of(reply), "【总目标】交 token")

    def test_only_the_first_block_is_taken(self):
        """多块只取第一对（`tool_of` 那边是全部块：并列调用是它的事，摘要不是）。"""
        reply = "<summary>第一份</summary>\n<summary>第二份</summary>"
        self.assertEqual(summary_of(reply), "第一份")

    def test_no_summary_or_half_written_yields_nothing(self):
        """没有 / 半截 / 空块 ⇒ `""`，不回落原文 —— 拿半截标记去凑，只会把标签串当内容用。"""
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
