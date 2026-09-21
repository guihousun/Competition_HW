"""Read-only public trace metrics; never invoked by game physics or policy."""
from collections import Counter


def metric_identity():
    try:
        from agent import strategy_config
    except ImportError:
        return {'source':'legacy_no_strategy_config'}, False
    config = strategy_config.get()
    return strategy_config.identity(), bool(config['enabled'] and config['defense']['single_operator_three_rockets'])


def command_errors(commands, require_single):
    """Independent R01 check: one attack/other action per controller."""
    counts = Counter(str(c.get('controllerId')) for c in commands.values() if c.get('action')=='attack')
    errors = []
    for uid, count in counts.items():
        if count > 1:
            errors.append({'kind':'multiple_attack_per_controller','controller':uid,'count':count})
        if uid in {str(k) for k in commands}:
            errors.append({'kind':'controller_also_has_role_command','controller':uid})
    if require_single and sum(counts.values()) > 1:
        errors.append({'kind':'single_operator_policy_multiple_attacks','count':sum(counts.values())})
    return errors


def summarize(trace):
    nights = {}
    for row in trace:
        r = row['round']
        if (r-1)%130 < 70:
            continue
        n = str((r-1)//130+1)
        report = nights.setdefault(n, Counter())
        shared = row.get('shared_rocket_control') or {}
        maintenance = row.get('maintenance') or {}
        report['rounds'] += 1
        report['phase:'+str(shared.get('phase'))] += 1
        if shared.get('reason') in ('return_blocked','no_reachable_inner_control_cell','common_stand_unreachable') or shared.get('phase')=='return_blocked':
            report['operator_post_blocked'] += 1
        roles = {str(u['id']):u for u in row['units_before']}
        roles_after = {str(u['id']):u for u in row['units_after']}
        operator = roles.get(str(shared.get('owner')))
        base = next((u for u in roles.values() if u['roleType']=='station'),None)
        if operator and base:
            p,b = operator['pos'],base['pos']
            inside = b['x']-1<=p['x']<=b['x']+2 and b['y']-2<=p['y']<=b['y']+1
            report['operator_inside' if inside else 'operator_outside'] += 1
        guns = [u for u in roles.values() if u['roleType']=='rocket' and u['health']>0]
        commands = row['response']['roleCommandMap']
        for uid, cmd in commands.items():
            if row['feedback'].get(str(uid)) is not True:
                continue
            if cmd['action']=='attack':
                report['successful_attacks'] += 1
                actor = roles.get(str(cmd.get('controllerId')))
                if actor and len(guns)==3 and all(max(abs(actor['pos']['x']-g['pos']['x']),abs(actor['pos']['y']-g['pos']['y']))==1 for g in guns):
                    report['successful_attacks_from_common_post'] += 1
                    if roles_after.get(str(actor['id']),{}).get('pos') == actor['pos']:
                        report['successful_stationary_attacks_from_common_post'] += 1
            if cmd['action']=='use' and cmd.get('name')=='WallFixer':
                report['successful_wallfixer_use_receipts'] += 1
        report['maintenance:'+str(maintenance.get('phase'))] += 1
        if maintenance.get('use_feedback'):
            report['maintenance_use:'+maintenance['use_feedback']] += 1
    supplies = []
    for row in trace:
        before = {str(u['id']):u for u in row['units_before']}
        after = {str(u['id']):u for u in row['units_after']}
        for uid, cmd in row['response']['roleCommandMap'].items():
            if cmd.get('name')!='WallFixer' or cmd.get('action') not in ('buy','use'):
                continue
            supplies.append({'round':row['round'],'role':str(uid),'action':cmd['action'],'quantity':cmd.get('num',1),
                'accepted':row['feedback'].get(str(uid)),
                'stock_before':before.get(str(uid),{}).get('backpack',[]).count('WallFixer'),
                'stock_after':after.get(str(uid),{}).get('backpack',[]).count('WallFixer')})
    return {'nightly':{key:dict(v) for key,v in nights.items()},'wallfixer_actions':supplies,
            'control_errors':[{'round':row['round'],**e} for row in trace for e in row.get('control_errors',[])]}
