"""Replay one defence case and save failed commands without changing its policy."""
import argparse
from collections import Counter
import json
from pathlib import Path
from compare_defences import brain, planner, scenario, observation, step, audit, fingerprint, LOADOUTS


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--strategy', choices=LOADOUTS, default='RRR')
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--side', default='defender')
    parser.add_argument('--pressure', type=int, default=3)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        parser.error('Refusing to overwrite existing evidence')
    source = fingerprint()[0]
    brain.TOWER_LOADOUT = LOADOUTS[args.strategy]
    planner.reset()
    state = scenario(args.seed, args.side, args.pressure)
    rows = []
    failed_actions = Counter()
    for _ in range(1300):
        public = observation(state)
        result = step(state)
        errors = audit(public, result['executed'])
        failed_ids = [str(uid) for uid, ok in result['state']['lastRoundRoleActionResults'].items() if not ok]
        commands = {str(k): v for k, v in result['executed'].items()}
        for uid in failed_ids:
            failed_actions[commands.get(uid, {}).get('action', 'unmapped')] += 1
        if errors or failed_ids:
            rows.append(dict(round=public['roundNo'], gold=public['teamOur']['goldNum'],
                commands=commands, failed_ids=failed_ids, audit_errors=errors,
                roles=public['teamOur']['roles'], events=result['events']))
        state = result['state']
        if result['done']:
            break
    if fingerprint()[0] != source:
        raise RuntimeError('Source changed during diagnosis')
    data = dict(source_sha256=source, case=vars(args), failed_actions=dict(failed_actions), rounds=rows)
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(failed_actions=failed_actions, affected_rounds=len(rows), output=str(output))))


if __name__ == '__main__':
    main()
