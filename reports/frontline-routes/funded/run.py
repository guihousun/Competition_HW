"""Explicit funded synthetic acceptance, not an official match/income claim."""
import json,sys,hashlib,subprocess
from pathlib import Path
from copy import deepcopy
ROOT=Path('D:/Research_vault/work/projects/Code_HW/.workflow/worktrees/frontline-routes')
INITIAL_LEVEL=1 if '--from-level1' in sys.argv else 2
INITIAL_GOLD=750 if INITIAL_LEVEL==1 else 450
OUT=Path(__file__).parent / ('from-level1' if INITIAL_LEVEL==1 else '.')
OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(ROOT/'Demo/CoreGeek/src'))
from agent import scenarios,simulator,brain,planner,defense_layout
from agent.protocol import Pos,Turn

def save(name,obj):
    (OUT/name).write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')

def role(uid,kind,x,y,hp,level=1):
    return dict(id=uid,roleType=kind,pos=dict(x=x,y=y),health=hp,level=level,
                backpack=[],backPackCapability=100,cooldown=0)

state=scenarios.scenario(1,'challenger')
state.update(roundNo=261,phaseTask='',worldNews={'officialNews':'','folkLegends':''})
state['teamOur'].update(goldNum=INITIAL_GOLD,totalScore=0,playerTasks=[])
state['teamOur']['roles']=[role(10013,'station',9,22,1500),
    role(10010,'worker',8,22,220),role(10012,'worker',9,20,220),
    role(10011,'pioneer',13,25,0),
    role(10101,'rocket',8,21,1000 if INITIAL_LEVEL==1 else 1500,INITIAL_LEVEL),role(10102,'rocket',8,23,1000 if INITIAL_LEVEL==1 else 1500,INITIAL_LEVEL),
    role(10103,'rocket',9,23,1000 if INITIAL_LEVEL==1 else 1500,INITIAL_LEVEL)]
state['mapInfo']['zones']=[dict(pos=dict(x=5,y=22),neutralType='weaponShop')]
layout=defense_layout.layout(Pos(9,22),41,32)
state['teamOur']['roles'].extend(role(10200+i,'wall',p.x,p.y,1000) for i,p in enumerate(layout.wall_order))
state['weaponShopList']=[dict(name='WeaponUpgradeVoucher2',price=150)]
if INITIAL_LEVEL==1:state['weaponShopList'].append(dict(name='WeaponUpgradeVoucher1',price=100))
state['vendorShopList']=[]
state['robot']={'roles':[]}
save('initial-public.json',scenarios.observation(state))
memory=planner.PlannerState();rows=[];failure=None;first333=None
source=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
assert source=='b53365553f3903efd610fb1e4e34de755fa43468'
before={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'Demo/CoreGeek/src/agent').glob('*.py')}
simulator.plan_for_state=lambda *a,**k: (_ for _ in ()).throw(AssertionError('second policy not allowed'))
for _ in range(70):
    public=scenarios.observation(state)
    assert '_demo' not in public and '_engine' not in public
    memory.note_round(public['roundNo'])
    memory.note_results(public,public['roundNo'],routed=brain.llm_router_enabled())
    response=brain.plan_for_state(public,memory,judge_tasks=False).build()
    memory.note_submission(response.get('prompt'),response.get('executeCmd'),public['roundNo'],bool(memory.tasks.get('cycle')))
    memory.last_round=public['roundNo']
    result=simulator.step(state,external_response=response);state=result['state']
    guns=[r for r in state['teamOur']['roles'] if r['roleType']=='rocket']
    rows.append(dict(round=public['roundNo'],commands=response['roleCommandMap'],
         levels=[r['level'] for r in guns],gold=state['teamOur']['goldNum'],
         outcomes=state.get('lastRoundRoleActionResults'),skipped=result.get('frame',{}).get('skipped'),
         events=result['events'],upgrade=brain.decision_report().get('upgrade_itinerary') if brain.decision_report() else None))
    if all(r['level']==3 for r in guns):first333=public['roundNo'];break
    if result.get('done'):break
buys=[(r['round'],c) for r in rows for c in r['commands'].values() if c['action']=='buy']
uses=[(r['round'],c) for r in rows for c in r['commands'].values() if c['action']=='use']
invalid=[r for r in rows if r['skipped'] or any(v is False for v in (r['outcomes'] or {}).values())]
after={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'Demo/CoreGeek/src/agent').glob('*.py')}
quantity=6 if INITIAL_LEVEL==1 else 3
report=dict(scope=f'Synthetic funded public-state progression; {INITIAL_GOLD} gold and three level{INITIAL_LEVEL} rockets supplied ONLY at initial round261. Accessible nearby shop; full existing wall ring; pioneer initially dead. No claim of earned income or official match PASS.',source=source,initial_gold=INITIAL_GOLD,expected_upgrade_quantity=quantity,expected_upgrade_cost=INITIAL_GOLD,
    first_all_three_level3_round=first333,rounds=len(rows),buys=buys,uses=uses,final_gold=state['teamOur']['goldNum'],
    invalid_rounds=[r['round'] for r in invalid],shots=sum(c['action']=='attack' for r in rows for c in r['commands'].values()),sources_unchanged=before==after,
    passed=first333 is not None and first333<=520 and len(buys)==quantity and len(uses)==quantity and state['teamOur']['goldNum']==0 and not invalid and before==after)
save('trace.json',rows);save('result.json',report);print(json.dumps(report,ensure_ascii=False))

