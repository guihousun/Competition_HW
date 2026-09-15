"""Actual archive HTTP task-pipeline probe; synthetic model and virtual tools."""
import argparse,hashlib,json,os,socket,subprocess,sys,tarfile,tempfile,time
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'Demo/CoreGeek/src'));sys.path.insert(0,str(ROOT/'tests'))
from agent.local_task_sandbox import execute
from test_tasks import observation
p=argparse.ArgumentParser();p.add_argument('--archive',required=True);p.add_argument('--output',required=True)
a=p.parse_args();archive=Path(a.archive).resolve();output=Path(a.output).resolve()
expected=archive.with_suffix('.gz.sha256').read_text().split()[0]
assert hashlib.sha256(archive.read_bytes()).hexdigest()==expected
rows=[]
with tempfile.TemporaryDirectory() as tmp:
    with tarfile.open(archive) as bundle: bundle.extractall(tmp,filter='data')
    package=Path(tmp)/'CoreGeek'
    source=json.loads((package/'submission-manifest.json').read_text())['commit']
    with socket.socket() as sock: sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env={k:v for k,v in os.environ.items() if not k.endswith('_API_KEY')}
    env.update(PYTHONIOENCODING='utf-8',COMPETITION_HW_TASK_AGENT='on',COMPETITION_HW_WORLD_AGENT='off',
        COMPETITION_HW_TRACE_DIR=str(Path(tmp)/'traces'))
    with (Path(tmp)/'service.log').open('wb') as log:
        process=subprocess.Popen([sys.executable,str(package/'main3.py'),str(port)],cwd=tmp,env=env,
            stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            url='http://127.0.0.1:'+str(port)+'/'
            for attempt in range(100):
                try:
                    urlopen(url,timeout=.2).close();break
                except HTTPError: break
                except URLError: time.sleep(.05)
            else: raise RuntimeError('server not ready')
            for index,side in enumerate(('challenger','defender')):
                fixture={'cwd':'/','files':{
                    '/tmp/varied/task_sample.md':'Read API_DOCS.md. Query area; return exactly JSON value.',
                    '/tmp/varied/API_DOCS.md':'python3 /api/records.py --area requested; returns value.'},
                    'programs':{'/api/records.py':{'filters':['area'],'required_filters':['area'],
                        'records':[{'area':'current','value':17+index}],'output_fields':['value']}}}
                llm=cmd='';kind_index=0;trace=[]
                for n in range(1,6):
                    payload=observation(round_no=n,team=side,role_pos=(6,5),
                        phase_task='请阅读task_sample.md，获取任务信息',llm_resp=llm,cmd_result=cmd,timeout_rounds=15)
                    start=time.perf_counter()
                    with urlopen(Request(url,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'}),timeout=5) as res:
                        decision=json.load(res)
                    ms=(time.perf_counter()-start)*1000
                    assert set(decision)<= {'roleCommandMap','prompt','executeCmd'}
                    trace.append({'round':n,'ms':ms,'prompt':bool(decision.get('prompt')),
                        'command':bool(decision.get('executeCmd')),'actions':decision['roleCommandMap']})
                    llm=cmd=''
                    if n==1: assert decision.get('executeCmd','').startswith('# task-workspace/1')
                    if decision.get('executeCmd'): cmd=execute(decision['executeCmd'],fixture,active=True)
                    if decision.get('prompt'):
                        import re
                        token=re.search(r'"request_id":"([^"]+)"',decision['prompt']).group(1)
                        ids=json.loads(re.search(r'可引用证据ID：(\[[^\n]*\])',decision['prompt']).group(1))
                        plan={'kind':'run','command':'python3 /api/records.py --area current'} if kind_index==0 else {
                            'kind':'answer','answer':json.dumps({'value':17+index})}
                        kind_index+=1;plan['evidence_ids']=ids
                        llm=json.dumps({'request_id':token,'plan':plan})
                submitted=[v for v in decision['roleCommandMap'].values() if v.get('action')=='submitAnswer']
                assert len(submitted)==1 and json.loads(submitted[0]['taskAnswer'])=={'value':17+index}
                assert kind_index==2
                rows.append({'side':side,'passed':True,'trace':trace})
        finally:
            process.terminate()
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired: process.kill();process.wait()
report={'scope':'Actual tar.gz main3 HTTP; scripted model/virtual tools, not intranet PASS',
    'source_commit':source,'archive_sha256':expected,'passed':len(rows)==2,'cases':rows}
output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'passed':report['passed'],'requests':10,'source':source,'http_max_ms':max(t['ms'] for r in rows for t in r['trace'])}))
