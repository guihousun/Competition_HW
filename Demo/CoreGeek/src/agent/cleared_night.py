"""Bounded public-observation clearance memory (strategy, R01/R02/R03).

The official interface has no damage-event feed. Unchanged HP is evidence of
no *observed* damage, not proof that no hit occurred (healing may mask a hit).
Never interpret lastRoundRoleActionResults as an incoming-attack receipt.
"""
from copy import deepcopy
import hashlib
import json

from .protocol import CONTROLLABLE_TYPES, TOWER_TYPES, Pos, distance


HAZARD_RADIUS = 4  # Published range 3 + one movement cell; conservative route buffer.


def hazard_cells(turn):
    """Strategy path exclusion, not a change to terrain or attack rules."""
    return {Pos(x, y) for robot in turn.robots if robot.health > 0
            for x in range(max(0, robot.pos.x - HAZARD_RADIUS), min(turn.width, robot.pos.x + HAZARD_RADIUS + 1))
            for y in range(max(0, robot.pos.y - HAZARD_RADIUS), min(turn.height, robot.pos.y + HAZARD_RADIUS + 1))}


def clean(value):
    if not isinstance(value, dict):
        return {}
    if (type(value.get('round')) is not int or not 1 <= value['round'] <= 1300
            or type(value.get('safe_rounds')) is not int or not 0 <= value['safe_rounds'] <= 20
            or not isinstance(value.get('identity'), str) or len(value['identity']) != 64
            or not isinstance(value.get('fingerprint'), str) or len(value['fingerprint']) != 64
            or type(value.get('complete')) is not bool
            or type(value.get('quiet')) is not bool
            or not isinstance(value.get('hp'), dict) or len(value['hp']) > 256):
        return {}
    hp = value['hp']
    if any(not isinstance(k, str) or len(k) > 40 or type(v) not in (int, float)
           or not 0 <= v <= 1000000 for k, v in hp.items()):
        return {}
    return {k: deepcopy(value[k]) for k in ('round', 'safe_rounds', 'identity',
                                            'fingerprint', 'complete', 'quiet', 'hp')}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    default=str).encode()).hexdigest()


def observe(turn, payload, memory, *, quiet_rounds=3, enabled=True):
    """Return next memory + diagnostic; caller commits only actual observations.

    First/discontinuous observations establish a baseline; three consecutive
    intervals with complete observations are required. Same-round repeats never
    count twice. Same-round revisions invalidate the window conservatively.
    Our-targeted or unlabelled robots block departure anywhere. An explicitly
    opponent-targeted robot blocks only near our footprint; productive paths
    separately avoid every robot's attack-range-plus-one region. No future wave.
    """
    previous = clean(memory)
    ours = payload.get('teamOur')
    robots = payload.get('robot')
    enemies = payload.get('teamEnemy')
    rows = ours.get('roles') if isinstance(ours, dict) else None
    robot_rows = robots.get('roles') if isinstance(robots, dict) else None
    enemy_rows = enemies.get('roles') if isinstance(enemies, dict) else None
    complete = (isinstance(rows, list) and bool(rows) and len(rows) <= 256
                and isinstance(robot_rows, list) and isinstance(enemy_rows, list)
                and turn.station() is not None)
    def valid_row(row):
        return (isinstance(row, dict) and type(row.get('id')) is int
                and type(row.get('health')) in (int, float) and 0 <= row['health'] <= 1000000
                and isinstance(row.get('pos'), dict)
                and type(row['pos'].get('x')) is int and 0 <= row['pos']['x'] < turn.width
                and type(row['pos'].get('y')) is int and 0 <= row['pos']['y'] < turn.height)
    complete = bool(complete and all(valid_row(row) for row in rows + robot_rows + enemy_rows))
    hp = {}
    if complete:
        for row in rows:
            if (not isinstance(row, dict) or type(row.get('id')) is not int
                    or type(row.get('health')) not in (int, float)
                    or not 0 <= row['health'] <= 1000000):
                complete = False
                break
            key = str(row['id'])
            if key in hp:
                complete = False
                break
            hp[key] = row['health']
    base = turn.station()
    own_group = ours if isinstance(ours, dict) else {}
    identity = _digest([turn.width, turn.height, own_group.get('type'),
                        own_group.get('id'), base.unit_id if base else None,
                        base.pos.dump() if base else None])
    fingerprint = _digest([hp, robot_rows, enemy_rows, complete])
    current = dict(round=turn.round_no, safe_rounds=0, identity=identity,
                   fingerprint=fingerprint, complete=bool(complete), quiet=False,
                   hp=hp if complete else {})
    reason = 'observing_baseline'
    continuous = (previous.get('identity') == identity and previous.get('complete')
                  and turn.round_no == previous.get('round', -2) + 1
                  and (turn.round_no - 1) // 130 == (previous['round'] - 1) // 130)
    damage = bool(continuous and any(value > 0 and
                  (key not in hp or hp[key] < value) for key, value in previous['hp'].items()))
    enemy_threat = any(e.health > 0 and e.kind in CONTROLLABLE_TYPES + TOWER_TYPES
                      for e in turn.enemies)
    targets = {str(row.get('id')): row.get('targetTeam') for row in robot_rows or () if isinstance(row, dict)}
    friendly = {cell for unit in turn.ours if unit.health > 0 for cell in turn.footprint(unit)}
    relevant = [r for r in turn.robots if r.health > 0 and
                (targets.get(str(r.robot_id)) not in ('challenger', 'defender')
                 or own_group.get('type') not in ('challenger', 'defender')
                 or targets.get(str(r.robot_id)) == own_group['type']
                 or any(distance(r.pos, cell) <= HAZARD_RADIUS for cell in friendly))]
    if not enabled:
        reason = 'disabled'
    elif turn.is_day:
        reason = 'daytime'
    elif not complete:
        reason = 'incomplete_public_observation'
    elif relevant or enemy_threat:
        reason = 'visible_threat'
    elif damage:
        reason = 'observed_hp_loss_or_disappearance'
    elif previous.get('identity') == identity and previous.get('round') == turn.round_no:
        if previous.get('fingerprint') == fingerprint:
            current['safe_rounds'] = min(quiet_rounds, previous['safe_rounds'])
            reason = 'repeat_observation'
        else:
            reason = 'same_round_observation_changed'
    elif continuous and previous.get('quiet') and (turn.round_no - 1) % 130 != 70:
        current['safe_rounds'] = min(quiet_rounds, previous['safe_rounds'] + 1)
        reason = 'observed_quiet_interval'
    elif previous:
        reason = 'observation_discontinuity_or_new_night'
    current['quiet'] = bool(enabled and not turn.is_day and complete and not damage
                            and not enemy_threat and not relevant)
    if (previous.get('round') == turn.round_no and previous.get('identity') == identity
            and previous.get('fingerprint') == fingerprint):
        current['quiet'] = previous['quiet']
    ready = current['safe_rounds'] >= quiet_rounds
    report = dict(phase='productive' if ready else 'daytime' if turn.is_day else 'defend',
                  safe_rounds=current['safe_rounds'], required_rounds=quiet_rounds,
                  reason='clearance_confirmed' if ready else reason,
                  damage_detection='public_hp_delta_and_disappearance_only',
                  damage_limit='healing_can_mask_same_round_damage; no_official_hit_receipts',
                  live_robots=sum(r.health > 0 for r in turn.robots), relevant_robots=len(relevant),
                  hazard_radius=HAZARD_RADIUS)
    return current, report
