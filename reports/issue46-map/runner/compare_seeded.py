"""Compare public initial states across old and candidate source trees."""
import argparse,json,subprocess,sys
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--base',required=True);p.add_argument('--candidate',required=True);p.add_argument('--output',required=True);a=p.parse_args()
script='''import sys,json,hashlib
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'Demo/CoreGeek/src'))
from agent.scenarios import scenario,observation
rows=[]
for seed in (1,7,19):
 for side in ('challenger','defender'):
  data=observation(scenario(seed,side))
  rows.append({'seed':seed,'side':side,'public_sha256':hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()})
print(json.dumps(rows))
'''
def run(folder):return json.loads(subprocess.check_output([sys.executable,'-X','utf8','-c',script],cwd=folder,text=True,encoding='utf-8'))
base,candidate=run(a.base),run(a.candidate)
report={'base':subprocess.check_output(['git','rev-parse','HEAD'],cwd=a.base,text=True).strip(),
        'candidate':subprocess.check_output(['git','rev-parse','HEAD'],cwd=a.candidate,text=True).strip(),
        'base_cases':base,'candidate_cases':candidate,'passed':base==candidate}
Path(a.output).write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report));sys.exit(0 if report['passed'] else 1)
