"""Paired local defence experiment. Changes loadout only inside worker processes.

No web-server changes, external LLM calls, rule tuning, or official win claims.
"""
import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'Demo/CoreGeek/src'))
from agent import brain, planner
from agent.scenarios import scenario, observation
from agent.simulator import step
from benchmark import audit

LOADOUTS = {
    'RRR': ('rocket', 'rocket', 'rocket'),
    'GGG': ('gatling', 'gatling', 'gatling'),
    'EEE': ('railgun', 'railgun', 'railgun'),
    'GER': ('gatling', 'railgun', 'rocket'),
    'RRE': ('rocket', 'rocket', 'railgun'),
}
LABELS = {'RRR':'三火箭', 'GGG':'三加特林', 'EEE':'三电磁炮', 'GER':'三种各一座', 'RRE':'两火箭一电磁炮'}


def fingerprint():
    paths = sorted((ROOT / 'Demo/CoreGeek/src/agent').glob('*.py'))
    paths += [ROOT / 'benchmark.py', Path(__file__), ROOT / 'docs/任务书.md', ROOT / 'docs/接口文档.md']
    hashes = {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    return hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest(), hashes


def run_case(case):
    name, seed, side, pressure, limit, expected_source, split = case
    if fingerprint()[0] != expected_source:
        raise RuntimeError('Source changed during experiment; refusing mixed-version results')
    before = brain.TOWER_LOADOUT
    brain.TOWER_LOADOUT = LOADOUTS[name]
    planner.reset()
    started = time.monotonic()
    state = scenario(seed, side, pressure)
    initial_hash = hashlib.sha256(json.dumps(state,sort_keys=True,ensure_ascii=True).encode()).hexdigest()
    errors, actions, requested, attacks = Counter(), Counter(), Counter(), Counter()
    failures = task_score = rewards = crew_deaths = walls_destroyed = base_damage = 0
    roster = None
    completed = 0
    try:
        for _ in range(limit):
            public = observation(state)
            result = step(state)
            completed += 1
            errors.update(audit(public, result['executed']))
            actions.update(c['action'] for c in result['executed'].values())
            requested.update((result.get('judgeRequest') or {}).keys())
            state = result['state']
            frame = result['frame']
            failures += sum(not value for value in state['lastRoundRoleActionResults'].values())
            attacks.update(a['kind'] for a in frame['actions'] if a['a'] == 'attack')
            base_damage += sum(a['damage'] for a in frame['robotAttacks'] if a.get('buildingKind') == 'station')
            crew_deaths += sum(a['kind'] in ('worker','pioneer') for a in frame['unitDeaths'])
            walls_destroyed += sum(a['kind'] == 'wall' for a in frame['unitDeaths'])
            reward = state['_demo'].get('task_report', {}).get('rewards')
            if reward:
                task_score += reward['score']; rewards += 1
            towers = [u for u in state['teamOur']['roles'] if u['roleType'] in ('rocket','gatling','railgun') and u['health'] > 0]
            if roster is None and len(towers) == 3:
                roster = dict(Counter(u['roleType'] for u in towers))
                if Counter(roster) != Counter(LOADOUTS[name]):
                    raise RuntimeError(f'Actual built roster differs: {name}: {roster}')
            if result['done']:
                break
        base = next(u for u in state['teamOur']['roles'] if u['roleType'] == 'station')
        if fingerprint()[0] != expected_source:
            raise RuntimeError('Source changed during case')
        return dict(strategy=name, label=LABELS[name], loadout=LOADOUTS[name], seed=seed, side=side,
                    pressure=pressure, split=split, rounds=completed, survived_full=completed == 1300 and base['health'] > 0,
                    base_hp=base['health'], base_level=base.get('level',1), base_damage=base_damage,
                    score_local=state['teamOur']['totalScore'], task_score=task_score, task_rewards=rewards,
                    kills=state['_demo']['kills'], crew_deaths=crew_deaths, walls_destroyed=walls_destroyed,
                    execution_failures=failures, audit_errors=dict(errors), actions=dict(actions), attacks=dict(attacks),
                    requested_channels=dict(requested), paid_llm_calls=0, first_complete_roster=roster,
                    initial_state_sha256=initial_hash, source_sha256=expected_source,
                    elapsed_seconds=round(time.monotonic()-started,2))
    finally:
        brain.TOWER_LOADOUT = before
        planner.reset()


def aggregate(rows):
    groups = []
    for split in ['development','holdout','all']:
        for pressure in sorted({r['pressure'] for r in rows}):
            for name in LOADOUTS:
                group = [r for r in rows if r['strategy']==name and r['pressure']==pressure and (split=='all' or r['split']==split)]
                if not group: continue
                groups.append(dict(split=split,pressure=pressure,strategy=name,label=LABELS[name],cases=len(group),
                    survived=sum(r['survived_full'] for r in group),
                    mean_base_hp=round(statistics.mean(r['base_hp'] for r in group),1),
                    mean_score=round(statistics.mean(r['score_local'] for r in group),1),
                    mean_task_score=round(statistics.mean(r['task_score'] for r in group),1),
                    mean_kills=round(statistics.mean(r['kills'] for r in group),1),
                    execution_failures=sum(r['execution_failures'] for r in group),
                    audit_errors=sum(sum(r['audit_errors'].values()) for r in group)))
    return groups


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seeds', default='1,7')
    parser.add_argument('--holdout', default='1371')
    parser.add_argument('--pressures', default='1,3')
    parser.add_argument('--sides', default='challenger,defender')
    parser.add_argument('--rounds', type=int, default=1300)
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    seeds = [int(x) for x in args.seeds.split(',') if x]
    held = [int(x) for x in args.holdout.split(',') if x]
    if set(seeds) & set(held): parser.error('Development and holdout seeds must not overlap')
    if not 1 <= args.rounds <= 1300: parser.error('rounds must be 1..1300')
    output = Path(args.output)
    output.mkdir(parents=True,exist_ok=True)
    if any(output.iterdir()): parser.error('Output must be empty; do not overwrite old evidence')
    source, hashes = fingerprint()
    metadata = dict(created_at=datetime.now(timezone.utc).isoformat(),source_sha256=source,files=hashes,
        loadouts=LOADOUTS,development_seeds=seeds,holdout_seeds=held,round_limit=args.rounds,
        pressures=args.pressures,sides=args.sides,mode='direct local simulator; no external LLM',
        limitations=['Random ring spawns conflict with fixed-spawn supplement S03',
                     'Local construction regions, wave counts, robot AI and task fixtures',
                     'Single-team survival is not official 1v1 win rate',
                     'Loadout slot order held fixed; not a full search over placements or strategies',
                     'Local economy memory differs from official observation-only route'])
    (output/'metadata.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    cases = [(name,seed,side,int(pressure),args.rounds,source,'holdout' if seed in held else 'development')
             for seed in seeds+held for side in args.sides.split(',') for pressure in args.pressures.split(',') for name in LOADOUTS]
    rows = []
    with ProcessPoolExecutor(max_workers=max(1,min(args.workers,4))) as pool:
        futures = {pool.submit(run_case,case):case for case in cases}
        for future in as_completed(futures):
            row = future.result(); rows.append(row)
            filename = f"{row['strategy']}-s{row['seed']}-{row['side']}-p{row['pressure']}.json"
            (output/filename).write_text(json.dumps(row,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({k:row[k] for k in ['strategy','seed','side','pressure','rounds','base_hp','score_local','execution_failures','audit_errors']}),flush=True)
    rows.sort(key=lambda r:(r['seed'],r['side'],r['pressure'],r['strategy']))
    for seed in seeds+held:
        for side in args.sides.split(','):
            for pressure in map(int,args.pressures.split(',')):
                assert len({r['initial_state_sha256'] for r in rows if (r['seed'],r['side'],r['pressure'])==(seed,side,pressure)}) == 1
    (output/'results.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    (output/'summary.json').write_text(json.dumps(aggregate(rows),ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'Completed {len(rows)} paired cases; results in {output}',flush=True)


if __name__ == '__main__': main()
