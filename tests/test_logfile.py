"""logfile.py 的用例：`seal`/`unseal` 的往返与防篡改、加密 handler 落盘的内容，
以及 `log/decode_log.py` 的端到端（起子进程跑一次，解出来的要逐字等于原文）。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import logging
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coregeek import logfile  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class SealTest(unittest.TestCase):
    """一行一条记录：能原样还原、看不出明文、坏了要报错。"""

    def test_a_record_survives_the_round_trip(self):
        text = "2026-09-19 10:00:00 | 【回合】\n中文、空行与换行都在同一条记录里\n\n尾"
        self.assertEqual(logfile.unseal(logfile.seal(text)), text)

    def test_an_empty_record_also_survives(self):
        self.assertEqual(logfile.unseal(logfile.seal("")), "")

    def test_the_same_text_seals_to_different_bytes(self):
        # 逐条换 nonce。日志行首的时间戳是可猜的明文，固定密钥流会被它反向破开。
        self.assertNotEqual(logfile.seal("同样一行"), logfile.seal("同样一行"))

    def test_the_cipher_text_leaks_nothing_and_has_no_newline(self):
        sealed = logfile.seal("###第1回合###")
        self.assertNotIn("第1回合", sealed)
        self.assertNotIn("\n", sealed)

    def test_a_tampered_line_is_rejected(self):
        raw = list(logfile.seal("一行"))
        raw[3] = "A" if raw[3] != "A" else "B"  # 落在 nonce 里 ⇒ 密钥流与 tag 都对不上
        with self.assertRaises(ValueError):
            logfile.unseal("".join(raw))

    def test_a_truncated_line_is_rejected(self):
        with self.assertRaises(ValueError):
            logfile.unseal(logfile.seal("一行")[:12])

    def test_a_plain_line_is_rejected(self):
        # 混进来的明文行（旧版留下的日志、手抄进去的一行）必须报错，不能悄悄解出一串垃圾。
        with self.assertRaises(ValueError):
            logfile.unseal("2026-09-19 10:00:00 | ###第1回合###")

    def test_the_path_names_the_run(self):
        self.assertRegex(logfile.new_path("log").name, r"^match-\d{8}-\d{6}-\d+\.enc$")


class EncryptedHandlerTest(unittest.TestCase):
    """handler 落盘的必须是密文：解回来要逐字等于格式化后的记录。"""

    def test_the_file_decodes_back_to_the_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = logfile.encrypted_handler(tmp)
            self.assertIsNotNone(handler)
            handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
            logger = logging.getLogger("coregeek.logfile.test")
            logger.propagate = False  # 别把两条测试记录漏到 root 上去
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            try:
                logger.info("###第1回合###")
                logger.info("第一行\n第二行")
            finally:
                logger.removeHandler(handler)
                handler.close()
            lines = Path(handler.baseFilename).read_text(encoding="utf-8").splitlines()

        # 记录里的换行被 base64 吃掉了 ⇒ 两条记录就是两行，正文不会把文件切乱。
        self.assertEqual(len(lines), 2)
        plain = [logfile.unseal(line) for line in lines]
        self.assertRegex(plain[0], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ \| ###第1回合###$")
        self.assertEqual(plain[1].split(" | ")[-1], "第一行\n第二行")

    def test_an_unwritable_sink_returns_none_instead_of_raising(self):
        # 入口靠这个 None 决定"退成只打 stdout"：写不了日志绝不能变成起不来。
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "log"
            blocker.write_text("占着这个名字的是个文件，不是目录", encoding="utf-8")
            self.assertIsNone(logfile.encrypted_handler(str(blocker)))


class DecodeLogTest(unittest.TestCase):
    """`log/decode_log.py` 是这套格式唯一的消费者，它解得开才算闭环。"""

    def _decode(self, tmp, body):
        src = Path(tmp) / "match-x.enc"
        src.write_text(body, encoding="utf-8")
        done = subprocess.run(
            [sys.executable, str(ROOT / "log" / "decode_log.py"), str(src)],
            cwd=tmp,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        out = Path(tmp) / "decode_log.log"
        return done, out.read_text(encoding="utf-8") if out.is_file() else ""

    def test_the_script_writes_back_the_plain_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = "".join(logfile.seal(t) + "\n" for t in ("###第1回合###", "第一行\n第二行"))
            done, out = self._decode(tmp, body)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(out, "###第1回合###\n第一行\n第二行\n")

    def test_a_broken_line_is_reported_and_the_rest_still_decodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = logfile.seal("头") + "\n" + "这不是加密行\n" + logfile.seal("尾") + "\n"
            done, out = self._decode(tmp, body)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("2 行成功，1 行失败", done.stdout)  # 统计在 stdout，不进明文文件
        self.assertIn("头", out)
        self.assertIn("尾", out)
        self.assertIn("# 第 2 行解密失败", out)

    def test_a_missing_file_is_an_error_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run(
                [sys.executable, str(ROOT / "log" / "decode_log.py"), str(Path(tmp) / "没有.enc")],
                cwd=tmp,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        self.assertEqual(done.returncode, 1)
        self.assertIn("读不到", done.stderr)


if __name__ == "__main__":
    unittest.main()
