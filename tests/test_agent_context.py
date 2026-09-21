"""agent/context.py 的用例：任务内会话上下文（构造即问、粘住去重、全量保真渲染成
标准 messages JSON）。

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
from coregeek.agent.context import Context  # noqa: E402


class ContextTest(unittest.TestCase):
    """`Context` —— 任务内会话上下文，渲染成标准 messages JSON。

    判题器的 LLM 每回合只看到我们发出的 `prompt` 一段字符串 ⇒ 渲染成
    `[{"role": "system"/"user"/"assistant", "content": ...}]`。这里钉 Context 本身
    （构造即问、进表规则、粘住去重、全量保真）；跨回合接线在 `ChatPromptTest`、
    判据链在 `TaskChannelTest`、端到端在 `HandleTest.test_the_task_loop_through_handle`。
    """

    SYSTEM = "# Agent定位\n（占位 header）"

    def setUp(self) -> None:
        self.ctx = Context("请查询北京天气")
        # system 由 Agent 每次发送前刷新，这里给个占位证明它进 JSON
        self.ctx.system = self.SYSTEM

    def messages(self) -> list[dict]:
        return json.loads(self.ctx.render())

    def test_a_fresh_context_opens_with_the_task(self):
        """构造即问：首条 user 消息 = 题目原文 —— 结构由 role 表达，
        正文不加 `【题目】` 这类包装。"""
        self.assertEqual(self.ctx.task, "请查询北京天气")
        self.assertEqual(
            self.messages(),
            [
                {"role": "system", "content": self.SYSTEM},
                {"role": "user", "content": "请查询北京天气"},
            ],
        )

    def test_hear_records_the_reply_verbatim(self):
        """回复原文进 assistant 消息 —— 会话记的是它真说过的话
        （纠错块才收 `answer_of` 解包后的那份）。"""
        self.ctx.hear("<tool>ls</tool>")
        self.assertEqual(
            self.messages()[-1], {"role": "assistant", "content": "<tool>ls</tool>"}
        )

    def test_a_sticky_reply_is_heard_only_once(self):
        """`llmResp` 可能粘住（接口文档对它一个字没写、对 `lastCmdResult` 却写明不粘）
        ⇒ 与最后一条消息相同的回复不进表第二遍。"""
        self.ctx.hear("同一条回复")
        self.ctx.hear("同一条回复")
        self.assertEqual(
            [m["role"] for m in self.messages()], ["system", "user", "assistant"]
        )

    def test_a_repeat_after_another_message_is_heard_again(self):
        """中间隔了别的消息之后又来同文 ⇒ 记：那不是粘住，是真的又说了。"""
        self.ctx.hear("同一句话")
        self.ctx.nudge()
        self.ctx.hear("同一句话")
        self.assertEqual(
            [m["role"] for m in self.messages()],
            ["system", "user", "assistant", "user", "assistant"],
        )

    def test_feed_adds_the_two_titled_blocks(self):
        """回灌轮按 role 分条：结果 = `tool` 消息、纠错 = `user` 消息 ——
        标题留在 content 里当内容标签（沙盒输出是任意文本，没标签分不清哪段是什么）；
        命令输出是工具的产出、不是人类指令 ⇒ 不能标成 `user`。"""
        self.ctx.feed("[exitCode:0]\n2", "晴 26 度")
        self.assertEqual(
            [m["role"] for m in self.messages()],
            ["system", "user", "tool", "user"],
        )
        self.assertEqual(
            self.messages()[-2],
            {"role": "tool", "content": "【上一条命令的执行结果（原文）】\n[exitCode:0]\n2"},
        )
        self.assertEqual(
            self.messages()[-1],
            {
                "role": "user",
                "content": "【你上一次提交的答案被判定为不正确】\n晴 26 度\n请重新作答。",
            },
        )

    def test_nudge_appends_the_standing_line(self):
        """无新内容的重问轮 ⇒ 一句固定收尾（会话不能停在它自己的输出上）。"""
        self.ctx.hear("<tool ls")
        self.ctx.nudge()
        self.assertEqual(self.messages()[-1], {"role": "user", "content": "请继续。"})

    def test_a_tool_output_at_the_tail_is_never_nudged(self):
        """尾巴上已经是 `tool` 产出 ⇒ 不补「请继续。」（第 111 步）。

        本地工具轮（`python_exec` / `readSandboxFile` / "调用不成立"的说明）当回合就往表里
        写一条 tool 消息 ⇒ 会话停在产出上、不含糊；再补一句 user 的「请继续。」既与沙盒
        回执那一轮（判据 ② 的 `feed`）不同形，也把"该看产出"的注意力岔到一句空话上。
        """
        self.ctx.hear("<tool ls")
        for label in ("【本地 python 的执行结果（原文）】", "【沙盒文件 x.md 的正文】"):
            with self.subTest(label=label):
                self.ctx.tool_output("产出", label)
                self.ctx.nudge()
                self.assertEqual(self.messages()[-1]["role"], "tool")
        # 产出之后它又说了新话 ⇒ 尾巴换回 assistant，nudge 照旧补上
        self.ctx.hear("<tool>ls</tool>")
        self.ctx.nudge()
        self.assertEqual(self.messages()[-1], {"role": "user", "content": "请继续。"})

    def test_every_message_survives_verbatim_and_in_order(self):
        """进表逐字：题目/回复/结果里的 `{}`、换行、标签一个都不许动，顺序就是
        进表的顺序 —— `json.dumps`/`loads` 负责转义与还原。（渲染侧的窗口与摘要
        钉在下面的窗口用例里；存储始终是全量逐字。）"""
        self.ctx.hear("回复 {'a': 1}")
        self.ctx.feed("结果 {task} {0}", "")
        self.ctx.hear("<answer>答案</answer>")
        self.assertEqual(
            [(m["role"], m["content"]) for m in self.messages()],
            [
                ("system", self.SYSTEM),
                ("user", "请查询北京天气"),
                ("assistant", "回复 {'a': 1}"),
                ("tool", "【上一条命令的执行结果（原文）】\n结果 {task} {0}"),
                ("assistant", "<answer>答案</answer>"),
            ],
        )


    def test_the_summary_rides_right_after_the_task(self):
        """`summary` 渲染成题目后面的一条 `tool` 消息（`【历史摘要】` 头 ——
        标题当内容标签的既有模式）：它替旧往来记账，与沙盒回执同类，不是用户说的话。
        没有摘要时这条不出现（首问 = `[system, user]`，
        由 `test_a_fresh_context_opens_with_the_task` 钉着）。"""
        self.ctx.summary = "【总目标】交 token"
        roles = [m["role"] for m in self.messages()]
        self.assertEqual(roles[:3], ["system", "user", "tool"])
        self.assertEqual(
            self.messages()[2],
            {"role": "tool", "content": "【历史摘要】\n【总目标】交 token"},
        )

    def test_nothing_is_dropped_before_a_summary_lands(self):
        """**摘要没盖到的往来一条都不丢**（用户口径）：渲染只掐"上一次压缩请求盖住的那段"。

        压缩请求是命令轮才发得出去的（回合末尾的闸门），中间可能连着好几轮轮不到 —— 按
        "最近 N 轮"掐会把这中间的往来丢在摘要之外（旧 `_WINDOW`(2) 就是那样）。
        """
        for i in (1, 2, 3):
            self.ctx.hear(f"回复{i}")
            self.ctx.feed(f"结果{i}", "")
        contents = [m["content"] for m in self.messages()]
        for i in (1, 2, 3):
            self.assertIn(f"回复{i}", contents, "还没压缩过 ⇒ 全量都在")
        self.assertNotIn("【历史摘要】", contents, "没有摘要就不占那一条")

    def test_only_what_the_summary_covers_drops_out(self):
        """摘要回来 ⇒ 只有**它盖住的那段**（= 上次压缩请求发出去时的全部往来）不再逐条渲染。

        覆盖点按"发请求时的快照"划，不按"摘要回来的时刻" —— 请求发出之后新添的往来
        （工具产出、纠错、重问）还没进任何摘要，照旧全量跟着走。
        """
        for i in (1, 2, 3):
            self.ctx.hear(f"回复{i}")
            self.ctx.feed(f"结果{i}", "")
        self.ctx.sent_for_compression()  # 压缩请求发出去（快照 = 现在这些往来）
        self.ctx.hear("回复4")  # 请求发出之后又添的一条：它没进摘要
        self.ctx.feed("结果4", "")
        self.ctx.adopt_summary("旧账都在这里")
        contents = [m["content"] for m in self.messages()]
        text = "\n".join(contents)
        self.assertIn("【历史摘要】\n旧账都在这里", contents)
        for i in (1, 2, 3):
            self.assertNotIn(f"回复{i}", text, f"第 {i} 轮已被摘要盖住 ⇒ 不再逐条渲染")
        self.assertIn("回复4", text, "摘要之后新添的照旧全量")
        self.assertIn("结果4", text)

    def test_a_summary_without_a_pending_request_covers_nothing(self):
        """凭空来的摘要（没发过压缩请求）⇒ 只记摘要、覆盖点不动 —— 绝不能因为"有摘要了"
        就把旧往来当成已被它盖住。"""
        self.ctx.hear("回复1")
        self.ctx.feed("结果1", "")
        self.ctx.adopt_summary("不知道盖到哪的摘要")
        contents = [m["content"] for m in self.messages()]
        self.assertIn("回复1", contents)

    def test_an_unanswered_compression_request_drops_nothing(self):
        """压缩请求发了、判题器没答（没摘要回来）⇒ 那些往来照旧全量渲染。"""
        self.ctx.hear("回复1")
        self.ctx.feed("结果1", "")
        self.ctx.sent_for_compression()
        self.ctx.hear("回复2")
        contents = [m["content"] for m in self.messages()]
        self.assertIn("回复1", contents)
        self.assertIn("回复2", contents)

    def test_a_tail_nudge_rides_along(self):
        """尾巴上的 nudge 照旧在最末（它本来就是最新那一条，与摘要无关）。

        收尾前再 `hear` 一条：尾巴上放着 `tool` 产出时 nudge 根本不落（第 111 步），
        而这条用例要的是"落了之后它在不在尾巴上"。
        """
        for i in (1, 2, 3):
            self.ctx.hear(f"回复{i}")
            self.ctx.feed(f"结果{i}", "")
        self.ctx.hear("回复4")
        self.ctx.nudge()
        self.assertEqual(self.messages()[-1], {"role": "user", "content": "请继续。"})


if __name__ == "__main__":
    unittest.main()
