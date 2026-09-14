"""Seeded local fixtures; wave counts/locations are demo parameters, not official."""
import random
from copy import deepcopy
from .protocol import (TASK_ITEM_PRICE, TASK_ITEMS, Pos, Turn, distance,
                       station_footprint)

ROBOT_STATS = {'smallRobot': (40, 5, 1), 'middleRobot': (60, 10, 2),
               'largeRobot': (500, 20, 4), 'bossRobot': (800, 40, 10)}


def observation(state):
    """Never let the competition policy see the local simulator's hidden state.

    Two separate jobs, both required by the rules:

    * private ``_demo`` / ``_engine`` keys are stripped (本地模拟内部状态);
    * enemy units outside our shared vision are removed (任务书 §4.3), so a local
      run cannot depend on a view the judge would never hand us.
    """
    from .vision import filter_observation

    public = {k: deepcopy(v) for k, v in state.items() if not k.startswith('_')}
    return filter_observation(public)


def scenario(seed=1, side='challenger', pressure=1):
    seed, pressure = int(seed), int(pressure)
    if side not in ('challenger', 'defender') or not 1 <= pressure <= 3:
        raise ValueError('side must be challenger/defender; pressure must be 1..3')
    rng = random.Random(seed)
    x, y = rng.randint(5, 12), rng.randint(22, 27)
    if side == 'defender':
        x, y = 39-x, 32-y
    base = 10000 if side == 'challenger' else 20000
    def unit(uid, kind, a, b, hp, capacity=0):
        return {'id': uid, 'roleType': kind, 'pos': {'x': a, 'y': b},
                'health': hp, 'level': 1, 'backPackCapability': capacity, 'backpack': []}
    roles = [unit(base+13, 'station', x, y, 1500),
             unit(base+10, 'worker', x-1, y, 220, 100),
             unit(base+11, 'pioneer', x+2, y-1, 200, 40),
             unit(base+12, 'worker', x, y+1, 220, 100)]
    enemy = unit((30000-base)+13, 'station', 39-x, 32-y, 1500)
    state = {'roundNo': 1, 'mapInfo': {'width': 41, 'height': 32, 'zones': []},
             'teamOur': {'type': side, 'goldNum': 75, 'totalScore': 0, 'roles': roles, 'playerTasks': []},
             'teamEnemy': {'roles': [enemy]}, 'robot': {'roles': []},
             'phaseTask': '', 'llmResp': '', 'lastCmdResult': '', 'errors': [],
             'lastRoundRoleActionResults': {}, 'worldNews': {'officialNews': '', 'folkLegends': ''},
             'vendorShopList': [{'name': k, 'price': p} for k,p in [('stone',1),('iron',3),('copper',5)]],
             'weaponShopList': [{'name': name, 'price': price}
                                for name, price in [('WeaponUpgradeVoucher1', 100),
                                                    ('WeaponUpgradeVoucher2', 150),
                                                    ('WallUpgradeVoucher1', 20),
                                                    ('WallUpgradeVoucher2', 30),
                                                    ('StationUpgradeVoucher1', 100),
                                                    ('StationUpgradeVoucher2', 150),
                                                    ('WallFixer', 10), ('Medicine', 10),
                                                    ('DizzyWeapon', 100), ('Bomb', 100),
                                                    ('SmallRobotSummonOrder', 20),
                                                    ('MiddleRobotSummonOrder', 30),
                                                    ('LargeRobotSummonOrder', 100),
                                                    ('BossRobotSummonOrder', 200)]
                                                    # 任务用品 are shop goods too
                                                    # (任务书 §4.6.3 末表), 15 each.
                                                    + [(item, TASK_ITEM_PRICE)
                                                       for item in TASK_ITEMS]],
             '_demo': {'seed': seed, 'pressure': pressure, 'mines': {}, 'dead': {},
                       'kills': 0, 'finished': False, 'waves': 0, 'elapsed': 0,
                       'errands': {}}}
    # Exclude both build rings and every occupied cell from neutral placement.
    available = free_cells(state, exclude_rings=True)
    rng.shuffle(available)
    for kind in ['vendor', 'weaponShop', 'challengerTaskPoint1', 'challengerTaskPoint2',
                 'defenderTaskPoint1', 'defenderTaskPoint2'] + ['stone']*6 + ['iron']*3 + ['copper']*3:
        p = available.pop()
        state['mapInfo']['zones'].append({'pos': p.dump(), 'neutralType': kind})
    # Seed the render identity ledger so the first round already reports moves.
    from .simulator import frame_view
    from . import taskworld, treasure
    taskworld.attach(state)
    # The treasure rite is a local fixture too: the official site, conditions and
    # timing are inferred from rumours and are not published (任务书 §5.2).
    treasure.attach(state, treasure.new_rite(seed, round_no=int(state.get('roundNo') or 1)))
    state['_demo']['vis_prev'] = frame_view(state)
    # This constructor knows it is starting a new match. Missing memory in an
    # imported snapshot remains a conservative restore, never free quota.
    from .planner import PlannerState
    state['_demo']['planner'] = PlannerState().dump()
    return state


