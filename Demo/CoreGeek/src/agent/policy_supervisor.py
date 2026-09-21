"""Arbitrate public-observation work and defence proposals (strategy, not rules).

Threat thresholds are conservative heuristics, not forecasts or a proof that a
base is safe. Unknown targets count towards pressure; global weapon range does
not make a distant opponent-targeted robot an imminent threat to our base.
"""
from dataclasses import dataclass
from .protocol import distance
from .home_defense import full_night


@dataclass(frozen=True)
class SupervisorTuning:
    return_margin: int = 3
    nearby_threat_radius: int = 4
    health_budget_per_worker: int = 80
    # Held-out local comparisons have mixed score effects. Record healthy-base
    # risk advice first; enable it only after broader/platform validation.
    enforce_healthy_base: bool = False


@dataclass(frozen=True)
class Directive:
    mode: str
    reserve_pioneer: bool
    reason: str
    relevant_robots: int
    return_steps_lower_bound: int
    ready_workers: int = 0
    visible_pressure_hp: int = 0
    recommended_reserve: bool = False

    def summary(self):
        return dict(vars(self))


def evaluate(turn, payload, pioneer_tower, *, tower_pairs=(), committed_work=False,
             dusk_index=55, tuning=SupervisorTuning(), cleared=False):
    if cleared and not turn.is_day:
        return Directive('work', False, 'cleared_night_productive_work', 0, 0)
    pioneer, base = turn.pioneer(), turn.station()
    if pioneer is None or base is None or pioneer_tower is None:
        if full_night(turn):
            return Directive('defend', True, 'night_four_all_roles_defend', 0, 0)
        return Directive('work', False, 'no_pioneer_defence_assignment', 0, 0)
    travel = max(0, distance(pioneer.pos, pioneer_tower.pos)-1)
    # Official R03 base capacities; their values are not tuning parameters.
    intact = base.health >= {1: 1500, 2: 3000, 3: 4500}.get(base.level, 4500)
    assigned_workers = sum(role.kind == 'worker' for role, _ in tower_pairs)
    workers = sum(role.kind == 'worker' and distance(role.pos, tower.pos) <= 1
                  for role, tower in tower_pairs)
    if turn.is_day:
        index = (turn.round_no-1) % 130
        prepare = index >= dusk_index or 70-index <= travel+tuning.return_margin
        # Do not abandon a funded itinerary or confirmed task just because dusk
        # is approaching. A damaged base removes this exception. Reassess visible
        # night pressure on every subsequent observation.
        preserve = prepare and intact and committed_work
        reserve = prepare and not preserve
        reason = ('preserve_committed_work' if preserve else
                  'return_before_night' if reserve else 'daytime_work')
        enforce = reserve and (not intact or tuning.enforce_healthy_base)
        mode = 'prepare' if enforce else 'watch' if reserve else 'work'
        return Directive(mode, enforce, reason, 0, travel, workers, 0, reserve)
    group = payload.get('robot') or {}
    raw = group.get('roles', []) if isinstance(group, dict) else []
    targets = {str(r.get('id')): r.get('targetTeam') for r in raw if isinstance(r, dict)}
    team = (payload.get('teamOur') or {}).get('type')
    relevant, pressure, imminent = 0, 0, False
    for robot in turn.robots:
        if robot.health <= 0:
            continue
        near = min(distance(robot.pos, p) for p in turn.footprint(base)) <= tuning.nearby_threat_radius
        target = targets.get(str(robot.robot_id))
        # Only a recognised opposing target can exclude a distant visible robot.
        own_or_unknown = target not in ('challenger', 'defender') or team is None or target == team
        if own_or_unknown or near:
            relevant += 1
            pressure += robot.health
            imminent = imminent or near
    reserve = bool(relevant and (not intact or imminent or workers == 0 or
                     pressure > assigned_workers*tuning.health_budget_per_worker))
    if full_night(turn):
        return Directive('defend', True, 'night_four_all_roles_defend', relevant,
                         travel, workers, pressure, True)
    reason = ('base_damaged' if relevant and not intact else
              'base_threat_nearby' if imminent else
              'visible_pressure_requires_controller' if reserve else
              'low_visible_pressure_workers_on_posts' if relevant else
              'no_relevant_visible_threat')
    enforce = reserve and (not intact or tuning.enforce_healthy_base)
    mode = 'defend' if enforce else 'watch' if reserve else 'work'
    return Directive(mode, enforce, reason, relevant, travel, workers, pressure, reserve)
