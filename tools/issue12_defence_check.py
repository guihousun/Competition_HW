"""Issue 12 check: baseline defence layout vs the candidate layout.

Local evidence only. This runs the *same* local simulator twice — once with the
baseline source (the merge-base commit, extracted read-only with ``git archive``)
and once with this working tree — and compares the observable consequences of the
defence change:

* the wall ring geometry (does the ring have an unintended hole? which cells are
  left open?), read through ``brain._wall_order`` and checked against a ring
  written out by hand here, not against the strategy's own idea of the ring;
* the tower sites (which side of the base they sit on);
* whether the crew can still leave the finished ring;
* how the match ended locally (base health, survival, score, kills);
* action-audit errors and execution failures;
* the policy decision latency per round.

It is deliberately *not* an official certification and not a claim about the
judge's server: it uses the local single-team simulator, local spawn fixtures and
local build-region assumptions. See ``reports/issue-12-defence/REPORT.md``.

Usage::

    python tools/issue12_defence_check.py --rounds 130 --seeds 1,90317

Everything is written under ``--output`` (default ``reports/issue-12-defence``);
an existing non-empty output directory is refused so old evidence is never
overwritten.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time

HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]
DEFAULT_SEEDS = "1,90317"
DEFAULT_SIDES = "challenger,defender"
# The commit Issue #12 was reported against; pinned so the comparison still means
# the same thing after Codex commits the candidate on top of it.
BASELINE_COMMIT = "dd5cf5577c55f421342e571f64757fafa79bbfaf"
# The four bases the hand-computed layout expectations use: the reported
# lower-left base with an eastern approach, its horizontal mirror, and a base
# high/low enough that the approach is vertical.
PROBE_BASES = ((9, 22), (30, 22), (20, 28), (20, 6))
WIDTH, HEIGHT = 41, 32
WALL_RADIUS = 2


# --------------------------------------------------------------------------
# Geometry, computed here independently of the strategy under test.
# --------------------------------------------------------------------------

def footprint_bounds(base):
    x, y = base
    return min(x, x + 1), max(x, x + 1), min(y - 1, y), max(y - 1, y)


def geometric_ring(base):
    """The radius-2 perimeter of a 2x2 base, written out literally."""
    xmin, xmax, ymin, ymax = footprint_bounds(base)
    cells = set()
    for cx in range(xmin - WALL_RADIUS, xmax + WALL_RADIUS + 1):
        for cy in range(ymin - WALL_RADIUS, ymax + WALL_RADIUS + 1):
            if cx in (xmin - WALL_RADIUS, xmax + WALL_RADIUS) or \
                    cy in (ymin - WALL_RADIUS, ymax + WALL_RADIUS):
                cells.add((cx, cy))
    return cells


def footprint_distance(cell, base):
    xmin, xmax, ymin, ymax = footprint_bounds(base)
    dx = max(xmin - cell[0], 0, cell[0] - xmax)
    dy = max(ymin - cell[1], 0, cell[1] - ymax)
    return max(dx, dy)


def probe_payload(base):
    """A minimal observation with just a base and a worker, for layout probes."""
    roles = [
        {"id": 10013, "roleType": "station", "pos": {"x": base[0], "y": base[1]},
         "health": 1500, "level": 1, "backpack": []},
        {"id": 10010, "roleType": "worker", "pos": {"x": base[0] + 3, "y": base[1] - 1},
         "health": 220, "level": 1, "backPackCapability": 100, "backpack": []},
    ]
    return {"roundNo": 13,
            "mapInfo": {"width": WIDTH, "height": HEIGHT, "zones": []},
            "teamOur": {"type": "challenger", "goldNum": 75, "totalScore": 0,
                        "roles": roles, "playerTasks": []},
            "teamEnemy": {"roles": []}, "robot": {"roles": []},
            "vendorShopList": [], "weaponShopList": []}


def layout_probe(brain, Pos, Turn):
    """Per-base wall ring/tower sites, read through the strategy's own helpers."""
    rows = []
    for base in PROBE_BASES:
        turn = Turn.load(probe_payload(base))
        walls = sorted((p.x, p.y) for p in brain._wall_order(turn))
        towers = sorted((p.x, p.y) for p in brain._tower_sites(turn))
        ring = geometric_ring(base)
        wanted = expected_exit(base)
        rows.append({
            "base": list(base),
            "wall_count": len(walls),
            "ring_count": len(ring),
            "expected_exit": sorted(wanted),
            "opening": sorted(ring - set(walls)),
            "unintended_holes": sorted(ring - set(walls) - wanted),
            "rear_walled_over": sorted(set(walls) & wanted),
            "walls": walls,
            "tower_sites": towers,
            "tower_sides": sorted({side_of(tower, base) for tower in towers}),
        })
    return rows


