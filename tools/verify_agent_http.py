"""Full games against an extracted competition package over actual root POST.

The source tree supplies only the simulator, public fixtures and scripted model.
All strategy decisions come from the package's separate HTTP server process.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import Request, urlopen

parser = argparse.ArgumentParser()
parser.add_argument('--source', required=True)
parser.add_argument('--archive', required=True)
parser.add_argument('--output', required=True)
parser.add_argument('--seed', type=int, required=True)
parser.add_argument('--side', choices=['challenger', 'defender'], required=True)
parser.add_argument('--rounds', type=int, default=1300)
parser.add_argument('--enable-agents', action='store_true', help='Record explicit runtime override; omission tests package defaults')
args = parser.parse_args()
source, archive, out = Path(args.source).resolve(), Path(args.archive).resolve(), Path(args.output).resolve()
out.mkdir(parents=True, exist_ok=False)
sys.path.insert(0, str(source / 'Demo/CoreGeek/src'))
from agent import local_scripted_model, local_task_cases, local_task_sandbox, local_world_news, scenarios, simulator

def forbidden_local_policy(*args, **kwargs):
    raise AssertionError('environment must not run a second strategy')
simulator.plan_for_state = forbidden_local_policy

def save(name, data):
    (out / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

def hashes():
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((source / 'Demo/CoreGeek/src/agent').glob('*.py'))}

expected_hash = archive.with_suffix('.gz.sha256').read_text(encoding='utf-8').split()[0]
assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected_hash
before = hashes()
times, rows = [], []
counts = {'prompts': 0, 'news_prompts': 0, 'treasure_prompts': 0, 'task_prompts': 0,
          'commands': 0, 'task_completions': 0, 'attacks': 0, 'channel_conflicts': 0}
failure = None
state = local_world_news.install(local_task_cases.install(scenarios.scenario(args.seed, args.side)))
started = time.monotonic()
with tempfile.TemporaryDirectory(prefix='competition-http-game-') as temp:
    folder = Path(temp)
    with tarfile.open(archive) as bundle:
        bundle.extractall(folder, filter='data')
    package = folder / 'CoreGeek'
    manifest = json.loads((package / 'submission-manifest.json').read_text(encoding='utf-8'))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.endswith('_API_KEY')
           and k not in ('COMPETITION_HW_TASK_AGENT', 'COMPETITION_HW_WORLD_AGENT', 'COMPETITION_HW_LLM_ROUTER')}
    env.update(PYTHONIOENCODING='utf-8', COMPETITION_HW_TRACE_DIR=str(out / 'server-traces'))
    if args.enable_agents:
        env.update(COMPETITION_HW_TASK_AGENT='on', COMPETITION_HW_WORLD_AGENT='on')
    with (out / 'server.log').open('wb') as log:
        process = subprocess.Popen([sys.executable, str(package / 'main3.py'), str(port)],
            cwd=folder, env=env, stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            url = f'http://127.0.0.1:{port}/'
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError('package_exited_before_ready')
                try:
                    with urlopen(url, timeout=.5) as response:
                        assert response.status == 200
                    break
                except OSError:
                    time.sleep(.05)
            else:
                raise TimeoutError('package_startup_over_10s')
            for _ in range(args.rounds):
                payload = scenarios.observation(state)
                assert '_demo' not in payload and '_engine' not in payload
                begin = time.perf_counter()
                data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                with urlopen(Request(url, data=data, headers={'Content-Type': 'application/json'}), timeout=5) as response:
                    decision = json.load(response)
                elapsed = (time.perf_counter() - begin) * 1000
                times.append(elapsed)
                if elapsed >= 5000:
                    raise TimeoutError('http_response_over_5s')
                assert isinstance(decision.get('roleCommandMap'), dict)
                assert set(decision) <= {'roleCommandMap', 'prompt', 'executeCmd'}
                counts['channel_conflicts'] += bool(decision.get('prompt') and decision.get('executeCmd'))
                result = simulator.step(state, external_response=decision)
                state = result['state']
                if decision.get('prompt'):
                    counts['prompts'] += 1
                    prompt = decision['prompt']
                    category = ('news_prompts' if '"events":' in prompt else 'treasure_prompts') if '\n公开来源：' in prompt else 'task_prompts'
                    counts[category] += 1
                    state['llmResp'] = local_scripted_model.complete(decision['prompt'])['answer']
                if decision.get('executeCmd'):
                    counts['commands'] += 1
                    fixture = local_task_sandbox.active_task_fixture(state)
                    state['lastCmdResult'] = local_task_sandbox.execute(decision['executeCmd'], fixture, active=True)
                report = state['_demo'].get('task_report') or {}
                counts['task_completions'] += report.get('ended') == 'completed'
                counts['attacks'] += sum(c.get('action') == 'attack' for c in decision['roleCommandMap'].values())
                rows.append({'round': payload['roundNo'], 'http_ms': elapsed, 'input_bytes': len(data),
                             'command_count': len(decision['roleCommandMap']), 'prompt': bool(decision.get('prompt')),
                             'tool': bool(decision.get('executeCmd')), 'task_ended': report.get('ended')})
                if len(rows) % 130 == 0:
                    save('live.json', {'rounds': len(rows), 'counts': counts, 'max_http_ms': max(times)})
                if result.get('done'):
                    break
        except Exception as error:
            failure = {'type': type(error).__name__, 'message': str(error)[:300]}
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
ordered = sorted(times)
def percentile(q):
    return ordered[min(len(ordered)-1, int((len(ordered)-1)*q))] if ordered else None
base = next(r for r in state['teamOur']['roles'] if r['roleType'] == 'station')
report = {'scope': 'Actual packaged main3.py HTTP agent; external-only simulator settlement, scripted cognition and virtual sandbox. Not real-model full game or intranet PASS.',
          'python': sys.version.split()[0], 'platform': os.name, 'archive_sha256': expected_hash,
          'package_source_commit': manifest['commit'], 'explicit_agent_override': args.enable_agents,
          'seed': args.seed, 'side': args.side, 'rounds': len(rows), 'requested_rounds': args.rounds,
          'counts': counts, 'score': state['teamOur']['totalScore'], 'base_hp': base['health'],
          'http_p95_ms': percentile(.95), 'http_p99_ms': percentile(.99), 'http_max_ms': max(times) if times else None,
          'failure': failure, 'stable_environment_sources': hashes() == before, 'environment_files': before,
          'elapsed_seconds': time.monotonic()-started,
          'passed': failure is None and len(rows) == args.rounds and counts['channel_conflicts'] == 0
              and counts['prompts'] > 0 and counts['commands'] > 0 and counts['task_completions'] > 0
              and all(counts[k] > 0 for k in ('news_prompts', 'treasure_prompts', 'task_prompts'))
              and bool(times) and max(times) < 5000 and hashes() == before}
save('requests.json', rows)
save('report.json', report)
print(json.dumps({k:v for k,v in report.items() if k!='environment_files'}, ensure_ascii=False), flush=True)
