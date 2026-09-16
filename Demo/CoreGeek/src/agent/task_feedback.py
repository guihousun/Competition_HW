"""Bounded diagnostics from observed receipts; never a grading oracle."""
import hashlib
import json
import re


def answer_digest(answer):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    try:
        value = json.loads(answer, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        text = 'json:' + json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        text = 'text:' + answer.strip()
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def rejection(answer, errors, round_no):
    descriptions = [e.get('description', '') for e in errors
                    if isinstance(e, dict) and e.get('errorCode') == 2]
    error = '; '.join(x for x in descriptions if isinstance(x, str))[:350]
    fields = {}
    try:
        obj = json.loads(answer)
        # Only expose short top-level values explicitly named by the judge.
        for key in re.findall(r'\$/([^\s/:：]+)', error)[:4]:
            if isinstance(obj, dict) and key in obj:
                fields[key[:80]] = str(obj[key])[:100]
    except (ValueError, TypeError, RecursionError):
        pass
    return {'sha256': answer_digest(answer), 'round': round_no,
            'error': error, 'fields': fields}


def http_quality(command, text):
    """Only inspect our own generated HTTP command's matched result.

    Completeness refers to this page, NOT all previously collected pages.
    No speculative pagination, credential logging or city-specific answers.
    """
    from .task_tools import http_request
    if http_request(command) is None:
        return None
    if '[TRUNCATED]' in text:
        return {'state': 'truncated', 'scope': 'this_response_only',
                'next': '平台截断了回执，不能据此确认集合完整；缩小输出并补齐数据'}
    try:
        raw = text.removeprefix('[exitCode:0]\n')
        obj = json.loads(raw)
        if not isinstance(obj, dict) or obj.get('tool') != 'task-http/1':
            return None
        result = {'http_status': obj.get('status'), 'scope': 'this_response_only'}
        if obj.get('truncated'):
            return {**result, 'state': 'truncated', 'next': '缩小输出并补齐原始数据后计算'}
        if obj.get('status') != 200:
            return {**result, 'state': 'request_failed', 'next': '不能把查询失败当作合法空数据'}
        data = obj.get('data')
        if isinstance(data, dict) and (data.get('status') == 'error' or
                (type(data.get('code')) is int and data['code'] >= 400)):
            return {**result, 'state': 'application_error', 'next': 'HTTP成功但业务返回错误，先修复请求'}
        container = data
        if isinstance(data, dict) and isinstance(data.get('data'), dict):
            container = data['data']
        if isinstance(container, dict):
            rows = container.get('records')
            page = container.get('pagination')
            if isinstance(rows, list) and isinstance(page, dict):
                total = page.get('total_count')
                offset = page.get('offset')
                if type(total) is int and total >= 0:
                    result.update(returned=len(rows), total=total)
                    if type(offset) is int and offset >= 0:
                        result['offset'] = offset
                    if len(rows) < total:
                        return {**result, 'state': 'partial_page',
                                'next': '仅此页不足以计算全集；按实际文档继续分页并去重，核对已收集总数，不猜page/offset语义'}
                    if len(rows) == total and offset == 0:
                        return {**result, 'state': 'count_consistent',
                                'next': '此页条数与总数一致；仍需核对筛选条件、去重和字段定义'}
        return {**result, 'state': 'unknown', 'next': '从真实响应结构核对集合和分页语义'}
    except (ValueError, TypeError, RecursionError):
        return None
