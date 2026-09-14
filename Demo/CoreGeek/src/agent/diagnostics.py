"""Local engineering diagnostics: submission identity + bounded response summaries.

Engineering tool only (R01/R02/R03/R04/R08 unchanged). This module never touches
the judge response: it only prints one compact identity line at startup and one
bounded summary line per judge request, so a FAIL report can be classified
without asking the owner for fields by hand.

Hard rules:

* fail open — any error here is swallowed and can never turn a valid response
  into ``{}``, change gameplay, or delay the HTTP answer;
* no raw request, prompt, executeCmd, LLM reply, exception message or stack in
  the structured summary;
* absent/invalid fields stay ``None`` ("unknown"), which is distinct from a real
  zero; missing robot data never becomes "robots cleared";
* summaries are local metadata, never judge-response fields;
* no secrets, no absolute host paths, no environment dumps.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

LOGGER = logging.getLogger("agent.diagnostics")

MANIFEST_NAME = "submission-manifest.json"
MANIFEST_SCHEMA = "competition-hw-submission/1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
# Inventory hint only: carrying these items is NOT proof an answer is ready.
TASK_ITEM_HINTS = ("AcientTablet", "StarSand", "FlameBreath", "FrostPotion", "ThornAmulet", "IronWhistle")
DAY_ROUNDS = 70
ROUNDS_PER_DAY = 130
MAX_ROUND = 1300
CONTROLLERS = ("worker", "pioneer")
WEAPONS = ("gatling", "railgun", "rocket")
ACTION_KEYS = ("move", "build", "attack", "collect", "sell", "buy", "acceptTask",
               "submitAnswer", "summonTreasure", "use", "drop", "remove")
# Official judge error codes (接口文档 v1.0): only code 4 is a reported command
# error. 1/2 are task-timeout/answer errors, 3 network, 5 LLM quota, 0 unknown.
ERROR_NAMED_CODES = (1, 2, 3, 4, 5)
DECISION_TEXT_LIMIT = 80

_summary_emitter = None
_identity_emitted = False
_start_root: Path | None = None


def set_summary_emitter(emitter) -> None:
    """Test/diagnostic hook: redirect structured lines (None restores logging)."""
    global _summary_emitter
    _summary_emitter = emitter


def _emit(line: str) -> None:
    if _summary_emitter is not None:
        _summary_emitter(line)
        return
    LOGGER.info("%s", line)


def _safe_text(value: Any, limit: int = 200) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------

def _git_commit(root: Path) -> str | None:
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, encoding="utf-8", timeout=5, shell=False, cwd=str(root))
        if top.returncode != 0:
            return None
        checkout = Path(top.stdout.strip()).resolve()
        entry = (root / "main3.py").resolve().relative_to(checkout).as_posix()
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", "--", entry],
                                 capture_output=True, timeout=5, shell=False, cwd=str(checkout))
        if tracked.returncode != 0:
            return None
        done = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, encoding="utf-8", timeout=5, shell=False,
                              cwd=str(checkout))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    sha = (done.stdout or "").strip().lower()
    return sha if COMMIT_RE.match(sha) else None


SOURCE_MANIFEST = ("src/agent/brain.py", "src/agent/server.py", "src/agent/protocol.py",
                   "src/agent/simulator.py", "main3.py")


def package_root() -> Path:
    """Directory that owns ``main3.py`` (bundle root or ``Demo/CoreGeek``)."""
    return _start_root or Path(__file__).resolve().parents[2]


def set_start_root(root: str | Path | None) -> None:
    """Record the real startup directory so the manifest is found from there."""
    global _start_root
    _start_root = Path(root).resolve() if root else None


def find_manifest(start: Path | None = None) -> Path | None:
    """Nearest manifest from the real launch path, bounded to the bundle.

    Checks the launch directory, its parent and its grandparent — the three
    layouts a launcher can live in (nested runtime, ``Demo/CoreGeek``, bundle
    root). Nothing above that is consulted, so a bundle extracted inside an
    unrelated checkout can never pick up that checkout's files.
    """
    base = Path(start).resolve() if start else package_root()
    roots: list[Path] = []
    for directory in (base, base.parent, base.parent.parent, package_root()):
        if directory not in roots:
            roots.append(directory)
    for directory in roots:
        path = directory / MANIFEST_NAME
        if path.is_file():
            return path
    return None


def _portable_relpath(rel: Any) -> str | None:
    """Return a safe POSIX relative path, or None when it must be rejected."""
    if not isinstance(rel, str) or not rel or len(rel) > 300:
        return None
    if rel.startswith("/") or rel.startswith("\\") or "\\" in rel or ":" in rel:
        return None
    if any(part in ("", ".", "..") for part in rel.split("/")):
        return None
    return rel


def source_identity(root: Path | None = None) -> dict[str, Any]:
    """Source digest + per-file hashes; missing files are reported, never faked."""
    base = Path(root) if root else package_root()
    hashes: dict[str, str] = {}
    missing: list[str] = []
    for rel in SOURCE_MANIFEST:
        path = base / rel
        try:
            hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            missing.append(rel)
    digest = hashlib.sha256(
        json.dumps(hashes, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest() if hashes else None
    return {"files": hashes, "files_missing": missing, "source_digest": digest}


def _recompute_source_digest(entries: list[dict[str, Any]]) -> str:
    declared = {entry.get("path"): str(entry.get("sha256", "")).lower()
                for entry in entries if isinstance(entry, dict) and isinstance(entry.get("path"), str)}
    return hashlib.sha256(json.dumps(declared, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _source_digest_of(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _bundle_root_for(base: Path) -> Path:
    """The directory that owns the discovered manifest (its bundle root)."""
    path = find_manifest(base)
    return path.parent if path is not None else base.resolve()


def _resolve_declared(base: Path, rel: str) -> Path | None:
    """Resolve one bundle-relative manifest path, safely contained.

    Manifest paths include the archive root (``CoreGeek/...``) while the manifest
    itself lives inside that root, so the first segment is stripped before the
    lookup. The target must stay inside the bundle root, so ``..`` or a symlink
    escape is refused.
    """
    root = _bundle_root_for(base)
    inner = rel.split("/", 1)[1] if "/" in rel else rel
    try:
        resolved = (root / inner).resolve()
        if os.path.commonpath([str(root), str(resolved)]) != str(root):
            return None
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def load_manifest(root: Path | None = None) -> dict[str, Any]:
    """Validate the optional bundle manifest; malformed detail is never echoed.

    "verified" here means the declared content hashes match the files on disk —
    not that the bundle is officially certified.
    """
    base = Path(root).resolve() if root else package_root()
    path = find_manifest(base)
    if path is None:
        return {"state": "absent"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "unreadable"}
    if not isinstance(data, dict) or data.get("schema") != MANIFEST_SCHEMA:
        return {"state": "unexpected_shape"}
    entries = data.get("files")
    if not isinstance(entries, list) or not entries:
        return {"state": "unexpected_shape"}
    manifest_commit = data.get("commit")
    if not (isinstance(manifest_commit, str) and COMMIT_RE.fullmatch(manifest_commit)):
        return {"state": "unexpected_shape"}
    claimed_digest = data.get("source_digest")
    if not (isinstance(claimed_digest, str) and SHA256_RE.fullmatch(claimed_digest)):
        return {"state": "unexpected_shape"}
    base_real = os.path.realpath(base)
    mismatched = 0
    unsafe = 0
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            unsafe += 1
            continue
        rel = _portable_relpath(entry.get("path"))
        expected = entry.get("sha256")
        if (rel is None or not rel.startswith("CoreGeek/") or rel in seen
                or not (isinstance(expected, str) and SHA256_RE.fullmatch(expected))):
            unsafe += 1
            continue
        seen.add(rel)
        target = _resolve_declared(base, rel)
        if target is None:
            unsafe += 1           # missing, escaping, or refused by containment
            continue
        try:
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError:
            mismatched += 1
            continue
        if actual != expected.lower():
            mismatched += 1
    notes: list[str] = []
    if unsafe:
        notes.append(f"unsafe_or_missing_paths={unsafe}")
    if mismatched:
        notes.append(f"hash_mismatch={mismatched}")
    if claimed_digest is not None and _recompute_source_digest(entries) != claimed_digest.lower():
        notes.append("source_digest_mismatch")
    if notes:
        return {"state": "mismatch", "notes": notes, "file_count": len(entries)}
    return {"state": "verified", "commit": manifest_commit,
            "source_digest": claimed_digest, "file_count": len(entries),
            "generated_at": data.get("generated_at") if isinstance(data.get("generated_at"), str) else None,
            "entry": data.get("entry") if isinstance(data.get("entry"), str) else None}


def default_policy_identity() -> dict[str, Any]:
    """Default policy identity/loadout, read from the shipped defaults only."""
    loadout: list[str] | None = None
    try:
        from .brain import TOWER_LOADOUT  # local import keeps startup cheap and fail-open
        loadout = [str(item) for item in TOWER_LOADOUT]
    except Exception:  # noqa: BLE001 - identity must never break startup
        loadout = None
    return {"name": "default", "tower_loadout": loadout}


def startup_identity(entry: str | None = None, root: Path | None = None,
                     manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """One compact identity record (no secrets, no absolute host paths).

    A verified bundle manifest outranks ``git rev-parse``: an extracted bundle
    sitting inside someone else's repository must never report that parent
    repository's commit as the package version.
    """
    base = Path(root) if root else package_root()
    record_manifest = manifest if manifest is not None else load_manifest(base)
    commit = None
    commit_source = "unknown"
    if record_manifest.get("state") == "verified" and record_manifest.get("commit"):
        commit = str(record_manifest["commit"]).lower()
        commit_source = "manifest"
    elif record_manifest.get("state") == "absent":
        found = _git_commit(base)
        if found:
            commit = found
            commit_source = "git_checkout"
    return {
        "code_commit": commit,
        "commit_source": commit_source,
        "source": source_identity(base),
        "manifest": record_manifest,
        "entry": os.path.basename(entry) if entry else os.path.basename(sys.argv[0] or "unknown"),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "policy": default_policy_identity(),
    }


def emit_startup_identity(entry: str | None = None, root: Path | None = None) -> dict[str, Any] | None:
    """Print the identity line once. Never raises."""
    global _identity_emitted
    try:
        if root is not None:
            set_start_root(root)
        if _identity_emitted:
            return None
        record = startup_identity(entry=entry, root=root)
        _emit("identity " + json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        _identity_emitted = True
        return record
    except Exception:  # noqa: BLE001 - diagnostics must never break startup
        return None


# ---------------------------------------------------------------------------
# request parsing helpers
# ---------------------------------------------------------------------------

def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _roles(request: Any, key: str) -> list[dict[str, Any]] | None:
    if not isinstance(request, dict):
        return None
    group = request.get(key)
    if not isinstance(group, dict):
        return None
    roles = group.get("roles")
    if not isinstance(roles, list):
        return None
    return [role for role in roles if isinstance(role, dict)]


def _health_state(role: dict[str, Any]) -> str:
    """observed_positive | observed_zero | unknown (missing/invalid/non-finite)."""
    value = role.get("health")
    if isinstance(value, bool) or value is None:
        return "unknown"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if not math.isfinite(number):
        return "unknown"
    return "observed_positive" if number > 0 else "observed_zero"


def _alive(role: dict[str, Any]) -> bool:
    return _health_state(role) == "observed_positive"


def _cooldown_state(role: dict[str, Any]) -> str:
    """ready | cooling | unknown (missing/invalid/non-finite) — missing is NOT ready."""
    value = role.get("cooldown")
    if isinstance(value, bool) or value is None:
        return "unknown"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if not math.isfinite(number):
        return "unknown"
    return "ready" if number <= 0 else "cooling"


def _tally(roles: list[dict[str, Any]]) -> tuple[int | None, int]:
    """(count of observed-positive, count of unknown) — None when any is unknown."""
    unknown = sum(1 for role in roles if _health_state(role) == "unknown")
    if unknown:
        return None, unknown
    return sum(1 for role in roles if _health_state(role) == "observed_positive"), 0


def _team_type(request: Any) -> str | None:
    team = request.get("teamOur") if isinstance(request, dict) else None
    kind = team.get("type") if isinstance(team, dict) else None
    return kind if isinstance(kind, str) and kind else None


def _robots_targeting_us(request: Any, our_type: str | None) -> int | None:
    """Robots whose ``targetTeam`` is our side — None when that field is absent.

    Global visible robots are not threats to us; ``targetTeam`` is the only
    published field that links a robot to a side, and it is not always present.
    A partially reported field stays unknown instead of implying zero.
    """
    if our_type is None or not isinstance(request, dict):
        return None
    group = request.get("robot")
    roles = group.get("roles") if isinstance(group, dict) else None
    if not isinstance(roles, list) or not roles:
        return None
    values = [role.get("targetTeam") for role in roles if isinstance(role, dict)]
    if len(values) != len(roles) or any(value is None for value in values):
        return None
    return sum(1 for value in values if value == our_type)


def _errors(request: Any) -> dict[str, Any]:
    """Exact judge error-code histogram; missing stays unknown, never zero.

    ``errorCode`` must be a real integer (bool/str/float/list are malformed and
    land in the unknown bucket). Code 0 is the published "unknown" code.
    """
    out: dict[str, Any] = {"judge_errors_total": None, "errors_by_code": None,
                           "command_errors": None, "task_errors": None,
                           "llm_quota_errors": None, "network_errors": None,
                           "unknown_errors": None}
    errors = request.get("errors") if isinstance(request, dict) else None
    if not isinstance(errors, list):
        return out
    by_code: dict[int, int] = {}
    unknown = 0
    for entry in errors:
        code = entry.get("errorCode") if isinstance(entry, dict) else None
        if isinstance(code, bool) or not isinstance(code, int):
            unknown += 1
            continue
        by_code[code] = by_code.get(code, 0) + 1
        if code not in ERROR_NAMED_CODES and code != 0:
            unknown += 1
    out["judge_errors_total"] = len(errors)
    out["errors_by_code"] = by_code
    out["command_errors"] = by_code.get(4, 0)
    out["task_errors"] = by_code.get(1, 0) + by_code.get(2, 0)
    out["llm_quota_errors"] = by_code.get(5, 0)
    out["network_errors"] = by_code.get(3, 0)
    out["unknown_errors"] = unknown + by_code.get(0, 0)
    return out


def _counts(request: Any) -> dict[str, Any]:
    ours = _roles(request, "teamOur")
    out: dict[str, Any] = {"base_hp": None, "base_level": None, "base_health_state": None,
                           "workers_live": None, "pioneers_live": None, "weapons_live": None,
                           "weapons_ready": None, "walls_live": None, "health_unknown": 0,
                           "cooldown_unknown": 0, "task_items_in_backpack": None,
                           "action_results_false": None, "protocol_errors": None,
                           "gold": None, "score": None, "team_roles_live": None,
                           "weapon_counts": None, "weapon_levels": None}
    team = request.get("teamOur") if isinstance(request, dict) else None
    if isinstance(team, dict):
        out["gold"] = _as_int(team.get("goldNum"))
        out["score"] = _as_int(team.get("totalScore"))
    out.update(_errors(request))
    if ours is None:
        return out
    unknown_total = 0
    for kind, key in (("worker", "workers_live"), ("pioneer", "pioneers_live"),
                      ("wall", "walls_live")):
        count, unknown = _tally([r for r in ours if r.get("roleType") == kind])
        out[key] = count
        unknown_total += unknown
    weapons = [r for r in ours if r.get("roleType") in WEAPONS]
    out["weapons_live"], unknown = _tally(weapons)
    unknown_total += unknown
    if out["weapons_live"] is not None:
        out["weapon_counts"] = {
            kind: sum(1 for r in weapons if r.get("roleType") == kind and _alive(r))
            for kind in WEAPONS
        }
    alive_weapons = [r for r in weapons if _alive(r)]
    if out["weapons_live"] is not None:
        levels = {}
        for role in alive_weapons:
            level = _as_int(role.get('level'))
            label = str(role['roleType'])+'.L'+str(level if level is not None else '?')
            levels[label] = levels.get(label, 0)+1
        out['weapon_levels'] = levels
    states = [_cooldown_state(r) for r in alive_weapons]
    out["cooldown_unknown"] = states.count("unknown")
    if states and out["weapons_live"] is not None:
        out["weapons_ready"] = None if out["cooldown_unknown"] else states.count("ready")
    out["health_unknown"] = unknown_total
    total_roles, total_unknown = _tally(ours)
    out["team_roles_live"] = None if total_unknown else total_roles
    stations = [r for r in ours if r.get("roleType") == "station"]
    if stations:
        station = max(stations, key=lambda r: _as_float(r.get("health")) or float("-inf"))
        out["base_hp"] = _as_int(station.get("health"))
        out["base_level"] = _as_int(station.get("level"))
        out["base_health_state"] = _health_state(station)
    carried = 0
    task_items = False
    for role in ours:
        if not _alive(role):
            continue
        backpack = role.get("backpack")
        if not isinstance(backpack, list):
            continue
        names = {str(item) for item in backpack}
        if names & set(TASK_ITEM_HINTS):
            task_items = True
            carried += 1
    out["task_items_in_backpack"] = task_items
    out["task_items_carriers"] = carried
    results = request.get("lastRoundRoleActionResults") if isinstance(request, dict) else None
    if isinstance(results, dict):
        out["action_results_false"] = sum(1 for value in results.values() if value is False)
    # Compatibility field. It counts reported command errors (errorCode 4) only;
    # task/answer/network/quota failures are NOT protocol violations, and this is
    # never proof that the judge disqualified the run.
    out["protocol_errors"] = out["command_errors"]
    return out


def _robots_visible(request: Any) -> int | None:
    """Living robots from the official observation; None when the field is absent."""
    if not isinstance(request, dict):
        return None
    group = request.get("robot")
    if not isinstance(group, dict):
        return None
    roles = group.get("roles")
    if not isinstance(roles, list):
        return None
    count, unknown = _tally([r for r in roles if isinstance(r, dict)])
    return None if unknown else count


def _controller_summary(request: Any, response: Any) -> dict[str, Any]:
    """Report issued attack assignments, never infer pairs from ID ordering.

    An issued assignment is not proof that the platform executed the attack.
    Cooldown readiness alone does not establish range or controller eligibility.
    """
    ours = _roles(request, "teamOur")
    if ours is None:
        return {"issued_attacks": None, "controllers_live": None, "weapons_ready": None}
    roles = [r for r in ours if r.get("roleType") in CONTROLLERS]
    weapons = [r for r in ours if r.get("roleType") in WEAPONS]
    by_id = {str(r.get("id")): r for r in weapons}
    commands = response.get("roleCommandMap") if isinstance(response, dict) else None
    pairs = []
    for wid, command in (commands.items() if isinstance(commands, dict) else []):
        if not isinstance(command, dict) or command.get("action") != "attack":
            continue
        weapon = by_id.get(str(wid), {})
        kind = weapon.get("roleType")
        pairs.append({"controller": _as_int(command.get("controllerId")),
                      "weapon": _as_int(wid), "kind": kind if kind in WEAPONS else None,
                      "cooldown": _as_int(weapon.get("cooldown")),
                      "cooldown_state": _cooldown_state(weapon)})
        if len(pairs) == 3:
            break
    states = [_cooldown_state(r) for r in weapons if _alive(r)]
    _, unknown = _tally(weapons)
    return {"issued_attacks": pairs if isinstance(commands, dict) else None,
            "controllers_live": _tally(roles)[0],
            "weapons_ready": None if unknown or "unknown" in states else states.count("ready")}


def _command_counts(response: Any) -> tuple[int | None, dict[str, int] | None, int | None]:
    if not isinstance(response, dict):
        return None, None, None
    commands = response.get("roleCommandMap")
    if not isinstance(commands, dict):
        return None, None, None
    counts: dict[str, int] = {}
    unknown = 0
    for command in commands.values():
        if not isinstance(command, dict):
            unknown += 1
            continue
        action = command.get("action")
        if isinstance(action, str) and action in ACTION_KEYS:
            counts[action] = counts.get(action, 0) + 1
        else:
            unknown += 1
    return len(commands), counts, unknown or None


def _phase(round_no: int | None) -> str | None:
    if round_no is None or round_no < 1 or round_no > MAX_ROUND:
        return None
    offset = (round_no - 1) % ROUNDS_PER_DAY
    return "day" if offset < DAY_ROUNDS else "night"


def _bounded_text(value: Any, limit: int = DECISION_TEXT_LIMIT) -> str | None:
    """Only accept a short, clean string; anything else stays unknown."""
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    return value if value.isprintable() else None


def _bounded_ids(value: Any) -> int | None:
    """Supervisor ``relevant_robots`` is a COUNT integer, never a robot id list."""
    return _as_int(value)


def _bounded_point(value: Any) -> dict[str, int] | None:
    """Bounded ``{x, y}`` coordinates; anything else stays absent."""
    if not isinstance(value, dict):
        return None
    point: dict[str, int] = {}
    for axis in ("x", "y"):
        number = _as_int(value.get(axis))
        if number is not None:
            point[axis] = number
    return point or None


def _bounded_bool_or_text(value: Any) -> Any:
    """Keep a real bool; otherwise accept a short clean string, else None."""
    if isinstance(value, bool):
        return value
    return _bounded_text(value)


def _bounded_decision(decision: Any) -> dict[str, Any] | None:
    """Allowlisted, bounded view of the optional supervisor decision report.

    Accepted interface (Codex-owned): ``supervisor`` carries ``mode``,
    ``reserve_pioneer``, ``reason``, the **count** ``relevant_robots``,
    ``return_steps_lower_bound``, ``ready_workers`` and ``visible_pressure_hp``;
    ``task`` carries the direct fields ``phase``, ``cycle_active``,
    ``plan_kind``, ``pending_command``, ``pending_prompt`` and the nested
    ``acceptance_status`` (``phase``, ``point`` {x,y}, ``retry_after``,
    ``reason``). Unknown keys are dropped, absent fields stay absent, and a
    report that carries nothing usable becomes ``None`` instead of an invented
    record. Nested ``cycle``/``last_plan`` are only tolerated as a fallback.
    """
    if not isinstance(decision, dict):
        return None
    out: dict[str, Any] = {}
    supervisor = decision.get("supervisor")
    if isinstance(supervisor, dict):
        record: dict[str, Any] = {}
        for key in ("mode", "reason"):
            text = _bounded_text(supervisor.get(key))
            if text is not None:
                record[key] = text
        advice = supervisor.get("recommended_reserve")
        if isinstance(advice, bool):
            record["recommended_reserve"] = advice
        reserve = supervisor.get("reserve_pioneer")
        if isinstance(reserve, bool):
            record["reserve_pioneer"] = reserve
        else:
            bound = _as_int(reserve)
            if bound is not None:
                record["reserve_pioneer"] = bound
        for key in ("return_steps_lower_bound", "relevant_robots",
                    "ready_workers", "visible_pressure_hp"):
            number = _as_int(supervisor.get(key))
            if number is not None:
                record[key] = number
        if record:
            out["supervisor"] = record
    task = decision.get("task")
    if isinstance(task, dict):
        record = {}
        phase = _bounded_text(task.get("phase"))
        if phase is not None:
            record["phase"] = phase
        if isinstance(task.get("cycle_active"), bool):
            record["cycle_active"] = task["cycle_active"]
        plan_kind = _bounded_text(task.get("plan_kind"))
        if plan_kind is not None:
            record["plan_kind"] = plan_kind
        pending_command = _bounded_bool_or_text(task.get("pending_command"))
        if pending_command is not None:
            record["pending_command"] = pending_command
        pending_prompt = _bounded_bool_or_text(task.get("pending_prompt"))
        if pending_prompt is not None:
            record["pending_prompt"] = pending_prompt
        # Fallback only: the accepted interface uses the direct fields above.
        cycle = task.get("cycle")
        if isinstance(cycle, dict) and "phase" not in record:
            fallback = _bounded_text(cycle.get("phase"))
            if fallback is not None:
                record["phase"] = fallback
        plan = task.get("last_plan")
        if isinstance(plan, dict) and "plan_kind" not in record:
            fallback = _bounded_text(plan.get("kind"))
            if fallback is not None:
                record["plan_kind"] = fallback
        status = task.get("acceptance_status")
        if isinstance(status, dict):
            view: dict[str, Any] = {}
            for key in ("phase", "reason"):
                text = _bounded_text(status.get(key))
                if text is not None:
                    view[key] = text
            point = _bounded_point(status.get("point"))
            if point is not None:
                view["point"] = point
            retry = _as_int(status.get("retry_after"))
            if retry is not None:
                view["retry_after"] = retry
            if view:
                record["acceptance_status"] = view
        if record:
            out["task"] = record
    return out or None


# ---------------------------------------------------------------------------
# response summary
# ---------------------------------------------------------------------------

def build_summary(request: Any, response: Any, *, plan_ms: float | None = None,
                  invalid_input: bool = False, decision_exception: str | None = None,
                  event_id: str | None = None,
                  decision: dict[str, Any] | None = None) -> dict[str, Any]:
    """Bounded structured summary of one judge request/response pair.

    Reads only the observation and the response that already exist: it never runs
    planning again and never mutates PlannerState. ``decision`` is the optional
    supervisor report Codex captures in the HTTP handler and passes in
    explicitly; ``None`` keeps the historical summary shape.
    """
    round_no = _as_int(request.get("roundNo")) if isinstance(request, dict) else None
    counts = _counts(request)
    commands = response.get("roleCommandMap") if isinstance(response, dict) else None
    tally = _command_counts(response)
    summary: dict[str, Any] = {
        "kind": "response",
        "round": round_no,
        "phase": _phase(round_no),
        "commands": tally[0],
    }
    if event_id:
        summary["event"] = event_id
    summary.update({
        "action_counts": tally[1],
        "unknown_actions": tally[2],
        "has_prompt": bool(response.get("prompt")) if isinstance(response, dict) else None,
        "has_execute_cmd": bool(response.get("executeCmd")) if isinstance(response, dict) else None,
        "base_hp": counts["base_hp"],
        "base_level": counts["base_level"],
        "base_health_state": counts.get("base_health_state"),
        "workers_live": counts["workers_live"],
        "pioneers_live": counts["pioneers_live"],
        "weapons_live": counts["weapons_live"],
        "weapons_ready": counts["weapons_ready"],
        "walls_live": counts["walls_live"],
        "health_unknown": counts.get("health_unknown"),
        "cooldown_unknown": counts.get("cooldown_unknown"),
        "robots_visible": _robots_visible(request),
        "robots_targeting_us": _robots_targeting_us(request, _team_type(request)),
        "controllers": _controller_summary(request, response),
        "task_items_in_backpack": counts.get("task_items_in_backpack"),
        "task_items_carriers": counts.get("task_items_carriers"),
        "action_results_false": counts["action_results_false"],
        "protocol_errors": counts["protocol_errors"],
        "gold": counts.get("gold"),
        "score": counts.get("score"),
        "team_roles_live": counts.get("team_roles_live"),
        "weapon_counts": counts.get("weapon_counts"),
        "weapon_levels": counts.get("weapon_levels"),
        "judge_errors_total": counts.get("judge_errors_total"),
        "errors_by_code": counts.get("errors_by_code"),
        "command_errors": counts.get("command_errors"),
        "task_errors": counts.get("task_errors"),
        "llm_quota_errors": counts.get("llm_quota_errors"),
        "network_errors": counts.get("network_errors"),
        "unknown_errors": counts.get("unknown_errors"),
        "plan_ms": None if plan_ms is None else round(float(plan_ms), 1),
        "empty_reason": None,
    })
    report = _bounded_decision(decision)
    if report is not None:
        summary["decision"] = report
    if invalid_input:
        summary["empty_reason"] = "invalid_input"
    elif decision_exception:
        summary["empty_reason"] = "decision_exception"
    elif tally[0] == 0:
        summary["empty_reason"] = classify_empty(
            counts, tally, robots_visible=summary["robots_visible"],
            channel_only=bool(summary["has_prompt"] or summary["has_execute_cmd"]))
    if decision_exception:
        summary["decision_error"] = decision_exception
    return summary


def classify_empty(counts: dict[str, Any], tally: tuple[Any, Any, Any],
                   channel_only: bool = False,
                   robots_visible: int | None = None) -> str:
    """Observable category for an empty command map — never a causal claim.

    Unknown health is not death: with missing data the category stays
    ``unclassified`` instead of asserting a destroyed base or cleared robots.
    ``all_weapons_cooling`` and ``ready_weapons_no_attack`` are observable
    states only; the digest never invents a tactical reason from thin data.
    """
    if channel_only:
        return "channel_only"
    state = counts.get("base_health_state")
    if state == "observed_zero":
        return "base_observed_dead"
    if state != "observed_positive":
        return "unclassified"
    if counts.get("health_unknown"):
        return "unclassified"
    worker_known = counts.get("workers_live") is not None
    pioneer_known = counts.get("pioneers_live") is not None
    if worker_known and pioneer_known and not (counts["workers_live"] + counts["pioneers_live"]):
        return "no_live_controllers"
    if counts.get("weapons_live") == 0:
        return "no_weapons"
    ready = counts.get("weapons_ready")
    if ready is None:
        return "unclassified"          # cooldown unknown: no reason is fabricated
    if ready == 0:
        return "all_weapons_cooling"
    if robots_visible == 0:
        return "observed_no_robots"
    return "ready_weapons_no_attack"


def emit_response_summary(summary: dict[str, Any], stream: str | None = None) -> None:
    """Console emission for one structured summary. Never raises (fail open).

    ``COMPETITION_HW_CONSOLE=compact`` (default) routes through the bounded
    console digest; ``full`` restores the historical one-line JSON summary;
    ``off`` silences this console stream only. The JSONL trace is unaffected.
    """
    try:
        from . import console_digest
        console_digest.emit_summary(summary, emitter=_emit, stream=stream)
    except Exception:  # noqa: BLE001 - diagnostics must never affect the judge path
        return


def response_summary(request: Any, response: Any, **kwargs: Any) -> None:
    """Convenience wrapper used by the HTTP hook: build + emit, swallowing errors.

    The supervisor ``decision`` report (Codex-owned) rides through ``kwargs`` so
    the flat submission server needs no change; it is always passed explicitly,
    never looked up from a background thread's context.
    """
    try:
        summary = build_summary(request, response, **kwargs)
    except Exception:  # noqa: BLE001
        return
    stream = None
    try:
        from . import console_digest
        stream = console_digest.stream_token(request, summary.get("event"))
    except Exception:  # noqa: BLE001
        stream = None
    emit_response_summary(summary, stream=stream)


class Timer:
    """Wall-clock helper for measuring the request handling region only."""

    def __init__(self) -> None:
        self._start = time.perf_counter()

    def ms(self) -> float:
        return (time.perf_counter() - self._start) * 1000.0


def utf8_stdout() -> None:
    """Make Windows stdout UTF-8 so structured lines stay readable."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            continue
