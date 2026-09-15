"""Issue #19 local regression: three unmanipulated nights on both sides.

Runs the real local strategy (no injected commands) with the default observed
wave profile, records the per-night spawn counts, the first night round where a
wall or the base takes robot damage, the final base HP and the error count.

Local evidence only: not an official judge, not a claim that the base survives.
    python reports/issue-19-monsters/regression_three_nights.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))

from agent.protocol import Pos  # noqa: E402
from agent.scenarios import scenario  # noqa: E402
from agent.simulator import step  # noqa: E402

ROUNDS = 390  # three full days/nights
SEEDS = (1, 7)


def run(seed: int, side: str) -> dict:
    state = scenario(seed, side)
    first_wall = first_base = None
    nights: dict[int, int] = {}
    errors = 0
    for _ in range(ROUNDS):
        round_no = int(state["roundNo"])
        result = step(state)
        state = result["state"]
        frame = result.get("frame") or {}
        night = (round_no - 1) // 130 + 1
        spawned = len(frame.get("spawned") or [])
        if spawned:
            nights[night] = nights.get(night, 0) + spawned
        for attack in frame.get("robotAttacks") or []:
            if "building" not in attack:
                continue
            if attack.get("buildingKind") == "wall" and first_wall is None:
                first_wall = round_no
        hp = next((u["health"] for u in state["teamOur"]["roles"]
                   if u["roleType"] == "station"), 0)
        if hp < 1500 and first_base is None:
            first_base = round_no
        errors += len(state.get("errors") or [])
        if state.get("_demo", {}).get("finished"):
            break
    base_hp = next((u["health"] for u in state["teamOur"]["roles"]
                    if u["roleType"] == "station"), 0)
    return {"seed": seed, "side": side, "profile": state["_demo"].get("profile"),
            "rounds_run": round_no, "spawned_per_night": nights,
            "first_wall_damage_round": first_wall,
            "first_base_damage_round": first_base,
            "final_base_hp": base_hp, "errors": errors,
            "finished": bool(state.get("_demo", {}).get("finished"))}


def main() -> int:
    rows = [run(seed, side) for seed in SEEDS for side in ("challenger", "defender")]
    out = ROOT / "reports/issue-19-monsters/three-nights-local.json"
    out.write_text(json.dumps({"profile_default": "observed-seven-days",
                               "rounds": ROUNDS, "runs": rows},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))
    print(f"written {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
