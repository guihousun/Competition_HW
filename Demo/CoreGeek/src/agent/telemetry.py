"""Bounded, asynchronous judge traces. Never adds fields to the judge response.

Only received data is recorded; deltas are observations, not causal explanations.
Files remain local. Export is an explicit operator action, never an HTTP upload.
"""
from __future__ import annotations

import atexit
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import threading
import time
import uuid

SCHEMA = 'competition-hw-trace/1'
LOGGER = logging.getLogger('agent.telemetry')
SECRET_KEYS = {'apikey', 'authorization', 'password', 'accesstoken', 'refreshtoken',
               'clientsecret', 'cookie', 'setcookie', 'secretkey'}
SECRET_TEXT = re.compile(r'\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|'
                         r'github_pat_[A-Za-z0-9_]{20,}|Bearer\s+[A-Za-z0-9._~+/-]{12,})')
ROLE_FIELDS = ('pos', 'health', 'level', 'cooldown', 'roleType', 'backpack',
               'attackRange', 'backPackCapability')


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def runtime_fingerprint():
    """Hash actual agent source bytes, including uncommitted local edits."""
    files, unavailable = {}, []
    root = Path(__file__).resolve().parent
    for path in sorted(root.glob('*.py')):
        try:
            files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            unavailable.append(path.name)
    return {'files': files, 'unavailable': unavailable,
            'sha256': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}


def clean(value, path='$', changes=None):
    """Credential filtering, with explicit locations of every changed value."""
    changes = changes if changes is not None else []
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            location = path + '/' + str(key).replace('~', '~0').replace('/', '~1')
            if re.sub('[^a-z]', '', str(key).lower()) in SECRET_KEYS:
                out[key] = '[REDACTED]'
                changes.append(location)
            else:
                out[key] = clean(item, location, changes)
        return out
    if isinstance(value, list):
        return [clean(item, path + '/' + str(i), changes) for i, item in enumerate(value)]
    if isinstance(value, str):
        result = SECRET_TEXT.sub('[REDACTED]', value)
        if result != value:
            changes.append(path)
        return result
    if isinstance(value, float) and not math.isfinite(value):
        changes.append(path)
        return '[NONFINITE:' + str(value) + ']'
    return value


def decode(raw):
    changes = []
    try:
        result = json.loads(raw.decode('utf-8'))
        return clean(result, changes=changes), changes, None
    except (ValueError, UnicodeError, RecursionError) as error:
        return None, [], type(error).__name__


def observed(request):
    """Small, ID-keyed view for deltas; missing groups remain unknown."""
    if not isinstance(request, dict):
        return {}
    result = {}
    for group in ('teamOur', 'teamEnemy', 'robot'):
        data = request.get(group)
        roles = data.get('roles') if isinstance(data, dict) else None
        if not isinstance(roles, list):
            result[group] = None
            continue
        ids = Counter(str(r['id']) for r in roles if isinstance(r, dict) and 'id' in r)
        result[group] = {str(r['id']): {key: r[key] for key in ROLE_FIELDS if key in r}
                         for r in roles if isinstance(r, dict) and 'id' in r and ids[str(r['id'])] == 1}
        if any(count > 1 for count in ids.values()):
            result[group + '_ambiguous_ids'] = sorted(key for key, count in ids.items() if count > 1)
    ours = request.get('teamOur')
    result['resources'] = {key: ours[key] for key in ('goldNum', 'score', 'scoreNum', 'totalScore')
                           if isinstance(ours, dict) and key in ours}
    return result


