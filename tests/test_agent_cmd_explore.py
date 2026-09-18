"""agent/cmd_explore.py 的用例：沙盒探查的状态机（列清单 → 过滤 → 逐个取 → 收工）、
回执认领（自己的收走、别人的原样放行）、落盘与已知路径表。

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
LIST = f"[exitCode:0]\n{ONE};{TWO};"


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
        """空槽的第一件事：一次遍历列清单（`find` + `tr`，路径 `;` 连接）。

        命令里**不带** task 过滤：口径（只认带 task 的）在 `_take_list` 里用代码滤 ——
        沙盒环境还剩什么、根目录长什么样，全靠这一趟拿回来。"""
        self.assertEqual(
            cmd_explore.next_command(),
            "find / -type f -name '*.md' 2>/dev/null | tr '\\n' ';'",
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
        """清单里只留全路径带 task 的 md：其余 md 既不进已知表、也不取回来。"""
        cmd_explore.next_command()
        cmd_explore.observe(f"[exitCode:0]\n# 说明.md;/opt/Task/a.MD 不算;{ONE};/opt/other.md;")
        self.assertEqual(cmd_explore.known_paths(), [ONE])

    def test_the_list_becomes_one_cat_per_path(self):
        """清单 ⇒ 一条路径一条 `cat`，按清单顺序取。"""
        cmd_explore.next_command()
        cmd_explore.observe(LIST)
        self.assertEqual(cmd_explore.next_command(), f"cat '{ONE}'")
        cmd_explore.observe("[exitCode:0]\n1")
        self.assertEqual(cmd_explore.next_command(), f"cat '{TWO}'")

    def test_the_paths_are_known_before_the_bodies_come_back(self):
        """列完清单路径就到手了（prompt 的【沙盒知识】段不等正文），收工之后照样在。"""
        cmd_explore.next_command()
        cmd_explore.observe(LIST)
        self.assertEqual(cmd_explore.known_paths(), [ONE, TWO])
        cmd_explore.next_command()
        cmd_explore.observe("[exitCode:0]\n1")
        cmd_explore.next_command()
        cmd_explore.observe("[exitCode:0]\n2")
        self.assertEqual(cmd_explore.known_paths(), [ONE, TWO])

    def test_a_reset_forgets_the_paths_too(self):
        """`reset` 回到"一次都没探查过"：已知路径表也清空。"""
        cmd_explore.next_command()
        cmd_explore.observe(LIST)
        cmd_explore.reset()
        self.assertEqual(cmd_explore.known_paths(), [])

    def test_the_file_lands_under_tmp_without_the_status_line(self):
        """内容落盘到 `tmp/<沙箱路径>`，判题器那行状态（`[exitCode:N]`）不算文件内容。"""
        cmd_explore.next_command()
        cmd_explore.observe("[exitCode:0]\n/docs/task/one.md;")
        cmd_explore.next_command()
        cmd_explore.observe("[exitCode:0]\n# 标题\n正文")
        self.assertEqual(
            self._saved("docs", "task", "one.md").read_text(encoding="utf-8"), "# 标题\n正文"
        )

    def test_the_probe_stops_once_the_list_is_drained(self):
        """取完 ⇒ 收工：此后再问也一条不发（「所有命令都执行完就不用这个操作了」）。"""
        cmd_explore.next_command()
        cmd_explore.observe("[exitCode:0]\n/docs/task/one.md;")
        cmd_explore.next_command()
        cmd_explore.observe("[exitCode:0]\n正文")
        self.assertEqual(cmd_explore.next_command(), "")

    def test_an_unparsable_list_stops_the_probe(self):
        """清单解析不出路径（沙盒没跑起来）⇒ 收工，别把回执当路径拿去 `cat`。"""
        cmd_explore.next_command()
        cmd_explore.observe("[TIMEOUT]")
        self.assertEqual(cmd_explore.next_command(), "")

    def test_a_list_without_any_task_path_stops_the_probe(self):
        """清单里一个带 task 的都没有 ⇒ 收工（别把整张清单照单全取）。"""
        cmd_explore.next_command()
        cmd_explore.observe("[exitCode:0]\n/docs/readme.md;/opt/notes.md;")
        self.assertEqual(cmd_explore.known_paths(), [])
        self.assertEqual(cmd_explore.next_command(), "")


if __name__ == "__main__":
    unittest.main()
