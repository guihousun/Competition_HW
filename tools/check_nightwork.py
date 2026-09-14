"""Paired A/B: same strategy and simulator, quiet-night work enabled/disabled."""
import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import ExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import patch

import compare_defences as experiment


def run(case):
    enabled, seed, side, source = case
    original_step = experiment.step
    work = Counter()

    def measured(state):
        result = original_step(state)
        if (state['roundNo'] - 1) % 130 >= 70 and not any(
                r['health'] > 0 for r in state['robot']['roles']):
            workers = {str(r['id']) for r in state['teamOur']['roles'] if r['roleType'] == 'worker'}
            for uid, command in result['executed'].items():
                if uid in workers and result['state']['lastRoundRoleActionResults'].get(uid):
                    work[command['action']] += 1
        return result

    with ExitStack() as stack:
        if not enabled:
            stack.enter_context(patch.object(experiment.brain.nightwork, 'plan', return_value={}))
        stack.enter_context(patch.object(experiment, 'step', measured))
        row = experiment.run_case(('RRR', seed, side, 3, 1300, source,
                                   'holdout' if seed == 6173 else 'regression'))
    row.update(nightwork_enabled=enabled, clear_night_worker_actions=dict(work))
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error('Use an empty output directory; preserve historical evidence')
    source, files = experiment.fingerprint()
    cases = [(enabled, seed, side, source) for seed in (1, 7, 6173)
             for side in ('challenger', 'defender') for enabled in (False, True)]
    metadata = dict(created_at=datetime.now(timezone.utc).isoformat(), source_sha256=source,
                    files=files, cases=cases, baseline='nightwork.plan disabled; everything else identical',
                    limitations=['Single-team local simulator, no paid LLM or intranet',
                                 'Known fixed-spawn, static-occupancy and swap settlement gaps remain',
                                 'Random future spawn positions may diverge as occupancy changes'])
    (output / 'metadata.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    rows = []
    with ProcessPoolExecutor(max_workers=2) as pool:
        for future in as_completed([pool.submit(run, case) for case in cases]):
            row = future.result()
            rows.append(row)
            filename = f"{'on' if row['nightwork_enabled'] else 'off'}-{row['seed']}-{row['side']}.json"
            (output / filename).write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({k: row[k] for k in ('nightwork_enabled', 'seed', 'side', 'base_hp',
                  'score_local', 'execution_failures', 'audit_errors', 'clear_night_worker_actions')}), flush=True)
    rows.sort(key=lambda r: (r['seed'], r['side'], r['nightwork_enabled']))
    for seed in (1, 7, 6173):
        for side in ('challenger', 'defender'):
            paired = [r for r in rows if r['seed'] == seed and r['side'] == side]
            assert len(paired) == 2 and paired[0]['initial_state_sha256'] == paired[1]['initial_state_sha256']
    (output / 'results.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Completed 12 paired cases', flush=True)


if __name__ == '__main__':
    main()
