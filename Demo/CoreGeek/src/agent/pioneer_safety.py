"""Observation-only night movement veto. Strategy, not monster AI or foresight."""
from .protocol import Pos, distance, move_command
from .home_defense import inside
from .combat_geometry import intervening_wall


def exposure(turn, cell):
    # All visible robots are hazards, including unknown targetTeam; this does
    # not assume the simulator's acquisition model is the official algorithm.
    return sum(1 for robot in turn.robots if robot.health > 0
               and distance(robot.pos, cell) <= 3
               and intervening_wall(turn, robot.pos, cell) is None)


def override(turn, commands):
    hero = turn.pioneer()
    if turn.is_day or hero is None or not turn.robots:
        return None
    issued = commands.get(hero.unit_id, {})
    destination = (Pos.load(issued['targetPos'][0])
                   if issued.get('action') == 'move' and issued.get('targetPos') else hero.pos)
    here, proposed = exposure(turn, hero.pos), exposure(turn, destination)
    if not here and not proposed:
        return None
    blocked = turn.blocked(hero)
    claimed = {Pos.load(c['targetPos'][0]) for uid,c in commands.items()
               if uid != hero.unit_id and c.get('action') == 'move' and c.get('targetPos')}
    candidates = [hero.pos] + [Pos(hero.pos.x+dx,hero.pos.y+dy)
                              for dx in (-1,0,1) for dy in (-1,0,1) if dx or dy]
    candidates = [p for p in candidates if p == hero.pos or
                  (turn.land(p) and p not in blocked and p not in claimed
                   and (not inside(turn,hero.pos) or inside(turn,p)))]
    base = turn.station()
    def rank(p):
        return (exposure(turn,p), 0 if inside(turn,p) else 1,
                min((distance(p,c) for c in turn.footprint(base)),default=0) if base else 0,
                0 if p == hero.pos else 1, p.x,p.y)
    best = min(candidates,key=rank)
    # Preserve an already-safe/reducing return route and useful stationary work.
    if proposed <= exposure(turn,best):
        return None
    return {'command':None if best == hero.pos else move_command(best),
            'reason':'visible_robot_exposure', 'before':here, 'proposed':proposed,
            'after':exposure(turn,best), 'destination':best.dump()}
