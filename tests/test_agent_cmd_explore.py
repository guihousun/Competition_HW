"""agent/cmd_explore.py 的用例：沙盒探查的状态机（发脚本 → 认领回执 → 切正文进 dict → 接着取 /
收工）、命令的形状、`skip` 的推进、以及**把生成的脚本拿去真跑一遍**（标记与剩余数对得上）。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用（discover 不一定把它放进 sys.path）；src/ 给 coregeek 用
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coregeek.agent import cmd_explore  # noqa: E402

ONE, TWO = "/opt/task/one.md", "/opt/task/two.md"


def receipt(*pairs: tuple[str, str], more: int | None = 0) -> str:
    """一条取文件的回执：`[exitCode:0]` + 每份一段标记与正文 + 末尾的剩余数。

    `more=None` = 没有那个尾标记（判题器 64KB 截断把它吃掉了）。
    """
    out = "[exitCode:0]\n" + "".join(f"@@@FILE {path}@@@\n{body}\n" for path, body in pairs)
    return out if more is None else f"{out}@@@MORE {more}@@@\n"


class CmdExploreStateTest(unittest.TestCase):
    """状态在模块里 ⇒ 每条用例先 `reset()`。"""

    def setUp(self) -> None:
        cmd_explore.reset()
        self.addCleanup(cmd_explore.reset)
        self._root = tempfile.TemporaryDirectory()
        self.addCleanup(self._root.cleanup)

    def test_the_first_free_slot_fetches_the_task_md_in_one_command(self):
        """空槽的第一件事：一条 python 命令，自己走目录、自己按预算取、自己打标记。

        命令里**带** task 过滤（口径进了脚本，见 `_NEEDLE`）：清单与正文同一趟拿到，比
        "先列清单、再按批取"少一个回合。末尾那个参数是跳过几份（第一趟 = 0）。"""
        cmd = cmd_explore.next_command()
        self.assertEqual(cmd, cmd_explore._command(0))
        self.assertTrue(cmd.startswith("python3 -c '"), cmd)
        self.assertTrue(cmd.endswith("' 0"), cmd)
        self.assertIn("task", cmd)
        self.assertIn(str(cmd_explore.FETCH_MAX), cmd)
        self.assertIn("@@@MORE", cmd)

    def test_it_never_sends_a_second_command_before_the_result(self):
        """发一条就等一条：回执没回来之前不再发 —— 再发一条会把它挤掉。"""
        cmd_explore.next_command()
        self.assertEqual(cmd_explore.next_command(), "")

    def test_a_result_we_did_not_ask_for_passes_through(self):
        """没发过命令 ⇒ 这条件回执是 LLM 的，原样放行给任务线（判据 ② 照旧回灌）。"""
        self.assertEqual(cmd_explore.observe("[exitCode:0]\nok"), "[exitCode:0]\nok")

    def test_our_own_result_is_taken_away(self):
        """发过 ⇒ 回执归我们：收走并返回 `""`（任务线当它没发生）。"""
        cmd_explore.next_command()
        self.assertEqual(cmd_explore.observe(receipt((ONE, "正文"))), "")

    def test_the_claim_is_released_after_the_result(self):
        """认领是一次性的：收走之后，下一条不是我们的回执照样放行。"""
        cmd_explore.next_command()
        cmd_explore.observe(receipt((ONE, "正文")))
        self.assertEqual(cmd_explore.observe("[exitCode:0]\nLLM 的结果"), "[exitCode:0]\nLLM 的结果")

    def test_the_bodies_are_kept_in_memory_by_full_path(self):
        """正文进 `_files`（key = 沙箱里的全路径），路径表就是它的键 —— 不落盘这件事由用例钉住。"""
        cmd_explore.next_command()
        cmd_explore.observe(receipt((ONE, "# 标题\n正文"), (TWO, "第二份")))
        self.assertEqual(cmd_explore.known_paths(), [ONE, TWO])
        self.assertEqual(cmd_explore._files[ONE], "# 标题\n正文")
        self.assertEqual(cmd_explore._files[TWO], "第二份")

    def test_a_body_keeps_its_own_newlines_and_markers(self):
        """正文里的换行、`%s`、甚至 `@@@…@@@` 都原样保留：切分只认 `@@@FILE <路径>@@@`。"""
        body = "第一行\n第二行 %s @@@ @@\n@@@MORE 7@@@\n末尾"
        cmd_explore.next_command()
        cmd_explore.observe(receipt((ONE, body), more=0))
        self.assertEqual(cmd_explore._files[ONE], body)

    def test_a_second_trip_skips_what_is_already_fetched(self):
        """还有剩的 ⇒ 下一回合接着取，命令里的跳过数 = 本趟取回几份。"""
        cmd_explore.next_command()
        cmd_explore.observe(receipt((ONE, "一"), more=1))
        self.assertEqual(cmd_explore.next_command(), cmd_explore._command(1))
        cmd_explore.observe(receipt((TWO, "二"), more=0))
        self.assertEqual(cmd_explore.known_paths(), [ONE, TWO])

    def test_a_truncated_receipt_keeps_going(self):
        """尾标记被截断吃掉 ⇒ 本趟有正文就保守接着取（下一趟从取回的那几份之后开始）。"""
        cmd_explore.next_command()
        cmd_explore.observe(receipt((ONE, "一[TRUNCATED]"), more=None))
        self.assertEqual(cmd_explore.next_command(), cmd_explore._command(1))

    def test_a_receipt_without_a_single_mark_stops_the_probe(self):
        """一个标记都切不出来（沙盒没跑起来 / 超时 / 真的没有含 task 的 md）⇒ 收工，不空转。"""
        cmd_explore.next_command()
        cmd_explore.observe("[TIMEOUT]")
        self.assertEqual(cmd_explore.known_paths(), [])
        self.assertEqual(cmd_explore.next_command(), "")

    def test_the_probe_stops_once_everything_is_fetched(self):
        """剩余数为 0 ⇒ 全部取完、收工：此后再问也一条不发（「所有命令都执行完就不用这个操作了」）。"""
        cmd_explore.next_command()
        cmd_explore.observe(receipt((ONE, "一"), (TWO, "二")))
        self.assertEqual(cmd_explore.next_command(), "")
        self.assertEqual(cmd_explore.known_paths(), [ONE, TWO])

    def test_a_bare_file_name_reaches_the_same_body(self):
        """取值表的第二种写法：只写文件名（最后一段）与整条全路径落到同一份正文。

        `file_name` 是这条对法的唯一定义处，`matches` 两种写法都认 —— 枚举值的两种形状
        都由它回答（`Agent.prompt_tools` 也拿它拼清单）。"""
        cmd_explore.next_command()
        cmd_explore.observe(receipt((ONE, "一"), (TWO, "二")))
        self.assertEqual(cmd_explore.file_name(ONE), "one.md")
        for spelling in (ONE, "one.md"):
            with self.subTest(spelling=spelling):
                self.assertEqual(cmd_explore.matches(spelling), [ONE])
                self.assertEqual(cmd_explore.body_of(spelling), "一")
        self.assertEqual(cmd_explore.body_of("nope.md"), "", "对不上就是空的，别拿别份顶替")
        self.assertEqual(cmd_explore.matches("nope.md"), [])

    def test_a_name_that_matches_two_files_is_not_a_hit(self):
        """文件名撞了（不同目录下的同名文件）⇒ 不成立。

        替它挑一份会把**错的那份**正文交出去，而它看不出拿错了 —— 宁可让它改写成全路径
        （`Agent._probed_note` 那一支会把撞上的几份列出来）。全路径那一侧照旧是唯一的。
        """
        cmd_explore.next_command()
        cmd_explore.observe(receipt(("/opt/task/a.md", "甲"), ("/home/task/a.md", "乙")))
        self.assertEqual(len(cmd_explore.matches("a.md")), 2)
        self.assertEqual(cmd_explore.body_of("a.md"), "")
        self.assertEqual(cmd_explore.body_of("/opt/task/a.md"), "甲")
        self.assertEqual(cmd_explore.body_of("/home/task/a.md"), "乙")

    def test_a_reset_forgets_the_paths_too(self):
        """`reset` 回到"一次都没探查过"：正文与跳过数一起清。

        它现在**只为用例隔离存在**（第 104 步起生产代码不调：探明的东西整场存活）。
        """
        cmd_explore.next_command()
        cmd_explore.observe(receipt((ONE, "一"), more=3))
        cmd_explore.reset()
        self.assertEqual(cmd_explore.known_paths(), [])
        self.assertEqual(cmd_explore.next_command(), cmd_explore._command(0))

    def test_the_body_reaches_the_log_with_its_path(self):
        """正文进日志（带路径）：沙盒里到底有什么，只有这一行能回答。"""
        cmd_explore.next_command()
        with self.assertLogs("coregeek.agent.cmd_explore", level="INFO") as logs:
            cmd_explore.observe(receipt((ONE, "# 任务说明")))
        self.assertTrue(any(ONE in line and "# 任务说明" in line for line in logs.output), logs.output)

    def test_a_long_body_is_clipped_in_the_log(self):
        """超长正文进日志时截断留痕（`…（共 N 字）`），dict 里那份仍是全文。"""
        body = "长" * (cmd_explore.BODY_LOG_MAX + 50)
        cmd_explore.next_command()
        with self.assertLogs("coregeek.agent.cmd_explore", level="INFO") as logs:
            cmd_explore.observe(receipt((ONE, body)))
        self.assertTrue(any(f"共 {len(body)} 字" in line for line in logs.output), logs.output)
        self.assertEqual(cmd_explore._files[ONE], body)

    def test_a_truncated_receipt_is_called_out_in_the_log(self):
        """回执被 64KB 截断 ⇒ 日志点名（正文照旧留着，是残缺的那一份）。"""
        cmd_explore.next_command()
        with self.assertLogs("coregeek.agent.cmd_explore", level="INFO") as logs:
            cmd_explore.observe(receipt((ONE, "半截正文"), more=None) + "[TRUNCATED]")
        self.assertTrue(any("截断" in line for line in logs.output), logs.output)

    def test_the_generated_script_really_runs(self):
        """把生成的脚本拿去**真跑一遍**：临时目录顶上沙盒根、本机 python 顶上 `python3`。

        这是这条线上唯一能本地验证的部分 —— 标记、正文、`skip`、末尾剩余数四样都在这里对账，
        回执还回灌给 `observe` 走一遍真解析（脚本与解析器对不上就会在这里露出来）。"""
        root = Path(self._root.name)
        (root / "a" / "task").mkdir(parents=True)
        (root / "b").mkdir()
        (root / "a" / "task" / "one.md").write_text("# 标题\n正文一\n", encoding="utf-8")
        (root / "a" / "task" / "two.md").write_text("正文二\n", encoding="utf-8")
        (root / "b" / "notes.md").write_text("路径里没有 task，不该被取\n", encoding="utf-8")
        # 沙盒根是 `/`（没有反斜杠）；本地这棵树得自己说一声，否则 Windows 路径会踩到脚本里的转义
        self._set_root(str(root).replace("\\", "/"))

        first = self._run_script(0)
        self.assertIn("@@@MORE 0@@@", first)
        self.assertIn("# 标题\n正文一\n", first)
        self.assertIn("正文二\n", first)
        self.assertNotIn("notes.md", first)

        cmd_explore.next_command()
        self.assertEqual(cmd_explore.observe(first), "")
        self.assertEqual(sorted(cmd_explore._files.values()), ["# 标题\n正文一\n", "正文二\n"])
        self.assertEqual(len(cmd_explore.known_paths()), 2)
        self.assertTrue(all(p.endswith(("one.md", "two.md")) for p in cmd_explore.known_paths()))

        second = self._run_script(1)  # 跳过第一份：只剩第二份，且已经取完了
        self.assertIn("正文二\n", second)
        self.assertNotIn("正文一", second)
        self.assertIn("@@@MORE 0@@@", second)

    def _set_root(self, root: str) -> None:
        old = cmd_explore._ROOT
        cmd_explore._ROOT = root
        self.addCleanup(setattr, cmd_explore, "_ROOT", old)

    def _run_script(self, skip: int) -> str:
        return subprocess.run(
            [sys.executable, "-c", cmd_explore._script(), str(skip)],
            capture_output=True, text=True, check=True,
        ).stdout


if __name__ == "__main__":
    unittest.main()
