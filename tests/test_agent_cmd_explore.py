"""agent/cmd_explore.py 的用例：沙盒探查的走法（发脚本 → 认领回执 → 切正文进 dict → 接着取 /
走完重头再来）、命令的形状、`skip` 的推进、以及**把生成的脚本拿去真跑一遍**（标记与剩余数对得上）。

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
        "先列清单、再按批取"少一个回合。第一趟的排除表是空的（什么都没读过）。"""
        cmd = cmd_explore.next_command()
        self.assertEqual(cmd, cmd_explore._command())
        self.assertTrue(cmd.startswith("python3 -c '"), cmd)
        self.assertTrue(cmd.endswith("'"), cmd)
        self.assertIn("task", cmd)
        self.assertIn(str(cmd_explore.FETCH_MAX), cmd)
        self.assertIn("@@@MORE", cmd)
        self.assertIn("known=set([])", cmd, "还没读过任何一份 ⇒ 排除表空着")

    def test_it_never_sends_a_second_command_before_the_result(self):
        """发一条就等一条：回执没回来之前不再发 —— 再发一条会把它挤掉。"""
        cmd_explore.next_command()
        self.assertEqual(cmd_explore.next_command(), "")

    def test_a_result_we_did_not_ask_for_passes_through(self):
        """没发过命令 ⇒ 这条件回执是 LLM 的，原样放行给任务线（判据 ② 照旧回灌）。"""
        self.assertEqual(cmd_explore.observe("[exitCode:0]\nok"), "[exitCode:0]\nok")

    def test_a_fallback_lookup_joins_the_inventory(self):
        """兜底查找（`readSandboxFile` 本地没有那份）的回执**照给 LLM 看**，正文同时并进
        `_files` —— 清单因此长得出来，同一份文件第二次点名不再花回合。

        与探查自己那趟两处不同（都在 `_waiting` 那一支里）：回执不给 LLM、末尾带
        `@@@MORE`（收工判据）。这一支只并账。"""
        cmd_explore.lookup_command("api.md")
        result = receipt((ONE, "接口文档"))
        self.assertEqual(cmd_explore.observe(result), result, "点名要的正文必须回给 LLM")
        self.assertEqual(cmd_explore.known_paths(), [ONE])
        self.assertFalse(cmd_explore._fallback, "认领是一次性的")
        self.assertFalse(cmd_explore._done, "兜底那趟没有 `@@@MORE` ⇒ 不算探查走完")
        # 没找到那份（`[NOT FOUND]`，一个标记都没有）⇒ 表原样不动、回执照样放行
        cmd_explore.lookup_command("nope.md")
        not_found = "[exitCode:0]\n[NOT FOUND] nope.md\n"
        self.assertEqual(cmd_explore.observe(not_found), not_found)
        self.assertEqual(cmd_explore.known_paths(), [ONE])

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

    def test_the_next_trip_carries_what_is_already_read(self):
        """还有剩的 ⇒ 下一回合接着取，**已读的那几份进排除表**（用户口径"排除掉之前读取过的"）。

        `_files` 就是游标：不传"跳过几份"，而是把读过的整份列给脚本，由它自己剔掉 ——
        那要求两次沙盒的文件表同序，而"有哪些文件"本来就可能不同。
        """
        cmd_explore.next_command()
        cmd_explore.observe(receipt((ONE, "一"), more=1))
        cmd = cmd_explore.next_command()
        self.assertEqual(cmd, cmd_explore._command())
        self.assertIn(f'"{ONE}"', cmd, "读过的那份进了排除表")
        self.assertNotIn(TWO, cmd, "没读过的当然不在排除表里")
        cmd_explore.observe(receipt((TWO, "二"), more=0))
        self.assertEqual(cmd_explore.known_paths(), [ONE, TWO])

    def test_a_truncated_receipt_keeps_going(self):
        """尾标记被截断吃掉 ⇒ 本趟有正文就保守接着取（残文留着，下一趟被排除表挡掉）。"""
        cmd_explore.next_command()
        cmd_explore.observe(receipt((ONE, "一[TRUNCATED]"), more=None))
        cmd = cmd_explore.next_command()
        self.assertEqual(cmd, cmd_explore._command())
        self.assertIn(f'"{ONE}"', cmd)

    def test_a_receipt_without_a_single_mark_tries_the_next_round(self):
        """命令没跑出结果（超时 / `python3` 不在）⇒ 下一回合接着试。

        ⚠️ 这一支**不置 `_done`**：它兜着"探查线整个没工作"（一次都没实测过），重试的代价只是
        那个本来就空着的槽。判据 = **没拿到末尾那个剩余数**（拿到且为 0 才叫收工）。
        """
        cmd_explore.next_command()
        cmd_explore.observe("[TIMEOUT]")
        self.assertEqual(cmd_explore.known_paths(), [])
        self.assertEqual(cmd_explore.next_command(), cmd_explore._command())

    def test_a_finished_walk_stops_until_the_next_task(self):
        """走完一遍就停：剩余数为 0 ⇒ `_done` 置位，空槽一条都不再发；下道题才重开一趟。

        一趟存档 = 一道题（第 113 步）：这道题的沙盒已经摸过一遍，再走是白跑（同一个沙盒的
        内容不会变）。沙盒**每道任务独立**这件事由"换任务重开"接住 —— 见下条。"""
        cmd_explore.new_task("甲题")
        cmd_explore.next_command()
        cmd_explore.observe(receipt((ONE, "一"), (TWO, "二"), more=0))
        self.assertEqual(cmd_explore.known_paths(), [ONE, TWO])
        self.assertEqual(cmd_explore.next_command(), "", "这道题摸完了 ⇒ 空槽不再占")
        self.assertEqual(cmd_explore.next_command(), "")
        cmd_explore.new_task("乙题")  # 换题 ⇒ 重开一趟
        self.assertEqual(cmd_explore.next_command(), cmd_explore._command())

    def test_the_same_task_never_restarts_the_walk(self):
        """同一道题每回合都调 `new_task` ⇒ 一次都不重开：这趟照走，排除表照旧。

        `task_channel` 每回合都调它，所以"没换题"必须是**幂等**的 —— 不然每回合都重开一趟。
        """
        cmd_explore.new_task("甲题")
        cmd_explore.next_command()
        cmd_explore.observe(receipt((ONE, "一"), more=5))  # 还剩 5 份 ⇒ 这趟没走完
        for _ in range(3):  # 真实调用方每回合都调一次
            cmd_explore.new_task("甲题")
        cmd = cmd_explore.next_command()
        self.assertEqual(cmd, cmd_explore._command(), "没换题 ⇒ 接着取")
        self.assertIn(f'"{ONE}"', cmd, "读过的那份照旧被排除")

    def test_an_empty_round_makes_the_same_text_a_new_task(self):
        """任务结束那一轮（空文本）也记 ⇒ 同一道题冷却后同文再现算**新任务**、重开一趟。

        沙盒每道任务独立：文本相同不代表是同一只沙盒。漏掉那个空轮就分不出来（`_done` 会一直
        粘着，新沙盒的文件永远发现不了）。
        """
        cmd_explore.new_task("甲题")
        cmd_explore.next_command()
        cmd_explore.observe(receipt((ONE, "一"), more=0))
        self.assertEqual(cmd_explore.next_command(), "", "先确认这趟真的收工了")
        cmd_explore.new_task("")  # 任务结束那一轮
        cmd_explore.new_task("甲题")  # 冷却后同文再现
        self.assertEqual(cmd_explore.next_command(), cmd_explore._command())

    def test_a_new_task_walk_never_carries_what_is_already_read_again(self):
        """换任务重开的那一趟把已读的整份列进排除表 —— 这是"省回合"的要害。

        不是"少打几行日志"：60KB 预算不再被读过的那几份占掉，新沙盒里没读过的一趟就能取回来。
        正文照旧留着（只累积不清）。真跑一遍脚本的对账见 `test_the_generated_script_really_runs`。
        """
        cmd_explore.new_task("甲题")
        cmd_explore.next_command()
        cmd_explore.observe(receipt((ONE, "一"), (TWO, "二")))
        cmd_explore.new_task("乙题")
        cmd = cmd_explore.next_command()
        self.assertIn(f'"{ONE}"', cmd)
        self.assertIn(f'"{TWO}"', cmd)
        self.assertEqual(cmd_explore._files, {ONE: "一", TWO: "二"}, "换题重开不清正文")

    def test_a_file_that_only_the_new_sandbox_has_joins_the_inventory(self):
        """新沙盒里多出来的那份，换任务重走时就进清单，旧的照旧留着（跨任务的并集）。"""
        cmd_explore.new_task("甲题")
        cmd_explore.next_command()
        cmd_explore.observe(receipt((ONE, "一")))
        cmd_explore.new_task("乙题")
        cmd_explore.next_command()
        cmd_explore.observe(receipt((TWO, "二")))
        self.assertEqual(cmd_explore.known_paths(), [ONE, TWO])

    def test_muting_is_the_test_only_switch(self):
        """`mute` 只给用例：静音之后一条不发，`reset` 解除（生产代码不调它）。"""
        cmd_explore.mute()
        self.assertEqual(cmd_explore.next_command(), "")
        cmd_explore.reset()
        self.assertEqual(cmd_explore.next_command(), cmd_explore._command())

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
        """`reset` 回到"一次都没探查过"：正文、跳过数、任务身份、收工标志、静音一起清。

        它**只为用例隔离存在**（生产代码不调：探明的成果整场累积、任务换了也不清）。任务身份
        也清掉是必须的 —— 用例里连着两道题文本相同时，不清就会串味成"没换题"。
        """
        cmd_explore.new_task("甲题")
        cmd_explore.next_command()
        cmd_explore.observe(receipt((ONE, "一"), more=3))
        cmd_explore.reset()
        self.assertEqual(cmd_explore.known_paths(), [])
        cmd_explore.new_task("甲题")  # 清过身份 ⇒ 同文也算新任务、从头上走
        self.assertEqual(cmd_explore.next_command(), cmd_explore._command())

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

        这是这条线上唯一能本地验证的部分 —— 标记、正文、排除表、末尾剩余数四样都在这里对账，
        回执还回灌给 `observe` 走一遍真解析（脚本与解析器对不上就会在这里露出来）。
        """
        root = Path(self._root.name)
        (root / "a" / "task").mkdir(parents=True)
        (root / "b").mkdir()
        (root / "a" / "task" / "one.md").write_text("# 标题\n正文一\n", encoding="utf-8")
        (root / "a" / "task" / "two.md").write_text("正文二\n", encoding="utf-8")
        (root / "b" / "notes.md").write_text("路径里没有 task，不该被取\n", encoding="utf-8")
        # 沙盒根是 `/`（没有反斜杠）；本地这棵树得自己说一声，否则 Windows 路径会踩到脚本里的转义
        self._set_root(str(root).replace("\\", "/"))

        first = self._run_script()
        self.assertIn("@@@MORE 0@@@", first)
        self.assertIn("# 标题\n正文一\n", first)
        self.assertIn("正文二\n", first)
        self.assertNotIn("notes.md", first)

        cmd_explore.next_command()
        self.assertEqual(cmd_explore.observe(first), "")
        self.assertEqual(sorted(cmd_explore._files.values()), ["# 标题\n正文一\n", "正文二\n"])
        self.assertEqual(len(cmd_explore.known_paths()), 2)
        self.assertTrue(all(p.endswith(("one.md", "two.md")) for p in cmd_explore.known_paths()))

        # 排除表的要害：库里的两份都被剔掉 ⇒ 这一趟一份正文都不搬（`MORE 0` 只说明没剩的了）
        again = self._run_script()
        self.assertNotIn("@@@FILE", again)
        self.assertIn("@@@MORE 0@@@", again)

        # 新沙盒多一份 ⇒ 排除表只让它一个人出得来，读过的那两份一个字都不再传
        (root / "a" / "task" / "three.md").write_text("正文三\n", encoding="utf-8")
        fresh = self._run_script()
        self.assertIn("正文三\n", fresh)
        self.assertNotIn("正文一", fresh)
        self.assertNotIn("正文二", fresh)

    def _set_root(self, root: str) -> None:
        old = cmd_explore._ROOT
        cmd_explore._ROOT = root
        self.addCleanup(setattr, cmd_explore, "_ROOT", old)

    def test_a_top_level_pseudo_dir_is_never_walked(self):
        """沙盒根那一层的伪文件系统整棵跳过（`_SKIP`）—— 列一遍 /proc 就要好几秒，会 `[TIMEOUT]`。

        用例用临时目录顶沙盒根，"proc" 这个名字对得上就跳（判据是**名字**，不是绝对路径）。
        """
        root = Path(self._root.name)
        (root / "a" / "task").mkdir(parents=True)
        (root / "a" / "task" / "one.md").write_text("正文一\n", encoding="utf-8")
        (root / "proc" / "task").mkdir(parents=True)
        (root / "proc" / "task" / "hidden.md").write_text("伪文件系统里的不该被走\n", encoding="utf-8")
        self._set_root(str(root).replace("\\", "/"))

        out = self._run_script()
        self.assertIn("正文一", out)
        self.assertNotIn("hidden.md", out)

    def test_a_doc_below_the_depth_cap_is_not_fetched(self):
        """往下只走到 `_MAXDEPTH` 层（相对沙盒根）—— 深到够不着的地方不找，换更短的时间。

        官方示例那条 `find / -maxdepth 6` 也是 6；这一条同时钉住"限深是相对根数的"。
        """
        root = Path(self._root.name)
        deep = root / "a"
        for name in ("b", "c", "d", "e", "f", "g", "task"):
            deep = deep / name
        deep.mkdir(parents=True)
        (deep / "buried.md").write_text("太深了\n", encoding="utf-8")
        (root / "a" / "task").mkdir(parents=True, exist_ok=True)
        (root / "a" / "task" / "one.md").write_text("正文一\n", encoding="utf-8")
        self._set_root(str(root).replace("\\", "/"))

        out = self._run_script()
        self.assertIn("正文一", out)
        self.assertNotIn("buried.md", out)


    def _run_script(self) -> str:
        return subprocess.run(
            [sys.executable, "-c", cmd_explore._script()],
            capture_output=True, text=True, check=True,
        ).stdout


if __name__ == "__main__":
    unittest.main()
