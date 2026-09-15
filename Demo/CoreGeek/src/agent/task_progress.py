"""Bounded observational task milestones. Never grades answers or drives actions."""
import hashlib
import json
import re
import shlex


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def stats(team):
    return {key: team.get(key) if type(team.get(key)) is int else None
            for key in ('gold', 'totalScore')}


def delta(before, after):
    return {key: after[key]-before[key] if before.get(key) is not None and after.get(key) is not None else None
            for key in ('gold', 'totalScore')}


def is_check(command):
    if not isinstance(command, str) or len(command) > 2000:
        return False
    if command.startswith('# task-check/1\n'):
        return True
    # Conservative: no heredoc, pipelines, subshells or shell-source inspection.
    if any(mark in command for mark in ('<<', '$(', '`', '|')):
        return False
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=';&')
        lex.whitespace_split = True
        lex.whitespace = ' \t\r'
        chunks = [[]]
        for word in lex:
            if word in ('&&', ';', '\n'):
                chunks.append([])
            else:
                chunks[-1].append(word)
    except ValueError:
        return False
    for words in chunks:
        if not words:
            continue
        executable = words[0]
        if executable.rsplit('/', 1)[-1] in ('bash', 'sh', 'python', 'python3'):
            executable = words[1] if len(words) > 1 else ''
        if executable.rsplit('/', 1)[-1] in ('check', 'check.py', 'check.sh'):
            return True
    return False


def check_result(raw, structured):
    result = {'status': 'unrecognized', 'exit_code': None, 'all_checks_passed': False,
              'token_seen': False, 'token_sha256': None}
    if not isinstance(raw, str) or not raw:
        result['status'] = 'missing'
        return result
    if len(raw) > 70000 or '[TRUNCATED]' in raw:
        result['status'] = 'truncated'
        return result
    if raw.startswith('[TIMEOUT]'):
        result['status'] = 'timeout'
        return result
    if raw.startswith('[JUDGER_ERROR]'):
        result['status'] = 'judger_error'
        return result
    match = re.match(r'^\[exitCode:(-?\d+)\]\r?\n', raw)
    if not match:
        return result
    result['exit_code'] = int(match[1])
    output = raw[match.end():]
    if result['exit_code'] != 0:
        result['status'] = 'failed'
        return result
    if structured:
        try:
            wrapper = json.loads(output)
            if not isinstance(wrapper, dict) or wrapper.get('tool') != 'task-check/1':
                return result
            if wrapper.get('truncated') is not False:
                result['status'] = 'truncated'
                return result
            code = wrapper.get('exit_code')
            if type(code) is not int or not isinstance(wrapper.get('stdout'), str):
                return result
            result['exit_code'] = code
            output = wrapper['stdout']
            if code != 0:
                result['status'] = 'failed'
                return result
        except (ValueError, RecursionError):
            return result
    tokens = set(re.findall(r'^\s*TOKEN:\s*([A-Za-z0-9_-]{6,128})\s*$', output, re.M))
    result['token_seen'] = bool(tokens)
    if len(tokens) == 1:
        result['token_sha256'] = digest(next(iter(tokens)))
    passed = re.search(r'^\s*\[\s*OK\s*\]\s*全部通过\s*\((\d+)/(\d+)\)\s*$', output, re.M)
    result['all_checks_passed'] = bool(passed and int(passed[1]) > 0 and passed[1] == passed[2]
                                      and not re.search(r'\bFAIL(?:ED)?\b', output, re.I))
    result['status'] = 'passed' if result['all_checks_passed'] else 'exit_ok_without_pass_marker'
    return result


def start(round_no, team):
    return {'start_round': round_no, 'start_stats': stats(team), 'gap': False,
            'prompts': 0, 'commands': 0, 'checks': 0, 'checks_passed': 0, 'submissions': 0, 'answer_errors': 0,
            'last_check': None, 'last_submission': None, 'last_feedback': None,
            'pending_check': None, 'pending_submission': None}


