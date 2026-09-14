"""Bounded per-task original-text memory with literal search and range reads.

Only data already received by the policy may be added. This is local memory,
not a filesystem, network client, sandbox or a source of future observations.
"""
from copy import deepcopy
import hashlib

SCHEMA = 'competition-evidence-memory/1'
PER_SOURCE = 131072
TOTAL_CHARS = 196608
MAX_SOURCES = 12
MAX_CHUNK = 2000


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class EvidenceMemory:
    def __init__(self, owner, generation=''):
        self.owner = owner
        self.generation = generation
        self.records = []
        self.degraded = False

    def put(self, identity, text, *, label='', pinned=False, upstream_truncated=False):
        if (self.degraded or not isinstance(identity, str) or not 1 <= len(identity) <= 160
                or not isinstance(text, str)):
            return False
        existing = next((r for r in self.records if r['id'] == identity), None)
        received_hash = sha(text)
        if existing and existing['received_sha256'] == received_hash:
            return True
        self.records = [r for r in self.records if r['id'] != identity]
        retained = text[:PER_SOURCE]
        self.records.append({'id': identity, 'text': retained, 'received_chars': len(text),
                             'received_sha256': received_hash, 'stored_sha256': sha(retained),
                             'label': str(label)[:80], 'pinned': bool(pinned),
                             'upstream_truncated': bool(upstream_truncated)})
        while len(self.records) > MAX_SOURCES:
            victim = next((r for r in self.records if not r['pinned']), self.records[0])
            self.records.remove(victim)
        while sum(len(r['text'] or '') for r in self.records) > TOTAL_CHARS:
            victim = next((r for r in self.records if not r['pinned'] and r['text'] is not None), None)
            if victim is None:
                self.degraded = True
                return False
            victim['text'] = None  # keep a tombstone; never pretend an evicted text is readable
            victim['stored_sha256'] = sha('')
        return True

    def index(self):
        return [{'id': r['id'], 'label': r['label'], 'received_chars': r['received_chars'],
                 'stored_chars': len(r['text'] or ''), 'available': r['text'] is not None,
                 'complete': self.complete(r)} for r in self.records]

    @staticmethod
    def complete(record):
        return (record['text'] is not None and not record['upstream_truncated']
                and len(record['text']) == record['received_chars'])

    def document(self, identity):
        record = next((r for r in self.records if r['id'] == identity), None)
        return deepcopy(record) if record is not None else None

    def inspect(self, identity, *, offset=0, length=MAX_CHUNK, query=''):
        result = self._inspect(identity, offset=offset, length=length, query=query)
        if (isinstance(identity, str) and type(offset) is int and type(length) is int and isinstance(query, str)):
            result['source_id'] = identity
            result['request'] = {'offset': offset, 'length': length, 'query': query}
        return result

    def _inspect(self, identity, *, offset=0, length=MAX_CHUNK, query=''):
        if (self.degraded or not isinstance(identity, str) or type(offset) is not int or offset < 0
                or type(length) is not int or not 1 <= length <= MAX_CHUNK
                or not isinstance(query, str) or len(query) > 200):
            return {'error': 'invalid_memory_request'}
        record = next((r for r in self.records if r['id'] == identity), None)
        if record is None or record['text'] is None:
            return {'error': 'source_not_available', 'source_id': identity}
        text = record['text']
        meta = {'source_id': identity, 'received_chars': record['received_chars'],
                'stored_chars': len(text), 'source_complete': self.complete(record),
                'received_sha256': record['received_sha256']}
        if query:
            found = text.find(query, offset)
            if found < 0:
                return {**meta, 'error': 'literal_not_found', 'query': query}
            offset = max(0, found - min(100, length // 4))
            meta['match_offset'] = found
        if offset >= len(text):
            return {**meta, 'error': 'outside_retained_content'}
        end = min(len(text), offset + length)
        return {**meta, 'offset': offset, 'end': end, 'text': text[offset:end],
                'more_retained': end < len(text)}

    def dump(self):
        return {'schema': SCHEMA, 'owner': self.owner, 'generation': self.generation,
                'records': deepcopy(self.records), 'degraded': self.degraded}

    @classmethod
    def load(cls, raw, *, owner, generation):
        memory = cls(owner, generation)
        try:
            if (not isinstance(raw, dict) or set(raw) != set(memory.dump()) or raw['schema'] != SCHEMA
                    or raw['owner'] != owner or raw['generation'] != generation
                    or type(raw['degraded']) is not bool or not isinstance(raw['records'], list)
                    or len(raw['records']) > MAX_SOURCES):
                raise ValueError('invalid archive')
            seen = set()
            for record in raw['records']:
                if (not isinstance(record, dict) or set(record) != {'id', 'text', 'received_chars',
                        'received_sha256', 'stored_sha256', 'label', 'pinned', 'upstream_truncated'}
                        or not isinstance(record['id'], str) or not 1 <= len(record['id']) <= 160
                        or record['id'] in seen or type(record['pinned']) is not bool
                        or type(record['upstream_truncated']) is not bool
                        or type(record['received_chars']) is not int or record['received_chars'] < 0
                        or not isinstance(record['label'], str) or len(record['label']) > 80):
                    raise ValueError('invalid archive record')
                seen.add(record['id'])
                text = record['text']
                if text is not None and (not isinstance(text, str) or len(text) > PER_SOURCE
                                         or len(text) > record['received_chars']):
                    raise ValueError('invalid retained text')
                if record['stored_sha256'] != sha(text or ''):
                    raise ValueError('retained text hash mismatch')
                if not isinstance(record['received_sha256'], str) or len(record['received_sha256']) != 64:
                    raise ValueError('invalid original digest')
                if text is not None and len(text) == record['received_chars'] and sha(text) != record['received_sha256']:
                    raise ValueError('received text hash mismatch')
            if sum(len(r['text'] or '') for r in raw['records']) > TOTAL_CHARS:
                raise ValueError('archive exceeds memory budget')
            memory.records = deepcopy(raw['records'])
            memory.degraded = raw['degraded']
        except (ValueError, TypeError, KeyError):
            memory.records = []
            memory.degraded = True
        return memory
