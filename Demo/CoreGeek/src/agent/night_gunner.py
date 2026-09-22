"""Observed, safe handover of a three-rocket post (strategy; R01/R04/R07).

No speculative occupancy or extra control range: a replacement enters the post
only after the current observation shows it empty. Worker coverage remains until
the pioneer actually reaches the common cell. No persistent success assumption.
"""
from . import home_defense, rocket_post
from .protocol import Pos, distance, move_command, attack_command_multi


def pioneer_status(turn, payload, state):
    hero = turn.pioneer()
    if hero is None:
        return False, 'no_living_pioneer'
    if state is None:
        return False, 'task_memory_unavailable'
    cycle = getattr(state, 'tasks', {}).get('cycle')
    if str((payload or {}).get('phaseTask') or '').strip():
        return False, 'public_task_active'
    if cycle and cycle.phase != 'ended' and not cycle.ended_round:
        return False, 'task_completion_not_confirmed'
    judge = getattr(state, 'judge', None)
    if judge and (judge.pending_prompt is not None or judge.pending_cmd is not None):
        return False, 'task_receipt_in_flight'
    router = getattr(state, 'llm_router', None)
    if router and (any(r is not None and r.owner == 'task' for r in router.pending.values())
                   or any(r.owner == 'task' for r in router.queue)):
        return False, 'task_router_in_flight'
    agent = getattr(getattr(state, 'team_agent', None), 'task', None)
    if agent is not None and getattr(agent, 'pending', None):
        return False, 'task_agent_in_flight'
    return True, 'pioneer_idle'


def plan(turn, payload, state, commands, claimed, aim_points, *, force=False):
    common, inner = rocket_post.common_cells(turn)
    if not common:
        return None  # Preserve existing worker fallback for non-shared layouts.
    guns = sorted((g for g in turn.weapons() if g.kind == 'rocket'), key=lambda g: g.unit_id)
    eligible, reason = pioneer_status(turn, payload, state)
    if force and reason == 'pioneer_idle':
        eligible = True
        reason = 'emergency_worker_loss_or_injury'
    hero = turn.pioneer()
    if hero is not None and hero.unit_id in commands:
        eligible, reason = False, 'pioneer_action_preserved'
    aims = {g.unit_id: aim_points(turn, g) if g.cooldown == 0 else [] for g in guns}
    ready = [g for g in guns if aims[g.unit_id]]
    used = {str(c.get('controllerId')) for c in commands.values() if c.get('action') == 'attack'}
    workers = [w for w in turn.workers() if w.unit_id not in commands and str(w.unit_id) not in used]

    def can_fire(role):
        return home_defense.inside(turn, role.pos) and any(distance(role.pos, g.pos) == 1 for g in ready)

    def path(role, goals, extra=()):
        blocked = turn.blocked(role) | set(claimed) | set(extra)
        if home_defense.inside(turn, role.pos):
            blocked |= {Pos(x, y) for x in range(turn.width) for y in range(turn.height)
                        if not home_defense.inside(turn, Pos(x, y))}
        return rocket_post._path(turn, role.pos, set(goals) - blocked, blocked)

    def move(role, route):
        if route and len(route) > 1:
            commands[role.unit_id] = move_command(route[1])
            claimed.add(route[1])
            return True
        return False

    owner = min(workers, key=lambda w: (w.pos not in common, not can_fire(w),
                min(distance(w.pos, p) for p in common), w.unit_id), default=None)
    report = dict(owner=owner.unit_id if owner else None, stand=None,
                  weapons=[g.unit_id for g in guns], active=False, single_operator=True,
                  phase='unavailable', reason=reason,
                  candidate=hero.unit_id if eligible else None, handover='not_requested',
                  reservation_owner=owner.unit_id if owner else None, owned_roles=[],
                  common_stand_feasible=bool(common))
    if eligible and hero.pos in common:
        owner = hero
        report.update(owner=hero.unit_id, reservation_owner=hero.unit_id,
                      reason='idle_pioneer_on_common_post', handover='complete')
    elif eligible:
        report['handover'] = 'approaching'
        occupants = [r for r in turn.controllable() if r.pos in common]
        # Approach a waiting bay while the current gunner keeps the occupied
        # post. A same-round move into its vacated cell would be illegal.
        if occupants:
            waiting = {p for p in inner - common if any(distance(p, c) == 1 for c in common)}
            route = path(hero, waiting)
            if hero.pos not in waiting:
                move(hero, route)
            at_wait = hero.pos in waiting
            if owner and owner.pos in common and at_wait:
                if can_fire(owner):
                    report['handover'] = 'waiting_for_cooldown'
                    report['reason'] = 'worker_preserves_available_shot'
                else:
                    # At t+1 the pioneer can move in but cannot also shoot.
                    # The bay must cover every gun ready by then, not just the
                    # one which happened to have a target at t.
                    next_ready = [g for g in guns if g.cooldown <= 1]
                    bays = [p for p in inner - common - turn.blocked(owner) - set(claimed)
                            if distance(owner.pos, p) == 1 and p != hero.pos
                            and all(distance(p, g.pos) == 1 for g in next_ready)
                            and any(distance(p, g.pos) == 1 for g in guns)]
                    if bays:
                        bay = min(bays, key=lambda p: (distance(p, hero.pos), p.x, p.y))
                        commands[owner.unit_id] = move_command(bay)
                        claimed.add(bay)
                        report.update(handover='worker_vacating_on_cooldown',
                                      reservation_owner=hero.unit_id, reason='safe_handover_bay')
                    else:
                        report.update(handover='waiting_for_safe_bay', reason='no_covered_handover_bay')
            report['owned_roles'].append(hero.unit_id)
        else:
            route = path(hero, common)
            if route and move(hero, route):
                report.update(reservation_owner=hero.unit_id, handover='pioneer_entering_empty_post')
                report['owned_roles'].append(hero.unit_id)
            elif not route:
                report.update(handover='approach_blocked', reason='pioneer_post_unreachable')
    if owner is None:
        report['reason'] = 'no_available_gunner' if not eligible else report['reason']
        return report
    report['owner'] = owner.unit_id
    report['owner_kind'] = owner.kind
    report['owned_roles'].append(owner.unit_id)
    report['stand'] = owner.pos.dump()
    if owner.unit_id in commands:
        report['phase'] = 'handover_move' if report['handover'] == 'worker_vacating_on_cooldown' else 'owner_busy'
        return report
    usable = [g for g in ready if home_defense.inside(turn, owner.pos) and distance(owner.pos, g.pos) == 1]
    if usable:
        def fire_rank(g):
            damage = sum(min(r.health, sum(20 if p == r.pos else 10 if distance(p, r.pos) == 1 else 0
                         for p in aims[g.unit_id])) for r in turn.robots if r.health > 0)
            return -damage, -g.level, g.unit_id
        gun = min(usable, key=fire_rank)
        commands[gun.unit_id] = attack_command_multi(owner.unit_id, aims[gun.unit_id])
        claimed.add(owner.pos)
        report.update(active=True, phase='firing', firing=gun.unit_id)
    elif report['reservation_owner'] != owner.unit_id:
        report.update(active=True, phase='covering_handover')
    elif owner.pos in common:
        report.update(active=True, phase='holding_cooldown_or_no_target')
    else:
        route = path(owner, common)
        if move(owner, route):
            report.update(phase='moving_to_common_stand', approach_steps=len(route)-1,
                          stand=route[-1].dump())
        else:
            report.update(phase='common_stand_blocked', reason='no_reachable_common_post')
    return report
