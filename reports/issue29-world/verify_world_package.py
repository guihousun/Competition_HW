"""Actual tar entry, scripted public world-task replies, both sides."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

p = argparse.ArgumentParser()
p.add_argument('--source', required=True)
p.add_argument('--archive', required=True)
p.add_argument('--output', required=True)
a = p.parse_args()
sys.path.insert(0, str(Path(a.source) / 'tests'))
from test_tasks import observation

archive = Path(a.archive).resolve()
checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
assert checksum == archive.with_suffix('.gz.sha256').read_text().split()[0]
cases = []
with tempfile.TemporaryDirectory() as tmp:
    with tarfile.open(archive) as bundle:
        bundle.extractall(tmp, filter='data')
    package = Path(tmp) / 'CoreGeek'
    source = json.loads((package / 'submission-manifest.json').read_text())['commit']
    for side in ('challenger', 'defender'):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = {k: v for k, v in os.environ.items() if not k.endswith('_API_KEY')}
        env.update(COMPETITION_HW_TASK_AGENT='off', COMPETITION_HW_WORLD_AGENT='on',
                   PYTHONIOENCODING='utf-8', COMPETITION_HW_TRACE_DIR=str(Path(tmp)/side/'traces'),
                   COMPETITION_HW_CONSOLE='compact')
        first = '祭坛位于横坐标3、纵坐标4，需献祭一份IronWhistle。'
        filler = '东边林场组织了巡逻队，今晚开始值夜。'
        final = '同一祭坛仅在第五日白昼开放，夜晚关闭。'
        trace, token = [], None
        with (Path(tmp) / (side+'.log')).open('wb') as log:
            proc = subprocess.Popen([sys.executable, str(package/'main3.py'), str(port)],
                cwd=tmp, env=env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                url = f'http://127.0.0.1:{port}/'
                for _ in range(100):
                    try:
                        urlopen(url, timeout=.2).close()
                        break
                    except HTTPError:
                        break
                    except URLError:
                        time.sleep(.05)
                else:
                    raise RuntimeError('server not ready')
                for n, text in ((1,first),(261,filler),(402,final),(403,final),(521,final),(522,final)):
                    obs = observation(round_no=n, team=side, role_pos=(4,4), task_points=[])
                    obs['teamOur']['teamId'] = side
                    obs['mapInfo']['zones'] = []
                    obs['teamOur']['roles'][0]['backpack'] = ['IronWhistle']
                    obs['worldNews']['folkLegends'] = text
                    obs['weaponShopList'] = [{'name':'IronWhistle','price':17}]
                    if n == 403:
                        def evidence(text):
                            return [{'sourceId': hashlib.sha256(text.encode()).hexdigest(), 'quote': text}]
                        obs['llmResp'] = json.dumps({'request_id':token,'hypothesis':{
                            'site':{'x':3,'y':4},'items':['IronWhistle'],'opensAt':521,'closesAt':590,
                            'uncertain':False,'unknowns':[], 'evidence':{
                                'site':evidence(first),'items':evidence(first),'window':evidence(final)}}})
                    if n == 522:
                        obs['lastSummonTreasureResult'] = 1
                    start = time.perf_counter()
                    with urlopen(Request(url, data=json.dumps(obs).encode(),
                        headers={'Content-Type':'application/json'}), timeout=5) as res:
                        decision = json.load(res)
                    ms = (time.perf_counter()-start)*1000
                    assert set(decision) <= {'roleCommandMap','prompt','executeCmd'}
                    assert not decision.get('executeCmd')
                    action = decision['roleCommandMap'].get('10011', {})
                    if n == 402:
                        prompt = decision['prompt']
                        assert first in prompt and filler in prompt
                        assert '摇晃时内部有金属片碰撞的脆响' in prompt
                        assert '"name": "IronWhistle", "price": 17' in prompt
                        token = re.search(r'request_id必须为([a-f0-9]+)', prompt).group(1)
                    if n == 521:
                        assert action == {'action':'summonTreasure','targetPos':[{'x':3,'y':4}],'item':['IronWhistle']}, action
                    else:
                        assert action.get('action') != 'summonTreasure', (n,action)
                    trace.append({'round':n,'ms':ms,'prompt':bool(decision.get('prompt')),'pioneer_action':action})
                cases.append({'side':side,'passed':True,'trace':trace})
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
report = {'scope':'Actual tar.gz main3 HTTP; scripted LLM and result receipts, not intranet PASS',
          'source_commit':source,'archive_sha256':checksum,'python':sys.version,
          'passed':len(cases)==2,'cases':cases}
output = Path(a.output)
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'passed':report['passed'],'requests':sum(len(c['trace']) for c in cases),
                  'max_ms':max(t['ms'] for c in cases for t in c['trace'])}))
