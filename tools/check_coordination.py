"""Focused complete-match regression for team budget and movement coordination."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path

from compare_defences import fingerprint, run_case


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--holdout-seed', type=int, default=2029)
    parser.add_argument('--holdout-only', action='store_true')
    args = parser.parse_args()
    if args.holdout_seed in (1, 7):
        parser.error('Holdout must differ from the known regression seeds')
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error('Output must be empty; preserve previous evidence')
    source, files = fingerprint()
    # Declared before execution: known failure seeds and a fresh holdout map.
    matrix = [('RRR', seed, side, 3, 1300, source,
               'holdout' if seed == args.holdout_seed else 'regression')
              for seed in ((args.holdout_seed,) if args.holdout_only else (1, 7, args.holdout_seed))
              for side in ('challenger', 'defender')]
    if not args.holdout_only:
        matrix += [('RRE', 1, side, 3, 1300, source, 'regression')
                   for side in ('challenger', 'defender')]
    metadata = dict(created_at=datetime.now(timezone.utc).isoformat(),
                    source_sha256=source, files=files, cases=matrix,
                    limitations=['Local single-team simulator; no paid LLM or intranet calls',
                                 'Random ring spawns still conflict with S03',
                                 'Local wave/AI/build-region/task assumptions remain'])
    (output / 'metadata.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    rows = []
    with ProcessPoolExecutor(max_workers=2) as pool:
        for future in as_completed([pool.submit(run_case, case) for case in matrix]):
            row = future.result()
            rows.append(row)
            filename = f"{row['strategy']}-s{row['seed']}-{row['side']}-p3.json"
            (output / filename).write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({k: row[k] for k in ('strategy', 'seed', 'side', 'rounds',
                  'base_hp', 'score_local', 'execution_failures', 'audit_errors')}), flush=True)
    rows.sort(key=lambda row: (row['strategy'], row['seed'], row['side']))
    (output / 'results.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Completed {len(rows)} cases', flush=True)


if __name__ == '__main__':
    main()
