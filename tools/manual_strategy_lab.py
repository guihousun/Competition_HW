"""Small, persisted, public-observation-only lab for manual strategy trials.

This is a local experiment tool.  It uses the seeded demo simulator and the
independent, partial ``benchmark.audit`` checks; its reports are not an
official judge result.  The only input a manual handler receives is the same
public observation returned by ``agent.scenarios.observation``.

The lab deliberately keeps its local task fixture explicit: each task point
has three cases, a 15-round timeout and an 80/80 score/gold baseline.  The
type-1 case is a failed outcome and the type-2 case is a two-of-three outcome.
Those are economy-sensitivity fixtures, not an LLM or an official task key.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Demo" / "CoreGeek" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import scenarios, simulator  # noqa: E402
from benchmark import audit  # noqa: E402


LAB_VERSION = "manual-strategy-lab-v1"
DIFFICULTY = "medium"
DEFAULT_SEED = 311
HOLDOUT_SEED = 2029
DEFAULT_SIDE = "challenger"
DEFAULT_PROFILE = "observed-seven-days"
DEFAULT_MAP_LAYOUT = "attack-map-observed-v1"
DEFAULT_PRESSURE = 1
TASKS_PER_POINT = 3
TASK_TIMEOUT = 15
TASK_SCORE = 80
TASK_GOLD = 80

# The answer strings are local fixture data.  They are never copied to a
# public observation or trace.  Type 1 intentionally has no useful answer;
# type 2 has three independently gradeable fields for a two-of-three trial.
TASK_CASES = (
    {
        "id": "manual-type1-failed-v1",
        "task_type": "type1",
        "outcome": "failed",
        "description": "本地经济敏感夹具 type1：记录一次失败任务。只提交最终结果。",
        "answer": "__manual_type1_answer_not_supplied__",
        "grading": "exact",
        "timeout_rounds": TASK_TIMEOUT,
        "score_reward": TASK_SCORE,
        "gold_reward": TASK_GOLD,
    },
    {
        "id": "manual-type2-two-of-three-v1",
        "task_type": "type2",
        "outcome": "two_of_three",
        "description": "本地经济敏感夹具 type2：提交三个字段中的任意两个正确字段；本轮预期为两项通过。",
        "answer": "alpha=7;beta=11;gamma=13",
        "grading": "fields",
        "timeout_rounds": TASK_TIMEOUT,
        "score_reward": TASK_SCORE,
        "gold_reward": TASK_GOLD,
    },
)

# These are the four fixed experiment identities used by the eight-agent
# exercise: development/holdout crossed with both official sides.
EXPERIMENTS = tuple(
    {"set": name, "seed": seed, "side": side, "difficulty": DIFFICULTY}
    for name, seed in (("development", DEFAULT_SEED), ("holdout", HOLDOUT_SEED))
    for side in ("challenger", "defender")
)


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_hashes() -> dict[str, str]:
    """Hash the implementation used by a session so later runs are frozen."""
    paths = [ROOT / "benchmark.py", Path(__file__).resolve()]
    paths.extend(sorted(SRC.glob("agent/*.py")))
    result: dict[str, str] = {}
    for path in paths:
        if path.is_file():
            result[str(path.relative_to(ROOT)).replace("\\", "/")] = _sha256_bytes(path.read_bytes())
    return result


def source_fingerprint(hashes: dict[str, str] | None = None) -> str:
    return _sha256_bytes(_json_dump(hashes or source_hashes()).encode("utf-8"))


def _write_json(path: Path, value: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _session_paths(session_dir: str | Path) -> tuple[Path, Path, Path, Path]:
    directory = Path(session_dir).expanduser().resolve()
    return directory, directory / "state.json", directory / "trace.jsonl", directory / "summary.json"


def _public_observation(state: dict[str, Any]) -> dict[str, Any]:
    """Return compact fields from the official public observation projection."""
    raw = scenarios.observation(state)
    # Keep everything a manual player needs to issue an action, while avoiding
    # bulky protocol echoes and any private simulator keys.
    keep = (
        "roundNo", "mapInfo", "teamOur", "teamEnemy", "robot", "phaseTask",
        "playerTasks", "worldNews", "vendorShopList", "weaponShopList",
        "lastRoundRoleActionResults", "errors", "lastCmdResult", "llmResp",
    )
    public = {key: deepcopy(raw[key]) for key in keep if key in raw}
    roles = public.get("teamOur", {}).get("roles") or []
    robots = public.get("robot", {}).get("roles") or []
    enemy_roles = public.get("teamEnemy", {}).get("roles") or []
    public["metrics"] = {
        "round": int(public.get("roundNo") or 1),
        "day": ((int(public.get("roundNo") or 1) - 1) // 130) + 1,
        "phase": "day" if ((int(public.get("roundNo") or 1) - 1) % 130) < 70 else "night",
        "gold": int(public.get("teamOur", {}).get("goldNum") or 0),
        "score": int(public.get("teamOur", {}).get("totalScore") or 0),
        "alive_roles": sum(int(role.get("health") or 0) > 0 for role in roles),
        "visible_robots": sum(int(robot.get("health") or 0) > 0 for robot in robots),
        "visible_enemy_roles": len(enemy_roles),
        "base_health": next((int(role.get("health") or 0) for role in roles
                             if role.get("roleType") == "station"), 0),
    }
    # A private answer can only enter through the simulator's private task
    # world.  This assertion makes accidental exposure a hard local failure.
    encoded = _json_dump(public)
    for forbidden in ("_demo", "_engine", '"answer"', "sandbox_fixture"):
        if forbidden in encoded:
            raise RuntimeError(f"public observation contains forbidden field: {forbidden}")
    return public


def _install_task_fixture(state: dict[str, Any]) -> None:
    world = state["_demo"]["task_world"]
    world["agent_cases"] = deepcopy(TASK_CASES)
    for point in world["points"].values():
        point["tasks_left"] = TASKS_PER_POINT
        point["cooldown"] = 0
        point["active"] = None
    # Rebuild the public task catalogue using the fixture's public reward terms.
    from agent import taskworld
    state["teamOur"]["playerTasks"] = taskworld.player_tasks(
        state, world, state["teamOur"]["type"])


def _new_state(seed: int, side: str) -> dict[str, Any]:
    if side not in ("challenger", "defender"):
        raise ValueError("side must be challenger or defender")
    state = scenarios.scenario(
        seed, side, DEFAULT_PRESSURE, profile=DEFAULT_PROFILE,
        map_layout=DEFAULT_MAP_LAYOUT,
    )
    _install_task_fixture(state)
    return state


def _require_frozen(meta: dict[str, Any]) -> None:
    expected = meta.get("source_fingerprint")
    current_hashes = source_hashes()
    current = source_fingerprint(current_hashes)
    if expected != current or meta.get("source_hashes") != current_hashes:
        raise RuntimeError("source changed since session init; start a new session")


def _meta(seed: int, side: str, hashes: dict[str, str]) -> dict[str, Any]:
    return {
        "lab_version": LAB_VERSION,
        "difficulty": DIFFICULTY,
        "seed": int(seed),
        "side": side,
        "profile": DEFAULT_PROFILE,
        "map_layout": DEFAULT_MAP_LAYOUT,
        "pressure": DEFAULT_PRESSURE,
        "task_fixture": {
            "scope": "local economy sensitivity; no real LLM",
            "tasks_per_point": TASKS_PER_POINT,
            "timeout_rounds": TASK_TIMEOUT,
            "score_reward": TASK_SCORE,
            "gold_reward": TASK_GOLD,
            "outcomes": {case["task_type"]: case["outcome"] for case in TASK_CASES},
        },
        "source_hashes": hashes,
        "source_fingerprint": source_fingerprint(hashes),
    }


def init_session(session_dir: str | Path, *, seed: int = DEFAULT_SEED,
                 side: str = DEFAULT_SIDE, overwrite: bool = False) -> dict[str, Any]:
    directory, state_path, trace_path, summary_path = _session_paths(session_dir)
    if directory.exists() and any(directory.iterdir()) and not overwrite:
        raise FileExistsError(f"session directory is not empty: {directory}")
    directory.mkdir(parents=True, exist_ok=True)
    hashes = source_hashes()
    state = _new_state(int(seed), side)
    meta = _meta(int(seed), side, hashes)
    state["_manual_lab"] = meta
    _write_json(state_path, state)
    trace_path.write_text("", encoding="utf-8")
    _write_json(summary_path, _summary(meta, state, []))
    return {"session_dir": str(directory), "observation": _public_observation(state),
            "summary": _summary(meta, state, [])}


def _load(session_dir: str | Path) -> tuple[Path, Path, Path, Path, dict[str, Any], dict[str, Any]]:
    directory, state_path, trace_path, summary_path = _session_paths(session_dir)
    if not state_path.is_file():
        raise FileNotFoundError(f"missing session state: {state_path}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    meta = state.get("_manual_lab")
    if not isinstance(meta, dict):
        raise RuntimeError("state is not a manual strategy lab session")
    _require_frozen(meta)
    return directory, state_path, trace_path, summary_path, state, meta


def observe_session(session_dir: str | Path) -> dict[str, Any]:
    _, _, _, _, state, meta = _load(session_dir)
    return {"observation": _public_observation(state),
            "summary": _summary(meta, state, _read_trace(session_dir))}


def _read_action(value: str | None) -> dict[str, Any]:
    if value is None:
        raise ValueError("step requires --actions JSON or --actions-file")
    path = Path(value)
    raw = path.read_text(encoding="utf-8") if path.is_file() else value
    if raw == "-":
        raw = sys.stdin.read()
    data = json.loads(raw)
    if isinstance(data, dict) and "roleCommandMap" in data:
        data = data["roleCommandMap"]
    if not isinstance(data, dict):
        raise ValueError("actions must be a JSON object mapping role id to one command")
    commands: dict[str, Any] = {}
    for role_id, command in data.items():
        if not isinstance(role_id, str) or not isinstance(command, dict):
            raise ValueError("each action must be an object keyed by a string role id")
        if not isinstance(command.get("action"), str) or not command["action"]:
            raise ValueError(f"role {role_id} must contain exactly one action field")
        commands[role_id] = command
    return commands


def _feedback(before: dict[str, Any], after: dict[str, Any], commands: dict[str, Any],
              audit_errors: list[str]) -> dict[str, Any]:
    return {
        "audit_errors": list(audit_errors),
        "action_results": deepcopy(after.get("lastRoundRoleActionResults") or {}),
        "protocol_errors": deepcopy(after.get("errors") or []),
        "gold_delta": int(after.get("teamOur", {}).get("goldNum") or 0)
        - int(before.get("teamOur", {}).get("goldNum") or 0),
        "score_delta": int(after.get("teamOur", {}).get("totalScore") or 0)
        - int(before.get("teamOur", {}).get("totalScore") or 0),
        "metrics": deepcopy(after.get("metrics") or {}),
    }


def _read_trace(session_dir: str | Path) -> list[dict[str, Any]]:
    _, _, trace_path, _, = _session_paths(session_dir)
    if not trace_path.is_file():
        return []
    rows = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _summary(meta: dict[str, Any], state: dict[str, Any], trace: list[dict[str, Any]]) -> dict[str, Any]:
    actions = Counter()
    audit_errors = Counter()
    failed = 0
    for row in trace:
        actions.update((command.get("action") for command in row.get("action", {}).values()
                        if isinstance(command, dict) and command.get("action")))
        audit_errors.update(row.get("feedback", {}).get("audit_errors") or [])
        failed += sum(not ok for ok in (row.get("feedback", {}).get("action_results") or {}).values())
    public = _public_observation(state)
    return {
        "lab_version": meta["lab_version"],
        "difficulty": meta["difficulty"],
        "seed": meta["seed"],
        "side": meta["side"],
        "profile": meta["profile"],
        "map_layout": meta["map_layout"],
        "rounds": len(trace),
        "terminal": bool((state.get("_demo") or {}).get("finished")),
        "metrics": public["metrics"],
        "actions": dict(actions),
        "audit_errors": dict(audit_errors),
        "execution_failures": failed,
        "task_fixture": deepcopy(meta["task_fixture"]),
        "audit_scope": "benchmark.audit partial structural/action checks; local simulator only",
        "official_pass": False,
        "source_fingerprint": meta["source_fingerprint"],
        "trace_sha256": _sha256_bytes("".join(_json_dump(row) + "\n" for row in trace).encode("utf-8")),
    }


def step_session(session_dir: str | Path, commands: dict[str, Any]) -> dict[str, Any]:
    directory, state_path, trace_path, summary_path, state, meta = _load(session_dir)
    before = _public_observation(state)
    try:
        audit_errors = audit(before, commands)
    except (KeyError, TypeError, ValueError) as exc:
        audit_errors = [f"audit_error:{type(exc).__name__}:{exc}"]
    result = simulator.step(state, external_response={"roleCommandMap": deepcopy(commands)})
    new_state = result["state"]
    after = _public_observation(new_state)
    feedback = _feedback(before, after, commands, audit_errors)
    row = {"step": int(before.get("roundNo") or 1), "observation": before,
           "action": deepcopy(commands), "feedback": feedback}
    with trace_path.open("a", encoding="utf-8") as handle:
        handle.write(_json_dump(row) + "\n")
    new_state["_manual_lab"] = meta
    _write_json(state_path, new_state)
    trace = _read_trace(directory)
    summary = _summary(meta, new_state, trace)
    _write_json(summary_path, summary)
    return {"observation": after, "feedback": feedback, "summary": summary,
            "done": bool(result.get("done"))}


def _load_handler(spec: str) -> Callable[[dict[str, Any]], Any]:
    path_text, separator, function_name = spec.rpartition(":")
    if not separator or not function_name:
        raise ValueError("handler must be PATH.py:function")
    path = Path(path_text).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    module_spec = importlib.util.spec_from_file_location("manual_lab_handler", path)
    if module_spec is None or module_spec.loader is None:
        raise ImportError(f"cannot import handler: {path}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    handler = getattr(module, function_name, None)
    if not callable(handler):
        raise TypeError(f"handler is not callable: {function_name}")
    return handler


def run_session(session_dir: str | Path, handler_spec: str, max_steps: int) -> dict[str, Any]:
    if max_steps < 1:
        raise ValueError("max_steps must be positive")
    handler = _load_handler(handler_spec)
    result: dict[str, Any] = observe_session(session_dir)
    for _ in range(max_steps):
        obs = result["observation"]
        action = handler(deepcopy(obs))
        if isinstance(action, dict) and "roleCommandMap" in action:
            action = action["roleCommandMap"]
        if not isinstance(action, dict):
            raise ValueError("handler must return an action map or roleCommandMap response")
        result = step_session(session_dir, action)
        if result.get("done"):
            break
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="create a seeded session")
    init.add_argument("--session-dir", required=True)
    init.add_argument("--seed", type=int, default=DEFAULT_SEED)
    init.add_argument("--side", choices=("challenger", "defender"), default=DEFAULT_SIDE)
    init.add_argument("--holdout", action="store_true", help="use fixed holdout seed 2029")
    init.add_argument("--overwrite", action="store_true")
    for name in ("observe", "step", "run"):
        command = sub.add_parser(name)
        command.add_argument("--session-dir", required=True)
        if name == "step":
            command.add_argument("--actions")
            command.add_argument("--actions-file")
        if name == "run":
            command.add_argument("--handler", required=True, help="PATH.py:function")
            command.add_argument("--max-steps", type=int, default=1300)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "init":
        seed = HOLDOUT_SEED if args.holdout else args.seed
        result = init_session(args.session_dir, seed=seed, side=args.side, overwrite=args.overwrite)
    elif args.command == "observe":
        result = observe_session(args.session_dir)
    elif args.command == "step":
        if bool(args.actions) == bool(args.actions_file):
            raise ValueError("provide exactly one of --actions and --actions-file")
        result = step_session(args.session_dir, _read_action(args.actions or args.actions_file))
    else:
        result = run_session(args.session_dir, args.handler, args.max_steps)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
