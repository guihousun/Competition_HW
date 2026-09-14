"""Actual-model public long-rumour evaluation, distinct from full-game scores.

Uses the configured OpenRouter client and a bounded number of real calls.
Prompts contain only the public fixture, never the evaluator's expected values.
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
    parser.add_argument('--output', required=True)
    parser.add_argument('--side', choices=['challenger', 'defender'], default='challenger')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / 'Demo/CoreGeek/src'))
    from agent import brain, planner, scenarios
    from agent.deepseek_client import DeepSeekClient, ProviderResponseError, MODEL, EFFORT
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    os.environ[brain.WORLD_AGENT_ENV] = 'on'
    os.environ[brain.TASK_AGENT_ENV] = 'off'
    client = DeepSeekClient(timeout=120, max_tokens=16384)
    if not client.configured:
        raise RuntimeError('missing selected-provider credential')

    def save(name, data):
        (out / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

    def hashes():
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted((root / 'Demo/CoreGeek/src/agent').glob('*.py'))}

    original_hashes = hashes()
    source_sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    head = '祭坛位于横坐标3、纵坐标4，最初告示要求IronWhistle。'
    middle = '补充：不能献祭AncientScroll。'
    tail = '更正：IronWhistle要求取消，只需两份AcientTablet。游戏第3天白昼开始后的前30回合开启。'
    text = head + '市井闲谈。' * 440 + middle + '无关见闻。' * 400 + tail
    payload = scenarios.observation(scenarios.scenario(90601, args.side))
    payload['worldNews'] = {'officialNews': '', 'folkLegends': text}
    state = planner.PlannerState()
    trace, calls, tokens, daily = [], 0, 0, {}
    started = time.monotonic()
    raw = ''
    for day in range(1, 4):
        for offset in range(10):
            n = (day - 1) * 130 + 1 + offset
            payload['roundNo'], payload['llmResp'], raw = n, raw, ''
            state.note_round(n)
            response = brain.plan_for_state(payload, state, judge_tasks=False)
            state.note_submission(response.prompt, response.execute, n, False)
            state = planner.PlannerState.load(json.loads(json.dumps(state.dump())))
            world = state.team_agent.world
            row = {'round': n, 'status': dict(world.status), 'response': response.build(),
                   'ordinary_used': state.judge.llm_used_today, 'degraded': state.team_agent.degraded,
                   'hypothesis': world.hypothesis, 'draft': world.drafts['treasure']}
            daily[str(day)] = state.judge.llm_used_today
            if response.prompt:
                if calls >= 8:
                    row['provider_error'] = 'evaluation_call_cap'
                    trace.append(row)
                    break
                calls += 1
                row['call'] = calls
                save('live.json', {'pid': os.getpid(), 'calls': calls, 'current_round': n,
                                   'trace': trace + [row], 'source_sha': source_sha})
                try:
                    result = client.complete(response.prompt)
                    row['model_result'] = result
                    raw = result['answer']
                    tokens += result.get('usage', {}).get('total_tokens', 0)
                except ProviderResponseError as error:
                    row.update(provider_error='invalid_provider_response', diagnostics=error.diagnostics)
                    tokens += error.diagnostics.get('usage', {}).get('total_tokens', 0)
                except Exception as error:
                    reason = str(error)
                    safe = reason in ('provider_timeout_or_network', 'missing_credential', 'invalid_provider_response')
                    safe = safe or (reason.startswith('provider_http_') and reason[14:].isdigit())
                    row['provider_error'] = reason if safe else 'provider_failed'
            trace.append(row)
            save('trace.json', trace)
            if world.status['treasure'] in ('interpreted', 'retry_limit', 'context_budget_exceeded') or state.team_agent.degraded:
                break
            if not raw and not response.prompt:
                break
        if world.status['treasure'] in ('interpreted', 'retry_limit', 'context_budget_exceeded') or state.team_agent.degraded or calls >= 8:
            break
    notes = world.policy_view(261)['treasure']
    sid = world.sources['treasure'][0]['id']
    checks = {'complete': notes.get('known') is True, 'site': notes.get('site') == {'x': 3, 'y': 4},
              'items': sorted(notes.get('items') or []) == ['AcientTablet', 'AcientTablet'],
              'window': (notes.get('opensAt'), notes.get('closesAt')) == (261, 290),
              'full_source_sent': world.memories['treasure'].reviewed(sid),
              'no_sandbox': all(not row['response'].get('executeCmd') for row in trace),
              'ordinary_quota': all(v <= 3 for v in daily.values()),
              'no_degraded_state': not state.team_agent.degraded, 'source_stable': original_hashes == hashes()}
    save('report.json', {'scope': 'Actual model and public long text through planner/router/JSON; not simulator settlement or intranet PASS',
        'source_sha': source_sha, 'files': original_hashes, 'side': args.side, 'model': MODEL, 'effort': EFFORT,
        'python': sys.version.split()[0], 'max_output_tokens': client.max_tokens, 'public_chars': len(text),
        'calls': calls, 'daily_used': daily, 'returned_usage_tokens': tokens,
        'usage_note': 'Only returned usage is counted; missing usage is unknown, not free',
        'elapsed_seconds': time.monotonic() - started, 'checks': checks, 'passed': all(checks.values()),
        'final_status': world.status, 'final_hypothesis': world.hypothesis})
    print(json.dumps({'passed': all(checks.values()), 'calls': calls, 'checks': checks}))
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
