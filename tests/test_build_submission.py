"""Focused tests for tools/build_submission.py.

Builds against tiny temporary Git repositories with the real allowlist shape, then
runs the extracted bundle for real (start entry → root POST → local GET).

    python -m unittest discover -s tests -p test_build_submission.py -v
"""
import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import tarfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
import build_submission as bs  # noqa: E402

REPO_ROOT = ROOT
FIXTURE = {
    "main.py": "print('entry')\n",
    "run.sh": "#!/usr/bin/env bash\nexit 0\n",
    "Demo/CoreGeek/main3.py": "print('nested entry')\n",
    "Demo/CoreGeek/pyproject.toml": "[project]\nname='fixture'\n",
    "Demo/CoreGeek/src/agent/__init__.py": "",
    "Demo/CoreGeek/src/agent/brain.py": "TOWER_LOADOUT = ('rocket',)\n",
    "Demo/CoreGeek/src/agent/server.py": "# fixture\n",
    "Demo/CoreGeek/src/agent/protocol.py": "# fixture\n",
    "Demo/CoreGeek/src/agent/simulator.py": "# fixture\n",
    "web/index.html": "<html>fixture</html>\n",
    "docs/request.txt": '{"roundNo": 1}\n',
    "docs/接口文档.md": "# 接口文档（fixture）\n",
    "reports/old.json": '{"never": "packaged"}\n',
    "Demo/CoreGeek.tar.gz": "not a real archive\n",
    "notes.txt": "outside the allowlist\n",
}


def _git(repo, *args):
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, encoding="utf-8", timeout=60, shell=False)
    if done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {done.stderr}")
    return done.stdout.strip()


def make_repo(root: Path, files: dict[str, str] | None = None) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "qa@example.invalid")
    _git(repo, "config", "user.name", "qa")
    _git(repo, "config", "core.autocrlf", "false")
    for rel, text in (files or FIXTURE).items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        # newline="" keeps the fixture bytes stable on Windows.
        target.write_text(text, encoding="utf-8", newline="")
    _git(repo, "add", "-A")
    env = {"GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z"}
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "fixture"], check=True,
                   capture_output=True, env={**__import__("os").environ, **env}, shell=False)
    return repo, _git(repo, "rev-parse", "HEAD")