def observe(progress, request, same_context, gap):
    """Process only the next observed round; no delayed guess across gaps/tasks."""
    rows = []
    round_no = request['roundNo']
    progress['gap'] |= gap
    for name in ('check', 'submission'):
        pending = progress.pop('pending_' + name, None)
        if not pending:
            continue
        linked = same_context and not gap and round_no == pending['round'] + 1
        association = 'adjacent_observation' if linked else 'unattributed_gap_or_task_change'
        if name == 'check':
            item = check_result(request.get('lastCmdResult'), pending['structured']) if linked else {
                'status': 'unattributed', 'all_checks_passed': False, 'token_seen': False,
                'token_sha256': None, 'exit_code': None}
            item.update(round=round_no, command_round=pending['round'], association=association)
            progress['last_check'] = item
            progress['checks_passed'] += int(item['all_checks_passed'])
            rows.append(('task_check_result', item))
        else:
            receipts = request.get('lastRoundRoleActionResults')
            legal = receipts.get(pending['role'], receipts.get(int(pending['role'])) if pending['role'].isdigit() else None) if isinstance(receipts, dict) else None
            errors = request.get('errors')
            errors = errors if isinstance(errors, list) else []
            codes = sorted({e['errorCode'] for e in errors[:32] if isinstance(e, dict) and type(e.get('errorCode')) is int})
            answer_error = any(isinstance(e, dict) and e.get('errorCode') == 2 and
                               re.search(r'答案|键值比对', str(e.get('description', ''))[:1000]) for e in errors[:32])
            descriptions = ' '.join(str(e.get('description', ''))[:1000] for e in errors[:32] if isinstance(e, dict) and e.get('errorCode') == 2)
            error_kind = ('invalid_json' if '不是合法 JSON' in descriptions else
                          'value_mismatch' if '键值比对' in descriptions else 'answer_error') if linked and answer_error else None
            item = {'submission_round': pending['round'], 'observed_round': round_no,
                    'role': pending['role'], 'association': association,
                    'action_legal': legal if linked and type(legal) is bool else None,
                    'answer_error_observed': bool(linked and answer_error),
                    'answer_error_kind': error_kind,
                    'observed_error_codes': codes, 'official_success_confirmed': False,
                    'stats_delta': delta(pending['stats'], stats(request.get('teamOur') or {})),
                    'reward_attribution': 'unknown_other_actions_may_contribute'}
            progress['last_feedback'] = item
            progress['answer_errors'] += int(bool(linked and answer_error))
            rows.append(('task_submission_feedback', item))
    return rows


def issued(progress, response, round_no, team):
    progress['prompts'] += int(bool(response.get('prompt')))
    command = response.get('executeCmd')
    progress['commands'] += int(bool(command))
    if is_check(command):
        progress['checks'] += 1
        progress['pending_check'] = {'round': round_no, 'structured': command.startswith('# task-check/1\n')}
    actions = response.get('roleCommandMap')
    for role, action in list(actions.items())[:32] if isinstance(actions, dict) else []:
        if not isinstance(action, dict) or action.get('action') != 'submitAnswer':
            continue
        answer = action.get('taskAnswer')
        if not isinstance(answer, str):
            continue
        kind, token_hash = 'invalid_json', None
        try:
            value = json.loads(answer) if len(answer) <= 70000 else None
            kind = 'json_' + type(value).__name__
            if isinstance(value, dict) and set(value) == {'token'} and isinstance(value['token'], str):
                kind, token_hash = 'token_object', digest(value['token'])
        except (ValueError, RecursionError):
            pass
        check = progress['last_check'] or {}
        item = {'round': round_no, 'role': str(role), 'answer_sha256': digest(answer), 'format': kind,
                'matches_last_check_token': bool(token_hash and check.get('all_checks_passed') and token_hash == check.get('token_sha256'))}
        progress['submissions'] += 1
        progress['last_submission'] = item
        progress['last_feedback'] = None
        progress['pending_submission'] = {**item, 'stats': stats(team)}


def summary(progress, round_no, team, replaced):
    feedback = progress['last_feedback'] or {}
    status = 'submitted_unconfirmed' if progress['submissions'] else 'check_passed_not_submitted' if progress['checks_passed'] else 'unfinished'
    if feedback.get('answer_error_observed'):
        status = 'answer_error_observed'
    elif feedback.get('action_legal') is False:
        status = 'submission_action_illegal'
    labels = {'submitted_unconfirmed': '已提交，官方判题结果未确认',
              'check_passed_not_submitted': 'check通过，未观察到答案提交',
              'unfinished': '尚未观察到check通过或答案提交',
              'answer_error_observed': '提交后观察到答案错误',
              'submission_action_illegal': '提交动作回执为不合法'}
    return {'status': status, 'meaning': labels[status], 'official_success_confirmed': False,
            'start_round': progress['start_round'], 'end_round': round_no,
            'end_reason': 'task_text_replaced' if replaced else 'task_text_ended',
            'observation_gap': progress['gap'],
            'counts': {k: progress[k] for k in ('prompts', 'commands', 'checks', 'checks_passed', 'submissions', 'answer_errors')},
            'last_check': progress['last_check'], 'last_submission': progress['last_submission'],
            'submission_feedback': progress['last_feedback'],
            'episode_stats_delta': delta(progress['start_stats'], stats(team)),
            'reward_attribution': 'unknown_other_actions_may_contribute'}
