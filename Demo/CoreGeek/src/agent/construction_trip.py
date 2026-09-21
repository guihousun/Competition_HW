"""Public-state stone -> wall -> gun-post trips for the spare daylight worker.

Strategy only: R02 (task book 4.1-4.3, eight-way movement and 70-round day)
and R03 (4.4-4.5, adjacent one-stone collection and one-stone wall builds).
The caller supplies ordered legal missing wall sites and owns worker assignment.
No persistent itinerary, inferred mine reserves, purchases or private inputs.
"""
from collections import deque
from functools import lru_cache

from .home_defense import inside
from .protocol import (DAY_ROUNDS, ROUNDS_PER_DAY, CONTROLLABLE_TYPES, TOWER_TYPES,
                       WALL, WALL_MATERIAL, WORKER, Pos, distance,
                       move_command, collect_command, build_command)

MARGIN = 2


@lru_cache(maxsize=4096)
def _neighbours(pos):
    # A reusable tuple avoids allocating the same eight frozen Pos values in
    # every BFS overlay. No mutable map, wall or role state enters this cache.
    return tuple(Pos(pos.x + dx, pos.y + dy) for dx in (-1, 0, 1)
                 for dy in (-1, 0, 1) if dx or dy)


class _Board:
    """Per-call BFS cache; hypothetical built walls never change the Turn."""
    def __init__(self, turn, role, claimed, route_guard=None):
        # A published TaskPoint2 anchor covers its right-hand cell as well
        # (R02/R07). Keep that footprint local to this planner; do not change
        # the shared Turn or any simulator geometry during an experiment.
        task_cells = {Pos(p.x + 1, p.y) for p, kind in turn.zones.items()
                      if kind in ('challengerTaskPoint2', 'defenderTaskPoint2')}
        blocked = turn.blocked(role) | frozenset(claimed) | task_cells
        self.free = {Pos(x, y) for x in range(turn.width) for y in range(turn.height)
                     if turn.land(Pos(x, y)) and Pos(x, y) not in blocked}
        self.homes = tuple(sorted({p for gun in turn.weapons() for p in _neighbours(gun.pos)
                                  if p in self.free and inside(turn, p)},
                                 key=lambda p: (p.x, p.y)))
        self.cache = {}
        self.role = role
        self.route_guard = route_guard

    def search(self, starts, added=frozenset()):
        key = (tuple(starts), added)
        if key in self.cache:
            return self.cache[key]
        origins = {p: p for p in starts if p in self.free and p not in added}
        distances = {p: 0 for p in origins}
        first = {p: None for p in origins}
        queue = deque(origins)
        while queue:
            cell = queue.popleft()
            for nxt in _neighbours(cell):
                if nxt in distances or nxt not in self.free or nxt in added:
                    continue
                distances[nxt] = distances[cell] + 1
                first[nxt] = nxt if first[cell] is None else first[cell]
                origins[nxt] = origins[cell]
                queue.append(nxt)
        self.cache[key] = distances, first, origins
        return self.cache[key]

    def building_prefixes(self, start, sites, limit):
        """Budget each built prefix including its post-build legal home route.

        Greedy ordered sites keep the policy bounded and explainable. Rejected
        sites may be skipped; this does not claim a globally shortest tour.
        Every build stand must still reach a gun post after that wall exists.
        """
        added = frozenset()
        current = start
        elapsed = 0
        first_command = None
        built = []
        for wall in sites:
            outward, first, _ = self.search((current,), added)
            new_added = added | {wall}
            returning, _, homes = self.search(self.homes, new_added)
            stands = [p for p in _neighbours(wall)
                      if p in outward and p in returning and p not in new_added]
            penalties = {}
            if self.route_guard is not None:
                for stand in stands:
                    allowed,penalty = self.route_guard.check(self.role.unit_id,stand,new_added)
                    if allowed:
                        penalties[stand] = penalty
                stands = [p for p in stands if p in penalties]
            if not stands:
                continue
            stand = min(stands, key=lambda p: (outward[p] + returning[p] + penalties.get(p,0),
                                               outward[p], p.x, p.y))
            if first_command is None:
                first_command = (build_command(wall, WALL) if outward[stand] == 0
                                 else move_command(first[stand]))
            elapsed += outward[stand] + 1
            current, added = stand, new_added
            built.append(wall)
            yield {'command': first_command, 'walls': tuple(built),
                   'actions': elapsed + returning[stand],
                   'post': homes[stand], 'return_actions': returning[stand]}
            if len(built) >= limit:
                break