def side_of(cell, base):
    """Which side(s) of the base a cell lies on (one ring out counts as that side)."""
    x, y = cell
    xmin, xmax, ymin, ymax = footprint_bounds(base)
    sides = []
    if x > xmax:
        sides.append("E")
    elif x < xmin:
        sides.append("W")
    if y > ymax:
        sides.append("N")
    elif y < ymin:
        sides.append("S")
    return "".join(sides) or "?"


def expected_exit(base):
    """The opening this tool *expects*, derived here from the prior alone.

    Same strategy prior as the candidate — the enemy comes from the map interior,
    so the wall is raised on the approach side and the exit cut on the rear — but
    computed here from the map centre, and with the two most central rear cells
    picked by a literal index. Used to detect an unintended hole independently of
    the strategy's own `exit_cells`.
    """
    xmin, xmax, ymin, ymax = footprint_bounds(base)
    base_cx = (xmin + xmax) / 2.0
    base_cy = (ymin + ymax) / 2.0
    dx = (WIDTH - 1) / 2.0 - base_cx
    dy = (HEIGHT - 1) / 2.0 - base_cy
    if abs(dx) >= abs(dy):
        approach = "E" if dx > 0 else "W"
    else:
        approach = "N" if dy > 0 else "S"
    rear = {"E": "W", "W": "E", "N": "S", "S": "N"}[approach]
    if rear == "S":
        run = [(x, ymin - WALL_RADIUS) for x in range(xmin - WALL_RADIUS, xmax + WALL_RADIUS + 1)]
    elif rear == "N":
        run = [(x, ymax + WALL_RADIUS) for x in range(xmin - WALL_RADIUS, xmax + WALL_RADIUS + 1)]
    elif rear == "W":
        run = [(xmin - WALL_RADIUS, y)
               for y in range(ymin - WALL_RADIUS + 1, ymax + WALL_RADIUS)]
    else:
        run = [(xmax + WALL_RADIUS, y)
               for y in range(ymin - WALL_RADIUS + 1, ymax + WALL_RADIUS)]
    start = (len(run) - 2) // 2
    return set(run[start:start + 2])


# --------------------------------------------------------------------------
# Crew escape: can a role inside the finished ring still get out?
# --------------------------------------------------------------------------

def crew_trapped(state, base):
    """How many workers/pioneers inside the ring cannot reach outside it.

    Blocked cells are walls, buildings and non-land terrain. Other roles and
    robots are transient and are ignored. Eight-way movement, as in R02.
    """
    zone_kind = {(z["pos"]["x"], z["pos"]["y"]): z["neutralType"]
                 for z in state["mapInfo"].get("zones") or []}
    blocked = set()
    for pos, kind in zone_kind.items():
        if kind != "land":
            blocked.add(pos)
    for unit in state["teamOur"]["roles"] + (state.get("teamEnemy") or {}).get("roles", []):
        if unit.get("health", 0) <= 0:
            continue
        kind = unit["roleType"]
        if kind in ("worker", "pioneer"):
            continue
        x, y = unit["pos"]["x"], unit["pos"]["y"]
        if kind == "station":
            blocked.update({(x, y), (x + 1, y), (x, y - 1), (x + 1, y - 1)})
        else:
            blocked.add((x, y))
    interior = []
    for unit in state["teamOur"]["roles"]:
        if unit.get("health", 0) <= 0 or unit["roleType"] not in ("worker", "pioneer"):
            continue
        cell = (unit["pos"]["x"], unit["pos"]["y"])
        if footprint_distance(cell, base) <= WALL_RADIUS:
            interior.append(cell)
    trapped = []
    for start in interior:
        seen = {start}
        queue = [start]
        escaped = False
        while queue and not escaped:
            cx, cy = queue.pop()
            if footprint_distance((cx, cy), base) > WALL_RADIUS:
                escaped = True
                break
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nxt = (cx + dx, cy + dy)
                    if nxt in seen or nxt in blocked:
                        continue
                    if not (0 <= nxt[0] < WIDTH and 0 <= nxt[1] < HEIGHT):
                        continue
                    seen.add(nxt)
                    queue.append(nxt)
        if not escaped:
            trapped.append(list(start))
    return trapped


