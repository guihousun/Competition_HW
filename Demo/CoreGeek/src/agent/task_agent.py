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
import re
from typing import Any
from .task_context import PROMPT_LIMIT, COMMAND_LIMIT
from . import task_tools
from .model_json import unwrap_json
from .task_answer_contract import bad_heredoc_chain
from . import task_feedback

SCHEMA = 'competition-task-agent/3'
MAX_PROMPTS = 8  # engineering limits for one task, not official LLM allowances
MAX_COMMANDS = 8
MAX_CONTEXT = 12000
MAX_REPLY = 16000
MAX_HISTORY = 12
ANSWER_LIMIT = 8000
PLAN_KINDS = ('run', 'http', 'check', 'answer', 'inspect', 'need_info', 'give_up')


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
        self.inspections = 0
        self.stage = 'idle'
        self.proposal = None
        self.pending = None
        self.history = []
        self.seen = []
        self.command_attempts = {}
        self.last_feedback_round = 0
        self.last_emit_round = 0
        self.stop_reason = ''
        self.last_submission = None
        self.rejected_answers = []

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

    def decide(self, context: str, evidence_ids: list[str], *, active: bool, round_no: int,
               bootstrap_command=None):
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
        if (self.commands == 0 and self.prompts == 0 and bootstrap_command
                and isinstance(bootstrap_command, str) and len(bootstrap_command) <= COMMAND_LIMIT):
            return self._propose('cmd', bootstrap_command, '一次定位任务文件并读取题文与相关说明')
        action = self._propose('prompt', '', '选择下一步解题操作')
        header = (
            '你负责一个比赛沙盒任务。依据题目格式要求和真实工具回执逐步求解。\n'
            '工具命令由平台沙盒执行；不生成角色移动/攻击等官方动作。\n'
            '只返回一个JSON对象：{"request_id":"' + action['token'] + '","plan":{'
            '"kind":"' + '|'.join(PLAN_KINDS) + '","command":"仅run需要",'
            '"answer":"仅answer需要，原样答案字符串","reason":"简短依据",'
            '"evidence_ids":["已提供的证据ID"]}}。不要输出隐藏思维链。\n'
            f'run命令上限{COMMAND_LIMIT}字符（工程限制），不要输出超长脚本；可以合并相关的查找、读取、查询和校验。'
            'run用于读取文档、查询、计算或按任务要求修改工作区并运行check；查看退出码和结果后决定下一步。'
            'answer必须符合题目指定格式，不能默认改成键值对。'
            '先核对每个字段的中文说明、单位、筛选条件与返回对象，不能只按英文键名猜语义。'
            '若题目要“年代最早的记录名称”，年代用于排序，答案填该记录的name；城市/数据改变后重新查询计算。'
            '修复部署题按当前spec逐项修改并运行check，TOKEN只取当前check输出，不复用历史值。'
            '遇到答案错误应检查数据和格式，修正后重新提交；不能声称已经通过判题。\n'
            '明确判错的答案不能仅换空白或键顺序重交；不要在已错的值之间循环。错误字段不等于其他字段都通过。'
            '改答案必须有题文/数据依据；无新依据时读字段定义或做可复现计算，不凭错误提示猜标准答案。\n'
            '文件名不代表当前目录存在该文件。优先使用已找到的绝对路径；只允许在任务工作区修改文件。'
            'API题先从实际文档获取接口、鉴权、字段和分页规则；工程题先读spec和check，再按要求修改并验证。'
            '运行check返回的有效答案应立即answer，不要再查找或为确认而重复运行。不要读取验证服务内存或伪造校验。'
            '一次文件缺失不等于任务无解；先有界定位。同一文件同一内容已在原文中则不要重复读取。\n'
            'inspect是读取已收到原文的本地记忆操作，不运行Shell。使用字段source_id（索引中的ID）、'
            'offset（从0起）、length（1..2000），可选query为要查找的原文片段。'
            '长资料先定位关键字，再读取附近内容；摘要截断不等于原文缺失。\n'
            '可在外层JSON附带summary字符串（最多600字符），仅简记待办和下一步；不额外请求摘要。'
            '摘要是模型建议，不能覆盖真实工具结果，不写入一次性token或凭据。\n'
            'HTTP查询优先用plan.kind=http和tool_args={url,headers,params}，所有参数值为字符串；'
            '工具会URL编码、描述响应JSON结构，并仅依据明确错误提示有限重试鉴权/参数。'
            '不要假定响应是数组，200空数据不代表任务完成；以当前服务回执修正文档假设。'
            '工程校验优先用kind=check和tool_args={path:"绝对check路径"}，只在内存处理CRLF，'
            '不修改check原件，保持工作目录和退出码。http/check不同时提供command或answer。\n'
        )
        from . import strategy_config
        history_count = strategy_config.get()['llm']['include_recent_history']
        events = json.dumps([{**item, 'text': item['text'][:200],
                              'truncated': item['truncated'] or len(item['text']) > 200}
                             for item in self.history[-history_count:]], ensure_ascii=False)
        body = '\n可引用证据ID：' + json.dumps(evidence_ids, ensure_ascii=False) + '\n' + context + '\n近期操作摘要：' + events
        label = '\n本任务已明确判错（同答案禁止重交；未展示旧项仍自动拦截）：'
        rows = [{'round': row['round'], 'error': row['error'][:100],
                 'fields': {k: v[:50] for k, v in list(row['fields'].items())[:2]}}
                for row in self.rejected_answers]
        budget = min(1500, PROMPT_LIMIT - len(header + body + label))
        while True:
            rejected = json.dumps({'count': len(self.rejected_answers), 'recent': rows}, ensure_ascii=False)
            if len(rejected) <= budget or not rows:
                break
            rows.pop(0)
        self.proposal['payload'] = header + label + rejected + body
        if len(self.proposal['payload']) > PROMPT_LIMIT:
            self.proposal = None
            self.stop_reason = 'assembled_prompt_over_limit'
            return None
        self.proposal['evidence_ids'] = list(evidence_ids)
        return deepcopy(self.proposal)

    def acknowledge(self, token: str, *, round_no: int):
        """Call only after this proposal actually enters the emitted response."""
        action = self.proposal
        if not action or action['kind'] == 'inspect' or action['token'] != token or self.pending or round_no <= self.last_emit_round:
            return False
        self.proposal = None
        self.last_emit_round = round_no
        self._event('emitted_' + action['kind'], action['payload'], round_no)
        if action['kind'] == 'submit':
            self.answers += 1
            self.last_submission = {'answer': action['payload'], 'round': round_no}
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
            quality = task_feedback.http_quality(pending['payload'], text)
            if quality:
                self._event('http_data_quality', json.dumps(quality, ensure_ascii=False), round_no)
            if (re.match(r'\[exitCode:[1-9][0-9]*\]', text)
                    and re.search(r'syntax error|SyntaxError|unexpected EOF|bad interpreter', text, re.I)):
                self._event('shell_syntax_failed', _digest(pending['payload']), round_no)
            recovery = task_tools.failed_check_recovery(pending['payload'], text)
            if (recovery and self.commands < MAX_COMMANDS
                    and self.command_attempts.get(_digest(recovery), 0) < 2):
                self._event('check_only_recovery', '已确认末尾check的CRLF126；仅重试check，不重放修改前缀', round_no)
                self._propose('cmd', recovery, '已确认CRLF失败后仅重新执行兼容check')
            return True  # next prompt incorporates the actual result/exit status
        try:
            envelope = _json_unique(unwrap_json(text))
            if (not isinstance(envelope, dict) or not {'request_id', 'plan'} <= set(envelope)
                    or set(envelope) - {'request_id', 'plan', 'summary'}):
                raise ValueError('invalid response envelope')
            if envelope['request_id'] != token:
                raise ValueError('wrong model correlation')
            plan = envelope['plan']
            if not isinstance(plan, dict) or set(plan) - {'kind', 'command', 'answer', 'reason', 'evidence_ids', 'source_id', 'offset', 'length', 'query', 'tool_args'}:
                raise ValueError('unsupported model fields')
            plan_kind = plan.get('kind')
            if plan_kind not in PLAN_KINDS:
                raise ValueError('unsupported plan kind')
            ids = plan.get('evidence_ids')
            if not isinstance(ids, list) or not ids or len(ids) > 32 or any(
                    not isinstance(k, str) or k not in pending['evidence_ids'] for k in ids):
                raise ValueError('unverified evidence reference')
            reason = plan.get('reason', '')
            if not isinstance(reason, str) or len(reason) > 400:
                raise ValueError('invalid plan reason')
            # Advisory, bounded, never a source of verified facts or an answer.
            summary = envelope.get('summary')
            if isinstance(summary, str) and 0 < len(summary) <= 600:
                self._event('model_summary_unverified', summary, round_no)
            if plan_kind in ('http', 'check'):
                if set(plan) - {'kind', 'tool_args', 'reason', 'evidence_ids'}:
                    raise ValueError('conflicting structured tool fields')
                try:
                    command = task_tools.command(plan_kind, plan.get('tool_args'))
                except ValueError as error:
                    self._event('invalid_tool_arguments', str(error), round_no)
                    return False
                if self.commands >= MAX_COMMANDS or self.command_attempts.get(_digest(command), 0) >= 2:
                    self.finish('command_soft_limit_or_repeated_no_progress', stopped=True)
                    return False
                self._propose('cmd', command, reason or '结构化' + plan_kind + '工具')
                return True
            if 'tool_args' in plan:
                raise ValueError('tool_args on a non-tool plan')
            if plan_kind == 'inspect':
                source_id = plan.get('source_id')
                offset, length, query = plan.get('offset', 0), plan.get('length', 2000), plan.get('query', '')
                if (source_id not in pending['evidence_ids'] or not isinstance(source_id, str)
                        or type(offset) is not int or offset < 0 or type(length) is not int
                        or not 1 <= length <= 2000 or not isinstance(query, str) or len(query) > 200
                        or plan.get('command') or plan.get('answer')):
                    raise ValueError('invalid memory operation')
                if self.inspections >= 8:
                    self.finish('memory_operation_limit', stopped=True)
                    return False
                self._propose('inspect', json.dumps({'source_id': source_id, 'offset': offset,
                                                   'length': length, 'query': query}, ensure_ascii=False), reason)
                return True
            if any(key in plan for key in ('source_id', 'offset', 'length', 'query')):
                raise ValueError('memory fields on a non-memory operation')
            if plan_kind in ('need_info', 'give_up'):
                self.finish(reason or plan_kind, stopped=True)
                return True
            field = 'command' if plan_kind == 'run' else 'answer'
            value = plan.get(field)
            other = 'answer' if field == 'command' else 'command'
            cap = COMMAND_LIMIT if plan_kind == 'run' else ANSWER_LIMIT
            if not isinstance(value, str) or not value.strip() or plan.get(other):
                raise ValueError('invalid or conflicting payload')
            if len(value) > cap:
                self._event('payload_over_limit', f'{field}实际{len(value)}字符，上限{cap}；请缩短或拆分，不会截断执行。', round_no)
                return False
            if plan_kind == 'run':
                from .task_tools import standalone_check
                compatible=standalone_check(value)
                if compatible:
                    value=compatible
                    self._event('check_tool_selected','独立check调用使用CRLF兼容工具；不修改check文件',round_no)
                last_command = next((e for e in reversed(self.history)
                    if e['kind'] in ('shell_syntax_failed','emitted_cmd')), None)
                repeated_syntax = (last_command and last_command['kind']=='shell_syntax_failed'
                                   and last_command['text']==_digest(value))
                if bad_heredoc_chain(value) or repeated_syntax:
                    self._event('command_syntax_rejected',
                        '命令存在heredoc终止后换行&&，或原样重复了已确认的语法失败。请改用单段Python写文件或printf；修复后调用check。', round_no)
                    return False
                if self.commands >= MAX_COMMANDS or self.command_attempts.get(_digest(value), 0) >= 2:
                    self.finish('command_soft_limit_or_repeated_no_progress', stopped=True)
                    return False
                self._propose('cmd', value, reason)
            else:
                if self.answer_rejected(value):
                    self._event('repeated_answer_rejected', '相邻官方回执已拒绝同一答案；请重新检查题文和数据，禁止轮流猜测旧值', round_no)
                    return False
                self._propose('submit', value, reason)
            return True
        except (ValueError, TypeError, RecursionError) as error:
            # Only our static validation messages may enter compact logs;
            # never echo arbitrary model text, credentials or parser payloads.
            allowed = {'invalid response envelope', 'wrong model correlation',
                       'unsupported model fields', 'unsupported plan kind',
                       'unverified evidence reference', 'invalid plan reason',
                       'conflicting structured tool fields', 'tool_args on a non-tool plan',
                       'invalid memory operation', 'memory fields on a non-memory operation',
                       'invalid or conflicting payload', 'duplicate JSON key', 'nonfinite JSON constant'}
            reason = str(error) if str(error) in allowed else type(error).__name__
            self._event('invalid_plan', reason, round_no)
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
            if (2 in codes and self.last_submission
                    and round_no == self.last_submission['round'] + 1):
                row = task_feedback.rejection(self.last_submission['answer'], errors, round_no)
                if not any(x['sha256'] == row['sha256'] for x in self.rejected_answers):
                    self.rejected_answers = (self.rejected_answers + [row])[-MAX_PROMPTS:]
            self._event('answer_feedback', json.dumps(errors, ensure_ascii=False)[:2400], round_no)
            self.stage = 'ready'

    def answer_rejected(self, value):
        return any(row['sha256'] == task_feedback.answer_digest(value) for row in self.rejected_answers)

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

    def inspected(self, token, result, *, round_no):
        """A bounded local read completed; it consumes no official channel."""
        if (not self.proposal or self.proposal['kind'] != 'inspect'
                or self.proposal['token'] != token or self.pending or self.inspections >= 8):
            return False
        self.inspections += 1
        self.proposal = None
        self.stage = 'ready'
        self._event('memory_result', json.dumps(result, ensure_ascii=False), round_no)
        return True

    def dump(self):
        return {'schema': SCHEMA, **deepcopy(self.__dict__)}

    @classmethod
    def load(cls, raw: Any):
        result = cls()
        try:
            if isinstance(raw, dict) and raw.get('schema') == 'competition-task-agent/2':
                raw = deepcopy(raw)
                raw['schema'] = SCHEMA
                # No reconstructed submissions or guessed historical judgements.
                if 'last_submission' in raw or 'rejected_answers' in raw:
                    raise ValueError('invalid legacy state')
                raw.update(last_submission=None, rejected_answers=[])
            if not isinstance(raw, dict) or raw.get('schema') != SCHEMA or set(raw) != set(result.dump()):
                raise ValueError('unknown schema or fields')
            if len(json.dumps(raw, ensure_ascii=False, allow_nan=False)) > 120000:
                raise ValueError('oversized state')
            submission = raw['last_submission']
            if submission is not None and (not isinstance(submission, dict)
                    or set(submission) != {'answer', 'round'}
                    or not isinstance(submission['answer'], str) or not 0 < len(submission['answer']) <= ANSWER_LIMIT
                    or type(submission['round']) is not int or not 0 < submission['round'] <= raw['last_emit_round']):
                raise ValueError('invalid last submission')
            if not isinstance(raw['rejected_answers'], list) or len(raw['rejected_answers']) > MAX_PROMPTS:
                raise ValueError('invalid rejection memory')
            for row in raw['rejected_answers']:
                if (not isinstance(row, dict) or set(row) != {'sha256', 'round', 'error', 'fields'}
                        or not isinstance(row['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', row['sha256'])
                        or type(row['round']) is not int or not 0 <= row['round'] <= 100000
                        or not isinstance(row['error'], str) or len(row['error']) > 350
                        or not isinstance(row['fields'], dict) or len(row['fields']) > 4
                        or any(not isinstance(k, str) or len(k) > 80 or not isinstance(v, str) or len(v) > 100
                               for k, v in row['fields'].items())):
                    raise ValueError('invalid rejection entry')
            for key in ('sequence', 'prompts', 'commands', 'answers', 'inspections', 'last_feedback_round', 'last_emit_round'):
                if type(raw[key]) is not int or not 0 <= raw[key] <= 100000:
                    raise ValueError('invalid counter')
            if raw['inspections'] > 8:
                raise ValueError('invalid inspection count')
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
                if (not isinstance(value, dict) or value.get('kind') not in ('prompt', 'cmd', 'submit', 'inspect')
                        or not isinstance(value.get('payload'), str) or len(value['payload']) > 24000
                        or not isinstance(value.get('token'), str) or len(value['token']) != 24
                        or not isinstance(value.get('purpose'), str) or len(value['purpose']) > 400
                        or value.get('generation') != raw['generation']):
                    raise ValueError('invalid pending proposal')
                if value['kind'] in ('cmd', 'submit') and len(value['payload']) > (
                        COMMAND_LIMIT if value['kind'] == 'cmd' else ANSWER_LIMIT):
                    raise ValueError('stored operation exceeds shared payload limit')
                fields = {'kind', 'payload', 'token', 'purpose', 'generation'}
                if value['kind'] == 'prompt':
                    fields.add('evidence_ids')
                    ids = value.get('evidence_ids')
                    if not isinstance(ids, list) or len(ids) > 32 or any(not isinstance(v, str) or not v or len(v) > 160 for v in ids):
                        raise ValueError('invalid evidence index')
                if value['kind'] == 'inspect':
                    operation = _json_unique(value['payload'])
                    if (not isinstance(operation, dict) or set(operation) != {'source_id', 'offset', 'length', 'query'}
                            or not isinstance(operation['source_id'], str) or not 1 <= len(operation['source_id']) <= 160
                            or type(operation['offset']) is not int or operation['offset'] < 0
                            or type(operation['length']) is not int or not 1 <= operation['length'] <= 2000
                            or not isinstance(operation['query'], str) or len(operation['query']) > 200):
                        raise ValueError('invalid stored memory operation')
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
