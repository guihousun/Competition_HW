"""Frozen candidate: actual local series, both sides, one full day/night."""
import argparse,hashlib,json,subprocess,sys,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--output',required=True);a=p.parse_args()
src=Path(a.source).resolve();out=Path(a.output);out.mkdir(parents=True,exist_ok=False)
head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=src,text=True).strip()
assert subprocess.run(['git','diff','--quiet','HEAD'],cwd=src).returncode==0
sys.path.insert(0,str(src/'Demo/CoreGeek/src'))
from agent import debug
rows=[];start=time.monotonic()
for side in ('challenger','defender'):
    run=debug.series_payload(1,side,1,130,map_layout='attack-map-observed-v1')
    assert run['rounds']==130 and len(run['states'])==131
    assert all(s['_demo']['map_layout']['id']=='attack-map-observed-v1' for s in run['states'])
    raw=json.dumps(run,ensure_ascii=False).encode('utf-8')
    (out/(side+'.json')).write_bytes(raw)
    final=run['final'];base=next(r for r in final['teamOur']['roles'] if r['roleType']=='station')
    errors=[{'round':s['roundNo'],'errors':s['errors']} for s in run['states'] if s.get('errors')]
    failures=[{'round':frame['round'],'actor':uid,'command':frame.get('executed',{}).get(uid)}
              for frame,after in zip(run['frames'],run['states'][1:])
              for uid,ok in after.get('lastRoundRoleActionResults',{}).items() if ok is False]
    row={'side':side,'rounds':130,'base_hp':base['health'],'score':final['teamOur']['totalScore'],
         'execution_failures':failures,
         'errors':errors,'trace_sha256':hashlib.sha256(raw).hexdigest(),'trace_bytes':len(raw)}
    rows.append(row);print(json.dumps(row),flush=True)
report={'source_commit':head,'map_layout':'attack-map-observed-v1','scope':'local one day/night both sides; no real LLM or official replay parity',
        'elapsed_seconds':time.monotonic()-start,'cases':rows,'passed':all(not r['errors'] for r in rows)}
(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
sys.exit(0 if report['passed'] else 1)
