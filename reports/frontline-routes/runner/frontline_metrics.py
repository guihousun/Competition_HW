"""Independent read-only audit of the requested frontline strategy."""
from collections import Counter

GUNS={'rocket','railgun','gatling'}
def xy(p):return p['x'],p['y']
def dist(a,b):return max(abs(a[0]-b[0]),abs(a[1]-b[1]))
def protected_cells(obs):
    base=next((u for u in obs['teamOur']['roles'] if u['roleType']=='station'),None)
    if not base:return set()
    x,y=xy(base['pos']);w,h=obs['mapInfo']['width'],obs['mapInfo']['height']
    cx,cy=(w-1)/2,(h-1)/2
    if x+1<cx:cells={(x+3,j) for j in range(y-3,y+3)}
    elif x>cx:cells={(x-2,j) for j in range(y-3,y+3)}
    elif cy!=y-.5:
        yy=y+2 if cy>y-.5 else y-3
        cells={(i,yy) for i in range(x-2,x+4)}
    else:
        xx=x+3 if cx>=x+.5 else x-2
        cells={(xx,j) for j in range(y-3,y+3)}
    terrain={xy(z['pos']):z['neutralType'] for z in obs['mapInfo'].get('zones',[])}
    return {p for p in cells if 0<=p[0]<w and 0<=p[1]<h and terrain.get(p,'land')=='land'}

def removal_errors(obs,commands):
    roles={str(u['id']):u for u in obs['teamOur']['roles']};protected=protected_cells(obs);errors=[]
    for uid,c in commands.items():
        if c.get('action')!='remove':continue
        actor=roles.get(str(uid));targets=c.get('targetPos',[])
        if len(targets)!=1:
            errors.append({'role':str(uid),'kind':'remove_target_count'});continue
        target=xy(targets[0]);wall=next((u for u in roles.values() if u['roleType']=='wall' and u['health']>0 and xy(u['pos'])==target),None)
        if target in protected:errors.append({'role':str(uid),'kind':'protected_front_wall_remove','target':list(target)})
        if not actor or actor['roleType']!='worker' or actor['health']<=0 or dist(xy(actor['pos']),target)!=1:
            errors.append({'role':str(uid),'kind':'remove_actor_not_living_adjacent_worker'})
        if wall is None:errors.append({'role':str(uid),'kind':'remove_not_live_own_wall'})
        if (obs['roundNo']-1)%130>=70:errors.append({'role':str(uid),'kind':'remove_at_night'})
    return errors

def summarize_frontline(trace):
    day_ends={};first=None;traffic=Counter();spent=Counter();unknown=[];removals=[]
    for row in trace:
        after=[u for u in row['units_after'] if u['roleType'] in GUNS and u['health']>0]
        guns=[{'id':u['id'],'kind':u['roleType'],'level':u['level'],'pos':u['pos']} for u in sorted(after,key=lambda u:(u['pos']['y'],u['pos']['x'],u['id']))]
        if first is None and len(guns)==3 and all(u['level']==3 for u in guns):first={'after_round':row['round'],'observable_next_round':row['round']+1,'day':(row['round']-1)//130+1}
        day=(row['round']-1)//130+1
        if day<=4 and row['round']%130 in (70,0):
            day_ends[f'day{day}_'+('daylight_end' if row['round']%130==70 else 'night_end')]=guns
        for e in (row.get('traffic') or {}).get('events',[]):traffic[str(e.get('reason','unknown'))]+=1
        quotes={q['name']:q.get('price') for q in (row.get('shop_quote') or []) if isinstance(q,dict) and isinstance(q.get('name'),str)}
        for uid,c in row['response']['roleCommandMap'].items():
            if c.get('action')=='remove':removals.append({'round':row['round'],'role':str(uid),'target':c.get('targetPos'),'accepted':row['feedback'].get(str(uid))})
            if row['feedback'].get(str(uid)) is not True:continue
            action,name=c.get('action'),c.get('name','')
            if action=='build' and name in GUNS:spent['weapon_build']+=25
            if action!='buy':continue
            category=('weapon_upgrade' if name.startswith('WeaponUpgradeVoucher') else 'wall_upgrade' if name.startswith('WallUpgradeVoucher') else 'wall_repair' if name=='WallFixer' else 'other_items')
            price=quotes.get(name)
            if type(price) not in (int,float) or price<0:unknown.append({'round':row['round'],'name':name,'category':category});continue
            spent[category]+=price*c.get('num',1)
    return {'first_observed_three_level3':first,'gun_levels_at_day_boundaries':day_ends,'traffic_event_counts':dict(traffic),'successful_purchase_and_build_spend':dict(spent),'unpriced_successful_purchases':unknown,'removals':removals,'protected_wall_and_removal_errors':[{'round':r['round'],**e} for r in trace for e in r.get('removal_errors',[])],'day4_333_target_met':first is not None and first['after_round']<=460}
