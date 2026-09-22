#!/usr/bin/env python3
"""Build and verify the uploadable CoreGeek submission archive.

Adapted from Competition_HW codex/sgh 649a334 for the GitCode src/coregeek layout.
The native main3.py, run.sh, pyproject.toml and runtime sources are preserved.

Engineering tooling only: it packages committed sources, never strategy code and
never the official sample archive.

Why this tool exists
--------------------
The company platform reported ``cant get package`` for an uploaded artifact.
That message alone does not prove whether the archive, the upload or the
platform's storage/parsing failed, so this tool removes the *package-side*
variables we can control:

* a real gzip + tar stream (``1f 8b`` magic, ``ustar`` header), not a ZIP with a
  ``.tar.gz`` name;
* exactly one top-level ``CoreGeek`` directory with a runnable ``main3.py`` entry,
  matching the official sample layout;
* an explicit committed-file allowlist (no caches, reports, secrets or
  unrelated working-tree files);
* an embedded manifest with per-file SHA256 plus a sidecar archive hash;
* byte-identical rebuilds from the same commit.

Build from a resolved Git commit only: uncommitted files are never included.
``--ref HEAD`` resolves once and prints the exact SHA it used.

Subcommands::

    build   --output CoreGeek.tar.gz [--ref HEAD] [--sidecar ...] [--manifest ...]
    verify  --archive CoreGeek.tar.gz [--sidecar ...] [--expect-root CoreGeek]
    list    --archive CoreGeek.tar.gz
    extract --archive CoreGeek.tar.gz --dest DIR [--force]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

SCHEMA = "competition-hw-submission/1"
BUNDLE_ROOT = "CoreGeek"
ENTRY = "CoreGeek/main3.py"
MANIFEST_NAME = "submission-manifest.json"
GENERATED_AT = "1980-01-01T00:00:00Z"        # fixed for reproducible bytes
ZIP_MAGIC = b"PK\x03\x04"
GZIP_MAGIC = b"\x1f\x8b"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

# Root-level files kept at the archive root (official sample layout).
ROOT_FILES = ("main3.py", "run.sh", "pyproject.toml", "log/decode_log.py")
# Support assets served by the local web/debug extension.
WEB_PREFIX = "src/coregeek/"
DOC_FILES = ("docs/request.txt", "docs/response.txt", "docs/任务书.md", "docs/接口文档.md")
TOOL_FILES = ()
# Layout reference only; never packaged.
KEEP_OUT = ("Demo/CoreGeek.tar.gz",)
# Generated compatibility files (bundle path → committed source path).
GENERATED = (
    (f"{BUNDLE_ROOT}/main.py", "main3.py"),  # compatibility alias; exact upstream bytes
)
# Committed runtime sources mirrored under CoreGeek/src for the pyproject layout.
MIRROR_PREFIX = "Demo/CoreGeek/src/"
MIRROR_TARGET = f"{BUNDLE_ROOT}/src/"

FORBIDDEN_PARTS = {".git", ".workflow", ".codegraph", "__pycache__", ".pytest_cache",
                   ".mypy_cache", "reports", "node_modules", ".venv", "venv"}
FORBIDDEN_SUFFIX = (".pyc", ".pyo", ".pyd", ".log", ".tmp", ".env", ".pem", ".key", ".zip")


class BuildError(Exception):
    """A packaging precondition failed; the message is safe to print."""


# ---------------------------------------------------------------------------
# git helpers (argv lists only, never a shell string)
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    merged = dict(os.environ)
    merged.setdefault("GIT_QUOTEPATH", "false")
    if env:
        merged.update(env)
    try:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              text=True, encoding="utf-8", errors="surrogateescape",
                              timeout=120, shell=False, env=merged)
    except (OSError, subprocess.SubprocessError) as error:
        raise BuildError(f"git {' '.join(args)} could not run: {type(error).__name__}") from None
    if done.returncode != 0:
        raise BuildError(f"git {' '.join(args)} failed: {(done.stderr or '').strip()[:200]}")
    return done.stdout or ""


def _git_bytes(repo: Path, *args: str) -> bytes:
    try:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              timeout=120, shell=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise BuildError(f"git {' '.join(args)} could not run: {type(error).__name__}") from None
    if done.returncode != 0:
        raise BuildError(f"git {' '.join(args)} failed")
    return done.stdout or b""


def resolve_commit(repo: Path, ref: str) -> str:
    sha = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip().lower()
    if not COMMIT_RE.match(sha):
        raise BuildError(f"cannot resolve {ref} to a commit SHA")
    return sha


def committed_paths(repo: Path, commit: str) -> list[str]:
    out = _git(repo, "ls-tree", "-r", "-z", "--name-only", commit)
    return [name for name in out.split("\0") if name]


def tree_modes(repo: Path, commit: str) -> dict[str, str]:
    """Mode per committed path, read NUL-delimited so non-ASCII names survive."""
    raw = _git_bytes(repo, "ls-tree", "-r", "-z", commit)
    modes: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        head, _, name = entry.partition(b"\t")
        fields = head.split()
        if len(fields) >= 3 and name:
            modes[name.decode("utf-8", "surrogateescape")] = fields[0].decode("ascii", "replace")
    return modes


def working_tree_dirty(repo: Path) -> list[str]:
    out = _git(repo, "status", "--porcelain")
    return [line for line in out.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# allowlist
# ---------------------------------------------------------------------------

def _forbidden(rel: str) -> str | None:
    for part in rel.split("/"):
        if part in FORBIDDEN_PARTS:
            return f"forbidden path segment {part!r}"
    if rel in KEEP_OUT:
        return "original sample archive is a layout reference, never packaged"
    if rel.lower().endswith(FORBIDDEN_SUFFIX):
        return "forbidden file type"
    return None


def select_files(paths: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Return (accepted, excluded) using the explicit committed allowlist."""
    accepted: list[str] = []
    excluded: list[tuple[str, str]] = []
    for rel in paths:
        reason = _forbidden(rel)
        if reason:
            excluded.append((rel, reason))
            continue
        if rel.startswith("src/coregeek/") and rel.endswith(".py"):
            accepted.append(rel)
        elif rel in ROOT_FILES or rel in DOC_FILES or rel in TOOL_FILES:
            accepted.append(rel)
        else:
            excluded.append((rel, "outside the submission allowlist"))
    return accepted, excluded


