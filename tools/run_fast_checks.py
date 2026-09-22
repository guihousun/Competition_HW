#!/usr/bin/env python3
"""Run the short acceptance gate; pass --full for the exhaustive suite."""
from __future__ import annotations

import argparse
import subprocess
import sys

FAST = (
    'test_strategy_config.py', 'test_ballistics.py', 'test_movement.py',
    'test_robots.py', 'test_simulator.py', 'test_fixed_spawns.py',
    'test_match.py', 'test_metal_economy.py', 'test_front_wall_interior.py',
    'test_navigation_recovery.py', 'test_pioneer_gunner.py',
    'test_center_wall_maintenance.py', 'test_weapon_upgrade_budget.py',
    'test_replay.py', 'test_competition_package.py', 'test_build_submission.py',
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--full', action='store_true', help='run all tests, including slow historical suites')
    args = parser.parse_args()
    if args.full:
        command = [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v']
        return subprocess.run(command, check=False).returncode
    else:
        # unittest accepts one pattern per invocation; keep the gate explicit
        # so each file gets an independent, readable failure boundary.
        for pattern in FAST:
            command = [sys.executable, '-m', 'unittest', 'discover', '-s',
                       'tests', '-p', pattern, '-v']
            result = subprocess.run(command, check=False)
            if result.returncode:
                return result.returncode
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
