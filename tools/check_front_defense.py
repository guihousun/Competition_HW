"""Small fixed-seed mirror/holdout check of the current candidate, no paid LLM."""
import json
from pathlib import Path
import compare_defences as check


if __name__ == '__main__':
    output = check.ROOT / 'reports/front-wall-interior/local-cases.json'
    check.LOADOUTS['RER'] = ('rocket', 'railgun', 'rocket')
    check.LABELS['RER'] = 'rocket-railgun-rocket'
    fingerprint, files = check.fingerprint()
    rows = []
    for seed in (1, 90601):
        for side in ('challenger', 'defender'):
            row = check.run_case(('RER', seed, side, 1, 260, fingerprint,
                                  'development' if seed == 1 else 'holdout'))
            rows.append(row)
            print(json.dumps({k: row[k] for k in ('seed', 'side', 'rounds', 'base_hp',
                                                 'score_local', 'execution_failures', 'audit_errors')}), flush=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({'scope': 'Local two-day cases, not official PASS',
                                 'source_sha256': fingerprint, 'files': files, 'cases': rows},
                                indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    assert all(not r['execution_failures'] and not r['audit_errors'] for r in rows)