# --------------------------------------------------------------------------
# Child process: one source tree, run the local simulator.
# --------------------------------------------------------------------------

def run_source(args):
    source = Path(args.source).resolve()
    sys.path.insert(0, str(source))
    sys.path.insert(0, str(source / "Demo/CoreGeek/src"))

    import agent.simulator as simulator
    from agent import brain
    from agent.protocol import Pos, Turn
    from agent.scenarios import observation, scenario
    from benchmark import audit

    policy_ms = []
    real_plan = simulator.plan_for_state

    def timed_plan(*call_args, **call_kwargs):
        started = time.perf_counter()
        try:
            return real_plan(*call_args, **call_kwargs)
        finally:
            policy_ms.append((time.perf_counter() - started) * 1000.0)

    simulator.plan_for_state = timed_plan

    def hashes():
        files = sorted((source / "Demo/CoreGeek/src/agent").glob("*.py"))
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}

    report = {
        "source": str(source),
        "source_sha256": hashlib.sha256(
            json.dumps(hashes(), sort_keys=True).encode()).hexdigest(),
        "files": hashes(),
        "layout_probe": layout_probe(brain, Pos, Turn),
        "cases": [],
    }

    for seed in args.seeds:
        for side in args.sides:
            policy_ms.clear()
            state = scenario(seed, side, args.pressure)
            audit_errors, actions = Counter(), Counter()
            failures, rounds, walls_seen = 0, 0, []
            towers = None
            for _ in range(args.rounds):
                public = observation(state)
                result = simulator.step(state)
                rounds += 1
                commands = result["executed"]
                audit_errors.update(audit(public, commands))
                actions.update(c["action"] for c in commands.values())
                state = result["state"]
                failures += sum(not value
                                for value in state["lastRoundRoleActionResults"].values())
                walls_seen.append(sum(1 for u in state["teamOur"]["roles"]
                                      if u["roleType"] == "wall" and u["health"] > 0))
                if towers is None:
                    built = [u for u in state["teamOur"]["roles"]
                             if u["roleType"] in ("gatling", "railgun", "rocket")
                             and u["health"] > 0]
                    if len(built) == 3:
                        towers = sorted([[u["roleType"], u["pos"]["x"], u["pos"]["y"]]
                                         for u in built])
                if result["done"]:
                    break
            station = next(u for u in state["teamOur"]["roles"]
                           if u["roleType"] == "station")
            base = (station["pos"]["x"], station["pos"]["y"])
            turn = Turn.load(state)
            walls = sorted((p.x, p.y) for p in brain._wall_order(turn))
            ring = geometric_ring(base)
            wanted = expected_exit(base)
            report["cases"].append({
                "seed": seed,
                "side": side,
                "pressure": args.pressure,
                "rounds": rounds,
                "base": list(base),
                "finished": bool(state.get("_demo", {}).get("finished")),
                "base_hp": station["health"],
                "base_alive": station["health"] > 0,
                "score_local": state["teamOur"]["totalScore"],
                "kills": state.get("_demo", {}).get("kills"),
                "task_score": sum(r.get("score", 0)
                                  for r in (state.get("_demo") or {}).get("task_report", {}).get("rewards", []) or []),
                "walls_final": walls_seen[-1] if walls_seen else 0,
                "first_three_towers": towers,
                "tower_sides": sorted({side_of((t[1], t[2]), base) for t in towers})
                if towers else [],
                "wall_order": walls,
                "ring_count": len(ring),
                "expected_exit": sorted(wanted),
                "opening": sorted(ring - set(walls)),
                "opening_size": len(ring - set(walls)),
                "unintended_holes": sorted(ring - set(walls) - wanted),
                "rear_walled_over": sorted(set(walls) & wanted),
                "crew_trapped": crew_trapped(state, base),
                "audit_errors": dict(audit_errors),
                "execution_failures": failures,
                "actions": dict(actions),
                "policy_ms_max": round(max(policy_ms), 2) if policy_ms else None,
                "policy_ms_mean": round(statistics.mean(policy_ms), 2) if policy_ms else None,
                "policy_ms_p95": round(sorted(policy_ms)[max(0, int(len(policy_ms) * 0.95) - 1)], 2)
                if policy_ms else None,
            })

    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    return 0


