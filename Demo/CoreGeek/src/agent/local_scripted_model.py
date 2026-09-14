"""Explicit local fixture model, not an LLM or an official solver.

Consumes ONLY the prompt that would have gone to the model. No seed, answer,
private simulator state, filesystem or network is available to this adapter.
It supports the demo's public document/query and news formats; other tasks stop.
"""
import json
import re

MODEL = '本地脚本模型（流程测试）'


def _line_json(prompt, prefix):
    for line in prompt.splitlines():
        if line.startswith(prefix):
            try:
                return json.loads(line[len(prefix):])
            except ValueError:
                return None
    return None


def complete(prompt):
    if not isinstance(prompt, str) or len(prompt) > 16000:
        raise ValueError('scripted_prompt_out_of_bounds')
    token = re.search(r'"request_id"\s*:\s*"([a-zA-Z0-9_-]+)"', prompt)
    sources = _line_json(prompt, '公开来源：')
    if token and isinstance(sources, list):
        answer = _world(prompt, token[1], sources)
    elif token:
        answer = _task(prompt, token[1])
    elif '十七加二十五' in prompt:
        answer = 'result=' + str(17 + 25)
    else:
        answer = '脚本模型不支持这道题；请切换真实模型。'
    return {'answer': answer, 'usage': {}, 'reported_model': MODEL}


def _world(prompt, token, rows):
    def proof(row):
        return [{'sourceId': row['id'], 'quote': row['text']}]
    if '"events":' in prompt:
        first = next((r for r in rows if '明天开始全面停工两天' in r.get('text', '') and not r.get('truncated')), None)
        events = []
        if first:
            day = (first['firstRound'] - 1) // 130 + 1
            events = [{'resource': 'iron', 'availability': status, 'startDay': start,
                       'endDay': end, 'priceDirection': direction, 'evidence': proof(first)}
                      for start, end, status, direction in ((day, day, 'available', 'unchanged'),
                          (day + 1, day + 2, 'unavailable', 'up'), (day + 3, 10, 'available', 'unchanged'))]
        return json.dumps({'request_id': token, 'events': events}, ensure_ascii=False)
    h = {'site': None, 'items': None, 'opensAt': None, 'closesAt': None, 'uncertain': True,
         'evidence': {'site': [], 'items': [], 'window': []}}
    for row in rows:
        if row.get('truncated'):
            continue
        text = row.get('text', '')
        site = re.search(r'横坐标(\d+)、纵坐标(\d+)', text)
        items = re.search(r'需要献祭([^，]+)，每种恰好一份', text)
        window = re.search(r'第(\d+)天白昼.*前30个回合', text)
        if site:
            h['site'] = {'x': int(site[1]), 'y': int(site[2])}; h['evidence']['site'] = proof(row)
        if items:
            h['items'] = items[1].split('、'); h['evidence']['items'] = proof(row)
        if window:
            h['opensAt'] = (int(window[1]) - 1) * 130 + 1
            h['closesAt'] = h['opensAt'] + 29; h['evidence']['window'] = proof(row)
    h['uncertain'] = any(h[k] is None for k in ('site', 'items', 'opensAt', 'closesAt'))
    return json.dumps({'request_id': token, 'hypothesis': h}, ensure_ascii=False)


def _task(prompt, token):
    ids = _line_json(prompt, '可引用证据ID：') or []
    index = _line_json(prompt, '本题原文记忆索引（inspect只能访问这些已收到的来源）：') or []
    evidence = []
    for line in prompt.splitlines():
        if line.startswith('关联工具摘要') and '：' in line:
            try:
                evidence.append(json.loads(line.split('：', 1)[1]))
            except ValueError:
                pass
    focus = _line_json(prompt, '最近本地原文检索结果：')
    question = prompt.split('任务原文 (', 1)[-1].split('答案格式约束:', 1)[0]
    plan = {'kind': 'need_info', 'reason': '脚本模型不支持该题；可切换真实模型', 'evidence_ids': ids}
    query = next((e for e in reversed(evidence) if e.get('exit_code') == 0
                  and e.get('command', '').startswith(('python3 ', 'python '))), None)
    if query:
        try:
            data = json.loads(query['text'])
            value = data[0] if isinstance(data, list) and data else (data.get('total') if isinstance(data, dict) else None)
            if value is not None:
                plan.update(kind='answer', answer=json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else str(value))
        except (ValueError, TypeError, KeyError):
            pass
    else:
        doc = next((e for e in reversed(evidence) if e.get('exit_code') == 0 and e.get('command', '').startswith('cat ')), None)
        text = (focus or {}).get('text', '') or (doc or {}).get('text', '')
        api = re.search(r'python3\s+(/[^\s]+)\s+(--[a-z]+)', text)
        if api:
            value = '上海' if '上海' in question else ('北京' if '北京' in question else 'A')
            plan.update(kind='run', command=f'python3 {api[1]} {api[2]} {value}')
        elif doc:
            plan.update(kind='inspect', source_id=doc['id'], query='接口入口' if '接口入口' in question else 'python3', offset=0, length=1000)
        else:
            # A retained cat result may have fallen out of the short summaries.
            retained = next((r for r in reversed(index) if r.get('available') and r.get('label', '').startswith('cat ')), None)
            candidate = re.search(r'"document_path":\s*"([^"\n]+)"', prompt)
            directory = re.search(r'/[A-Za-z0-9_/-]+', question)
            listing = next((e for e in reversed(evidence) if e.get('exit_code') == 0 and e.get('command', '').startswith('ls ')), None)
            if retained:
                plan.update(kind='inspect', source_id=retained['id'], query='python3', offset=0, length=1000)
            elif candidate:
                plan.update(kind='run', command='cat ' + candidate[1])
            elif listing and directory and listing.get('text', '').strip():
                name = listing['text'].splitlines()[0].strip()
                plan.update(kind='run', command='cat ' + directory[0].rstrip('/') + '/' + name)
            elif directory:
                plan.update(kind='run', command='ls ' + directory[0])
            elif '十七加二十五' in question:
                plan.update(kind='answer', answer='result=' + str(17 + 25))
    return json.dumps({'request_id': token, 'plan': plan}, ensure_ascii=False)
