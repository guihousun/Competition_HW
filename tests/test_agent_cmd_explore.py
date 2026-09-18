"""agent/cmd_explore.py 的用例：沙盒探查的状态机（列清单 → 过滤 → 装箱 → 一批取 → 收工）、
回执认领（自己的收走、别人的原样放行）、回执切成 `{路径: 正文}`、落盘与已知路径表。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import sys
import tempfile
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用（discover 不一定把它放进 sys.path）；src/ 给 coregeek 用
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coregeek.agent import cmd_explore  # noqa: E402

ONE, TWO = "/opt/task/one.md", "/opt/task/two.md"
#: 清单回执：`wc -c` 的 `字节数 路径`，末尾那行 `total` 与判题器的状态行都是杂质
LIST = f"[exitCode:0]\n  1200 {ONE}\n   300 {TWO}\n  1500 total\n"


def batch_cmd(*paths: str) -> str:
    return "".join(f"echo '@@@FILE {p}@@@'; cat '{p}' 2>/dev/null; " for p in paths)


def batch_result(*bodies: str) -> str:
    """一条把若干文件装在一起的取文件回执（`[exitCode:0]` + 标记分隔）。"""
    return "[exitCode:0]\n" + "".join(
        f"@@@FILE {path}@@@\n{body}" for path, body in zip((ONE, TWO), bodies)
    )


class CmdExploreStateTest(unittest.TestCase):
    """状态在模块里 ⇒ 每条用例先 `reset()`；落盘目录换到临时目录（别往仓库根写）。"""

    def setUp(self) -> None:
        cmd_explore.reset()
        self._tmp_dir = cmd_explore.TMP_DIR
        self._root = tempfile.TemporaryDirectory()
        cmd_explore.TMP_DIR = self._root.name
        self.addCleanup(setattr, cmd_explore, "TMP_DIR", self._tmp_dir)
        self.addCleanup(self._root.cleanup)

    def _saved(self, *parts: str) -> Path:
        return Path(self._root.name).joinpath(*parts)

    def test_the_first_free_slot_lists_the_md_files(self):
        """空槽的第一件事：一次遍历列清单（`find` + `wc -c`，带上字节数）。

        命令里**不带** task 过滤：口径（只认带 task 的）在 `_take_list` 里用代码滤 ——
        沙盒环境还剩什么、根目录长什么样，全靠这一趟拿回来。字节数是后面装箱预算的依据。"""
        self.assertEqual(
            cmd_explore.next_command(),
            "find / -type f -name '*.md' -exec wc -c {} + 2>/dev/null",
        )

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
        self.assertEqual(cmd_explore.observe(LIST), "")

    def test_the_claim_is_released_after_the_result(self):
        """认领是一次性的：收走之后，下一条不是我们的回执照样放行。"""
        cmd_explore.next_command()
        cmd_explore.observe(LIST)
        self.assertEqual(cmd_explore.observe("[exitCode:0]\nLLM 的结果"), "[exitCode:0]\nLLM 的结果")

    def test_only_the_paths_with_task_in_them_are_kept(self):
        """清单里只留全路径带 task 的 md：`total` 行、没有 `/` 的、非 md 的都不进已知表。"""
        cmd_explore.next_command()
        cmd_explore.observe(
            "[exitCode:0]\n"
            "    42 /opt/notes.md\n"
            "   999 /opt/Task/Upper.MD\n"
            f"  1200 {ONE}\n"
            "     0 说明.md\n"
            "  1242 total\n"
        )
        self.assertEqual(cmd_explore.known_paths(), [ONE])

    def test_a_status_line_stuck_to_the_first_row_is_still_parsed(self):
        """判题器那行状态若没换行、粘在第一行前面，字节数与路径照样认得出（取末两段）。"""
        cmd_explore.next_command()
        cmd_explore.observe(f"[exitCode:0]  1200 {ONE}\n   300 {TWO}\n  1500 total\n")
        self.assertEqual(cmd_explore.known_paths(), [ONE, TWO])
        self.assertEqual(cmd_explore.next_command(), batch_cmd(ONE, TWO))

    def test_the_list_becomes_one_command_for_the_whole_batch(self):
        """清单里装得下 ⇒ **一条**命令取回全部：每个文件前一行标记，`cat` 紧跟其后。"""
        cmd_explore.next_command()
        cmd_explore.observe(LIST)
        self.assertEqual(cmd_explore.next_command(), batch_cmd(ONE, TWO))

    def test_a_file_over_the_budget_gets_a_batch_of_its_own(self):
        """单个文件本身就超预算 ⇒ 它独占一批（正文由判题器的 64KB 截断兜着），下一批照旧发。"""
        cmd_explore.next_command()
        cmd_explore.observe(f"[exitCode:0]\n {cmd_explore.BATCH_MAX} {ONE}\n   300 {TWO}\n")
        self.assertEqual(cmd_explore.next_command(), batch_cmd(ONE))
        cmd_explore.observe(batch_result("# 大文件"))
        self.assertEqual(cmd_explore.next_command(), batch_cmd(TWO))

    def test_the_paths_are_known_before_the_bodies_come_back(self):
        """列完清单路径就到手了（prompt 的【沙盒知识】段不等正文），取完照样在。"""
        cmd_explore.next_command()
        cmd_explore.observe(LIST)
        self.assertEqual(cmd_explore.known_paths(), [ONE, TWO])
        cmd_explore.next_command()
        cmd_explore.observe(batch_result("1", "2"))
        self.assertEqual(cmd_explore.known_paths(), [ONE, TWO])

    def test_a_reset_forgets_the_paths_too(self):
        """`reset` 回到"一次都没探查过"：已知路径表也清空。"""
        cmd_explore.next_command()
        cmd_explore.observe(LIST)
        cmd_explore.reset()
        self.assertEqual(cmd_explore.known_paths(), [])

    def test_the_bodies_land_under_tmp_without_the_status_line(self):
        """一条回执里的多个正文各自落盘到 `tmp/<沙箱路径>`；判题器那行状态不算文件内容。"""
        cmd_explore.next_command()
        cmd_explore.observe(LIST)
        cmd_explore.next_command()
        cmd_explore.observe(batch_result("# 标题\n正文", "第二份"))
        self.assertEqual(
            self._saved("opt", "task", "one.md").read_text(encoding="utf-8"), "# 标题\n正文"
        )
        self.assertEqual(
            self._saved("opt", "task", "two.md").read_text(encoding="utf-8"), "第二份"
        )

    def test_a_body_that_never_came_back_is_only_reported(self):
        """正文没回来（截断吃掉 / 文件读不到）⇒ 只点名，**不重试**：那一条不再发命令。"""
        cmd_explore.next_command()
        cmd_explore.observe(LIST)
        cmd_explore.next_command()
        cmd_explore.observe(f"[exitCode:0]\n@@@FILE {ONE}@@@\n只有第一份")
        self.assertTrue(self._saved("opt", "task", "one.md").exists())
        self.assertFalse(self._saved("opt", "task", "two.md").exists())
        self.assertEqual(cmd_explore.next_command(), "")

    def test_a_batch_that_came_back_empty_is_skipped(self):
        """整批一个标记都没有（命令没跑起来 / 超时）⇒ 什么都不落盘、这一批也不重发，下一批照旧。"""
        cmd_explore.next_command()
        cmd_explore.observe(f"[exitCode:0]\n {cmd_explore.BATCH_MAX} {ONE}\n   300 {TWO}\n")
        cmd_explore.next_command()
        cmd_explore.observe("[TIMEOUT]")
        self.assertEqual(list(Path(self._root.name).rglob("*.md")), [])
        self.assertEqual(cmd_explore.next_command(), batch_cmd(TWO))

    def test_a_truncated_receipt_is_called_out_in_the_log(self):
        """回执被 64KB 截断 ⇒ 日志点名（正文照旧落盘，是残缺的那一份）。"""
        cmd_explore.next_command()
        cmd_explore.observe(LIST)
        cmd_explore.next_command()
        with self.assertLogs("coregeek.agent.cmd_explore", level="INFO") as logs:
            cmd_explore.observe(f"{batch_result('半截正文')}[TRUNCATED]")
        self.assertTrue(any("截断" in line for line in logs.output))

    def test_the_probe_stops_once_the_list_is_drained(self):
        """取完 ⇒ 收工：此后再问也一条不发（「所有命令都执行完就不用这个操作了」）。"""
        cmd_explore.next_command()
        cmd_explore.observe(LIST)
        cmd_explore.next_command()
        cmd_explore.observe(batch_result("1", "2"))
        self.assertEqual(cmd_explore.next_command(), "")
        self.assertEqual(cmd_explore.known_paths(), [ONE, TWO])

    def test_an_unparsable_list_stops_the_probe(self):
        """清单解析不出路径（沙盒没跑起来）⇒ 收工，别把回执当路径拿去 `cat`。"""
        cmd_explore.next_command()
        cmd_explore.observe("[TIMEOUT]")
        self.assertEqual(cmd_explore.next_command(), "")

    def test_a_list_without_any_task_path_stops_the_probe(self):
        """清单里一个带 task 的都没有 ⇒ 收工（别把整张清单照单全取）。"""
        cmd_explore.next_command()
        cmd_explore.observe("[exitCode:0]\n   90 /docs/readme.md\n  120 /opt/notes.md\n")
        self.assertEqual(cmd_explore.known_paths(), [])
        self.assertEqual(cmd_explore.next_command(), "")


if __name__ == "__main__":
    unittest.main()