def committed_bytes(repo: Path, commit: str, rel: str) -> bytes:
    """Exact committed blob bytes — the same source the builder reads."""
    done = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", f"{commit}:{rel}"],
                          capture_output=True, shell=False, timeout=60)
    if done.returncode != 0:
        raise AssertionError(f"cannot read {rel} from {commit}")
    return done.stdout


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bundle-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo, self.commit = make_repo(self.root)

    def build(self, name="CoreGeek.tar.gz", **kwargs):
        return bs.build(self.repo, self.commit, self.root / name, **kwargs)

    def test_build_verify_extract_and_layout(self):
        summary = self.build()
        self.assertEqual(summary["commit"], self.commit)
        self.assertTrue(summary["self_check"]["ok"])
        self.assertEqual(summary["self_check"]["roots"], ["CoreGeek"])
        self.assertGreaterEqual(summary["self_check"]["directory_headers"], 2)
        report = bs.verify(self.root / "CoreGeek.tar.gz", sidecar=self.root / "CoreGeek.tar.gz.sha256")
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["source_digest_ok"], True)
        with tarfile.open(self.root / "CoreGeek.tar.gz") as tar:
            names = tar.getnames()
            self.assertEqual(names[0], "CoreGeek")
            self.assertIn("CoreGeek/Demo/CoreGeek/src/agent/brain.py", names)
            self.assertIn("CoreGeek/src/agent/brain.py", names)          # pyproject mirror
            self.assertIn("CoreGeek/main3.py", names)                    # entry alias
            self.assertIn("CoreGeek/main.py", names)
            self.assertNotIn("CoreGeek/reports/old.json", names)
            self.assertNotIn("CoreGeek/Demo/CoreGeek.tar.gz", names)
            self.assertNotIn("CoreGeek/notes.txt", names)
            self.assertEqual(tar.extractfile("CoreGeek/main3.py").read(),
                             committed_bytes(self.repo, self.commit, "main.py"),
                             "entry alias must be byte-identical to the committed main.py")

    def test_generated_aliases_are_declared_with_provenance(self):
        self.build()
        with tarfile.open(self.root / "CoreGeek.tar.gz") as tar:
            manifest = json.loads(tar.extractfile("CoreGeek/submission-manifest.json").read())
        entries = {item["path"]: item for item in manifest["files"]}
        for name in ("CoreGeek/main3.py", "CoreGeek/pyproject.toml", "CoreGeek/src/agent/brain.py"):
            self.assertTrue(entries[name].get("generated"), name)
            self.assertIn("source", entries[name])
        self.assertEqual(entries["CoreGeek/main3.py"]["sha256"],
                         hashlib.sha256(committed_bytes(self.repo, self.commit, "main.py")).hexdigest())
        self.assertEqual(entries["CoreGeek/src/agent/brain.py"]["sha256"],
                         entries["CoreGeek/Demo/CoreGeek/src/agent/brain.py"]["sha256"])

    def test_rebuild_is_byte_identical(self):
        first = self.build("a.tar.gz")
        second = self.build("b.tar.gz")
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual((self.root / "a.tar.gz").read_bytes(), (self.root / "b.tar.gz").read_bytes())

    def test_preflight_rejects_existing_or_colliding_outputs(self):
        (self.root / "taken.tar.gz").write_bytes(b"x")
        with self.assertRaises(bs.BuildError):
            self.build("taken.tar.gz")
        with self.assertRaises(bs.BuildError):     # sidecar collides with another output
            bs.build(self.repo, self.commit, self.root / "c.tar.gz",
                     sidecar=self.root / "c.tar.gz", manifest_path=self.root / "c.tar.gz")
        self.assertFalse((self.root / "c.tar.gz").exists(), "nothing may be written when preflight fails")

    def test_dirty_working_tree_files_are_excluded(self):
        (self.repo / "Demo/CoreGeek/src/agent/brain.py").write_text("SABOTAGE = True\n", encoding="utf-8")
        summary = self.build()
        self.assertTrue(summary["dirty_working_tree"])
        with tarfile.open(self.root / "CoreGeek.tar.gz") as tar:
            packaged = tar.extractfile("CoreGeek/Demo/CoreGeek/src/agent/brain.py").read()
        self.assertNotIn(b"SABOTAGE", packaged)
        self.assertIn("NOT in this bundle", summary["note"])

    def test_non_ascii_committed_path_survives_tree_metadata(self):
        summary = bs.build(self.repo, self.commit, self.root / "non-ascii.tar.gz")
        self.assertTrue(summary["self_check"]["ok"])
        with tarfile.open(self.root / "non-ascii.tar.gz") as tar:
            self.assertIn("CoreGeek/docs/接口文档.md", tar.getnames())
            self.assertIn("（fixture）".encode("utf-8"),
                          tar.extractfile("CoreGeek/docs/接口文档.md").read())

    def test_real_published_baseline_builds_and_verifies(self):
        """The actual repository baseline must package cleanly (smoke, not final)."""
        if not (REPO_ROOT / ".git").exists():
            self.skipTest("not a git checkout")
        commit = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse",
                                 "aa6eb3e79c96e15e40ebc0c58817dd031ac6c933"],
                                capture_output=True, text=True, shell=False)
        if commit.returncode != 0:
            self.skipTest("baseline commit unavailable")
        out = self.root / "baseline.tar.gz"
        summary = bs.build(REPO_ROOT, commit.stdout.strip(), out)
        self.assertTrue(summary["self_check"]["ok"], summary["self_check"]["problems"])
        report = bs.verify(out)
        self.assertTrue(report["ok"], report["problems"])
        with tarfile.open(out) as tar:
            names = tar.getnames()
            self.assertIn("CoreGeek/Demo/CoreGeek/src/agent/server.py", names)
            self.assertNotIn("CoreGeek/tests/test_baseline.py", names)
            self.assertNotIn("CoreGeek/.workflow", " ".join(names))

    def test_original_sample_archive_is_detected_not_rebuilt(self):
        sample = REPO_ROOT / "Demo/CoreGeek.tar.gz"
        if not sample.is_file():
            self.skipTest("sample archive unavailable")
        raw = sample.read_bytes()
        if bs.known_original_fingerprint(raw):
            with self.assertRaises(bs.BuildError):
                bs.extract(sample, self.root / "sample-out")
        self.assertEqual(sample.read_bytes(), raw, "original archive must stay untouched")


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="verify-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo, self.commit = make_repo(self.root)
        self.archive = self.root / "CoreGeek.tar.gz"
        bs.build(self.repo, self.commit, self.archive)

    def _rewrite(self, mutate, name="tampered.tar.gz"):
        with tarfile.open(self.archive) as tar:
            members = {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isreg()}
            dirs = [m.name for m in tar.getmembers() if m.isdir()]
        payload = mutate(dict(members), dirs)
        out = self.root / name
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tar:
            for directory in dirs:
                info = tarfile.TarInfo(directory)
                info.type = tarfile.DIRTYPE
                info.mtime = 0
                tar.addfile(info)
            for path, data in payload.items():
                info = tarfile.TarInfo(path)
                info.size = len(data)
                info.mtime = 0
                tar.addfile(info, io.BytesIO(data))
        with gzip.GzipFile(filename=str(out), mode="wb", mtime=0) as gz:
            gz.write(raw.getvalue())
        return out

    def test_detects_renamed_zip(self):
        zip_like = self.root / "CoreGeek.tar.gz"
        zip_like.write_bytes(b"PK\x03\x04" + b"0" * 64)
        report = bs.verify(zip_like)
        self.assertFalse(report["ok"])
        self.assertIn("renamed_zip: file has ZIP magic but a tar.gz name", report["problems"])

    def test_detects_plain_bytes_with_gz_name(self):
        plain = self.root / "plain.tar.gz"
        plain.write_bytes(b"just text, not an archive")
        report = bs.verify(plain)
        self.assertFalse(report["ok"])
        self.assertIn("not_gzip: missing 1f 8b magic", report["problems"])

    def test_detects_missing_entry_and_wrong_root(self):
        archive = self._rewrite(lambda files, dirs: {
            name.replace("CoreGeek/main3.py", "CoreGeek/entry.py"): data
            for name, data in files.items()})
        report = bs.verify(archive)
        self.assertFalse(report["ok"])
        self.assertTrue(any(problem.startswith("missing_entry=") for problem in report["problems"]))

    def test_detects_manifest_hash_tampering(self):
        def mutate(files, _dirs):
            manifest = json.loads(files["CoreGeek/submission-manifest.json"])
            manifest["files"][0]["sha256"] = "0" * 64
            files["CoreGeek/submission-manifest.json"] = json.dumps(manifest).encode()
            return files

        report = bs.verify(self._rewrite(mutate))
        self.assertFalse(report["ok"])
        self.assertTrue(any(problem.startswith("hash_mismatch=") for problem in report["problems"]))

    def test_detects_source_digest_tampering(self):
        def mutate(files, _dirs):
            manifest = json.loads(files["CoreGeek/submission-manifest.json"])
            manifest["source_digest"] = "f" * 64
            files["CoreGeek/submission-manifest.json"] = json.dumps(manifest).encode()
            return files

        report = bs.verify(self._rewrite(mutate))
        self.assertFalse(report["ok"])
        self.assertIn("source_digest_mismatch", report["problems"])

    def test_detects_undeclared_file(self):
        def mutate(files, _dirs):
            files["CoreGeek/extra.py"] = b"# not declared\n"
            return files

        report = bs.verify(self._rewrite(mutate))
        self.assertFalse(report["ok"])
        self.assertTrue(any(problem.startswith("undeclared_files=") for problem in report["problems"]))

    def test_rejects_unsafe_member_paths(self):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tar:
            for name in ("CoreGeek", "CoreGeek/main3.py"):
                info = tarfile.TarInfo(name)
                info.size = 0
                info.mtime = 0
                tar.addfile(info, io.BytesIO(b""))
            evil = tarfile.TarInfo("../escape.py")
            evil.size = 3
            evil.mtime = 0
            tar.addfile(evil, io.BytesIO(b"bad"))
        out = self.root / "unsafe.tar.gz"
        with gzip.GzipFile(filename=str(out), mode="wb", mtime=0) as gz:
            gz.write(raw.getvalue())
        report = bs.verify(out)
        self.assertFalse(report["ok"])
        self.assertIn("unsafe_member_paths", report["problems"])
        with self.assertRaises(bs.BuildError):
            bs.extract(out, self.root / "unsafe-out")

    def test_detects_truncated_gzip_trailer(self):
        """tarfile stops at the tar end marker; the gzip trailer must be checked."""
        raw = self.archive.read_bytes()
        truncated = self.root / "truncated.tar.gz"
        truncated.write_bytes(raw[:-8])
        report = bs.verify(truncated)
        self.assertFalse(report["ok"])
        self.assertTrue(any("gzip_stream_integrity" in problem for problem in report["problems"]),
                        report["problems"])

    def test_detects_flipped_gzip_crc(self):
        """A CRC-only corruption must fail: tar parsing alone would accept it."""
        raw = bytearray(self.archive.read_bytes())
        crc_index = len(raw) - 8                     # last 8 bytes: CRC32 + ISIZE
        raw[crc_index] ^= 0xFF
        corrupted = self.root / "bad-crc.tar.gz"
        corrupted.write_bytes(bytes(raw))
        report = bs.verify(corrupted)
        self.assertFalse(report["ok"])
        self.assertTrue(any("gzip_stream_integrity" in problem for problem in report["problems"]),
                        report["problems"])
        with self.assertRaises(bs.BuildError):
            bs.extract(corrupted, self.root / "crc-out")

    def test_complete_archive_still_passes_integrity(self):
        self.assertIsNone(bs.gzip_stream_problem(self.archive.read_bytes()))
        self.assertTrue(bs.verify(self.archive)["ok"])

    def test_detects_duplicate_member_paths(self):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tar:
            for name, data in (("CoreGeek", b""), ("CoreGeek/main3.py", b"first\n"),
                               ("CoreGeek/main3.py", b"second\n")):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mtime = 0
                tar.addfile(info, io.BytesIO(data))
        out = self.root / "duplicate.tar.gz"
        with gzip.GzipFile(filename=str(out), mode="wb", mtime=0) as gz:
            gz.write(raw.getvalue())
        report = bs.verify(out)
        self.assertFalse(report["ok"])
        self.assertTrue(any(problem.startswith("duplicate_member_paths=") for problem in report["problems"]))

    def test_detects_special_members(self):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tar:
            info = tarfile.TarInfo("CoreGeek")
            info.type = tarfile.DIRTYPE
            info.mtime = 0
            tar.addfile(info)
            fifo = tarfile.TarInfo("CoreGeek/pipe")
            fifo.type = tarfile.FIFOTYPE
            fifo.mtime = 0
            tar.addfile(fifo)
        out = self.root / "special.tar.gz"
        with gzip.GzipFile(filename=str(out), mode="wb", mtime=0) as gz:
            gz.write(raw.getvalue())
        report = bs.verify(out)
        self.assertFalse(report["ok"])
        self.assertTrue(any(problem.startswith("special_members=") for problem in report["problems"]))

    def test_malformed_manifest_entries_fail_without_raising(self):
        cases = {
            "not-a-dict": "just a string",
            "missing-hash": {"path": "CoreGeek/main3.py"},
            "bad-hash": {"path": "CoreGeek/main3.py", "sha256": "zz"},
            "duplicate": None,          # filled below
            "odd-types": {"path": ["x"], "sha256": 5},
        }
        for name, entry in cases.items():
            with self.subTest(case=name):
                def mutate(files, _dirs, entry=entry, name=name):
                    manifest = json.loads(files["CoreGeek/submission-manifest.json"])
                    if name == "duplicate":
                        manifest["files"] = [manifest["files"][0], dict(manifest["files"][0])]
                    else:
                        manifest["files"] = [entry]
                    files["CoreGeek/submission-manifest.json"] = json.dumps(manifest).encode()
                    return files

                report = bs.verify(self._rewrite(mutate, name=f"manifest-{name}.tar.gz"))
                self.assertFalse(report["ok"], name)
                self.assertTrue(report["problems"], name)

    def test_sidecar_mismatch_is_reported(self):
        (self.root / "bad.sha256").write_text("0" * 64 + "  CoreGeek.tar.gz\n", encoding="utf-8")
        report = bs.verify(self.archive, sidecar=self.root / "bad.sha256")
        self.assertFalse(report["ok"])
        self.assertIn("sidecar_hash_mismatch", report["problems"])

    def test_unverified_archive_is_not_extracted(self):
        broken = self.root / "broken.tar.gz"
        broken.write_bytes(b"PK\x03\x04nope")
        with self.assertRaises(bs.BuildError):
            bs.extract(broken, self.root / "out")
        self.assertFalse((self.root / "out").exists())


