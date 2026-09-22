#!/usr/bin/env python3
"""Compact acceptance audit for a local competition-hw replay.

This is deliberately separate from the simulator oracle: it reports observed
commands/actions and does not turn a local replay into official evidence.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _streaks(rounds: list[int]) -> list[dict]:
    if not rounds:
        return []
    out, start, previous = [], rounds[0], rounds[0]
    for value in rounds[1:] + [None]:
        if value is not None and value == previous + 1:
            previous = value
            continue
        out.append({'start': start, 'end': previous, 'length': previous - start + 1})
        if value is not None:
            start = previous = value
    return out


def audit(path: Path) -> dict:
    root = json.loads(path.read_text(encoding='utf-8-sig'))
    recording = root.get('recording', root)
    frames = recording.get('frames') or []
    states = recording.get('states') or []
    role_ids = sorted({int(action['id']) for frame in frames
                       for action in frame.get('actions', [])
                       if isinstance(action.get('id'), int) and action.get('kind') in ('worker', 'pioneer')})
    idle = {}
    for uid in role_ids:
        missing = [frame['round'] for frame in frames
                   if not any(action.get('id') == uid for action in frame.get('actions', []))]
        idle[str(uid)] = sorted(_streaks(missing), key=lambda row: row['length'], reverse=True)[:5]
    deaths = {}
    for frame in frames:
        for death in frame.get('unitDeaths', []):
            if isinstance(death, dict):
                deaths[str(death.get('id'))] = frame['round']
    base = []
    for state in states:
        for role in (state.get('teamOur') or {}).get('roles') or []:
            if role.get('roleType') == 'station':
                base.append({'round': state.get('roundNo'), 'health': role.get('health')})
    attacks = [frame['round'] for frame in frames if any(a.get('a') == 'attack' for a in frame.get('actions', []))]
    return {
        'source': str(path),
        'local': root.get('local', recording.get('metadata', {}).get('local', True)),
        'rounds': len(frames),
        'role_idle_streaks': idle,
        'deaths': deaths,
        'base_first_health_loss': next((row for row in base if row['health'] is not None and row['health'] < 1500), None),
        'base_last': base[-1] if base else None,
        'last_attack_round': max(attacks) if attacks else None,
        'empty_frames': [frame['round'] for frame in frames
                         if not frame.get('actions') and not frame.get('commands') and not frame.get('executed')],
        'note': 'Local replay audit; no official PASS or engine semantics inferred.',
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('replay', type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.replay), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

