"""Seeded local fixtures; wave counts/locations are demo parameters, not official."""
import random
from copy import deepcopy
from .protocol import (TASK_ITEM_PRICE, TASK_ITEMS, Pos, Turn, distance,
                       station_footprint)
from . import wave_data, map_layout as layouts
from .wave_data import DEFAULT_PROFILE

ROBOT_STATS = {'smallRobot': (40, 5, 1), 'middleRobot': (60, 10, 2),
               'largeRobot': (500, 20, 4), 'bossRobot': (800, 40, 10)}

# User reports a central vendor and a shop northeast of it. Exact cells are
# taken from the immutable request sample, NOT asserted universal official rules.
CENTRAL_MARKET = {'vendor': Pos(20, 16), 'weaponShop': Pos(25, 20)}
DEFAULT_MARKET_LAYOUT = 'central-sample-v1'
LEGACY_MARKET_LAYOUT = 'legacy-random-v0'


def _central_market(state):
    """Move market fixtures only; keep other seeded cells unless they conflict.

    Reserve full two-cell task footprints too. No imported state is migrated:
    this runs only while constructing a new local scenario.
    """
    from .taskworld import point_cells
    turn = Turn.load(state)
    reserved = set(CENTRAL_MARKET.values())
    legal = set(free_cells({**state, 'mapInfo': {**state['mapInfo'], 'zones': []}}, exclude_rings=True))
    if not reserved <= legal:
        raise ValueError('central market cells conflict with this fixture map')
    used = set(reserved)
    zones = state['mapInfo']['zones']
    for zone in zones:
        kind, pos = zone['neutralType'], Pos.load(zone['pos'])
        if kind in CENTRAL_MARKET:
            zone['pos'] = CENTRAL_MARKET[kind].dump()
            continue
        cells = set(point_cells(kind, pos))
        if cells & used or not cells <= legal:
            candidates = sorted(legal - used, key=lambda p: (distance(pos, p), p.x, p.y))
            pos = next((p for p in candidates if set(point_cells(kind, p)) <= legal - used), None)
            if pos is None:
                raise ValueError('no room for non-overlapping neutral fixture')
            zone['pos'] = pos.dump()
            cells = set(point_cells(kind, pos))
        used.update(cells)


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


def scenario(seed=1, side='challenger', pressure=1, *, spawn_points=None,
             profile=DEFAULT_PROFILE, market_layout=DEFAULT_MARKET_LAYOUT,
             map_layout=layouts.SEEDED, task_world_profile=None):
    seed, pressure = int(seed), int(pressure)
    if side not in ('challenger', 'defender') or not 1 <= pressure <= 3:
        raise ValueError('side must be challenger/defender; pressure must be 1..3')
    profile = wave_data.validate_profile(profile)
    map_layout = layouts.validate(map_layout, default=layouts.SEEDED)
    if market_layout not in (DEFAULT_MARKET_LAYOUT, LEGACY_MARKET_LAYOUT):
        raise ValueError('unknown market layout')
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
             '_demo': {'seed': seed, 'pressure': pressure, 'profile': profile,
                       **({'task_world_profile': task_world_profile} if task_world_profile else {}),
                       'mines': {}, 'dead': {},
                       'kills': 0, 'finished': False, 'waves': 0, 'elapsed': 0,
                       'errands': {}}}
    # Exclude both build rings and every occupied cell from neutral placement.
    if map_layout == layouts.OBSERVED:
        layouts.install_observed(state, rng)
        market_layout = DEFAULT_MARKET_LAYOUT
    else:
        available = free_cells(state, exclude_rings=True)
        rng.shuffle(available)
        for kind in ['vendor', 'weaponShop', 'challengerTaskPoint1', 'challengerTaskPoint2',
                     'defenderTaskPoint1', 'defenderTaskPoint2'] + ['stone']*6 + ['iron']*3 + ['copper']*3:
            p = available.pop()
            state['mapInfo']['zones'].append({'pos': p.dump(), 'neutralType': kind})
        if market_layout == DEFAULT_MARKET_LAYOUT:
            _central_market(state)
    state['_demo']['map_layout'] = layouts.metadata(map_layout, side)
    state['_demo']['market_layout'] = {
        'id': market_layout,
        'basis': 'user_report_and_request_sample' if market_layout == DEFAULT_MARKET_LAYOUT else 'historical_local_random',
        'official_coordinates_confirmed': False,
        'positions': {z['neutralType']: dict(z['pos']) for z in state['mapInfo']['zones']
                      if z['neutralType'] in CENTRAL_MARKET}}
    # Seed the render identity ledger so the first round already reports moves.
    from .simulator import frame_view
    from . import taskworld, treasure
    taskworld.attach(state)
    # The treasure rite is a local fixture too: the official site, conditions and
    # timing are inferred from rumours and are not published (任务书 §5.2).
    treasure.attach(state, treasure.new_rite(seed, round_no=int(state.get('roundNo') or 1)))
    configure_spawns(state, spawn_points)
    state['_demo']['wave_source'] = wave_data.source_summary()
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