def plan(turn, role, wall_sites, *, claimed=(), deadline=55, batch_limit=10,
         mine_available=True, route_guard=None):
    """Return ``(command, report)`` or None (safe hold/no reachable return).

    Remaining actions include the action emitted now, all planned collection
    and construction, and walking back to an interior adjacent gun post. At
    least two rounds remain before the caller's deadline. Threats, darkness,
    expired budgets and unavailable supplies yield only a current legal return
    move, never a new outbound trip. The caller should reserve this worker even
    when None is returned, rather than retrying an unrelated mining planner.

    Standing next to observed stone is the stateless mining-phase marker:
    partial stone loads there may be topped up; carried stone elsewhere is
    used for construction first. A disappeared mine is never recollected.
    """
    if role.kind != WORKER or role.health <= 0 or role not in turn.workers():
        return None
    base = turn.station()
    if base is None or base.health <= 0:
        return None
    claimed = frozenset(claimed)
    board = _Board(turn, role, claimed, route_guard)
    index = (turn.round_no - 1) % ROUNDS_PER_DAY
    deadline = min(DAY_ROUNDS, max(0, int(deadline)))
    limit = max(0, min(10, int(batch_limit)))

    def report(phase, actions, reason, **extra):
        return {'phase': phase, 'reason': reason, 'worker': role.unit_id,
                'remaining_actions': actions, 'day_index': index,
                'deadline': deadline, 'margin': MARGIN,
                'slack': deadline - index - actions, **extra}

    def return_home(reason):
        # Returning must never send an already sheltered worker outside the
        # ring to reach a different gun, especially under threat or at night.
        # Outside workers can still use the observed entrance to get home.
        outside = (frozenset(p for p in board.free if not inside(turn, p))
                   if inside(turn, role.pos) else frozenset())
        distances, first, _ = board.search((role.pos,), outside)
        posts = [p for p in board.homes if p in distances]
        if not posts:
            return None
        for goal in sorted(posts,key=lambda p:(distances[p],p.x,p.y)):
            if first[goal] is None:
                return None
            command = move_command(first[goal])
            if route_guard is None or route_guard.allows_command(role,command):
                return command,report('return',distances[goal],reason,post=goal.dump())
        return None

    threat = (any(r.health > 0 for r in turn.robots)
              or any(r.health > 0 and r.kind in CONTROLLABLE_TYPES + TOWER_TYPES
                     for r in turn.enemies))
    if not turn.is_day:
        return return_home('night_return')
    if threat:
        return return_home('visible_threat')
    if index + MARGIN >= deadline:
        return return_home('deadline')

    from . import frontline, strategy_config
    rear = set(frontline.rear_walls(turn)) if strategy_config.get()['enabled'] else set()
    occupied = turn.occupied_cells()
    sites = tuple(dict.fromkeys(p for p in wall_sites
                               if p not in rear and p in board.free and (p not in occupied or p == role.pos)
                               and p not in claimed))
    if not sites or not limit:
        return return_home('no_missing_wall')
    if not board.homes:
        return None
    stones = role.backpack.count(WALL_MATERIAL)
    capacity = 100 if role.capacity is None else role.capacity
    # Total stone capacity stays constant while collecting; a shrinking free
    # slot count must not make a partial batch stop halfway through.
    stone_capacity = max(0, capacity - (len(role.backpack) - stones))
    maximum = min(len(sites), limit, stone_capacity)
    adjacent = sorted((p for p in turn.stone_mines() if distance(p, role.pos) == 1),
                      key=lambda p: (p.x, p.y))
    mining = mine_available and bool(adjacent) and stones < maximum
    options = []

    def consider(start, mine=None, walk=0, outbound=None):
        count = maximum if mine is not None else min(stones, len(sites), limit)
        if count <= 0:
            return
        for chain in board.building_prefixes(start, sites, count):
            collect = max(0, len(chain['walls']) - stones) if mine is not None else 0
            actions = walk + collect + chain['actions']
            if index + actions + MARGIN > deadline:
                continue
            if walk:
                command, phase = move_command(outbound), 'to_mine'
            elif collect:
                command, phase = collect_command(mine), 'collect'
            else:
                command = chain['command']
                phase = 'build' if command['action'] == 'build' else 'to_wall'
            details = report(phase, actions, 'complete_trip_fits',
                             planned_walls=[p.dump() for p in chain['walls']],
                             collect_remaining=collect, stones_held=stones,
                             post=chain['post'].dump(),
                             return_actions=chain['return_actions'])
            if mine is not None:
                details['mine'] = mine.dump()
            options.append((-len(chain['walls']), actions, len(options), command, details))

    if stones and not mining:
        consider(role.pos)
    elif mining:
        # Do not wander around the mine between collections. If topping up no
        # longer fits, a carried-stone prefix can still safely go to construction.
        consider(role.pos, adjacent[0])
    elif mine_available and maximum > 0:
        distances, first, _ = board.search((role.pos,))
        for mine in sorted(turn.stone_mines(), key=lambda p: (p.x, p.y)):
            stands = [p for p in _neighbours(mine) if p in distances]
            if not stands:
                continue
            stand = min(stands, key=lambda p: (distances[p], p.x, p.y))
            consider(stand, mine, distances[stand], first[stand])
    if not options:
        return return_home('no_complete_trip')
    _, _, _, command, details = min(options, key=lambda item: item[:3])
    if route_guard is not None and not route_guard.allows_command(role,command):
        return None
    return command, details
