"""Diagnostic for the known Issue #19 regression failure (do not fix here).

``test_long_clues_lead_to_actual_purchase_travel_and_summon_on_both_sides``,
side=defender, seed 90317: the news agent interprets the clues but never opens
the treasure. This script reruns the same loop and dumps the pioneer's round /
day / position / executed action / health around the day-3 window so Codex and
the news agent can locate the scheduling problem. It does not change fixtures.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))

from unittest.mock import patch  # noqa: E402

from agent import brain, local_scripted_model, local_world_news, scenarios, simulator  # noqa: E402


def main(side: str = "defender") -> int:
    with patch.dict(os.environ, {brain.WORLD_AGENT_ENV: "on", brain.TASK_AGENT_ENV: "off"}):
        state = local_world_news.install(
            scenarios.scenario(90317, side, 1, profile="local-pressure"), long_context=True)
        timeline = []
        opened = False
        for _ in range(300):
            result = simulator.step(state)
            state = result["state"]
            pioneer = next(r for r in state["teamOur"]["roles"] if r["roleType"] == "pioneer")
            memory = state["_demo"]["planner"]
            timeline.append({
                "round": result["frame"]["round"],
                "pioneer": pioneer["pos"],
                "hp": pioneer["health"],
                "action": (result["executed"].get(str(pioneer["id"])) or {}).get("action"),
                "focus": bool(memory.team_agent.world.focus.get("treasure")),
                "summonResult": state.get("lastSummonTreasureResult"),
            })
            if result["judgeRequest"].get("prompt"):
                state["llmResp"] = local_scripted_model.complete(
                    result["judgeRequest"]["prompt"])["answer"]
            state["_demo"]["planner"] = memory.dump()
            state = json.loads(json.dumps(state))
            if state.get("lastSummonTreasureResult") == 1:
                opened = True
                break
        window = [row for row in timeline if 255 <= row["round"] <= 290]
        out = {"side": side, "opened": opened, "rounds": len(timeline),
               "window": window, "final": timeline[-1]}
        path = ROOT / f"reports/issue-19-monsters/treasure-{side}-diagnostic.json"
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"side": side, "opened": opened, "rounds": len(timeline),
                          "actions": sorted({str(r["action"]) for r in window})},
                         ensure_ascii=False))
        print(f"written {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "defender"))
