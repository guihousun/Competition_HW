"""Reproducible console-evidence loader/stitcher/summarizer (Issues 14-17).

Engineering tool only (R01/R08): it reads already-captured console/trace JSON
(summary rows) as **data**, stiches split log parts, removes exact duplicates,
replays them through the compact console digest and reports the line/byte
reduction against the historical one-line JSON console. It never executes
anything from the logs, never calls the network and never changes the agent.

Usage::

    python tools/analyze_console.py reduce rows-a.json rows-b.jsonl
    python tools/analyze_console.py reduce rows.json --dump compact-console.txt

Input may be a JSON array of summary rows (as produced by the issue analysis)
or JSONL where ``response {...}`` lines are extracted (``digest``/``identity``
parts are already-compact and skipped). Positions/rounds are reported so the
reduction can be re-checked by hand.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))

from agent import console_digest  # noqa: E402


def _rows_from_text(text: str) -> list[dict[str, Any]]:
    stripped = text.lstrip()
    if stripped.startswith("["):
        try:
            data = json.loads(text)
        except ValueError:
            return []
        return [row for row in data if isinstance(row, dict)]
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("response "):
            line = line[len("response "):]
        elif line.startswith("digest ") or line.startswith("identity "):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and ("round" in value or value.get("kind") == "response"):
            rows.append(value)
    return rows


def load_rows(paths: Iterable[str | Path]) -> tuple[list[dict[str, Any]], list[str]]:
    """Load one or more console parts, in order; returns (rows, warnings)."""
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for raw in paths:
        path = Path(raw)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            warnings.append(f"unreadable:{path.name}:{type(error).__name__}")
            continue
        found = _rows_from_text(text)
        if not found:
            warnings.append(f"no-summary-rows:{path.name}")
        rows.extend(found)
    return rows, warnings


def stitch(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Drop exact duplicate turns so split log parts do not inflate counts."""
    seen: set[str] = set()
    kept: list[dict[str, Any]] = []
    removed = 0
    for row in rows:
        if not isinstance(row, dict):
            removed += 1
            continue
        event = row.get("event")
        if isinstance(event, str) and event:
            key = "event:" + event
        else:
            key = "fp:" + json.dumps(
                {k: row.get(k) for k in ("round", "action_counts", "base_hp", "commands")},
                sort_keys=True, ensure_ascii=False)
        if key in seen:
            removed += 1
            continue
        seen.add(key)
        kept.append(row)
    return kept, removed


def old_console(rows: Iterable[dict[str, Any]]) -> list[str]:
    return ["response " + json.dumps(row, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")) for row in rows]


def compact_console(rows: Iterable[dict[str, Any]],
                    rollup_rounds: int | None = None) -> list[str]:
    digest = console_digest.ConsoleDigest(
        rollup_rounds=rollup_rounds or console_digest.ROLLUP_ROUNDS)
    lines: list[str] = []
    for row in rows:
        lines.extend(digest.observe(row))
    lines.extend(digest.flush())
    return lines


def measure(lines: Iterable[str]) -> dict[str, int]:
    count = 0
    size = 0
    for line in lines:
        count += 1
        size += len(line.encode("utf-8")) + 1
    return {"lines": count, "bytes": size}


def _int_token(tokens: dict[str, str], key: str) -> int:
    value = tokens.get(key)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _line_tokens(line: str) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for part in line.split():
        if "=" in part:
            key, value = part.split("=", 1)
            tokens[key] = value
        else:
            tokens[part] = ""
    return tokens


def _action_total(rows: Iterable[dict[str, Any]], name: str) -> int:
    total = 0
    for row in rows:
        actions = row.get("action_counts")
        if isinstance(actions, dict):
            value = actions.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                total += value
    return total


def _error_total(rows: Iterable[dict[str, Any]]) -> int:
    total = 0
    for row in rows:
        value = row.get("judge_errors_total")
        if value is None:
            value = row.get("protocol_errors")
        if isinstance(value, int) and not isinstance(value, bool):
            total += value
    return total


def _observed_damage(rows: Iterable[dict[str, Any]]) -> int:
    damage = 0
    last: int | None = None
    for row in rows:
        value = row.get("base_hp")
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        if last is not None and value < last:
            damage += last - value
        last = value
    return damage


def _phase_changes(rows: Iterable[dict[str, Any]]) -> int:
    changes = 0
    last = None
    for row in rows:
        phase = row.get("phase")
        if last is not None and phase != last:
            changes += 1
        last = phase
    return changes


def _compact_totals(lines: Iterable[str]) -> dict[str, int]:
    totals = {"errors": 0, "accept": 0, "submit": 0}
    for line in lines:
        if not line.startswith("digest rollup "):
            continue
        tokens = _line_tokens(line)
        totals["errors"] += _int_token(tokens, "errors")
        totals["accept"] += _int_token(tokens, "accept")
        totals["submit"] += _int_token(tokens, "submit")
    return totals


def _reduction(old: dict[str, int], new: dict[str, int]) -> dict[str, Any]:
    def ratio(before: int, after: int) -> float | None:
        return None if not before else round(1.0 - after / before, 4)
    return {
        "lines": old["lines"], "lines_new": new["lines"],
        "line_reduction": ratio(old["lines"], new["lines"]),
        "bytes": old["bytes"], "bytes_new": new["bytes"],
        "byte_reduction": ratio(old["bytes"], new["bytes"]),
    }


def reduce_report(paths: Iterable[str], *, rollup_rounds: int | None = None,
                  dump: str | None = None) -> dict[str, Any]:
    rows, warnings = load_rows(paths)
    rows, duplicates = stitch(rows)
    old_lines = old_console(rows)
    new_lines = compact_console(rows, rollup_rounds=rollup_rounds)
    if dump:
        Path(dump).write_text("\n".join(new_lines) + ("\n" if new_lines else ""),
                              encoding="utf-8")
    totals = _compact_totals(new_lines)
    expected = {
        "error_total": _error_total(rows),
        "accept": _action_total(rows, "acceptTask"),
        "submit": _action_total(rows, "submitAnswer"),
        "observed_damage": _observed_damage(rows),
        "phase_changes": _phase_changes(rows),
    }
    preserved = {
        "error_total": totals["errors"] == expected["error_total"],
        "accept": totals["accept"] == expected["accept"],
        "submit": totals["submit"] == expected["submit"],
    }
    return {
        "tool": "analyze_console",
        "inputs": [Path(p).name for p in paths],
        "rows": len(rows),
        "duplicates_removed": duplicates,
        "warnings": warnings,
        "old": measure(old_lines),
        "compact": measure(new_lines),
        "reduction": _reduction(measure(old_lines), measure(new_lines)),
        "expected": expected,
        "compact_totals": totals,
        "conservation": preserved,
        "dump": dump,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="replay console summary rows compactly")
    sub = parser.add_subparsers(dest="command", required=True)
    reduce_cmd = sub.add_parser("reduce", help="stitch + dedupe + compact + compare")
    reduce_cmd.add_argument("paths", nargs="+", help="JSON array or JSONL summary rows")
    reduce_cmd.add_argument("--rollup", type=int, default=None, help="rounds per rollup")
    reduce_cmd.add_argument("--dump", default=None, help="write compact console lines here")
    args = parser.parse_args(argv)
    if args.command == "reduce":
        report = reduce_report(args.paths, rollup_rounds=args.rollup, dump=args.dump)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if all(report["conservation"].values()) else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
