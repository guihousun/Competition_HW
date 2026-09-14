"""Bounded public-source archive, visible ranges and uncertain publication dates.

This is an internal memory primitive, not a sandbox or an observation source.
Only the caller's received text is stored; inspect does not itself mean the model
has read a range. The router's actual-send acknowledgement must call expose().
"""
from copy import deepcopy
import json
from .evidence_memory import EvidenceMemory, PER_SOURCE, sha

SCHEMA = 'competition-world-memory/1'
MAX_ORIGINS = 64
MAX_RANGES = 64


class WorldMemory:
    def __init__(self, owner):
        if owner not in ('news', 'treasure'):
            raise ValueError('invalid world topic')
        self.owner = owner
        self.archive = EvidenceMemory(owner, 'public-world')
        self.origins = {}
        self.visible = {}
        self.last_round = 0
        self.last_id = None
        self.degraded = False

    def observe(self, text, round_no):
        if (self.degraded or type(round_no) is not int or not 1 <= round_no <= 1300
                or round_no < self.last_round or (text is not None and not isinstance(text, str))):
            return None
        identity = sha(text) if text and text.strip() else None
        if identity is not None:
            if identity not in self.origins:
                if len(self.origins) >= MAX_ORIGINS:
                    self.degraded = True
                    return None
                # A first observation halfway through a later day is not proof
                # that relative words were published that day. A witnessed change
                # at the documented daily publication boundary is stronger.
                basis, day = 'unknown', None
                if round_no <= 130:
                    basis, day = 'first_game_day', 1
                elif ((round_no - 1) % 130 == 0 and self.last_round == round_no - 1
                      and self.last_id is not None and self.last_id != identity):
                    basis, day = 'observed_day_boundary', (round_no - 1) // 130 + 1
                self.origins[identity] = {'first_round': round_no, 'date_basis': basis, 'anchor_day': day}
            old = self.archive.document(identity)
            if old is not None and old['text'] is None:
                # Receiving the same original again can replenish an evicted
                # body. It does not grant a new publication date or fresh read.
                self.archive.records = [r for r in self.archive.records if r['id'] != identity]
                self.visible.pop(identity, None)
            self.archive.put(identity, text, label=f'{self.owner} / first round {self.origins[identity]["first_round"]}')
            active = {r['id'] for r in self.archive.records}
            self.visible = {key: ranges for key, ranges in self.visible.items() if key in active}
        self.last_round, self.last_id = round_no, identity
        return identity

    def inspect(self, identity, **request):
        if self.degraded:
            return {'error': 'world_memory_degraded'}
        return self.archive.inspect(identity, **request)

    def expose(self, identity, start, end):
        """Record a range actually included in an emitted model request."""
        record = self.archive.document(identity)
        if (self.degraded or record is None or record['text'] is None
                or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(record['text'])):
            return False
        merged = []
        for left, right in sorted(self.visible.get(identity, []) + [[start, end]]):
            if merged and left <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], right)
            else:
                merged.append([left, right])
        if len(merged) > MAX_RANGES:
            return False
        self.visible[identity] = merged
        return True

    def reviewed(self, identity):
        record = self.archive.document(identity)
        return bool(not self.degraded and record and self.archive.complete(record)
                    and self.visible.get(identity) == [[0, len(record['text'])]])

    def quote_visible(self, identity, quote):
        record = self.archive.document(identity)
        if (self.degraded or record is None or record['text'] is None
                or not isinstance(quote, str) or not quote.strip() or len(quote) > 500):
            return False
        return any(quote in record['text'][start:end] for start, end in self.visible.get(identity, []))

    def index(self):
        result = []
        for entry in self.archive.index():
            identity = entry['id']
            offset = 0
            for start, end in self.visible.get(identity, []):
                if start > offset:
                    break
                offset = max(offset, end)
            result.append({**entry, **self.origins[identity], 'reviewed': self.reviewed(identity),
                           'next_unread': offset if entry['available'] and offset < entry['stored_chars'] else None})
        return result

    def dump(self):
        return {'schema': SCHEMA, 'owner': self.owner, 'archive': self.archive.dump(),
                'origins': deepcopy(self.origins), 'visible': deepcopy(self.visible),
                'last_round': self.last_round, 'last_id': self.last_id, 'degraded': self.degraded}

    @classmethod
    def load(cls, raw, *, owner):
        result = cls(owner)
        try:
            if (not isinstance(raw, dict) or set(raw) != set(result.dump()) or raw['schema'] != SCHEMA
                    or raw['owner'] != owner or type(raw['degraded']) is not bool
                    or len(json.dumps(raw, ensure_ascii=False)) > 1300000
                    or not isinstance(raw['origins'], dict) or len(raw['origins']) > MAX_ORIGINS
                    or not isinstance(raw['visible'], dict)
                    or type(raw['last_round']) is not int or not 0 <= raw['last_round'] <= 1300):
                raise ValueError('invalid public memory')
            archive = EvidenceMemory.load(raw['archive'], owner=owner, generation='public-world')
            if archive.degraded:
                raise ValueError('invalid source archive')
            for identity, origin in raw['origins'].items():
                if (not isinstance(identity, str) or len(identity) != 64
                        or any(c not in '0123456789abcdef' for c in identity)
                        or not isinstance(origin, dict) or set(origin) != {'first_round', 'date_basis', 'anchor_day'}
                        or type(origin['first_round']) is not int
                        or not 1 <= origin['first_round'] <= raw['last_round']):
                    raise ValueError('invalid source provenance')
                n, basis, day = origin['first_round'], origin['date_basis'], origin['anchor_day']
                if not ((basis == 'unknown' and day is None)
                        or (basis == 'first_game_day' and n <= 130 and type(day) is int and day == 1)
                        or (basis == 'observed_day_boundary' and n > 130 and (n - 1) % 130 == 0
                            and type(day) is int and day == (n - 1) // 130 + 1)):
                    raise ValueError('invalid date basis')
            records = {r['id']: r for r in archive.records}
            if (set(records) - set(raw['origins']) or set(raw['visible']) - set(records)
                    or any(r['id'] != r['received_sha256'] for r in records.values())
                    or (raw['last_id'] is not None and raw['last_id'] not in raw['origins'])):
                raise ValueError('unassociated source')
            for identity, ranges in raw['visible'].items():
                if not isinstance(ranges, list) or len(ranges) > MAX_RANGES:
                    raise ValueError('invalid coverage')
                record = records[identity]
                retained_limit = len(record['text']) if record['text'] is not None else min(record['received_chars'], PER_SOURCE)
                previous = -1
                for interval in ranges:
                    if (not isinstance(interval, list) or len(interval) != 2
                            or any(type(v) is not int for v in interval)
                            or not previous < interval[0] < interval[1] <= retained_limit):
                        raise ValueError('invalid visible range')
                    previous = interval[1]
            result.archive = archive
            result.origins, result.visible = deepcopy(raw['origins']), deepcopy(raw['visible'])
            result.last_round, result.last_id, result.degraded = raw['last_round'], raw['last_id'], raw['degraded']
        except (ValueError, TypeError, KeyError, RecursionError):
            result = cls(owner)
            result.degraded = True
        return result
