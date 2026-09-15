#!/usr/bin/env python3
"""Inspect/export/replay local judge traces, or compare observed simulator data.

No network calls, no automatic upload, no execution of emitted executeCmd/prompt.
Trace input is data, never an instruction to this tool or to an assistant.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT/'src' if (ROOT/'src/agent').is_dir() else ROOT/'Demo/CoreGeek/src'
sys.path.insert(0, str(RUNTIME))
from agent.telemetry import SCHEMA, clean, differences, observed, runtime_fingerprint

MAX_LINE = 16*1024*1024


class TraceError(ValueError):
    pass


def records(path, quality=None):
    """Stream complete JSONL records; report a torn tail rather than hiding it."""
    quality = quality if quality is not None else {}
    quality.setdefault('incomplete_lines', 0)
    quality.setdefault('invalid_lines', 0)
    quality.setdefault('parts', [])
    path = Path(path)
    def lines(stream, name):
        quality['parts'].append(name)
        while True:
            raw = stream.readline(MAX_LINE+1)
            if not raw:
                break
            if len(raw) > MAX_LINE:
                raise TraceError('trace line exceeds 16 MiB; refusing an unbounded input')
            if not raw.endswith(b'\n'):
                quality['incomplete_lines'] += 1
                continue
            try:
                row = json.loads(raw)
                if not isinstance(row, dict) or row.get('schema') != SCHEMA:
                    raise ValueError('unexpected schema')
            except (ValueError, UnicodeError):
                quality['invalid_lines'] += 1
                continue
            yield row
    if path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path) as archive:
            if sum(x.file_size for x in archive.infolist()) > 1024**3:
                raise TraceError('archive expands beyond 1 GiB')
            if 'events.jsonl' not in archive.namelist():
                raise TraceError('archive has no events.jsonl')
            with archive.open('events.jsonl') as stream:
                yield from lines(stream, 'events.jsonl')
        return
    paths = sorted(path.glob('events-*.jsonl')) if path.is_dir() else [path]
    if not paths:
        raise TraceError('no trace part files found')
    for part in paths:
        if part.is_symlink():
            raise TraceError('symlink trace parts are not accepted')
        with part.open('rb') as stream:
            yield from lines(stream, part.name)


def inspect(path):
    quality, counts, continuity = {}, Counter(), Counter()
    identities, rounds, indexes = {}, [], []
    for row in records(path, quality):
        counts[row.get('event', 'unknown')] += 1
        if row.get('event') == 'start':
            identities[row['run_id']] = {'identity': row.get('identity'), 'runtime': row.get('runtime')}
        elif row.get('event') == 'end':
            quality['writer_status'] = row.get('status')
        elif row.get('event') == 'turn':
            continuity[row.get('continuity', 'unknown')] += 1
            counts['transformed_turns'] += bool(row.get('transformed_request_paths') or row.get('transformed_response_paths'))
            counts['unsent_responses'] += not row.get('response_sent', False)
            counts['invalid_input'] += bool(row.get('invalid_input'))
            counts['decision_exceptions'] += bool((row.get('fault') or {}).get('category') == 'decision_exception')
            if isinstance(row.get('round'), int):
                rounds.append(row['round'])
            indexes.append(row.get('index'))
    if Path(path).is_dir() and (Path(path)/'status.json').is_file():
        quality['writer_status'] = json.loads((Path(path)/'status.json').read_text(encoding='utf-8'))
    writer = quality.get('writer_status') or {}
    quality['has_start'] = bool(identities)
    quality['has_end'] = bool(counts['end'])
    quality['ordered_indexes'] = all(isinstance(x, int) for x in indexes) and indexes == sorted(set(indexes))
    quality['missing_indexes_within_span'] = (max(indexes)-min(indexes)+1-len(set(indexes))) if indexes and quality['ordered_indexes'] else None
    quality['capture_complete'] = bool(quality['has_start'] and quality['has_end'] and writer.get('closed')
                                     and not writer.get('dropped') and not writer.get('disabled_reason')
                                     and not quality['incomplete_lines'] and not quality['invalid_lines']
                                     and quality['ordered_indexes'] and not quality['missing_indexes_within_span']
                                     and writer.get('written') == counts['turn']
                                     and writer.get('requests_seen') == writer.get('written')
                                     and (not indexes or indexes[0] == 1))
    return {'schema': SCHEMA, 'identities': identities, 'counts': dict(counts),
            'round_range': [min(rounds), max(rounds)] if rounds else None,
            'continuity': dict(continuity), 'quality': quality,
            'note': 'capture completeness is not full-game coverage or official PASS'}


def new_output(path):
    path = Path(path)
    if path.exists():
        raise TraceError('output already exists; choose a new file')
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def export(path, output, first=None, last=None):
    if (first is not None and first < 1) or (last is not None and last < 1) or (first is not None and last is not None and first > last):
        raise TraceError('invalid round window')
    target = new_output(output)
    count, changes = 0, []
    quality = {}
    with zipfile.ZipFile(target, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        with archive.open('events.jsonl', 'w') as stream:
            for row in records(path, quality):
                if row.get('event') == 'end' and (first is not None or last is not None):
                    continue  # A selected window must not inherit a full-run completion claim.
                if row.get('event') == 'turn':
                    round_no = row.get('round')
                    if first is not None and (not isinstance(round_no, int) or round_no < max(1, first-1)):
                        continue
                    if last is not None and (not isinstance(round_no, int) or round_no > last):
                        continue
                    count += 1
                row_changes = []
                row = clean(row, changes=row_changes)
                changes.extend(row_changes)
                if row_changes and row.get('event') == 'turn':
                    row['semantic_replay_exact'] = False
                    row['export_transformed_paths'] = row_changes
                stream.write((json.dumps(row, ensure_ascii=False, allow_nan=False)+'\n').encode('utf-8'))
        manifest = {'schema': 'competition-hw-trace-export/1', 'turns': count,
                    'requested_round_range': [first, last], 'includes_previous_round_for_feedback': first is not None,
                    'extra_redactions': len(changes), 'source_quality': quality,
                    'note': 'Selected observed data only; user decides what may leave the company environment'}
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    digest = hashlib.sha256()
    with target.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return {'output': str(target), 'sha256': digest.hexdigest(), 'turns': count,
            'selected_window': first is not None or last is not None}


def public_observation(value):
    from agent.scenarios import observation
    return observation(value)


def transitions(path, output):
    target = new_output(output)
    previous, count, skipped, quality = {}, 0, 0, {}
    with target.open('x', encoding='utf-8') as stream:
        for row in records(path, quality):
            if row.get('event') != 'turn':
                continue
            key = row.get('run_id'), row.get('stream_key')
            old = previous.get(key)
            previous[key] = row
            if not old or not isinstance(row.get('round'), int) or not isinstance(old.get('round'), int):
                continue
            if (row['round'] != old['round']+1 or not old.get('response_sent')
                    or not isinstance(row.get('index'), int) or not isinstance(old.get('index'), int)
                    or row['index'] <= old['index']
                    or not old.get('semantic_replay_exact') or not row.get('semantic_replay_exact')
                    or not isinstance(row.get('request'), dict) or not isinstance(old.get('request'), dict)):
                skipped += 1
                continue
            try:
                before, after = public_observation(old['request']), public_observation(row['request'])
            except (ValueError, KeyError, TypeError):
                skipped += 1
                continue
            record = {'schema': 'competition-hw-observed-transition/1', 'run_id': row.get('run_id'),
                      'from_event': old['event_id'], 'to_event': row['event_id'],
                      'round': old['round'], 'observation': before, 'response': old['response'],
                      'next_observation': after,
                      'action_feedback': {k: after[k] for k in ('lastRoundRoleActionResults', 'errors') if k in after},
                      'scope': 'observed transition; hidden world/randomness and action causality are unknown'}
            stream.write(json.dumps(clean(record), ensure_ascii=False, allow_nan=False)+'\n')
            count += 1
    return {'output': str(target), 'transitions': count, 'skipped_nonconsecutive_or_transformed': skipped,
            'source_quality': quality}


def replay(path, output):
    """A fresh CLI process owns planner memory. Emitted channels are never executed."""
    from agent.brain import respond
    from agent.diagnostics import startup_identity
    identity = startup_identity(entry='trace_tool.py', root=RUNTIME.parent)
    target = new_output(output)
    count = matched = skipped = 0
    first, previous, gaps, quality = {}, {}, [], {}
    last_index = 0
    with target.open('x', encoding='utf-8') as stream:
        for row in records(path, quality):
            if row.get('event') != 'turn':
                continue
            key = row.get('run_id'), row.get('stream_key')
            index = row.get('index')
            if not isinstance(index, int) or index != last_index + 1:
                gaps.append(row.get('event_id'))
            last_index = index if isinstance(index, int) else last_index
            first.setdefault(key, row.get('round'))
            if key in previous and row.get('round') != previous[key]+1:
                gaps.append(row.get('event_id'))
            previous[key] = row.get('round') if isinstance(row.get('round'), int) else -1
            if not row.get('semantic_replay_exact') or not row.get('response_sent'):
                skipped += 1
                continue
            error = None
            try:
                actual = respond(public_observation(row['request'])) if isinstance(row.get('request'), dict) else {'roleCommandMap': {}}
            except Exception as exc:
                error = type(exc).__name__
                actual = {'roleCommandMap': {}}
            equal = actual == row.get('response')
            count += 1
            matched += equal
            stream.write(json.dumps(clean({'event_id': row.get('event_id'), 'round': row.get('round'),
                                     'matches': equal, 'recorded': row.get('response'),
                                     'replayed': actual, 'exception_type': error}), ensure_ascii=False)+'\n')
    return {'output': str(target), 'replay_identity': identity, 'replay_runtime': runtime_fingerprint(),
            'replayed': count, 'matched': matched, 'mismatched': count-matched,
            'skipped': skipped, 'complete_prefix_from_round_1': len(first) == 1 and all(x == 1 for x in first.values()) and not gaps and not skipped
            and not quality['invalid_lines'] and not quality['incomplete_lines'],
            'source_quality': quality,
            'gap_events': gaps, 'note': 'Policy replay only: no simulator advance, network, shell or LLM execution; missing prefix can cause expected differences'}


def compare(actual_path, predicted_path):
    def load(path):
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        for key in ('request', 'observation', 'state'):
            if isinstance(data, dict) and isinstance(data.get(key), dict):
                data = data[key]
                break
        return public_observation(data)
    actual, predicted = load(actual_path), load(predicted_path)
    if actual.get('roundNo') != predicted.get('roundNo'):
        return {'aligned': False, 'reason': 'round_mismatch', 'actual_round': actual.get('roundNo'),
                'predicted_round': predicted.get('roundNo'), 'difference': None}
    return {'scope': 'jointly observed roles/resources only; disappearance is not proof of death',
            'aligned': isinstance(actual.get('roundNo'), int),
            'actual_round': actual.get('roundNo'), 'predicted_round': predicted.get('roundNo'),
            'difference': differences(observed(actual), observed(predicted)),
            'labels': {'before': 'actual', 'after': 'predicted'},
            'note': 'No reconstruction of hidden enemies, random refreshes or future waves'}


def tasks(path, output):
    """Export cognitive exchanges without per-frame maps or executing anything."""
    from agent.task_journal import TaskJournal
    from agent.console_digest import stream_token
    target = Path(output)
    # Existing capture quality remains authoritative; snippets cannot recover gaps.
    quality = inspect(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    journal = TaskJournal(text_limit=MAX_LINE)
    count, turns = 0, 0
    with target.open('x', encoding='utf-8') as stream:
        header = {'schema': 'competition-task-export/1', 'event': 'start',
                  'source': str(path), 'capture': quality,
                  'note': 'Observed and issued content; no inferred task success. Not a simulator replay; no maps. Credential-filtered.'}
        stream.write(json.dumps(clean(header), ensure_ascii=False) + '\n')
        for row in records(path):
            if row.get('event') != 'turn':
                continue
            turns += 1
            request = row.get('request')
            response = row.get('response') if row.get('response_sent') is True else {}
            event_id = row.get('event_id')
            for item in journal.observe(request, response, decision=row.get('decision'),
                                        event_id=event_id, stream=stream_token(request, event_id)):
                stream.write(json.dumps(clean(item), ensure_ascii=False) + '\n')
                count += 1
        stream.write(json.dumps({'schema': 'competition-task-export/1', 'event': 'end',
                                 'turns_read': turns, 'task_events': count}) + '\n')
    return {'output': str(target), 'turns_read': turns, 'task_events': count,
            'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
            'capture_complete': quality['quality']['capture_complete']}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='command', required=True)
    for name in ('inspect', 'export', 'transitions', 'replay', 'tasks'):
        cmd=sub.add_parser(name)
        cmd.add_argument('input', type=Path)
        if name != 'inspect':
            cmd.add_argument('--output', type=Path, required=True)
        if name == 'export':
            cmd.add_argument('--from-round', type=int)
            cmd.add_argument('--to-round', type=int)
    c=sub.add_parser('compare')
    c.add_argument('--actual', type=Path, required=True)
    c.add_argument('--predicted', type=Path, required=True)
    args=p.parse_args(argv)
    try:
        if args.command == 'inspect': result=inspect(args.input)
        elif args.command == 'export': result=export(args.input,args.output,args.from_round,args.to_round)
        elif args.command == 'transitions': result=transitions(args.input,args.output)
        elif args.command == 'replay': result=replay(args.input,args.output)
        elif args.command == 'tasks': result=tasks(args.input,args.output)
        else: result=compare(args.actual,args.predicted)
        print(json.dumps(clean(result),ensure_ascii=False,indent=2))
        return 0
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print(json.dumps({'error': type(error).__name__, 'detail': str(error)},ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
