"""Bounded observation-only evidence. Never infer a robot collision rule."""
from collections import Counter, defaultdict
import hashlib
import json

MAX_ROWS = 4096
MAX_GROUPS = 3
MAX_MEMBERS = 4
MAX_LINE_BYTES = 8192


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and abs(value) < 2**63


def _id(value):
    if isinstance(value, str) and len(value) <= 19 and value.isascii() and value.isdecimal():
        value = int(value)
    return value if _integer(value) and value >= 0 else None


def summarize(request):
    """Count only live, located, unambiguous IDs; duplicates are all excluded.

    Counts cover the scanned prefix when input_truncated > 0. No request data
    is changed. Missing data is unknown, never a claim of zero overlap.
    """
    group = request.get('robot') if isinstance(request, dict) else None
    rows = group.get('roles') if isinstance(group, dict) else None
    if not isinstance(rows, list):
        return {'available': False, 'data_insufficient': 'missing_robot_roles'}
    map_info = request.get('mapInfo')
    width = map_info.get('width') if isinstance(map_info, dict) else None
    height = map_info.get('height') if isinstance(map_info, dict) else None
    if not (_integer(width) and _integer(height) and width > 0 and height > 0):
        return {'available': False, 'data_insufficient': 'invalid_map_dimensions',
                'input_entries': len(rows), 'scanned_entries': 0}
    invalid = dead = metadata_truncated = 0
    candidates = []
    counts = Counter()
    for row in rows[:MAX_ROWS]:
        if not isinstance(row, dict) or not _integer(row.get('health')):
            invalid += 1
            continue
        if row['health'] <= 0:
            dead += 1
            continue
        rid = _id(row.get('id'))
        if rid is not None:
            counts[rid] += 1
        pos = row.get('pos')
        if (rid is None or not isinstance(pos, dict)
                or not _integer(pos.get('x')) or not _integer(pos.get('y'))
                or not 0 <= pos['x'] < width or not 0 <= pos['y'] < height):
            invalid += 1
            continue
        copy = {'id': rid, 'health': row['health']}
        for field, limit in [('roleType', 16), ('targetTeam', 16)]:
            value = row.get(field)
            copy[field] = value[:limit] if isinstance(value, str) else None
            metadata_truncated += isinstance(value, str) and len(value) > limit
        candidates.append(((pos['x'], pos['y']), copy))
    duplicate_entries = sum(n for n in counts.values() if n > 1)
    cells = defaultdict(list)
    for pos, row in candidates:
        if counts[row['id']] == 1:
            cells[pos].append(row)
    overlaps = [(pos, sorted(members, key=lambda row: row['id']))
                for pos, members in sorted(cells.items()) if len(members) > 1]
    # Location/health drift of the same co-located members is not a new anomaly.
    topology = sorted(tuple(r['id'] for r in members) for _, members in overlaps)
    quality = [invalid, duplicate_entries, max(0, len(rows)-MAX_ROWS), metadata_truncated]
    signature = hashlib.sha256(json.dumps([topology, quality], separators=(',', ':')).encode()).hexdigest()
    return {
        'available': True, 'valid_live_count': sum(map(len, cells.values())),
        'input_entries': len(rows), 'scanned_entries': min(len(rows), MAX_ROWS),
        'unique_cell_count': len(cells), 'overlap_group_count': len(overlaps),
        'invalid_entries': invalid, 'dead_entries': dead,
        'duplicate_id_entries': duplicate_entries,
        'input_truncated': quality[2], 'metadata_truncated': metadata_truncated,
        'groups_truncated': max(0, len(overlaps)-MAX_GROUPS),
        'members_truncated': sum(max(0, len(rows)-MAX_MEMBERS) for _, rows in overlaps[:MAX_GROUPS]),
        'groups': [{'pos': {'x': pos[0], 'y': pos[1]}, 'count': len(members),
                    'robots': members[:MAX_MEMBERS]} for pos, members in overlaps[:MAX_GROUPS]],
        'change_key': signature,
    }


def digest_line(summary, memory, stream):
    """Called inside ConsoleDigest's existing per-stream lock and reset window."""
    value = summary.get('robot_occupancy')
    if not isinstance(value, dict):
        return []
    round_no = summary.get('round')
    if not _integer(round_no) or round_no < 1:
        return []
    night = (round_no-1)//130 if (round_no-1)%130 >= 70 else None
    anomaly = bool(value.get('overlap_group_count') or value.get('invalid_entries')
                   or value.get('duplicate_id_entries') or value.get('input_truncated')
                   or not value.get('available'))
    signature = value.get('change_key') or ('unavailable', value.get('data_insufficient'))
    changed = signature != memory.get('key')
    emit = (night is not None and night != memory.get('night')) or (
        changed and (anomaly or memory.get('anomaly')))
    memory.update(key=signature, anomaly=anomaly)
    if night is not None:
        memory['night'] = night
    if not emit:
        return []
    record = {k:v for k,v in value.items() if k != 'change_key'}
    record.update(round=round_no, side=summary.get('side'), stream=stream,
                  event=summary.get('event'), version=summary.get('version_ref'),
                  scope='observed_same_frame_not_collision_rule')
    line = 'robot_occupancy ' + json.dumps(record, ensure_ascii=True, separators=(',', ':'))
    # Limits above bound normal records. Keep a safe summary if an injected
    # diagnostic envelope is oversized; never spill arbitrary input to logs.
    if len(line.encode()) > MAX_LINE_BYTES:
        return ['robot_occupancy ' + json.dumps({'round':round_no, 'output_omitted':True,
                                                'reason':'size_limit'}, separators=(',', ':'))]
    return [line]
