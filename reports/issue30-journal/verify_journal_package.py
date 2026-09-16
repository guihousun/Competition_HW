"""Real package/brain/post-send journal; synthetic observations, no live LLM."""
import argparse, hashlib, json, os, socket, subprocess, sys, tarfile, tempfile, time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

p=argparse.ArgumentParser()
p.add_argument('--source',required=True);p.add_argument('--archive',required=True);p.add_argument('--output',required=True)
a=p.parse_args();sys.path.insert(0,str(Path(a.source)/'tests'))
from test_tasks import observation
archive=Path(a.archive).resolve(); checksum=hashlib.sha256(archive.read_bytes()).hexdigest()
assert checksum==archive.with_suffix('.gz.sha256').read_text().split()[0]
cases=[]
with tempfile.TemporaryDirectory() as tmp:
    with tarfile.open(archive) as tar:tar.extractall(tmp,filter='data')
    package=Path(tmp)/'CoreGeek';source=json.loads((package/'submission-manifest.json').read_text())['commit']
    for side in ('challenger','defender'):
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        env={k:v for k,v in os.environ.items() if not k.endswith('_API_KEY')}
        env.update(COMPETITION_HW_TASK_AGENT='off',COMPETITION_HW_WORLD_AGENT='off',
                   COMPETITION_HW_CONSOLE='compact',PYTHONIOENCODING='utf-8',
                   COMPETITION_HW_TRACE_DIR=str(Path(tmp)/side/'traces'))
        path=Path(tmp)/(side+'.log');trace=[]
        with path.open('wb') as log:
            process=subprocess.Popen([sys.executable,str(package/'main3.py'),str(port)],cwd=tmp,env=env,
                stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                url=f'http://127.0.0.1:{port}/'
                for _ in range(100):
                    try:urlopen(url,timeout=.2).close();break
                    except HTTPError:break
                    except URLError:time.sleep(.05)
                else:raise RuntimeError('not ready')
                for n,question in ((71,'diagnostic-task-A'),(72,''),(73,'diagnostic-task-B')):
                    req=observation(round_no=n,team=side,role_pos=(2,2),task_points=[],phase_task=question)
                    req['mapInfo']['zones']=[]
                    req['robot']['roles']=[{'id':30001,'roleType':'smallRobot','health':40,'pos':{'x':5,'y':2}}]
                    start=time.perf_counter()
                    with urlopen(Request(url,data=json.dumps(req).encode(),headers={'Content-Type':'application/json'}),timeout=5) as res:
                        response=json.load(res)
                    trace.append({'round':n,'ms':(time.perf_counter()-start)*1000,'response':response})
                    assert 'roleCommandMap' in response
                for _ in range(200):
                    text=path.read_text(encoding='utf-8',errors='replace')
                    rows=[json.loads(line.split('task_event ',1)[1]) for line in text.splitlines() if 'task_event ' in line]
                    if any(r['kind']=='task_text_observed' and r['round']==73 for r in rows):break
                    time.sleep(.01)
                assert [r['round'] for r in rows if r['kind']=='task_text_observed']==[71,73], [(r['kind'],r['round']) for r in rows]
                assert any(r['kind']=='task_text_ended' and r['round']==72 for r in rows)
                assert any(r['kind']=='pioneer_safety' and r['round']==71 for r in rows)
                summaries=[json.loads(r['content']['text']) for r in rows if r['kind']=='task_outcome_summary']
                assert len(summaries)==1 and not summaries[0]['official_success_confirmed']
                cases.append({'side':side,'passed':True,'trace':trace,
                    'events':[{'kind':r['kind'],'round':r['round']} for r in rows]})
            finally:
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait()
                if sys.exc_info()[0] is not None:
                    Path(a.output).with_suffix('.failure.log').write_bytes(path.read_bytes())
report={'scope':'Real tar main3/brain/journal; synthetic diagnostic requests; no official PASS',
        'source_commit':source,'archive_sha256':checksum,'python':sys.version,'cases':cases,'passed':True}
Path(a.output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'passed':True,'requests':6,'max_ms':max(t['ms'] for c in cases for t in c['trace'])}))
