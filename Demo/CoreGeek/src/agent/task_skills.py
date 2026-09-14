"""Team-local, evidence-gated query methods. No answer or query-data cache.

A candidate only points to an observed document. Its method is exposed only
after a new verified read matches the document fingerprint. It never executes
anything, grants task permissions or claims official judge success.
"""
from copy import deepcopy
import hashlib
import json
import posixpath
import re
import shlex

SCHEMA = 'competition-query-skills/1'
LIMIT = 8


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _verified(event):
    return (isinstance(event, dict) and event.get('verified') is True
            and type(event.get('exit_code')) is int and event['exit_code'] == 0
            and event.get('truncated') is False
            and isinstance(event.get('generation'), str) and bool(event['generation']))


def _path(value):
    if not isinstance(value, str) or not value.startswith('/') or len(value) > 256 or any(c in value for c in '\n\r\0'):
        return None
    return posixpath.normpath(value)


def _template(command):
    if not isinstance(command, str) or len(command) > 8000:
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|<>')
        lexer.whitespace_split = True
        lexer.commenters = ''
        argv = list(lexer)
    except ValueError:
        return None
    if len(argv) < 2 or len(argv) > 32 or argv[0] not in ('python3', 'python') or not _path(argv[1]):
        return None
    if any(v and all(c in ';&|<>' for c in v) for v in argv):
        return None
    # Preserve only program and option names. Never retain the previous city,
    # record id, answer, token or any other argument value.
    result = [argv[0], _path(argv[1])]
    for arg in argv[2:]:
        option, sep, _ = arg.partition('=')
        if re.fullmatch(r'--?[A-Za-z_][A-Za-z0-9_-]*', option):
            result.append(option + ('=<current-value>' if sep else ''))
        else:
            result.append('<current-value>')
    return result


class SkillLibrary:
    def __init__(self, owner):
        if not isinstance(owner, str) or not owner or len(owner) > 160:
            raise ValueError('invalid skill owner')
        self.owner = owner
        self.entries = {}
        self.degraded = False

    def learn(self, document, query):
        if self.degraded or not _verified(document) or not _verified(query):
            return None
        if document['generation'] != query['generation']:
            return None
        path = _path(document.get('path'))
        text = document.get('text')
        template = _template(query.get('command'))
        if not path or not isinstance(text, str) or not text or len(text) > 65536 or not template:
            return None
        doc_hash = _sha(text)
        identity = _sha(path + '\0' + doc_hash + '\0' + json.dumps(template))
        self.entries.pop(identity, None)
        self.entries[identity] = {'id': identity, 'document_path': path,
                                  'document_sha256': doc_hash, 'template': template}
        while len(self.entries) > LIMIT:
            self.entries.pop(next(iter(self.entries)))
        return identity

    def candidates(self, task_text):
        if self.degraded or not isinstance(task_text, str):
            return []
        references = [_path(p) for p in re.findall(r'/[^\s\"\'`<>，。；、（）()]+', task_text)]
        found = []
        for entry in reversed(list(self.entries.values())):
            path = entry['document_path']
            if any(ref and (path == ref or path.startswith(ref.rstrip('/') + '/')) for ref in references):
                found.append({'id': entry['id'], 'document_path': path,
                              'status': 'requires_current_document_verification'})
            if len(found) == 3:
                break
        return found

    def hint(self, identity, current_document):
        if self.degraded or not _verified(current_document):
            return None
        entry = self.entries.get(identity)
        if not entry or _path(current_document.get('path')) != entry['document_path']:
            return None
        text = current_document.get('text')
        if not isinstance(text, str) or len(text) > 65536 or _sha(text) != entry['document_sha256']:
            return None
        return {'document_path': entry['document_path'], 'template': list(entry['template']),
                'status': 'document_revalidated_query_method',
                'instruction': '参数必须从当前题目重新确定，并实际查询当前数据；此方法不包含答案或判题通过声明。'}

    def dump(self):
        return {'schema': SCHEMA, 'owner': self.owner, 'degraded': self.degraded,
                'entries': deepcopy(list(self.entries.values()))}

    @classmethod
    def load(cls, raw, *, owner):
        result = cls(owner)
        try:
            if not isinstance(raw, dict) or set(raw) != {'schema', 'owner', 'degraded', 'entries'} or raw['schema'] != SCHEMA or raw['owner'] != owner:
                raise ValueError('foreign or malformed state')
            if type(raw['degraded']) is not bool or not isinstance(raw['entries'], list) or len(raw['entries']) > LIMIT:
                raise ValueError('invalid state bounds')
            for item in raw['entries']:
                if not isinstance(item, dict) or set(item) != {'id', 'document_path', 'document_sha256', 'template'}:
                    raise ValueError('invalid method')
                path = _path(item['document_path'])
                template = item['template']
                if path != item['document_path'] or not isinstance(item['document_sha256'], str) or not re.fullmatch('[a-f0-9]{64}', item['document_sha256']):
                    raise ValueError('invalid document')
                if not isinstance(template, list) or not 2 <= len(template) <= 32 or any(not isinstance(v, str) or len(v) > 256 for v in template):
                    raise ValueError('invalid template')
                if _template(shlex.join(template)) != template:
                    raise ValueError('unmasked or invalid template')
                expected = _sha(path + '\0' + item['document_sha256'] + '\0' + json.dumps(template))
                if item['id'] != expected or expected in result.entries:
                    raise ValueError('invalid method identity')
                result.entries[expected] = deepcopy(item)
            result.degraded = raw['degraded']
        except (ValueError, TypeError, KeyError):
            result.entries = {}
            result.degraded = True
        return result