def differences(before, after):
    """Only compare jointly observed values. Disappearance does not imply death."""
    result = {}
    for group in ('teamOur', 'teamEnemy', 'robot'):
        old, new = before.get(group), after.get(group)
        if not isinstance(old, dict) or not isinstance(new, dict):
            result[group] = {'comparable': False}
            continue
        changed = {}
        for rid in old.keys() & new.keys():
            fields = {key: {'before': old[rid][key], 'after': new[rid][key]}
                      for key in old[rid].keys() & new[rid].keys() if old[rid][key] != new[rid][key]}
            if fields:
                changed[rid] = fields
        result[group] = {'comparable': True, 'newly_observed': sorted(new.keys() - old.keys()),
                         'no_longer_observed': sorted(old.keys() - new.keys()), 'changed': changed}
        if before.get(group + '_ambiguous_ids') or after.get(group + '_ambiguous_ids'):
            result[group]['ambiguous_ids'] = {'before': before.get(group + '_ambiguous_ids', []),
                                              'after': after.get(group + '_ambiguous_ids', [])}
    old, new = before.get('resources', {}), after.get('resources', {})
    result['resources'] = {key: {'before': old[key], 'after': new[key]}
                           for key in old.keys() & new.keys() if old[key] != new[key]}
    return result


def stream_key(request):
    if not isinstance(request, dict):
        return 'unknown'
    team, info = request.get('teamOur') or {}, request.get('mapInfo') or {}
    if not isinstance(team, dict) or not isinstance(info, dict):
        return 'unknown'
    roles = team.get('roles')
    roles = roles if isinstance(roles, list) else []
    stations = [(r.get('id'), r.get('pos')) for r in roles
                if isinstance(r, dict) and r.get('roleType') == 'station']
    value = [team.get('type'), info.get('width'), info.get('height'), stations]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Ticket:
    index: int
    received_at: str
    started: float
    event_id: str