# --------------------------------------------------------------------------
# Driver: extract the baseline, run both sources, merge.
# --------------------------------------------------------------------------

def extract_baseline(commit=BASELINE_COMMIT):
    """A read-only checkout of a commit, straight from git (no worktree changes)."""
    raw = subprocess.run(["git", "archive", "--format=tar", commit],
                         cwd=ROOT, capture_output=True, check=True).stdout
    target = Path(tempfile.mkdtemp(prefix="issue12-baseline-"))
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        archive.extractall(target)
    return target


def run_child(source, seeds, sides, pressure, rounds, output):
    command = [sys.executable, str(HERE), "--run", "--source", str(source),
               "--seeds", ",".join(str(s) for s in seeds),
               "--sides", ",".join(sides), "--pressure", str(pressure),
               "--rounds", str(rounds), "--output", str(output)]
    subprocess.run(command, cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", action="store_true", help="internal child mode")
    parser.add_argument("--source", default=str(ROOT))
    parser.add_argument("--baseline", default=None,
                        help="baseline source tree (default: the pinned commit via git archive)")
    parser.add_argument("--baseline-commit", default=BASELINE_COMMIT,
                        help="commit to extract when --baseline is not given")
    parser.add_argument("--seeds", default=DEFAULT_SEEDS)
    parser.add_argument("--sides", default=DEFAULT_SIDES)
    parser.add_argument("--pressure", type=int, default=1)
    parser.add_argument("--rounds", type=int, default=130)
    parser.add_argument("--output", default=str(ROOT / "reports/issue-12-defence"))
    args = parser.parse_args()
    args.seeds = [int(x) for x in args.seeds.split(",") if x]
    args.sides = [x for x in args.sides.split(",") if x]

    if args.run:
        sys.exit(run_source(args))

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error(f"{output} is not empty; do not overwrite old evidence")

    baseline_source = (Path(args.baseline) if args.baseline
                       else extract_baseline(args.baseline_commit))
    run_child(baseline_source, args.seeds, args.sides, args.pressure, args.rounds,
              output / "raw-baseline.json")
    run_child(ROOT, args.seeds, args.sides, args.pressure, args.rounds,
              output / "raw-candidate.json")
    baseline = json.loads((output / "raw-baseline.json").read_text(encoding="utf-8"))
    candidate = json.loads((output / "raw-candidate.json").read_text(encoding="utf-8"))
    merged = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "seeds": args.seeds,
        "sides": args.sides,
        "pressure": args.pressure,
        "rounds": args.rounds,
        "baseline": baseline,
        "candidate": candidate,
        "assumptions": [
            "local single-team simulator; not an official 1v1 result and not a win claim",
            "local build-region assumption D01: the ring is the radius-2 perimeter of the "
            "base footprint, the weapon ring radius 1",
            "the approach prior is 'the enemy comes from the map interior'; the first wave "
            "may differ",
            "local wave composition and spawn fixtures, not official spawn data",
        ],
    }
    (output / "comparison.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "baseline_sha256": baseline["source_sha256"],
        "candidate_sha256": candidate["source_sha256"],
        "baseline": [{"seed": c["seed"], "side": c["side"], "rounds": c["rounds"],
                      "base_hp": c["base_hp"], "opening_size": c["opening_size"],
                      "towers": c["first_three_towers"], "trapped": c["crew_trapped"],
                      "audit": c["audit_errors"]} for c in baseline["cases"]],
        "candidate": [{"seed": c["seed"], "side": c["side"], "rounds": c["rounds"],
                       "base_hp": c["base_hp"], "opening_size": c["opening_size"],
                       "towers": c["first_three_towers"], "trapped": c["crew_trapped"],
                       "audit": c["audit_errors"]} for c in candidate["cases"]],
    }, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
