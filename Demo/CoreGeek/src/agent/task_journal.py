"""Bounded, credential-filtered cognitive events; never controls game actions.

R01/R06: record wire observations and issued requests, not inferred judge success.
Used after HTTP send and by offline trace export. Full wire data stays in trace.
"""
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import threading

from .telemetry import clean
from . import task_progress

TEXT_LIMIT = 1200
MAX_STREAMS = 8


def excerpt(value, limit=TEXT_LIMIT):
    """Redact before hashing/truncation; retain both ends of long results."""
    value = clean(value)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    truncated = len(text) > limit
    shown = text if not truncated else text[:limit // 2] + '\n[... omitted ...]\n' + text[-limit // 2:]
    return {'text': shown, 'chars': len(text), 'truncated': truncated,
            'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}


class TaskJournal:
    def __init__(self, text_limit=TEXT_LIMIT):
        self.streams = OrderedDict()
        self.lock = threading.Lock()
        self.text_limit = text_limit

    def observe(self, request, response, *, decision=None, event_id=None, stream='unknown'):
        with self.lock:
            return self._observe(request, response, decision, event_id, stream)

    def _observe(self, request, response, decision, event_id, stream):
        if not isinstance(request, dict) or not isinstance(response, dict):
            return []
        round_no = request.get('roundNo')
        if type(round_no) is not int or round_no < 1:
            return []
        previous = self.streams.get(stream)
        # Round-one starts a new match when a previous match had advanced.
        reset = previous is not None and round_no == 1 and previous['round'] > 1
        first = previous is None
        if previous is not None and round_no <= previous['round'] and not reset:
            return []  # duplicate/late observation; no invented new execution
        gap = previous is not None and not reset and round_no != previous['round'] + 1
        if previous is None or reset:
            previous = {'round': round_no - 1, 'fields': {}, 'question': None, 'episode': None}
        previous['round'] = round_no
        self.streams[stream] = previous
        self.streams.move_to_end(stream)
        while len(self.streams) > MAX_STREAMS:
            self.streams.popitem(last=False)
        rows = []
        team = request.get('teamOur') if isinstance(request.get('teamOur'), dict) else {}

        def add(kind, content):
            rows.append({'schema': 'competition-task-event/1', 'kind': kind,
                         'stream': stream, 'round': round_no, 'event_id': event_id,
                         'team': team.get('type'), 'episode': previous['episode'],
                         'continuity': 'reset' if reset else 'gap' if gap else
                                       'first_observation' if first else 'consecutive',
                         'attribution': 'observed_context_only_not_official_request_id',
                         'content': excerpt(content, max(self.text_limit, 4096) if kind == 'task_outcome_summary' else self.text_limit)})

        question = request.get('phaseTask')
        question = question if isinstance(question, str) else ''
        question_hash = excerpt(question)['sha256'] if question else None
        progress = previous.get('progress')
        if progress:
            for kind, content in task_progress.observe(progress, request,
                    not question or question_hash == previous['question'], gap):
                add(kind, content)
        if question_hash != previous['question']:
            if previous['question']:
                if progress:
                    add('task_outcome_summary', task_progress.summary(progress, round_no, team, bool(question)))
                submission = previous.get('submission')
                changes = {}
                for field in ('gold', 'totalScore'):
                    old = previous.get('stats', {}).get(field)
                    new = team.get('goldNum' if field=='gold' else field)
                    changes[field] = new-old if type(old) is int and type(new) is int else None
                receipts = request.get('lastRoundRoleActionResults')
                add('task_text_ended' if not question else 'task_text_replaced',
                    {'previous_question_sha256': previous['question'],
                     'outcome': 'unknown_without_judge_feedback',
                     'meaning': '本地未确认判题结果，不是官方失败回执',
                     'official_success_confirmed': False,
                     'last_submission': submission,
                     'change_since_previous_observation': changes,
                     'change_is_task_reward': 'unknown_other_actions_may_contribute',
                     'observation_gap': gap,
                     'action_receipt': receipts.get(submission['role']) if submission and isinstance(receipts, dict) else None,
                     'errors': request.get('errors', [])})
            previous['submission'] = None
            previous['progress'] = task_progress.start(round_no, team) if question else None
            if question:
                previous['episode'] = f'{round_no}:{question_hash[:12]}'
                add('task_text_observed', question)
            previous['question'] = question_hash

        # These are observed values, not assumed matches to a prior request ID.
        for field in ('llmResp', 'lastCmdResult', 'errors', 'lastRoundRoleActionResults',
                      'lastSummonTreasureResult'):
            value = request.get(field)
            relevant = field not in ('lastRoundRoleActionResults',) or bool(question or previous.get('task_action'))
            present = value not in (None, '', [], {})
            signature = excerpt(value)['sha256'] if present else None
            if relevant and present and signature != previous['fields'].get(field):
                add('observed_' + field, value)
            previous['fields'][field] = signature

        if not question:
            previous['episode'] = None

        agent = decision.get('agent') if isinstance(decision, dict) else None
        if isinstance(decision, dict):
            for field in ('upgrade_itinerary','weapon_readiness'):
                value=decision.get(field)
                if not value:continue
                # Route progress does not need another line on every move.
                signature_value=deepcopy(value)
                if field=='upgrade_itinerary':signature_value.pop('gold_available',None)
                else:
                    for weapon in signature_value:weapon.pop('cooldown',None)
                signature=excerpt(signature_value)['sha256']
                if signature!=previous['fields'].get(field):add(field,value)
                previous['fields'][field]=signature
        if isinstance(agent, dict):
            summary = {key: agent.get(key) for key in ('generation', 'stage', 'stopReason',
                       'prompts', 'commands', 'answers', 'memoryReads', 'methodCount',
                       'httpMethodCount', 'memorySources', 'degraded')}
            signature = excerpt(summary)['sha256']
            if signature != previous['fields'].get('agent'):
                add('agent_state', summary)
            previous['fields']['agent'] = signature
        task = decision.get('task') if isinstance(decision, dict) else None
        if isinstance(task, dict) and task.get('plan_kind') in ('task_end', 'accept_unconfirmed'):
            add('task_lifecycle', task)
        for field in ('prompt', 'executeCmd'):
            if response.get(field):
                add('issued_' + field, response[field])
        previous['task_action'] = False
        commands = response.get('roleCommandMap')
        if isinstance(commands, dict):
            for role_id, action in list(commands.items())[:32]:
                if isinstance(action, dict) and action.get('action') in ('acceptTask', 'submitAnswer', 'summonTreasure'):
                    previous['task_action'] = True
                    add('issued_' + action['action'], {'role': role_id, 'command': deepcopy(action)})
                    if action['action'] == 'submitAnswer' and question:
                        answer = action.get('taskAnswer')
                        previous['submission'] = {'round': round_no, 'role': str(role_id),
                            'answer_sha256': hashlib.sha256(answer.encode()).hexdigest() if isinstance(answer, str) else None}
        if question and previous.get('progress'):
            task_progress.issued(previous['progress'], response, round_no, team)
        previous['stats'] = task_progress.stats(team)
        return rows


_journal = TaskJournal()


def emit(request, response, *, decision=None, event_id=None, emitter=None):
    """Fail-open console hook. It works even if the disk trace is unavailable."""
    try:
        from .console_digest import console_mode, stream_token
        if console_mode() == 'off':
            return []
        rows = _journal.observe(request, response, decision=decision, event_id=event_id,
                                stream=stream_token(request, event_id))
        for row in rows:
            if emitter is not None:
                emitter('task_event ' + json.dumps(row, ensure_ascii=False, separators=(',', ':')))
        return rows
    except Exception:
        return []