def check_modes(repo: Path, commit: str, files: list[str]) -> None:
    """Reject symlinks and unsafe modes, using the NUL-delimited tree metadata."""
    modes = tree_modes(repo, commit)
    bundle_paths: list[str] = []
    for rel in files:
        mode = modes.get(rel, "")
        if not mode:
            raise BuildError(f"{rel}: not a regular committed blob")
        if mode == "120000":
            raise BuildError(f"{rel}: symlinks are rejected")
        if mode not in ("100644", "100755"):
            raise BuildError(f"{rel}: unexpected mode {mode}")
        if mode == "100755" and not rel.endswith(".sh"):
            raise BuildError(f"{rel}: unexpected executable bit")
        bundle_paths.append(mapped_path(rel))
    duplicated = sorted({p for p in bundle_paths if bundle_paths.count(p) > 1})
    if duplicated:
        raise BuildError(f"bundle path collision: {duplicated[:3]}")


def preflight(outputs: list[Path]) -> None:
    """All outputs must be distinct and absent before anything is written."""
    resolved = [path.resolve() for path in outputs]
    for path in resolved:
        if path.exists():
            raise BuildError(f"{path} already exists; choose a new output path")
    seen: list[Path] = []
    for path in resolved:
        if path in seen:
            raise BuildError(f"output paths collide: {path}")
        seen.append(path)


def mapped_path(rel: str) -> str:
    """Repository path → nested bundle path (runtime keeps Demo/CoreGeek/src)."""
    return f"{BUNDLE_ROOT}/{rel}"


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_manifest(commit: str, entries: list[dict]) -> dict:
    files = sorted(entries, key=lambda item: item["path"])
    canonical = json.dumps({item["path"]: item["sha256"] for item in files},
                           sort_keys=True, ensure_ascii=False).encode("utf-8")
    return {
        "schema": SCHEMA,
        "generated_at": GENERATED_AT,
        "commit": commit,
        "source_digest": _sha256(canonical),
        "entry": ENTRY,
        "entry_compat": ["CoreGeek/main.py", "CoreGeek/run.sh", "CoreGeek/main3.py"],
        "rules_baseline": {"version": "v1.0", "date": "2026-09-09",
                           "note": "official rule documents are referenced, not copied",
                           "sha256": "see docs/接口文档.md and docs/任务书.md entries"},
        "generated_by": "tools/build_submission.py",
        "profile": "gitcode-coregeek-flat-v1",
        "upstream": {"url": "https://gitcode.com/sa1tyfsh/CoreGeek", "branch": "main", "commit": "becfd90b2807ba137ab279a753a55ddd61ab4b59"},
        "packaging_reference": {"url": "https://github.com/guihousun/Competition_HW", "branch": "main", "commit": "becfd90b2807ba137ab279a753a55ddd61ab4b59"},
        "files": files,
    }