def free_cells(state, exclude_rings=False):
    turn = Turn.load(state)
    blocked = set(turn.occupied_cells()) | set(turn.zones)
    bases = [u for u in turn.ours + turn.enemies if u.kind == 'station']
    return [Pos(x,y) for x in range(turn.width) for y in range(turn.height)
            if Pos(x,y) not in blocked and (not exclude_rings or all(
                min(distance(Pos(x,y), p) for p in station_footprint(b.pos)) > 2 for b in bases))]


def prepare_round(state, events, extra_load=None):
    """Initialize the next visible round; only generated scenarios own lifecycle.

    Returns the robots spawned this round so the viewer can animate arrivals
    that really happened instead of inventing them. ``extra_load`` carries
    summon-order robots (map of robot kind -> amount) that arrive on top of the
    base wave, exactly as the official rules describe.
    """
    meta = state.get('_demo')
    if not meta or 'seed' not in meta:
        # An imported request snapshot has no generated lifecycle: waves, mines
        # and revives are owned by the generator, so there is nothing to prepare.
        return []
    spawned = []
    n = state['roundNo']
    from .local_world_news import publish
    publish(state, events)
    rng = random.Random(meta['seed'] * 10000 + n)
    if (n-1) % 130 == 0:
        state['robot']['roles'] = []
        events.append('黎明：清除残余机器人')
    if (n-1) % 130 == 70:
        day = (n-1)//130 + 1
        available = free_cells(state, True)
        station = Turn.load(state).station()
        available = [p for p in available if 8 <= distance(p, station.pos) <= 13]
        rng.shuffle(available)
        robots = state['robot']['roles']
        plan = []
        for i in range(min(len(available), day * meta['pressure'] + 1)):
            kind = 'middleRobot' if day >= 3 and i % 3 == 0 else 'smallRobot'
            if meta['pressure'] == 3 and day >= 5 and i == 0:
                kind = 'largeRobot'
            plan.append(kind)
        for kind, amount in sorted((extra_load or {}).items()):
            if kind not in ROBOT_STATS:
                continue
            plan.extend([kind] * max(0, int(amount)))
        pending = meta.get('summon_load') or {}
        for kind, amount in sorted(pending.items()):
            if kind not in ROBOT_STATS:
                continue
            plan.extend([kind] * max(0, int(amount)))
        meta.pop('summon_load', None)
        for i, kind in enumerate(plan):
            if not available:
                break
            robot = {'id': 300000+n*100+i, 'roleType': kind, 'health': ROBOT_STATS[kind][0],
                     'pos': available.pop().dump(), 'targetTeam': state['teamOur']['type'],
                     'abnormalState': ''}
            robots.append(robot)
            spawned.append({'robot': robot['id'], 'kind': kind, 'pos': robot['pos'],
                            'health': robot['health']})
        meta['waves'] += 1
        events.append(f'第 {day} 夜：生成本地压力波次（非官方数量）')
    for unit in state['teamOur']['roles']:
        key = str(unit['id'])
        if unit['health'] <= 0 and unit['roleType'] in ('worker','pioneer'):
            if key not in meta['dead']:
                meta['dead'][key] = ((n-2)//130+1)*130+21
            if n >= meta['dead'][key]:
                base = Turn.load(state).station()
                spots = sorted(free_cells(state), key=lambda p: distance(p,base.pos))
                if spots and distance(spots[0],base.pos) <= 3:
                    unit['pos'] = spots[0].dump()
                    unit['health'] = 220 if unit['roleType']=='worker' else 200
                    del meta['dead'][key]
                    events.append(f'{key} 复活（背包保留）')
    for kind in meta.pop('refresh', []):
        available = free_cells(state, True)
        if available:
            state['mapInfo']['zones'].append({'neutralType': kind, 'pos': rng.choice(available).dump()})
    return spawned
