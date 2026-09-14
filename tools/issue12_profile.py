"""Cold/warm decision latency for the Issue 12 defence work (local evidence only).

Measures the strategy decision on the official sample request and on the local
scenario, in a fresh process (cold caches) and after a warm-up call. Run:

    python tools/issue12_profile.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))

from agent import brain  # noqa: E402
from agent import defense_layout  # noqa: E402
from agent.brain import decide  # noqa: E402
from agent.scenarios import observation, scenario  # noqa: E402


def timed(payload, repeats=1):
    best = None
    for _ in range(repeats):
        start = time.perf_counter()
        decide(payload)
        elapsed = time.perf_counter() - start
        best = elapsed if best is None else min(best, elapsed)
    return best


def clear_caches():
    brain._DEFENCE_CACHE.clear()
    brain._TOWER_SITES_CACHE.clear()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", default=None,
                        help="write the measurements as JSON here as well")
    parser.add_argument("--no-cache", action="store_true",
                        help="disable the per-snapshot memos to show the uncached cost")
    args = parser.parse_args()
    if args.no_cache:
        # Bypass both memos, so the numbers show what the change costs without
        # them. Straight replacement of the two entry points, not a tuning knob.
        real_geometry = brain._defence_geometry
        real_sites = brain._tower_sites

        def uncached_geometry(turn):
            station = turn.station()
            if station is None:
                return None
            standing = frozenset(u.pos for u in turn.walls())
            key = (brain._terrain_key(turn, station.pos), standing)
            plan = defense_layout.layout(
                station.pos, turn.width, turn.height, land=turn.land,
                standing_walls=standing)
            graph = defense_layout.interior_graph(station.pos, plan.exit_cells,
                                                 turn.land)
            return key, plan, graph

        def uncached_sites(turn):
            brain._TOWER_SITES_CACHE.clear()
            return real_sites(turn)

        brain._defence_geometry = uncached_geometry
        brain._tower_sites = uncached_sites

    sample = json.loads((ROOT / "docs/request.txt").read_text(encoding="utf-8"))
    local = observation(scenario(1, "challenger"))

    clear_caches()
    cold_sample = timed(sample)
    warm_sample = timed(sample, repeats=5)
    clear_caches()
    cold_local = timed(local)
    warm_local = timed(local, repeats=5)
    clear_caches()
    single_local = timed(local)

    measurement = {
        "cached": not args.no_cache,
        "official_sample_cold_ms": round(cold_sample * 1000, 2),
        "official_sample_warm_ms": round(warm_sample * 1000, 2),
        "local_scenario_cold_ms": round(cold_local * 1000, 2),
        "local_scenario_warm_ms": round(warm_local * 1000, 2),
        "local_scenario_cleared_cache_single_ms": round(single_local * 1000, 2),
        "note": "min of 5 warm calls; cold means the defence memo and the tower-site "
                "memo are empty; decide() is the judge entry, not HTTP",
    }
    print(f"official sample  cold={cold_sample*1000:.2f} ms  warm(min of 5)={warm_sample*1000:.2f} ms")
    print(f"local scenario   cold={cold_local*1000:.2f} ms  warm(min of 5)={warm_local*1000:.2f} ms")
    print(f"local scenario   caches cleared + one call={single_local*1000:.2f} ms")
    print("cache sizes after warm run:",
          len(brain._DEFENCE_CACHE), len(brain._TOWER_SITES_CACHE))
    if args.output:
        Path(args.output).write_text(json.dumps(measurement, ensure_ascii=False, indent=2),
                                     encoding="utf-8")


if __name__ == "__main__":
    main()
