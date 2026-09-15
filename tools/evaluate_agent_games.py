"""One full local game with a prompt-only scripted model and virtual sandbox.

This measures strategy/coordination under a repeatable model, not model quality
or official scores. A parent runner can compare frozen source trees identically.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--seed', required=True, type=int)
    parser.add_argument('--side', choices=['challenger', 'defender'], required=True)
    parser.add_argument('--short-world', action='store_true')
    parser.add_argument('--price-drop-news', action='store_true', help='Explicit local one-day copper price-drop fixture')
    parser.add_argument('--disable-news-economy', action='store_true', help='Evaluation-only sale-advice ablation')
    parser.add_argument('--disable-night-staging', action='store_true', help='Explicit evaluation-only ablation')
    parser.add_argument('--legacy-route-estimate', action='store_true', help='Evaluation-only old projected-occupancy estimate')
    args = parser.parse_args()
    source, out = Path(args.source).resolve(), Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    if subprocess.call(['git', 'diff', '--quiet', 'HEAD', '--', 'Demo/CoreGeek/src/agent'], cwd=source):
        raise RuntimeError('freeze runtime source before evaluation')
    source_sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    sys.path.insert(0, str(source / 'Demo/CoreGeek/src'))
    from agent import brain, local_scripted_model, local_task_cases, local_task_sandbox, local_world_news, scenarios, simulator
    os.environ[brain.WORLD_AGENT_ENV] = 'on'
    os.environ[brain.TASK_AGENT_ENV] = 'on'
    overrides = []
    if args.disable_news_economy:
        brain.news_economy.sale_signals = lambda *unused, **kwargs: {}
        overrides.append('news_economy_disabled')
    if args.disable_night_staging:
        brain._treasure_night_staging = lambda *unused: None
        overrides.append('night_staging_disabled')
    if args.legacy_route_estimate:
        original_cost = brain._route_cost
        brain._route_cost = lambda turn, start, goal, moving=None: original_cost(turn, start, goal)
        overrides.append('legacy_projected_route_estimate')

    def hashes():
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted((source / 'Demo/CoreGeek/src/agent').glob('*.py'))}

    def save(name, data):
        (out / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

    initial = hashes()
    fixture_options = {'long_context': not args.short_world}
    if args.price_drop_news:
        fixture_options['price_drop'] = True
    state = local_world_news.install(local_task_cases.install(scenarios.scenario(args.seed, args.side)), **fixture_options)
    counts = {'prompts': 0, 'news': 0, 'treasure': 0, 'task': 0, 'legacy': 0, 'commands': 0,
              'tasks_completed': 0, 'attacks': 0, 'summons': 0, 'summon_successes': 0,
              'channel_conflicts': 0, 'invalid_agent_states': 0}
    elapsed, events, days, errors = [], [], {}, []
    started = time.monotonic()
    min_base_hp = 1500
    sales = []
    for _ in range(1300):
        tick = time.perf_counter()
        result = simulator.step(state)
        elapsed.append((time.perf_counter() - tick) * 1000)
        state = result['state']
        memory = state['_demo']['planner']
        request = result['judgeRequest']
        n = result['frame']['round']
        base = next(r for r in state['teamOur']['roles'] if r['roleType'] == 'station')
        min_base_hp = min(min_base_hp, base['health'])
        day = (n - 1) // 130 + 1
        days[day] = max(days.get(day, 0), memory.judge.llm_used_today)
        counts['channel_conflicts'] += bool(request.get('prompt') and request.get('executeCmd'))
        counts['invalid_agent_states'] += bool(memory.team_agent and memory.team_agent.degraded)
        if request.get('prompt'):
            counts['prompts'] += 1
            pending = memory.llm_router.pending['prompt'] if memory.llm_router else None
            owner = pending.owner if pending else 'legacy'
            counts[owner if owner in ('news', 'treasure', 'task') else 'legacy'] += 1
            # The fixed model only sees the exact emitted prompt, never the
            # archive, a private recipe, future publications or answer fixture.
            state['llmResp'] = local_scripted_model.complete(request['prompt'])['answer']
        if request.get('executeCmd'):
            counts['commands'] += 1
            fixture = local_task_sandbox.active_task_fixture(state)
            state['lastCmdResult'] = local_task_sandbox.execute(request['executeCmd'], fixture, active=True) if fixture else '[JUDGER_ERROR]\nno active fixture'
        counts['attacks'] += sum(c.get('action') == 'attack' for c in result['executed'].values())
        summons = sum(c.get('action') == 'summonTreasure' for c in result['executed'].values())
        counts['summons'] += summons
        counts['summon_successes'] += bool(summons and state.get('lastSummonTreasureResult') == 1)
        report = state['_demo'].get('task_report') or {}
        counts['tasks_completed'] += report.get('ended') == 'completed'
        sales.extend({'round': n, **sale} for sale in result['frame'].get('actions', [])
                     if sale.get('a') == 'sell')
        if report.get('ended') or summons:
            events.append({'round': n, 'task': report, 'summon_result': state.get('lastSummonTreasureResult')})
        if state.get('errors'):
            errors.append({'round': n, 'errors': state['errors']})
        state['_demo']['planner'] = memory.dump()
        state = json.loads(json.dumps(state))
        if n % 130 == 0:
            save('live.json', {'pid': os.getpid(), 'round': n, 'counts': counts,
                               'score': state['teamOur']['totalScore'], 'base_hp': base['health']})
        if result['done']:
            break
    ordered = sorted(elapsed)
    summary = {'scope': 'Full local simulator game; prompt-only scripted model and virtual sandbox; not intranet PASS',
        'source_sha': source_sha, 'files': initial, 'stable_source': hashes() == initial,
        'python': sys.version.split()[0], 'seed': args.seed, 'side': args.side, 'long_world': not args.short_world,
        'runtime_overrides': overrides, 'price_drop_news': args.price_drop_news,
        'rounds': len(elapsed), 'score': state['teamOur']['totalScore'], 'base_hp': base['health'],
        'min_base_hp': min_base_hp, 'daily_calls': days, 'counts': counts,
        'simulation_step_p99_ms': ordered[int((len(ordered) - 1) * .99)], 'simulation_step_max_ms': max(ordered),
        'elapsed_seconds': time.monotonic() - started, 'error_rounds': len(errors)}
    save('report.json', summary)
    save('events.json', events)
    save('errors.json', errors)
    save('sales.json', sales)
    print(json.dumps({k: v for k, v in summary.items() if k != 'files'}, ensure_ascii=False), flush=True)
    return 0 if summary['stable_source'] and counts['invalid_agent_states'] == 0 and all(v <= 3 for v in days.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
