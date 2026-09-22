"""Local map fixtures, with observed cells separated from unresolved evidence.

Issue #46 describes replay-grid coordinates for five attack_map starts. Public
protocol coordinates have a bottom-left origin (R02); this module never changes
an imported observation or supplies map knowledge to the competition policy.
"""
from .protocol import Pos

SEEDED = 'seeded-local-v1'
OBSERVED = 'attack-map-observed-v1'
DEFAULT = OBSERVED
LAYOUTS = (OBSERVED, SEEDED)
BASES = {'challenger': (9, 22), 'defender': (30, 10)}
NEUTRALS = {
    'vendor': (20, 16), 'weaponShop': (25, 20),
    'challengerTaskPoint1': (14, 14), 'challengerTaskPoint2': (16, 17),
    'defenderTaskPoint1': (23, 14), 'defenderTaskPoint2': (26, 17),
}
UNRESOLVED_GRID = {
    23: ((2, 7), (32, 12), (38, 13), (12, 14), (1, 15), (33, 27)),
    24: ((9, 1), (35, 1), (35, 24), (30, 27)),
    25: ((27, 0), (40, 11), (1, 21), (15, 29)),
}


def validate(value, default=DEFAULT):
    value = default if value is None else value
    if value not in LAYOUTS:
        raise ValueError('unknown map layout')
    return value


def grid_to_protocol(x, y):
    """41x32 replay row index -> protocol coordinates, no side mirroring."""
    if not 0 <= x < 41 or not 0 <= y < 32:
        raise ValueError('replay grid position outside 41x32')
    return {'x': x, 'y': 31 - y}


def metadata(layout, side):
    if layout == SEEDED:
        return {'id': layout, 'label': '随机压力地图', 'official_certified': False,
                'evidence_scope': '历史本地种子布局；不代表官方首帧',
                'confirmed': [], 'unconfirmed': ['基地与任务点为本地生成', '地形、资源、出生与刷怪位置未校准']}
    return {
        'id': layout, 'label': 'attack_map 已观测布局', 'official_certified': False,
        'evidence_scope': 'Issue #46：5 场 attack_map 首帧摘录，非其他地图或完整官方复现',
        'source_issue': 'https://github.com/guihousun/Competition_HW/issues/46',
        'replay_ids': [691534, 692108, 692068, 692369, 692069],
        'coordinate_transform': 'protocol_y = 31 - replay_grid_y',
        'confirmed': ['双方基地 2×2', '小贩与武器商店', '双方任务点',
                      'challenger 初始角色', 'defender 一名工人 (30,8)'],
        'unconfirmed': ['数字类型 23/24/25 未解析，不据此增加阻挡',
                        '矿点仍为本地随机生成', '完整地形与建造区域未确认',
                        'defender 其余角色为本地补位', '刷怪左首列按 Issue48 x=22；镜像、纵向与后续列待核验'],
        'unresolved_cells': [
            {'mapType': kind, 'grid': {'x': x, 'y': y},
             'pos': grid_to_protocol(x, y), 'status': 'unresolved_not_applied'}
            for kind, cells in UNRESOLVED_GRID.items() for x, y in cells],
        'local_role_fallbacks': ({'worker': {'x': 29, 'y': 10},
                                  'pioneer': {'x': 29, 'y': 11}}
                                 if side == 'defender' else {}),
        'local_role_ids': True,
    }


def install_observed(state, rng):
    """Construct a NEW observed-layout fixture before tasks/spawns are attached."""
    from .scenarios import free_cells
    from .taskworld import point_cells

    side = state['teamOur']['type']
    positions = ([(9, 22), (8, 21), (8, 23), (8, 22)] if side == 'challenger'
                 else [(30, 10), (30, 8), (29, 11), (29, 10)])
    for role, (x, y) in zip(state['teamOur']['roles'], positions):
        role['pos'] = {'x': x, 'y': y}
    enemy_side = 'defender' if side == 'challenger' else 'challenger'
    x, y = BASES[enemy_side]
    state['teamEnemy']['roles'][0]['pos'] = {'x': x, 'y': y}
    zones = state['mapInfo']['zones']
    zones[:] = [{'neutralType': kind, 'pos': {'x': x, 'y': y}}
                for kind, (x, y) in NEUTRALS.items()]
    reserved = {cell for kind, (x, y) in NEUTRALS.items()
                for cell in point_cells(kind, Pos(x, y))}
    available = [p for p in free_cells(state, exclude_rings=True) if p not in reserved]
    rng.shuffle(available)
    for kind in ['stone'] * 6 + ['iron'] * 3 + ['copper'] * 3:
        zones.append({'neutralType': kind, 'pos': available.pop().dump()})