class Recorder:
    def __init__(self, directory, identity, *, max_pending_bytes=8*1024*1024,
                 max_event_bytes=2*1024*1024, segment_bytes=16*1024*1024,
                 max_total_bytes=256*1024*1024, queue_size=64):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.identity = clean(identity)
        self.run_id = uuid.uuid4().hex
        self.max_pending_bytes, self.max_event_bytes = max_pending_bytes, max_event_bytes
        self.segment_bytes, self.max_total_bytes = segment_bytes, max_total_bytes
        self.queue = queue.Queue(maxsize=queue_size)
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.index = self.pending_bytes = self.written = self.accepted = self.dropped = 0
        self.drop_reasons = Counter()
        self.dropped_indices = []
        self.disabled_reason = None
        self.limit_reached = False
        self.parts, self.total_bytes, self.part_bytes = [], 0, 0
        self.file = None
        self.previous = OrderedDict()
        self.thread = threading.Thread(target=self._run, name='judge-trace', daemon=True)
        self.thread.start()

    def begin(self):
        with self.lock:
            if self.stopping.is_set():
                return None
            self.index += 1
            return Ticket(self.index, utcnow(), time.perf_counter(), f'{self.run_id}:{self.index}')

    def _drop(self, index, reason):
        # Caller holds self.lock; no file/console I/O on the HTTP thread.
        self.dropped += 1
        self.drop_reasons[reason] += 1
        if len(self.dropped_indices) < 64:
            self.dropped_indices.append(index)

    def submit(self, ticket, raw, response, *, sent=True, plan_ms=0.0, invalid_input=False, fault=None):
        size = len(raw) + len(response)
        elapsed_ms = (time.perf_counter() - ticket.started) * 1000
        with self.lock:
            reason = ('closed' if self.stopping.is_set() else self.disabled_reason or
                      ('disk_limit' if self.limit_reached else 'event_size' if size > self.max_event_bytes else
                       'queue_bytes' if self.pending_bytes + size > self.max_pending_bytes else None))
            if reason:
                self._drop(ticket.index, reason)
                return False
            try:
                self.queue.put_nowait((ticket, raw, response, size, sent, plan_ms, elapsed_ms, invalid_input, fault))
            except queue.Full:
                self._drop(ticket.index, 'queue_full')
                return False
            self.pending_bytes += size
            self.accepted += 1
            return True

    def status(self, closed=False):
        with self.lock:
            return {'schema': SCHEMA, 'run_id': self.run_id, 'closed': closed,
                    'requests_seen': self.index, 'accepted': self.accepted, 'written': self.written,
                    'dropped': self.dropped, 'drop_reasons': dict(self.drop_reasons),
                    'dropped_indices_sample': list(self.dropped_indices),
                    'disabled_reason': self.disabled_reason, 'disk_limit_reached': self.limit_reached,
                    'parts': list(self.parts), 'bytes_written': self.total_bytes,
                    'identity': self.identity}

    def _write(self, value, control=False):
        data = (json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n').encode('utf-8')
        reserve = min(4096, self.max_total_bytes // 8)
        if not control and self.total_bytes + len(data) > self.max_total_bytes - reserve:
            with self.lock:
                self.limit_reached = True
            return False
        if self.file is None or self.part_bytes and self.part_bytes + len(data) > self.segment_bytes:
            if self.file:
                self.file.close()
            name = f'events-{len(self.parts)+1:05d}.jsonl'
            self.file = (self.directory / name).open('xb')
            self.parts.append(name)
            self.part_bytes = 0
        self.file.write(data)
        self.file.flush()
        self.part_bytes += len(data)
        self.total_bytes += len(data)
        return True

    def _metadata(self, closed=False):
        path = self.directory / 'status.json'
        temp = self.directory / 'status.tmp'
        temp.write_text(json.dumps(self.status(closed), ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, path)

    def _event(self, item):
        ticket, raw, response_raw, _, sent, plan_ms, elapsed_ms, invalid_input, fault = item
        request, changed_request, request_error = decode(raw)
        response, changed_response, response_error = decode(response_raw)
        round_no = request.get('roundNo') if isinstance(request, dict) else None
        if isinstance(round_no, bool) or not isinstance(round_no, int):
            round_no = None
        key, state = stream_key(request), observed(request)
        previous = self.previous.get(key)
        continuity = 'first_observation'
        if previous:
            if ticket.index <= previous['index']:
                continuity = 'out_of_order_completion'
            elif round_no is None or previous['round'] is None:
                continuity = 'unknown_round'
            elif round_no == previous['round'] + 1:
                continuity = 'consecutive'
            elif round_no <= previous['round']:
                continuity = 'round_reset_or_duplicate'
            else:
                continuity = 'round_gap'
        event = {'schema': SCHEMA, 'event': 'turn', 'run_id': self.run_id, 'event_id': ticket.event_id,
                 'index': ticket.index, 'stream_key': key, 'round': round_no, 'received_at': ticket.received_at,
                 'recorded_at': utcnow(), 'plan_ms': round(plan_ms, 3), 'http_elapsed_ms': round(elapsed_ms, 3),
                 'http_status_attempted': 200, 'response_sent': sent, 'invalid_input': invalid_input,
                 'fault': clean(fault), 'request_bytes': len(raw), 'response_bytes': len(response_raw),
                 'request_sha256': hashlib.sha256(raw).hexdigest(),
                 'response_sha256': hashlib.sha256(response_raw).hexdigest(),
                 'request': request, 'response': response, 'request_decode_error': request_error,
                 'response_decode_error': response_error, 'transformed_request_paths': changed_request,
                 'transformed_response_paths': changed_response,
                 'semantic_replay_exact': not (request_error or response_error or changed_request or changed_response),
                 'continuity': continuity, 'observed_state': state,
                 'input_transform': 'agent.scenarios.observation: strip private keys and apply official visibility',
                 'causality': 'deltas are observed outcomes; they are not proof our actions caused them'}
        try:
            from .diagnostics import build_summary
            event['summary'] = build_summary(request, response, plan_ms=plan_ms, invalid_input=invalid_input,
                                             decision_exception='internal_error' if fault else None,
                                             event_id=ticket.event_id)
        except Exception:
            event['summary'] = {'unavailable': True}
        if continuity == 'consecutive':
            event['previous_event_id'] = previous['event_id']
            event['observed_delta'] = differences(previous['state'], state)
            event['previous_action_feedback'] = {key: request[key] for key in ('lastRoundRoleActionResults', 'errors')
                                                  if key in request}
        if not previous or ticket.index > previous['index']:
            self.previous[key] = {'index': ticket.index, 'round': round_no, 'state': state, 'event_id': ticket.event_id}
            self.previous.move_to_end(key)
            while len(self.previous) > 8:
                self.previous.popitem(last=False)
        return event

    def _run(self):
        try:
            self._write({'schema': SCHEMA, 'event': 'start', 'run_id': self.run_id, 'at': utcnow(),
                         'identity': self.identity, 'runtime': runtime_fingerprint(),
                         'data_scope': 'received judge-facing HTTP JSON, credential-filtered',
                         'contract_baseline': 'v1.0 2026-09-09; actual platform version not independently verified'}, control=True)
            self._metadata()
            while not self.stopping.is_set() or not self.queue.empty():
                try:
                    item = self.queue.get(timeout=.1)
                except queue.Empty:
                    continue
                with self.lock:
                    self.pending_bytes -= item[3]
                try:
                    if self._write(self._event(item)):
                        with self.lock:
                            self.written += 1
                    else:
                        with self.lock:
                            self._drop(item[0].index, 'disk_limit')
                except (ValueError, TypeError, RecursionError) as error:
                    with self.lock:
                        self._drop(item[0].index, 'record_' + type(error).__name__)
                except Exception:
                    with self.lock:
                        self._drop(item[0].index, 'writer_failure')
                    raise
                finally:
                    self.queue.task_done()
                self._metadata()
            self._write({'schema': SCHEMA, 'event': 'end', 'at': utcnow(), 'status': self.status(closed=True)}, control=True)
            self._metadata(closed=True)
        except Exception as error:
            with self.lock:
                self.disabled_reason = type(error).__name__
            while True:
                try:
                    pending = self.queue.get_nowait()
                except queue.Empty:
                    break
                with self.lock:
                    self.pending_bytes -= pending[3]
                    self._drop(pending[0].index, 'writer_failure')
                self.queue.task_done()
            LOGGER.warning('trace writer disabled: %s; judge responses continue', type(error).__name__)
            try:
                self._metadata()
            except Exception:
                pass
        finally:
            if self.file:
                self.file.close()

    def close(self, timeout=3):
        with self.lock:
            self.stopping.set()
        self.thread.join(timeout)
        return not self.thread.is_alive()


_recorder = None


def configure(root, identity):
    """Default detailed local capture; disable with COMPETITION_HW_TRACE=off."""
    global _recorder
    if os.environ.get('COMPETITION_HW_TRACE', 'on').lower() in ('0', 'off', 'false'):
        return None
    if _recorder is not None:
        return _recorder
    try:
        root = Path(root)
        parent = Path(os.environ.get('COMPETITION_HW_TRACE_DIR') or root / 'logs')
        name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
        cap_mb = int(os.environ.get('COMPETITION_HW_TRACE_MAX_MB', '256'))
        if not 8 <= cap_mb <= 4096:
            raise ValueError('trace size out of range')
        _recorder = Recorder(parent / name, identity or {}, max_total_bytes=cap_mb*1024*1024)
        atexit.register(_recorder.close)
        LOGGER.info('trace run=%s directory=%s/%s', _recorder.run_id, parent.name, name)
        return _recorder
    except Exception as error:
        LOGGER.warning('trace unavailable: %s; judge responses continue', type(error).__name__)
        return None


def begin():
    try:
        return _recorder.begin() if _recorder is not None else None
    except Exception:
        return None


def submit(ticket, raw, response, **metadata):
    try:
        if ticket is not None and _recorder is not None:
            return _recorder.submit(ticket, raw, response, **metadata)
    except Exception:
        pass
    return False