def _tar_bytes(manifest: dict, blobs: dict[str, bytes], directories: set[str]) -> bytes:
    """Deterministic gzip+tar stream: explicit dir headers, fixed metadata/order.

    Mirrors the reference member order: the ``CoreGeek`` directory header first,
    then the explicit sub-directories (``CoreGeek/src``, ``CoreGeek/src/agent``),
    then regular files in sorted order.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        def add_dir(name: str) -> None:
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.size = 0
            info.mtime = 0
            info.mode = 0o755
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            tar.addfile(info)

        def add_file(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(data))

        for name in sorted(directories):
            add_dir(name)
        for name in sorted(blobs):
            add_file(name, blobs[name])
        add_file(f"{BUNDLE_ROOT}/{MANIFEST_NAME}",
                 json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    raw = buffer.getvalue()
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0, compresslevel=9) as gz:
        gz.write(raw)
    return out.getvalue()


def build(repo: Path, ref: str, output: Path, sidecar: Path | None = None,
          manifest_path: Path | None = None, mirror_src: bool = True) -> dict:
    repo = repo.resolve()
    if not (repo / ".git").exists():
        raise BuildError(f"{repo} is not a Git repository")
    sidecar_path = sidecar or output.with_suffix(output.suffix + ".sha256")
    outputs = [output, sidecar_path] + ([manifest_path] if manifest_path else [])
    preflight(outputs)
    commit = resolve_commit(repo, ref)
    dirty = working_tree_dirty(repo)
    paths = committed_paths(repo, commit)
    accepted, excluded = select_files(paths)
    if not accepted:
        raise BuildError("no files matched the submission allowlist")
    check_modes(repo, commit, accepted)
    blobs: dict[str, bytes] = {}
    entries: list[dict] = []

    def read(rel: str) -> bytes:
        """Exact committed bytes (binary-safe; empty blobs are legitimate)."""
        done = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", f"{commit}:{rel}"],
                              capture_output=True, shell=False, timeout=120)
        if done.returncode != 0:
            raise BuildError(f"cannot read {rel} from {commit}")
        return done.stdout

    for rel in sorted(accepted):
        data = read(rel)
        name = mapped_path(rel)
        blobs[name] = data
        entries.append({"path": name, "sha256": _sha256(data), "bytes": len(data), "source": rel})
    available = set(paths)
    generated: list[str] = []
    # Generated compatibility files: byte-identical copies with declared provenance.
    for bundle_rel, source_rel in GENERATED:
        if source_rel not in available:
            raise BuildError(f"generated file {bundle_rel} needs committed source {source_rel}")
        data = read(source_rel)
        blobs[bundle_rel] = data
        entries.append({"path": bundle_rel, "sha256": _sha256(data), "bytes": len(data),
                        "source": source_rel, "generated": True})
        generated.append(bundle_rel)
    if mirror_src:
        for rel in sorted(accepted):
            if not rel.startswith(MIRROR_PREFIX):
                continue
            target = MIRROR_TARGET + rel[len(MIRROR_PREFIX):]
            blobs[target] = blobs[mapped_path(rel)]
            entries.append({"path": target, "sha256": _sha256(blobs[target]),
                            "bytes": len(blobs[target]), "source": rel, "generated": True})
            generated.append(target)
    directories = {BUNDLE_ROOT}
    for name in list(blobs):
        parts = name.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            directories.add("/".join(parts[:index]))
    manifest = build_manifest(commit, entries)
    archive = _tar_bytes(manifest, blobs, directories)
    if len(archive) < 3 or archive[:2] != GZIP_MAGIC:
        raise BuildError("internal error: produced bytes are not gzip")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(archive)
    summary = {"archive": str(output), "sha256": _sha256(archive), "bytes": len(archive),
               "commit": commit, "dirty_working_tree": bool(dirty), "files": len(entries),
               "generated": sorted(generated), "excluded": len(excluded)}
    # Read back the artifact we are about to hand over: required entries present and
    # every generated alias declared in the manifest with matching bytes.
    gate = verify(output, expected_paths=set(blobs), generated=set(generated))
    summary["self_check"] = gate
    if not gate["ok"]:
        output.unlink(missing_ok=True)
        raise BuildError(f"built archive failed its own verification: {gate['problems']}")
    sidecar_path.write_text(f"{summary['sha256']}  {output.name}\n", encoding="utf-8")
    summary["sidecar"] = str(sidecar_path)
    if manifest_path:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8")
        summary["manifest"] = str(manifest_path)
    if dirty:
        summary["note"] = ("working tree has uncommitted changes; they are NOT in this bundle "
                           "(built from committed files only)")
    return summary


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def detect_container(raw: bytes) -> str:
    if raw[:2] == GZIP_MAGIC:
        return "gzip"
    if raw[:4] == ZIP_MAGIC or raw[:2] == b"PK":
        return "zip"
    return "unknown"


def gzip_stream_problem(raw: bytes) -> str | None:
    """Consume the whole gzip stream so CRC32 and the 8-byte trailer are checked.

    ``tarfile`` stops reading at the archive end marker and never verifies the
    gzip trailer, so truncated or CRC-corrupted uploads would otherwise pass.
    Returns a problem label, or None when the stream is complete and intact.
    """
    if raw[:2] != GZIP_MAGIC:
        return "not_gzip"
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
            while gz.read(1 << 16):
                pass
    except (OSError, EOFError, ValueError):
        return "gzip_stream_integrity"
    return None


def _safe_member(name: str) -> bool:
    if name.startswith("/") or "\\" in name or ":" in name:
        return False
    parts = [part for part in name.split("/") if part not in ("", ".")]
    return bool(parts) and all(part != ".." for part in parts)


def verify(archive: Path, expect_root: str = BUNDLE_ROOT, expect_entry: str = ENTRY,
           sidecar: Path | None = None, expected_paths: set[str] | None = None,
           generated: set[str] | None = None) -> dict:
    """Validate container, layout, allowlist coverage, aliases, manifest and sidecar."""
    report: dict = {"archive": str(archive), "ok": False, "problems": [], "warnings": []}
    try:
        raw = archive.read_bytes()
    except OSError:
        report["problems"].append("archive_unreadable")
        return report
    report["bytes"] = len(raw)
    report["sha256"] = _sha256(raw)
    container = detect_container(raw)
    report["container"] = container
    if container == "zip":
        report["problems"].append("renamed_zip: file has ZIP magic but a tar.gz name")
        return report
    if container != "gzip":
        report["problems"].append("not_gzip: missing 1f 8b magic")
        return report
    integrity = gzip_stream_problem(raw)
    if integrity is not None:
        report["problems"].append(
            f"{integrity}: gzip stream is truncated or its CRC/trailer does not match")
        return report
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            members = tar.getmembers()
            names = [m.name for m in members]
            report["members"] = len(names)
            if any(not _safe_member(name) for name in names):
                report["problems"].append("unsafe_member_paths")
            duplicates = sorted({name for name in names if names.count(name) > 1})
            if duplicates:
                report["problems"].append(f"duplicate_member_paths={len(duplicates)}")
            special = [m.name for m in members if not (m.isdir() or m.isreg())]
            if special:
                report["problems"].append(f"special_members={len(special)}")
            roots = {name.split("/")[0] for name in names if name.split("/")[0]}
            report["roots"] = sorted(roots)
            if roots != {expect_root}:
                report["problems"].append(f"unexpected_root={sorted(roots)}")
            headers = {m.name for m in members if m.isdir()}
            report["directory_headers"] = len(headers)
            if expect_root not in headers:
                report["problems"].append("missing_top_level_directory_header")
            if any(m.issym() or m.islnk() for m in members):
                report["problems"].append("links_present")
            regular = {m.name for m in members if m.isreg()}
            if expect_entry not in regular:
                report["problems"].append(f"missing_entry={expect_entry}")
            for required in (f"{expect_root}/main.py", f"{expect_root}/run.sh",
                             f"{expect_root}/pyproject.toml",
                             f"{expect_root}/src/coregeek/app.py",
                             f"{expect_root}/src/coregeek/web/server.py"):
                if required not in regular:
                    report["problems"].append(f"missing_required={required}")
            if expected_paths is not None:
                absent = sorted(expected_paths - regular)
                if absent:
                    report["problems"].append(f"expected_bytes_missing={len(absent)}")
            manifest_name = f"{expect_root}/{MANIFEST_NAME}"
            if manifest_name not in regular:
                report["warnings"].append("no_manifest: legacy/unversioned bundle")
                report["manifest_state"] = "absent"
                report["ok"] = not report["problems"]
                return report
            manifest = json.loads(tar.extractfile(manifest_name).read().decode("utf-8"))
            report["manifest_state"] = "present"
            if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
                report["problems"].append("manifest_schema_unexpected")
                return report
            commit = manifest.get("commit")
            report["commit"] = commit if isinstance(commit, str) and COMMIT_RE.match(commit) else None
            if report["commit"] is None:
                report["problems"].append("manifest_commit_invalid")
            entries = manifest.get("files")
            if not isinstance(entries, list) or not entries:
                report["problems"].append("manifest_files_missing")
                return report
            declared: dict[str, dict] = {}
            malformed = 0
            duplicate_entries = 0
            for entry in entries:
                if not isinstance(entry, dict):
                    malformed += 1
                    continue
                rel, digest = entry.get("path"), entry.get("sha256")
                if not isinstance(rel, str) or not rel.startswith(f"{expect_root}/") \
                        or not _safe_member(rel) or not isinstance(digest, str) \
                        or not SHA256_RE.match(digest.lower()):
                    malformed += 1
                    continue
                if rel in declared:
                    duplicate_entries += 1
                    continue
                declared[rel] = entry
            if malformed:
                report["problems"].append(f"manifest_entry_malformed={malformed}")
            if duplicate_entries:
                report["problems"].append(f"manifest_duplicate_entries={duplicate_entries}")
            payload = {name for name in regular if not name.endswith("/" + MANIFEST_NAME)}
            undeclared = sorted(name for name in payload if name not in declared)
            if undeclared:
                report["problems"].append(f"undeclared_files={len(undeclared)}")
            absent = sorted(name for name in declared if name not in regular)
            if absent:
                report["problems"].append(f"declared_but_absent={len(absent)}")
            mismatch = 0
            for name, entry in declared.items():
                if name not in regular:
                    continue
                if _sha256(tar.extractfile(name).read()) != str(entry["sha256"]).lower():
                    mismatch += 1
            if mismatch:
                report["problems"].append(f"hash_mismatch={mismatch}")
            # Generated aliases need declared provenance and identical source bytes.
            for name in sorted(generated or ()):  # validated when the caller knows them
                entry = declared.get(name)
                if entry is None:
                    report["problems"].append(f"generated_alias_undeclared={name}")
                    continue
                source = entry.get("source")
                if not entry.get("generated") or not isinstance(source, str):
                    report["problems"].append(f"generated_alias_without_provenance={name}")
                    continue
                if source in declared and declared[source]["sha256"] != entry["sha256"]:
                    report["problems"].append(f"generated_alias_hash_differs={name}")
            claimed = manifest.get("source_digest")
            recomputed = _sha256(json.dumps({item["path"]: item["sha256"] for item in entries
                                             if isinstance(item, dict)
                                             and isinstance(item.get("path"), str)
                                             and isinstance(item.get("sha256"), str)},
                                            sort_keys=True, ensure_ascii=False).encode("utf-8"))
            report["source_digest_ok"] = claimed == recomputed
            if claimed != recomputed:
                report["problems"].append("source_digest_mismatch")
            report["files"] = len(declared)
            report["entry"] = manifest.get("entry") if isinstance(manifest.get("entry"), str) else None
    except (tarfile.TarError, OSError, ValueError, TypeError, KeyError, AttributeError):
        report["problems"].append("tar_unreadable: gzip present but tar stream is invalid")
        return report
    if sidecar is not None:
        if not sidecar.is_file():
            report["problems"].append("sidecar_missing")
        else:
            text = sidecar.read_text(encoding="utf-8").strip().split()
            expected = text[0].lower() if text else ""
            report["sidecar_ok"] = bool(SHA256_RE.match(expected)) and expected == report["sha256"]
            if not report["sidecar_ok"]:
                report["problems"].append("sidecar_hash_mismatch")
    report["ok"] = not report["problems"]
    return report


def known_original_fingerprint(raw: bytes) -> bool:
    """The published sample archive must be recognized, never rebuilt or edited."""
    return _sha256(raw) == "ba391bd7cb67751722ff48f4b4b566427590e6e22e08a28269eeae31a4533c7a"


def extract(archive: Path, dest: Path, force: bool = False) -> dict:
    """Verify into a staging dir, then move into place; refuse unsafe members.

    Nothing is written to the final destination until the whole archive has been
    validated, so a rejected upload never leaves partial files behind.
    """
    raw = archive.read_bytes()
    if known_original_fingerprint(raw):
        raise BuildError("this is the published original sample archive (layout reference only)")
    report = verify(archive)
    if not report.get("ok"):
        raise BuildError(f"refusing to extract an unverified archive: {report['problems']}")
    dest = dest.resolve()
    if dest.exists() and any(dest.iterdir()) and not force:
        raise BuildError(f"{dest} is not empty; pass --force to extract anyway")
    staging = Path(tempfile.mkdtemp(prefix="coregeek-extract-"))
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            seen: set[str] = set()
            for member in tar.getmembers():
                if not member.isreg() and not member.isdir():
                    raise BuildError(f"refusing special member {member.name!r}")
                if not _safe_member(member.name) or member.name in seen:
                    raise BuildError(f"refusing unsafe or duplicate member {member.name!r}")
                seen.add(member.name)
            tar.extractall(staging)
        dest.mkdir(parents=True, exist_ok=True)
        for item in sorted(staging.iterdir()):
            target = dest / item.name
            if target.exists():
                if not force:
                    raise BuildError(f"{target} already exists")
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            shutil.move(str(item), str(target))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"extracted_to": str(dest), "entries": report.get("members")}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build/verify the CoreGeek submission archive")
    sub = parser.add_subparsers(dest="command", required=True)

    build_cmd = sub.add_parser("build", help="build a deterministic bundle from a resolved commit")
    build_cmd.add_argument("--repo", type=Path, default=Path("."))
    build_cmd.add_argument("--ref", default="HEAD")
    build_cmd.add_argument("--output", type=Path, required=True)
    build_cmd.add_argument("--sidecar", type=Path, default=None)
    build_cmd.add_argument("--manifest", type=Path, default=None, help="also write the manifest beside the archive")
    build_cmd.add_argument("--no-src-mirror", action="store_true",
                           help="skip the CoreGeek/src compatibility mirror")

    verify_cmd = sub.add_parser("verify", help="validate container, layout, manifest and sidecar")
    verify_cmd.add_argument("--archive", type=Path, required=True)
    verify_cmd.add_argument("--sidecar", type=Path, default=None)
    verify_cmd.add_argument("--expect-root", default=BUNDLE_ROOT)
    verify_cmd.add_argument("--expect-entry", default=ENTRY)

    list_cmd = sub.add_parser("list", help="list members (no extraction)")
    list_cmd.add_argument("--archive", type=Path, required=True)

    extract_cmd = sub.add_parser("extract", help="verify, then extract to a directory")
    extract_cmd.add_argument("--archive", type=Path, required=True)
    extract_cmd.add_argument("--dest", type=Path, required=True)
    extract_cmd.add_argument("--force", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            _print(build(args.repo, args.ref, args.output, args.sidecar, args.manifest,
                         mirror_src=not args.no_src_mirror))
            return 0
        if args.command == "verify":
            report = verify(args.archive, expect_root=args.expect_root,
                            expect_entry=args.expect_entry, sidecar=args.sidecar)
            _print(report)
            return 0 if report["ok"] else 1
        if args.command == "list":
            report = verify(args.archive)
            if not report.get("ok"):
                _print(report)
                return 1
            with tarfile.open(args.archive, mode="r:gz") as tar:
                _print({"members": tar.getnames()})
            return 0
        if args.command == "extract":
            _print(extract(args.archive, args.dest, force=args.force))
            return 0
    except BuildError as error:
        _print({"ok": False, "error": str(error)})
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
