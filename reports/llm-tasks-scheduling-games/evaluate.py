"""Frozen full-game ablations. Scripted model sees public observations/evidence.

No production solver reads fixtures, seeds or expected answers. These trials
measure control/strategy under a deterministic model, not real-model win rate.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument('--source', required=True)
parser.add_argument('--output', required=True)
parser.add_argument('--child', action='store_true')
parser.add_argument('--seed', type=int)
parser.add_argument('--side')
parser.add_argument('--mode')
args = parser.parse_args()
source, out = Path(args.source), Path(args.output)

def hashes():
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((source / 'Demo/CoreGeek/src/agent').glob('*.py'))}

def world_reply(world, owner):
    rows = world.sources[owner]
    token = world.links[owner]['token']
    def proof(row):
        return [{'sourceId': row['id'], 'quote': row['text']}]
    if owner == 'news':
        first = next(r for r in rows if '明天开始全面停工两天' in r['text'])
        day = (first['firstRound'] - 1) // 130 + 1
        events = [{'resource': 'iron', 'availability': availability, 'startDay': start,
                   'endDay': end, 'priceDirection': price, 'evidence': proof(first)}
                  for start, end, availability, price in ((day, day, 'available', 'unchanged'),
                    (day + 1, day + 2, 'unavailable', 'up'), (day + 3, 10, 'available', 'unchanged'))]
        return json.dumps({'request_id': token, 'events': events}, ensure_ascii=False)
    h = {'site': None, 'items': None, 'opensAt': None, 'closesAt': None, 'uncertain': True,
         'evidence': {'site': [], 'items': [], 'window': []}}
    for row in rows:
        site = re.search(r'横坐标(\d+)、纵坐标(\d+)', row['text'])
        items = re.search(r'需要献祭([^，]+)，每种恰好一份', row['text'])
        window = re.search(r'第(\d+)天白昼.*前30个回合', row['text'])
        if site:
            h['site'] = {'x': int(site[1]), 'y': int(site[2])}; h['evidence']['site'] = proof(row)
        if items:
            h['items'] = items[1].split('、'); h['evidence']['items'] = proof(row)
        if window:
            h['opensAt'] = (int(window[1]) - 1) * 130 + 1
            h['closesAt'] = h['opensAt'] + 29; h['evidence']['window'] = proof(row)
    h['uncertain'] = any(h[k] is None for k in ('site', 'items', 'opensAt', 'closesAt'))
    return json.dumps({'request_id': token, 'hypothesis': h}, ensure_ascii=False)

def task_reply(coordinator, question):
    pending = coordinator.task.pending
    evidence = coordinator.evidence
    ids = pending['evidence_ids']
    reason = '固定模型：依据本题已收到的文档和查询结果'
    plan = {'kind': 'need_info', 'reason': reason, 'evidence_ids': ids}
    # Only the actual observed output is usable, not an environment answer.
    query = next((e for e in reversed(evidence) if e['status'] == 'exit' and e['exit_code'] == 0
                  and e['command'].startswith(('python ', 'python3 '))), None)
    if query:
        data = json.loads(query['text'])
        value = data[0] if isinstance(data, list) and data else (data.get('total') if isinstance(data, dict) else None)
        if value is not None:
            answer = json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else str(value)
            plan.update(kind='answer', answer=answer)
    else:
        document = next((e for e in reversed(evidence) if e['status'] == 'exit' and e['exit_code'] == 0
                         and e['command'].startswith('cat ')), None)
        if document:
            api = re.search(r'python3\s+(/[^\s]+)\s+(--[a-z]+)', document['text'])
            if api:
                value = '上海' if '上海' in question else ('北京' if '北京' in question else 'A')
                plan.update(kind='run', command=f'python3 {api[1]} {api[2]} {value}')
        else:
            # The candidates are exactly those supplied in the actual prompt.
            candidate = re.search(r'"document_path":\s*"([^"\n]+)"', pending['payload'])
            directory = re.search(r'/[A-Za-z0-9_/-]+', question)
            listing = next((e for e in reversed(evidence) if e['status'] == 'exit' and e['exit_code'] == 0
                            and e['command'].startswith('ls ')), None)
            if candidate:
                plan.update(kind='run', command='cat ' + candidate[1])
            elif listing and directory:
                name = listing['text'].splitlines()[0].strip()
                plan.update(kind='run', command='cat ' + directory[0].rstrip('/') + '/' + name)
            elif directory:
                plan.update(kind='run', command='ls ' + directory[0])
    return json.dumps({'request_id': pending['token'], 'plan': plan}, ensure_ascii=False)

if args.child:
    sys.path.insert(0, str(source / 'Demo/CoreGeek/src'))
    from agent import brain, local_task_cases, local_task_sandbox, local_world_news, planner, scenarios, simulator
    os.environ[brain.TASK_AGENT_ENV] = 'off' if args.mode == 'baseline' else 'on'
    os.environ[brain.WORLD_AGENT_ENV] = 'on' if args.mode in ('all', 'all-faults') else 'off'
    os.environ[brain.ROUTER_ENV] = 'off'
    state = local_world_news.install(local_task_cases.install(scenarios.scenario(args.seed, args.side)))
    counters = {'task': 0, 'news': 0, 'treasure': 0, 'legacy_unanswered': 0, 'commands': 0,
                'tasks_completed': 0, 'task_ends': 0, 'bad_replies': 0, 'tool_timeouts': 0,
                'attacks': 0, 'response_conflicts': 0}
    times, events = [], []
    initial = hashes()
    start = time.monotonic()
    for _ in range(1300):
        started = time.perf_counter()
        result = simulator.step(state)
        times.append(time.perf_counter() - started)
        state = result['state']
        memory = state['_demo']['planner']
        request = result['judgeRequest']
        if request.get('prompt') and request.get('executeCmd'):
            counters['response_conflicts'] += 1
        if request.get('prompt'):
            pending = memory.llm_router.pending['prompt'] if memory.llm_router else None
            if pending and pending.owner in ('news', 'treasure'):
                counters[pending.owner] += 1
                state['llmResp'] = world_reply(memory.team_agent.world, pending.owner)
            elif pending and pending.owner == 'task' and memory.team_agent and memory.team_agent.task.pending:
                counters['task'] += 1
                state['llmResp'] = task_reply(memory.team_agent, state.get('phaseTask', ''))
            else:
                counters['legacy_unanswered'] += 1
            count = counters['task'] + counters['news'] + counters['treasure']
            if args.mode == 'all-faults' and count and count % 7 == 0:
                state['llmResp'] = 'malformed model output'
                counters['bad_replies'] += 1
        if request.get('executeCmd'):
            counters['commands'] += 1
            fixture = local_task_sandbox.active_task_fixture(state)
            state['lastCmdResult'] = local_task_sandbox.execute(request['executeCmd'], fixture, active=True) if fixture else '[JUDGER_ERROR]\nno fixture'
            if args.mode == 'all-faults' and counters['commands'] % 5 == 0:
                state['lastCmdResult'] = '[TIMEOUT]\npartial output'
                counters['tool_timeouts'] += 1
        counters['attacks'] += sum(c.get('action') == 'attack' for c in result['executed'].values())
        report = state['_demo'].get('task_report', {})
        if report.get('ended'):
            counters['task_ends'] += 1
            counters['tasks_completed'] += report['ended'] == 'completed'
        if report.get('ended') or any(c.get('action') == 'summonTreasure' for c in result['executed'].values()):
            events.append({'round': result['frame']['round'], 'task': report,
                           'treasure_result': state.get('lastSummonTreasureResult')})
        state['_demo']['planner'] = memory.dump()
        state = json.loads(json.dumps(state))
        if result['frame']['round'] % 130 == 0:
            (out / f'live-{args.seed}-{args.side}-{args.mode}.json').write_text(json.dumps({
                'round': result['frame']['round'], 'score': state['teamOur']['totalScore'], 'counters': counters}), encoding='utf-8')
        if result['done']:
            break
    base = next(r for r in state['teamOur']['roles'] if r['roleType'] == 'station')
    times.sort()
    row = {'seed': args.seed, 'side': args.side, 'mode': args.mode, 'rounds': len(times),
           'score': state['teamOur']['totalScore'], 'base_hp': base['health'],
           'treasure_opened': state['_demo']['treasure']['opened'], 'counters': counters,
           'simulation_step_p99_ms': times[int((len(times) - 1) * .99)] * 1000,
           'simulation_step_max_ms': max(times) * 1000, 'elapsed_seconds': time.monotonic() - start,
           'stable_sources': initial == hashes(), 'files': initial}
    (out / f'{args.seed}-{args.side}-{args.mode}.json').write_text(json.dumps({'summary': row, 'events': events}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in row.items() if k != 'files'}, ensure_ascii=False), flush=True)
else:
    out.mkdir(parents=True, exist_ok=False)
    jobs = [(seed, side, mode) for seed in (1, 90713, 90317, 90601) for side in ('challenger', 'defender')
            for mode in ('all',)]
    jobs += [(90713, side, 'all-faults') for side in ('challenger', 'defender')]
    running, rows = [], []
    while jobs or running:
        while jobs and len(running) < 2:
            seed, side, mode = jobs.pop(0)
            log = (out / f'{seed}-{side}-{mode}.log').open('w', encoding='utf-8')
            process = subprocess.Popen([sys.executable, __file__, '--child', '--source', str(source), '--output', str(out),
                                        '--seed', str(seed), '--side', side, '--mode', mode], stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            running.append((process, log, seed, side, mode))
        time.sleep(1)
        for job in list(running):
            process, log, seed, side, mode = job
            if process.poll() is None:
                continue
            log.close()
            running.remove(job)
            path = out / f'{seed}-{side}-{mode}.json'
            row = json.loads(path.read_text(encoding='utf-8'))['summary'] if process.returncode == 0 and path.exists() else {
                'seed': seed, 'side': side, 'mode': mode, 'failure': process.returncode}
            rows.append(row)
            (out / 'progress.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({k: v for k, v in row.items() if k != 'files'}, ensure_ascii=False), flush=True)
    report = {'scope': 'Frozen 1300-round mixed local task/news/treasure ablations with scripted model and virtual tools; not intranet PASS.',
              'python': sys.version.split()[0], 'source': str(source), 'results': rows}
    (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