# Issue #48, official replay pk-696563: team 4474/defender's right-base
# attack column is x=22. User requests this observed anchor as the simulator
# default. Only that x is observed: opposite-side mirroring, row order and
# later columns remain local.
# Keep the schema stable so saved/custom pools retain their original geometry.
SPAWN_SCHEMA = 'local-fixed-spawn/1'
SPAWN_MAX_PER_COLUMN = 9
SPAWN_FIRST_COLUMN_FRACTION = 22 / 40


def spawn_row_band(center_y: int, height: int) -> list[int]:
    """The 9-row band for one column, without squashing it at the map edge.

    The band is *shifted* so it stays nine rows whenever the map is at least
    nine tall (``height >= 9``); an edge base therefore keeps a full column
    instead of collapsing to five. Only a genuinely shorter map uses fewer rows.
    Within the band, rows are ordered from the centre outward so the nearest
    slot is the one at the base's own row.
    """
    if height >= SPAWN_MAX_PER_COLUMN:
        top = min(max(center_y - SPAWN_MAX_PER_COLUMN // 2, 0),
                  height - SPAWN_MAX_PER_COLUMN)
        band = list(range(top, top + SPAWN_MAX_PER_COLUMN))
    else:
        band = list(range(height))
    return sorted(band, key=lambda y: (abs(y - center_y), y))


def spawn_column_pool(turn: Turn):
    """Fixed columns on the base's attack side: x near-to-far, <=9 y per column.

    On the official 41-column map, right bases start at observed x=22 and step
    left; left bases provisionally mirror to x=18 and step right. Scaling to
    other map widths is only a local experiment, not an official coordinate.
    Each column uses :func:`spawn_row_band`, so it keeps nine distinct rows even
    when the base sits on the top or bottom edge. Returned cells are not yet
    filtered for terrain or occupancy.
    """
    station = turn.station()
    if station is None:
        return None, None, []
    on_left = station.pos.x < turn.width / 2
    first_x = round((turn.width - 1) * (1 - SPAWN_FIRST_COLUMN_FRACTION if on_left else
                                        SPAWN_FIRST_COLUMN_FRACTION))
    direction = 1 if on_left else -1
    band = spawn_row_band(station.pos.y, turn.height)
    columns, slots = [], []
    for step in range(turn.width):
        x = first_x + direction * step
        if not 0 <= x < turn.width:
            break
        columns.append(x)
        slots.extend(Pos(x, y) for y in band)
    return first_x, columns, slots


def configure_spawns(state, points=None):
    """Freeze one local spawn pool for the whole match (Issue #12/#19).

    Default: a fixed column array, never a nightly ring sample and never a
    four-side siege. Static neutral cells and buildings are excluded when the
    pool is built; a slot that is occupied later is skipped at spawn time and the
    shortage is reported instead of spreading outside the pool. Caller-supplied
    ``spawn_points`` are kept exactly in the given order and labelled as custom
    local geometry.
    """
    turn = Turn.load(state)
    station = turn.station()
    if station is None:
        raise ValueError('spawn configuration needs a live station')
    owner = str((state.get('teamOur') or {}).get('type') or '')
    if points is not None:
        if (not isinstance(points, list) or not points or len(points) > turn.width * turn.height
                or any(not isinstance(p, dict) or set(p) != {'x', 'y'}
                       or type(p['x']) is not int or type(p['y']) is not int
                       or not 0 <= p['x'] < turn.width or not 0 <= p['y'] < turn.height for p in points)):
            raise ValueError('spawn_points must be in-map integer coordinates')
        slots = [Pos.load(p) for p in points]
        if len(set(slots)) != len(slots):
            raise ValueError('duplicate spawn point')
        layout = {'schema': SPAWN_SCHEMA, 'center': slots[0].dump(),
                  'slots': [p.dump() for p in slots], 'columns': [],
                  'max_per_column': None, 'custom': True, 'ownerTeam': owner,
                  'source': 'explicit_local_configuration',
                  'geometry': '调用方给定的本地坐标，顺序保持不变（非官方几何）'}
    else:
        first_x, columns, candidates = spawn_column_pool(turn)
        free = set(free_cells(state))
        slots = [p for p in candidates if p in free]
        layout = {'schema': SPAWN_SCHEMA, 'center': Pos(first_x, station.pos.y).dump(),
                  'slots': [p.dump() for p in slots], 'columns': columns,
                  'max_per_column': SPAWN_MAX_PER_COLUMN, 'custom': False, 'ownerTeam': owner,
                  'source': 'issue48_pk696563_first_x22_rows_and_mirror_provisional',
                  'observed_first_column': 22,
                  'source_replay': 'pk-696563',
                  'geometry': '右侧基地来袭首列 x=22（Issue48 回放摘录）；左侧镜像 x=18、纵向排列与后续列仍为本地补全'}
    layout['occupancy'] = 'skip occupied fixed slots; report shortages; never spread outside pool'
    layout['pool_size'] = len(layout['slots'])
    layout['min_pool_size'] = wave_data.DEFAULT_POOL_REQUIRED
    state['_demo']['spawn_layout'] = layout
    return layout


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
        # A snapshot generated before Issue19 has no `profile`. It was produced by
        # the old day*pressure+1 experiment, so migrate it to that profile and say
        # so — silently switching an old recording to the observed counts would
        # change its wave sizes while still calling the replay "consistent".
        legacy_profile = meta.get('profile') is None
        profile = wave_data.PROFILE_PRESSURE if legacy_profile else meta.get('profile')
        profile = wave_data.validate_profile(profile)
        if legacy_profile:
            meta['profile'] = profile
            meta['profile_migration'] = 'legacy snapshot had no wave profile; kept as local-pressure'
            events.append('旧快照缺少波次 profile：保留旧本地压力实验身份（day*pressure+1），'
                          '未改标为附件实测')
        layout = meta.get('spawn_layout')
        if not isinstance(layout, dict) or layout.get('schema') != SPAWN_SCHEMA:
            layout = configure_spawns(state)  # old generated snapshots migrate once
            meta['layout_migration'] = 'legacy snapshot had no fixed spawn layout; rebuilt as local geometry'
        free = set(free_cells(state, True))
        available = [Pos.load(p) for p in layout['slots'] if Pos.load(p) in free]
        robots = state['robot']['roles']
        extras = []
        for kind, amount in sorted((extra_load or {}).items()):
            if kind in ROBOT_STATS:
                extras.extend([kind] * max(0, int(amount)))
        pending = meta.get('summon_load') or {}
        for kind, amount in sorted(pending.items()):
            if kind in ROBOT_STATS:
                extras.extend([kind] * max(0, int(amount)))
        meta.pop('summon_load', None)
        plan, wave_meta = wave_data.wave_plan(profile, day, meta.get('pressure', 1), extras)
        meta['wave'] = wave_meta
        for i, kind in enumerate(plan):
            if not available:
                break
            robot = {'id': 300000+n*100+i, 'roleType': kind, 'health': ROBOT_STATS[kind][0],
                     'pos': available.pop(0).dump(), 'targetTeam': state['teamOur']['type'],
                     'abnormalState': ''}
            robots.append(robot)
            spawned.append({'robot': robot['id'], 'kind': kind, 'pos': robot['pos'],
                            'health': robot['health']})
        meta['waves'] += 1
        meta['spawn_shortfall'] = len(plan) - len(spawned)
        if profile == wave_data.PROFILE_PRESSURE:
            events.append(f'第 {day} 夜：本地旧压力波次 {wave_meta["formula"]}'
                          f'（{wave_meta["base_count"]} 只基础，非官方数量）')
        elif wave_meta.get('observed'):
            events.append(f'第 {day} 夜：附件实测 {wave_meta["base_count"]} 只'
                          f'（Issue19 default.xlsx Sheet1）')
        else:
            events.append(f'第 {day} 夜：未观测（附件第8–10天为空）：本地续演假设每夜多 '
                          f'{wave_meta["local_future_small_per_day"]} 只小型，共 '
                          f'{wave_meta["base_count"]} 只；非官方统计')
        if wave_meta.get('blank_types_zeroed'):
            blanks = '、'.join(wave_meta['blank_types_zeroed'])
            events.append(f'附件中 {blanks} 为空白：本地运行暂按 0 解释（假设，非实测）')
        events.append(f'固定刷新区落位（精确格子与红方镜像为本地几何）；'
                      f'每列≤{layout.get("max_per_column") or 9}')
        if meta['spawn_shortfall']:
            events.append(f"固定刷新格被占用或不足，少生成{meta['spawn_shortfall']}只；占格处理为本地假设")
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