class ExtractedRunTests(unittest.TestCase):
    """The strongest packaging test: run the real extracted bundle end to end."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="runbundle-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        if not (REPO_ROOT / ".git").exists():
            self.skipTest("not a git checkout")
        commit = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse",
                                 "aa6eb3e79c96e15e40ebc0c58817dd031ac6c933"],
                                capture_output=True, text=True, shell=False)
        if commit.returncode != 0:
            self.skipTest("baseline commit unavailable")
        self.archive = self.root / "CoreGeek.tar.gz"
        bs.build(REPO_ROOT, commit.stdout.strip(), self.archive)
        self.dest = self.root / "out"
        bs.extract(self.archive, self.dest)

    def test_extracted_bundle_serves_root_post_and_local_get(self):
        bundle = self.dest / "CoreGeek"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        log = (self.root / "run.log").open("wb")
        self.addCleanup(log.close)
        process = subprocess.Popen([sys.executable, "main3.py", str(port)],
                                   cwd=str(bundle), stdout=log, stderr=subprocess.STDOUT)
        self.addCleanup(process.wait, 10)
        self.addCleanup(process.terminate)
        try:
            for _ in range(100):
                if process.poll() is not None:
                    self.fail("bundled entry exited during startup")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.1):
                        break
                except OSError:
                    time.sleep(.05)
            request = (bundle / "docs/request.txt").read_bytes()
            with urlopen(Request(f"http://127.0.0.1:{port}/", data=request,
                                 headers={"Content-Type": "application/json"}), timeout=10) as response:
                self.assertEqual(response.status, 200)
                body = json.load(response)
            self.assertEqual(set(body), {"roleCommandMap"})
            self.assertTrue(body["roleCommandMap"])
            with urlopen(f"http://127.0.0.1:{port}/", timeout=10) as response:
                self.assertIn("策略调试台", response.read().decode("utf-8"))
            with urlopen(f"http://127.0.0.1:{port}/sample", timeout=10) as response:
                self.assertEqual(json.load(response), json.loads(request.decode("utf-8")))
            with urlopen(Request(f"http://127.0.0.1:{port}/", data=b"invalid json"), timeout=10) as response:
                # Official contract: malformed input still answers 200 with an empty map.
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response), {"roleCommandMap": {}})
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log.flush()
        text = (self.root / "run.log").read_text(encoding="utf-8", errors="replace")
        self.assertIn("listening on 0.0.0.0", text)


if __name__ == "__main__":
    unittest.main()
