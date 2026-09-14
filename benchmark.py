"""Run seeded matches through the real HTTP policy; save auditable summaries."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import threading
import time
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'Demo/CoreGeek/src'))
from agent.server import Handler
from agent.scenarios import scenario, observation
from agent.simulator import step


MATERIALS = ('stone', 'iron', 'copper')
TOWER_KINDS = ('gatling', 'railgun', 'rocket')
BUILDING_MAX_HEALTH = {
    'gatling': (1000, 1500, 2000), 'railgun': (1000, 1500, 2000),
    'rocket': (1000, 1500, 2000), 'wall': (1000, 1500, 2000),
    'station': (1500, 3000, 4500),
}
# Voucher -> (allowed building kinds, required current level), 任务书 §4.6.3.
VOUCHERS = {
    'WeaponUpgradeVoucher1': (TOWER_KINDS, 1),
    'WeaponUpgradeVoucher2': (TOWER_KINDS, 2),
    'WallUpgradeVoucher1': (('wall',), 1),
    'WallUpgradeVoucher2': (('wall',), 2),
    'StationUpgradeVoucher1': (('station',), 1),
    'StationUpgradeVoucher2': (('station',), 2),
}
TARGETED_ITEMS = set(VOUCHERS) | {'WallFixer', 'DizzyWeapon', 'Bomb'}


def _angle_between(first, second):
    """Angle in degrees between two 2D vectors, computed independently."""
    import math
    dot = first[0] * second[0] + first[1] * second[1]
    norm = math.hypot(*first) * math.hypot(*second)
    if norm == 0:
        return 180.0
    return math.degrees(math.acos(max(-1.0, min(1.0, dot / norm))))


def own_task_cells(payload):
    """Cells a pioneer may accept from: the one-cell ring of our own points.

    Task point 2 spans two cells (任务书 §4.6.2), so its right neighbour counts too.
    """
    team = payload['teamOur']['type']
    cells = []
    for zone in payload['mapInfo']['zones']:
        kind = zone['neutralType']
        if not (kind.startswith(team) and 'TaskPoint' in kind):
            continue
        base = zone['pos']
        cells.append(base)
        if kind.endswith('TaskPoint2'):
            cells.append({'x': base['x'] + 1, 'y': base['y']})
    return cells


def may_accept_from(payload, pos):
    return any(max(abs(pos['x'] - cell['x']), abs(pos['y'] - cell['y'])) <= 1
               for cell in own_task_cells(payload))


def audit(payload, commands):
    """Independent structural/action precondition checks, not a win oracle.

    Written from the official text only (任务书 §4.4/§4.5/§4.6, 接口文档 §2): it
    deliberately does not import the agent's helpers, so a shared bug cannot
    make both sides agree.
    """
    errors = []
    roles = {str(r['id']): r for r in payload['teamOur']['roles']}
    used = set()
    width, height = payload['mapInfo']['width'], payload['mapInfo']['height']
    day = (payload['roundNo'] - 1) % 130 < 70
    budget = payload['teamOur']['goldNum']
    builds = 0
    vendor_prices = {z['name']: z['price'] for z in payload.get('vendorShopList') or []}
    shop_prices = {z['name']: z['price'] for z in payload.get('weaponShopList') or []}
    vendor_cells = [z['pos'] for z in payload['mapInfo']['zones'] if z['neutralType'] == 'vendor']
    shop_cells = [z['pos'] for z in payload['mapInfo']['zones'] if z['neutralType'] == 'weaponShop']
    buildings = {tuple(sorted(((r['pos']['x']+dx, r['pos']['y']+dy) for dx in (0, 1) for dy in (0, -1))))
                 if r['roleType'] == 'station' else ((r['pos']['x'], r['pos']['y']),): r
                 for r in roles.values() if r['health'] > 0}

    def dist(a, b):
        return max(abs(a['x']-b['x']), abs(a['y']-b['y']))

    def building_at(target):
        for cells, role in buildings.items():
            if (target['x'], target['y']) in cells:
                return role
        return None

    for uid, c in commands.items():
        u = roles.get(uid)
        if not u or u['health'] <= 0:
            errors.append('unknown/dead issuer'); continue
        action = c.get('action')
        if action not in ('move', 'collect', 'build', 'attack', 'sell', 'buy', 'use',
                          'remove', 'acceptTask', 'submitAnswer', 'summonTreasure'):
            errors.append('unsupported action'); continue
        targets = c.get('targetPos') or []
        if action in ('sell', 'buy', 'remove', 'use') and not targets:
            # sell/buy may omit targetPos; the role's own position is the target.
            targets = [u['pos']] if action in ('sell', 'buy') else targets
        if action == 'summonTreasure':
            # 任务书 §4.6: pioneer only, sacrifice 任务用品 in a cell within one step.
            # The site/conditions themselves come from the local fixture, so only
            # the published preconditions are checked here.
            if u['roleType'] != 'pioneer':
                errors.append('summonTreasure by non-pioneer')
            if len(targets) != 1 or dist(u['pos'], targets[0]) > 1:
                errors.append('summonTreasure without an adjacent site')
            offered = c.get('item') or []
            if isinstance(offered, str):
                offered = [offered]
            if not offered:
                errors.append('summonTreasure without items')
            continue
        if action in ('acceptTask', 'submitAnswer'):
            # 任务书 §4.6.2 / 接口文档 §2.3: pioneer only, and only while a task is
            # published. `submitAnswer` carries an answer string, not a position.
            if u['roleType'] != 'pioneer':
                errors.append('task action by non-pioneer')
            if action == 'acceptTask':
                if not may_accept_from(payload, u['pos']):
                    errors.append('accept without an own task point in range')
            elif not str(c.get('taskAnswer') or '').strip():
                errors.append('empty task answer')
            continue
        if not targets or any(not isinstance(p.get('x'), int) or not isinstance(p.get('y'), int)
                              or not 0 <= p['x'] < width or not 0 <= p['y'] < height for p in targets):
            errors.append('invalid targets'); continue
        if action in ('move', 'collect', 'build', 'remove'):
            if len(targets) != 1 or dist(u['pos'], targets[0]) != 1:
                errors.append('nonadjacent action')
            if u['roleType'] not in ('worker', 'pioneer') or (action in ('build', 'collect', 'remove') and u['roleType'] != 'worker'):
                errors.append('wrong role')
        if action == 'build':
            if not day: errors.append('night build')
            base = next(r for r in roles.values() if r['roleType'] == 'station')['pos']
            footprint = [{'x': base['x']+dx, 'y': base['y']+dy} for dx in (0, 1) for dy in (0, -1)]
            ring = min(dist(targets[0], p) for p in footprint)
            if ring != (2 if c.get('name') == 'wall' else 1): errors.append('local build ring')
            if c.get('name') != 'wall':
                budget -= 25; builds += 1
            elif 'stone' not in u.get('backpack', []): errors.append('missing stone')
        if action == 'sell':
            amount = int(c.get('num') or 1)
            name = c.get('name')
            if name not in vendor_prices: errors.append('vendor does not buy material')
            if amount < 1 or u.get('backpack', []).count(name) < amount:
                errors.append('sell amount exceeds backpack')
            if not any(dist(u['pos'], cell) <= 1 for cell in vendor_cells):
                errors.append('sell without vendor adjacency')
            budget += vendor_prices.get(name, 0) * amount
        if action == 'buy':
            amount = int(c.get('num') or 1)
            name = c.get('name')
            if name not in shop_prices: errors.append('shop does not sell item')
            if amount < 1: errors.append('buy amount')
            if len(u.get('backpack', [])) + amount > u.get('backPackCapability', 0):
                errors.append('buy exceeds backpack capacity')
            if not any(dist(u['pos'], cell) <= 1 for cell in shop_cells):
                errors.append('buy without shop adjacency')
            budget -= shop_prices.get(name, 0) * amount
        if action == 'use':
            name = c.get('name')
            if name not in u.get('backpack', []): errors.append('use item not in backpack')
            if name in TARGETED_ITEMS and not c.get('targetPos'): errors.append('use needs target')
            if name in VOUCHERS:
                kinds, level = VOUCHERS[name]
                victim = building_at(targets[0])
                if victim is None: errors.append('voucher without target building')
                elif victim['roleType'] not in kinds: errors.append('voucher wrong building kind')
                elif max(1, victim.get('level', 1)) != level: errors.append('voucher wrong level')
                elif min(dist(u['pos'], p) for p in
                         [{'x': victim['pos']['x']+dx, 'y': victim['pos']['y']+dy}
                          for dx in ((0, 1) if victim['roleType'] == 'station' else (0,))
                          for dy in ((0, -1) if victim['roleType'] == 'station' else (0,))]) > 1:
                    errors.append('voucher without adjacency')
            if name in ('DizzyWeapon', 'Bomb'):
                center = targets[0]
                if not any(dist(r['pos'], center) <= 1 for r in (payload.get('robot') or {}).get('roles') or []):
                    errors.append('battle item with no robot in blast')
        if action == 'remove':
            victim = building_at(targets[0])
            if victim is None or victim['roleType'] != 'wall':
                errors.append('remove without wall target')
        if action == 'attack':
            owner = str(c.get('controllerId'))
            if day or owner in used or owner in commands or owner not in roles or roles[owner]['health'] <= 0 or dist(roles[owner]['pos'], u['pos']) > 1 or u.get('cooldown', 0) > 0:
                errors.append('invalid controller/time/cooldown')
            used.add(owner)
            kind, level = u['roleType'], max(1, min(u.get('level', 1), 3))
            ranges = {'gatling': [3, 5, 7], 'railgun': [6, 8, 10], 'rocket': [10, 15, 10**9]}
            if kind not in ranges:
                errors.append('nonweapon attack')
            elif len(targets) != (1 if kind == 'railgun' else level) or any(dist(u['pos'], p) > (u.get('attackRange') or ranges[kind][level-1]) for p in targets):
                errors.append('attack range/count')
            elif kind == 'gatling' and len(targets) > 1:
                # 任务书 §4.5.4: every pair of aim directions must fit inside one
                # 90° cone, otherwise the whole attack is illegal. Recomputed here
                # from the published positions so it is not our own helper again.
                vectors = [(p['x'] - u['pos']['x'], p['y'] - u['pos']['y']) for p in targets]
                for i in range(len(vectors)):
                    if vectors[i] == (0, 0):
                        errors.append('bullet aimed at the tower')
                        break
                    for j in range(i + 1, len(vectors)):
                        if _angle_between(vectors[i], vectors[j]) > 90 + 1e-6:
                            errors.append('gatling cone wider than 90')
                            break
        if action == 'collect':
            mines = [z for z in payload['mapInfo']['zones'] if z['neutralType'] in ('stone', 'iron', 'copper')]
            if not any(z['pos'] == targets[0] for z in mines) or len(u.get('backpack', [])) >= u.get('backPackCapability', 100):
                errors.append('invalid mine/capacity')
    if budget < 0: errors.append('overspend')
    if builds + sum(r['health'] > 0 and r['roleType'] in ('gatling', 'railgun', 'rocket') for r in roles.values()) > 3:
        errors.append('tower limit')
    return errors


def sanitize(value, *, depth=0):
    """Make local state JSON-safe without dropping what a report needs.

    The simulator keeps live objects (planner state) and UI-only render data
    under its private keys; a replay file should carry the observable match, so
    objects become a readable placeholder instead of killing the report.
    """
    if depth > 12:
        return '<deep>'
    if isinstance(value, dict):
        return {key: sanitize(item, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item, depth=depth + 1) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return f'<{type(value).__name__}>'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seeds', default='1,7,19')
    parser.add_argument('--rounds', type=int, default=1300)
    parser.add_argument('--pressure', type=int, default=1)
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    output = ROOT / 'reports'
    output.mkdir(exist_ok=True)
    summaries = []
    try:
        for seed in map(int,args.seeds.split(',')):
            for side in ('challenger','defender'):
                state = scenario(seed,side,args.pressure)
                initial = sanitize(state)
                actions, errors, failed = Counter(), [], 0
                times, frames = [], []
                rewards = []
                for _ in range(min(args.rounds,1300)):
                    request = observation(state)
                    started = time.perf_counter()
                    with urlopen(Request(f'http://127.0.0.1:{server.server_port}/',data=json.dumps(request).encode(),headers={'Content-Type':'application/json'}),timeout=5) as r:
                        response = json.load(r)
                    times.append((time.perf_counter()-started)*1000)
                    commands = response['roleCommandMap']
                    errors.extend(audit(request, commands))
                    actions.update(c['action'] for c in commands.values())
                    outcome = step(state,commands)
                    failed += sum(not v for v in outcome['state']['lastRoundRoleActionResults'].values())
                    frames.append({'round':state['roundNo'], 'commands':commands,'events':outcome['events']})
                    state = outcome['state']
                    report = (state.get('_demo') or {}).get('task_report') or {}
                    if report.get('accepted'):
                        actions['acceptTask'] += 1
                    if report.get('rewards'):
                        rewards.append({'round': state['roundNo'], **report['rewards']})
                    if outcome['done']: break
                summary = {'seed':seed,'side':side,'pressure':args.pressure,'rounds':len(times),
                           'terminal':state['_demo']['finished'],'base_hp':next(u['health'] for u in state['teamOur']['roles'] if u['roleType']=='station'),
                           'score_local':state['teamOur']['totalScore'],'kills':state['_demo']['kills'],
                           'waves':state['_demo']['waves'],'actions':dict(actions),
                           'task_rewards':[{'round': r['round'], 'rate': round(r['rate'], 3),
                                            'score': r['score'], 'gold': r['gold']} for r in rewards],
                           'audit_errors':dict(Counter(errors)),'execution_failures':failed,
                           'max_http_ms':round(max(times),2)}
                summaries.append(summary)
                (output / f'replay-{seed}-{side}-p{args.pressure}.json').write_text(
                    json.dumps({'initial':initial,'frames':frames,'final':sanitize(state),
                                'task_rewards':sanitize(rewards)},ensure_ascii=False,indent=2),
                    encoding='utf-8')
                print(json.dumps(summary,ensure_ascii=False),flush=True)
    finally:
        server.shutdown(); server.server_close(); thread.join()
    (output / f'benchmark-p{args.pressure}.json').write_text(json.dumps(summaries,ensure_ascii=False,indent=2),encoding='utf-8')
    if any(r['audit_errors'] for r in summaries):
        raise SystemExit('Action audit failed; see reports')


if __name__ == '__main__':
    main()
