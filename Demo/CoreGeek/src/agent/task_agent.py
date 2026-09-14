"""Task-solving state machine, independent of transport and official quotas.

The caller supplies a public, bounded context and verified router receipts.
This module proposes operations; it never invokes a model, runs a command,
spends game resources or declares judge success. Only acknowledge() records an
operation actually emitted by the common arbiter. Integration is opt-in.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any

SCHEMA = 'competition-task-agent/1'
MAX_PROMPTS = 8  # engineering limits for one task, not official LLM allowances
MAX_COMMANDS = 8
MAX_CONTEXT = 12000
MAX_REPLY = 16000
MAX_HISTORY = 12


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]


def _json_unique(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def constant(value):
        raise ValueError('nonfinite JSON constant')
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


class TaskAgent:
    def __init__(self):
        self.generation = ''
        self.source = ''
        self.sequence = 0
        self.prompts = 0
        self.commands = 0
        self.answers = 0
        self.stage = 'idle'
        self.proposal = None
        self.pending = None
        self.history = []
        self.seen = []
        self.command_attempts = {}
        self.last_feedback_round = 0
        self.last_emit_round = 0
        self.stop_reason = ''

    def begin(self, generation: str, source: str):
        if self.stop_reason == 'state_restore_rejected':
            return False  # deterministic fallback until caller explicitly resets the match
        if not isinstance(generation, str) or not generation or len(generation) > 160:
            raise ValueError('invalid generation')
        if not isinstance(source, str) or not source or len(source) > 160:
            raise ValueError('invalid source')
        if (self.generation, self.source) != (generation, source):
            self.__init__()
            self.generation, self.source = generation, source
            self.stage = 'ready'
        return True

    def _event(self, kind, text, round_no):
        self.history.append({'kind': kind, 'text': text[:1200], 'round': round_no,
                             'truncated': len(text) > 1200, 'sha256': _digest(text)})
        self.history = self.history[-MAX_HISTORY:]

    def _propose(self, kind, payload, purpose):
        self.sequence += 1
        token = _digest(f'{self.generation}\0{self.source}\0{self.sequence}')
        self.proposal = {'kind': kind, 'payload': payload, 'token': token,
                         'purpose': purpose, 'generation': self.generation}
        return deepcopy(self.proposal)

    def decide(self, context: str, evidence_ids: list[str], *, active: bool, round_no: int):
        """Return a proposal or None. Repeated previews/offers do not count calls."""
        if not active:
            self.finish('task_not_confirmed')
            return None
        if self.stage in ('stopped', 'ended', 'awaiting_judgement') or self.pending:
            return None
        if self.proposal:
            return deepcopy(self.proposal)
        if not self.generation or not isinstance(context, str) or len(context) > MAX_CONTEXT:
            self.stop_reason = 'context_unavailable_or_over_limit'
            return None
        if not isinstance(evidence_ids, list) or len(evidence_ids) > 32 or any(
                not isinstance(k, str) or not k or len(k) > 160 for k in evidence_ids):
            self.stop_reason = 'invalid_evidence_index'
            return None
        if self.prompts >= MAX_PROMPTS:
            self.finish('task_prompt_soft_limit', stopped=True)
            return None
        action = self._propose('prompt', '', '选择下一步解题操作')
        header = (
            '你负责一个比赛沙盒任务。依据题目格式要求和真实工具回执逐步求解。\n'
            '工具命令由平台沙盒执行；不生成角色移动/攻击等官方动作。\n'
            '只返回一个JSON对象：{"request_id":"' + action['token'] + '","plan":{'
            '"kind":"run|answer|need_info|give_up","command":"仅run需要",'
            '"answer":"仅answer需要，原样答案字符串","reason":"简短依据",'
            '"evidence_ids":["已提供的证据ID"]}}。不要输出隐藏思维链。\n'
            'run用于读取文档、查询或计算；查看退出码和结果后决定下一步。'
            'answer必须符合题目指定格式，不能默认改成键值对。'
            '遇到答案错误应检查数据和格式，修正后重新提交；不能声称已经通过判题。\n'
        )
        events = json.dumps(self.history[-4:], ensure_ascii=False)
        self.proposal['payload'] = header + '\n可引用证据ID：' + json.dumps(evidence_ids, ensure_ascii=False) + '\n' + context + '\n近期操作摘要：' + events
        if len(self.proposal['payload']) > 24000:
            self.proposal = None
            self.stop_reason = 'assembled_prompt_over_limit'
            return None
        self.proposal['evidence_ids'] = list(evidence_ids)
        return deepcopy(self.proposal)

    def acknowledge(self, token: str, *, round_no: int):
        """Call only after this proposal actually enters the emitted response."""
        action = self.proposal
        if not action or action['token'] != token or self.pending or round_no <= self.last_emit_round:
            return False
        self.proposal = None
        self.last_emit_round = round_no
        self._event('emitted_' + action['kind'], action['payload'], round_no)
        if action['kind'] == 'submit':
            self.answers += 1
            self.stage = 'awaiting_judgement'
            return True
        self.pending = {**action, 'sent_round': round_no}
        if action['kind'] == 'prompt':
            self.prompts += 1
            self.stage = 'waiting_model'
        else:
            self.commands += 1
            key = _digest(action['payload'])
            self.command_attempts[key] = self.command_attempts.get(key, 0) + 1
            self.stage = 'waiting_tool'
        return True

    def receive(self, token: str, kind: str, text: str, *, round_no: int, verified: bool):
        """Consume only the router's matched receipt, never raw llmResp blindly."""
        pending = self.pending
        if not verified or not pending or pending['token'] != token or pending['kind'] != kind:
            return False
        if token in self.seen or round_no <= pending['sent_round']:
            return False
        self.pending = None
        self.seen = (self.seen + [token])[-32:]
        self.stage = 'ready'
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_REPLY:
            self._event('invalid_receipt', '结果为空或超出本地处理上限，未执行任何模型计划', round_no)
            return False
        self._event('received_' + kind, text, round_no)
        if kind == 'cmd':
            return True  # next prompt incorporates the actual result/exit status
        try:
            envelope = _json_unique(text)
            if not isinstance(envelope, dict) or set(envelope) != {'request_id', 'plan'}:
                raise ValueError('invalid response envelope')
            if envelope['request_id'] != token:
                raise ValueError('wrong model correlation')
            plan = envelope['plan']
            if not isinstance(plan, dict) or set(plan) - {'kind', 'command', 'answer', 'reason', 'evidence_ids'}:
                raise ValueError('unsupported model fields')
            plan_kind = plan.get('kind')
            if plan_kind not in ('run', 'answer', 'need_info', 'give_up'):
                raise ValueError('unsupported plan kind')
            ids = plan.get('evidence_ids')
            if not isinstance(ids, list) or not ids or len(ids) > 32 or any(
                    not isinstance(k, str) or k not in pending['evidence_ids'] for k in ids):
                raise ValueError('unverified evidence reference')
            reason = plan.get('reason', '')
            if not isinstance(reason, str) or len(reason) > 400:
                raise ValueError('invalid plan reason')
            if plan_kind in ('need_info', 'give_up'):
                self.finish(reason or plan_kind, stopped=True)
                return True
            field = 'command' if plan_kind == 'run' else 'answer'
            value = plan.get(field)
            other = 'answer' if field == 'command' else 'command'
            if not isinstance(value, str) or not value.strip() or len(value) > 8000 or plan.get(other):
                raise ValueError('invalid or conflicting payload')
            if plan_kind == 'run':
                if self.commands >= MAX_COMMANDS or self.command_attempts.get(_digest(value), 0) >= 2:
                    self.finish('command_soft_limit_or_repeated_no_progress', stopped=True)
                    return False
                self._propose('cmd', value, reason)
            else:
                self._propose('submit', value, reason)
            return True
        except (ValueError, TypeError, RecursionError):
            self._event('invalid_plan', '模型计划格式、关联或证据不合法，需要重新规划', round_no)
            return False

    def feedback(self, errors, *, round_no: int):
        if self.stage != 'awaiting_judgement' or round_no <= max(self.last_emit_round, self.last_feedback_round):
            return
        if not isinstance(errors, list):
            return
        self.last_feedback_round = round_no
        codes = [e.get('errorCode') for e in errors if isinstance(e, dict)]
        if 1 in codes:
            self.finish('official_task_timeout')
        elif 2 in codes or 4 in codes:
            self._event('answer_feedback', json.dumps(errors, ensure_ascii=False)[:2400], round_no)
            self.stage = 'ready'

    def failed(self, token: str, reason: str, *, round_no: int):
        """Router rejection/expiry is an event, not a fabricated model reply."""
        action = self.pending or self.proposal
        if not action or action['token'] != token or self.stage in ('ended', 'stopped'):
            return False
        if self.pending and round_no <= self.pending['sent_round']:
            return False
        self.pending = self.proposal = None
        self._event('operation_failed', str(reason)[:1200], round_no)
        self.stage = 'ready'
        return True

    def finish(self, reason, *, stopped=False):
        self.proposal = self.pending = None
        self.stage = 'stopped' if stopped else 'ended'
        self.stop_reason = str(reason)[:400]

    def dump(self):
        return {'schema': SCHEMA, **deepcopy(self.__dict__)}

    @classmethod
    def load(cls, raw: Any):
        result = cls()
        try:
            if not isinstance(raw, dict) or raw.get('schema') != SCHEMA or set(raw) != set(result.dump()):
                raise ValueError('unknown schema or fields')
            if len(json.dumps(raw, ensure_ascii=False, allow_nan=False)) > 120000:
                raise ValueError('oversized state')
            for key in ('sequence', 'prompts', 'commands', 'answers', 'last_feedback_round', 'last_emit_round'):
                if type(raw[key]) is not int or not 0 <= raw[key] <= 100000:
                    raise ValueError('invalid counter')
            for key, cap in (('generation', 160), ('source', 160), ('stop_reason', 400)):
                if not isinstance(raw[key], str) or len(raw[key]) > cap:
                    raise ValueError('invalid identity')
            if raw['stage'] not in ('idle', 'ready', 'waiting_model', 'waiting_tool', 'awaiting_judgement', 'stopped', 'ended'):
                raise ValueError('invalid stage')
            if not isinstance(raw['history'], list) or len(raw['history']) > MAX_HISTORY or not isinstance(raw['seen'], list) or len(raw['seen']) > 32:
                raise ValueError('invalid bounded history')
            if not isinstance(raw['command_attempts'], dict) or len(raw['command_attempts']) > MAX_COMMANDS:
                raise ValueError('invalid command history')
            if any(not isinstance(k, str) or len(k) != 24 or type(v) is not int or not 1 <= v <= 2
                   for k, v in raw['command_attempts'].items()):
                raise ValueError('invalid command attempts')
            if any(not isinstance(k, str) or len(k) != 24 for k in raw['seen']):
                raise ValueError('invalid receipt identities')
            for event in raw['history']:
                if (not isinstance(event, dict) or set(event) != {'kind', 'text', 'round', 'truncated', 'sha256'}
                        or not isinstance(event['kind'], str) or len(event['kind']) > 80
                        or not isinstance(event['text'], str) or len(event['text']) > 1200
                        or type(event['round']) is not int or not 0 <= event['round'] <= 100000
                        or type(event['truncated']) is not bool or not isinstance(event['sha256'], str)
                        or len(event['sha256']) != 24):
                    raise ValueError('invalid event')
            for key in ('proposal', 'pending'):
                value = raw[key]
                if value is None:
                    continue
                if (not isinstance(value, dict) or value.get('kind') not in ('prompt', 'cmd', 'submit')
                        or not isinstance(value.get('payload'), str) or len(value['payload']) > 24000
                        or not isinstance(value.get('token'), str) or len(value['token']) != 24
                        or not isinstance(value.get('purpose'), str) or len(value['purpose']) > 400
                        or value.get('generation') != raw['generation']):
                    raise ValueError('invalid pending proposal')
                fields = {'kind', 'payload', 'token', 'purpose', 'generation'}
                if value['kind'] == 'prompt':
                    fields.add('evidence_ids')
                    ids = value.get('evidence_ids')
                    if not isinstance(ids, list) or len(ids) > 32 or any(not isinstance(v, str) or not v or len(v) > 160 for v in ids):
                        raise ValueError('invalid evidence index')
                if key == 'pending':
                    fields.add('sent_round')
                    if type(value.get('sent_round')) is not int or value['sent_round'] != raw['last_emit_round'] or value['kind'] == 'submit':
                        raise ValueError('invalid pending round')
                if set(value) != fields:
                    raise ValueError('unknown proposal fields')
                if value['token'] != _digest(f"{raw['generation']}\0{raw['source']}\0{raw['sequence']}"):
                    raise ValueError('invalid proposal identity')
            if raw['pending'] and raw['proposal']:
                raise ValueError('conflicting work')
            pending_stage = {'waiting_model': 'prompt', 'waiting_tool': 'cmd'}.get(raw['stage'])
            if bool(pending_stage) != bool(raw['pending']) or (pending_stage and raw['pending']['kind'] != pending_stage):
                raise ValueError('inconsistent waiting state')
            if raw['stage'] in ('stopped', 'ended', 'awaiting_judgement') and raw['proposal']:
                raise ValueError('proposal after ending')
            result.__dict__.update(deepcopy({k: v for k, v in raw.items() if k != 'schema'}))
        except (ValueError, TypeError, RecursionError):
            result.finish('state_restore_rejected', stopped=True)
        return result
