"""Actual package HTTP regression; model and API replies are labelled fixtures."""
import argparse,hashlib,json,os,re,socket,subprocess,sys,tarfile,tempfile,time
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import URLError

p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--archive',required=True);p.add_argument('--output',required=True)
a=p.parse_args();source=Path(a.source);sys.path[:0]=[str(source/'tests'),str(source/'Demo/CoreGeek/src')]
from test_tasks import observation
from test_issue28_task_semantics import TEMPLATE,data_doc,answer
from agent import local_task_sandbox

archive=Path(a.archive);cases=[]
with tempfile.TemporaryDirectory() as temp:
    with tarfile.open(archive) as tar:tar.extractall(temp,filter='data')
    package=Path(temp)/'CoreGeek'
    manifest=json.loads((package/'submission-manifest.json').read_text(encoding='utf-8'))
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env={k:v for k,v in os.environ.items() if not k.endswith('_API_KEY')}
    env.update(COMPETITION_HW_TASK_AGENT='on',COMPETITION_HW_WORLD_AGENT='off',COMPETITION_HW_CONSOLE='compact')
    with (Path(temp)/'server.log').open('wb') as log:
        child=subprocess.Popen([sys.executable,str(package/'main3.py'),str(port)],cwd=temp,env=env,
            stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            url='http://127.0.0.1:'+str(port)+'/'
            for _ in range(100):
                try:urlopen(url,timeout=.2).close();break
                except URLError:time.sleep(.05)
            else:raise RuntimeError('server unavailable')
            for side in ('challenger','defender'):
                fixture={'cwd':'/','files':{'/tmp/demo/task_semantics.md':TEMPLATE}}
                llm=cmd='';trace=[];doc=data_doc()
                for n in range(1,7):
                    payload=observation(round_no=n,team=side,role_pos=(6,5),phase_task='请阅读task_semantics.md，获取任务信息',
                        llm_resp=llm,cmd_result=cmd,timeout_rounds=20)
                    start=time.perf_counter()
                    with urlopen(Request(url,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'}),timeout=5) as response:
                        decision=json.load(response)
                    trace.append({'round':n,'ms':1000*(time.perf_counter()-start),
                                  'actions':decision['roleCommandMap'],'has_prompt':bool(decision.get('prompt'))})
                    llm=cmd=''
                    if n==1:cmd=local_task_sandbox.execute(decision['executeCmd'],fixture,active=True)
                    if n in (2,4,5):
                        token=re.search(r'"request_id":"([0-9a-f]{24})"',decision['prompt'])[1]
                        ids=json.loads(re.search(r'可引用证据ID：(\[[^\n]+\])',decision['prompt'])[1])
                        plan={'kind':'run','command':doc['command'],'evidence_ids':ids} if n==2 else {
                            'kind':'answer','answer':answer('旧石器时代' if n==4 else '遗址甲'),'evidence_ids':ids}
                        llm=json.dumps({'request_id':token,'plan':plan},ensure_ascii=False)
                    if n==3:
                        assert decision['executeCmd']==doc['command'];cmd='[exitCode:0]\n'+doc['text']
                    if n==5:
                        assert not any(c['action']=='submitAnswer' for c in decision['roleCommandMap'].values())
                        assert '记录名称' in decision['prompt']
                    if n==6:
                        assert [c['taskAnswer'] for c in decision['roleCommandMap'].values() if c['action']=='submitAnswer']==[answer('遗址甲')]
                cases.append({'side':side,'passed':True,'trace':trace})
        finally:
            child.terminate()
            try:child.wait(timeout=10)
            except subprocess.TimeoutExpired:child.kill();child.wait()
            if sys.exc_info()[0] is not None:
                Path(a.output).with_suffix('.failure.log').write_bytes((Path(temp)/'server.log').read_bytes())
report={'scope':'Actual tar.gz POSTs with synthetic model/API fixtures; not intranet PASS','source_commit':manifest['commit'],
    'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'python':sys.version,'passed':True,'cases':cases}
Path(a.output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'passed':True,'requests':12,'source':manifest['commit'],'max_ms':max(t['ms'] for c in cases for t in c['trace'])}))
