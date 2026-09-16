"""Real packaged HTTP buy/return/use chain, synthetic board and local settlement."""
import argparse,hashlib,json,os,socket,subprocess,sys,tarfile,tempfile,time
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
from test_issue27_economy import board
from agent import turnactions
from agent.protocol import Turn,Pos,distance


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--archive',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();archive=Path(args.archive).resolve();output=Path(args.output)
    sha=hashlib.sha256(archive.read_bytes()).hexdigest()
    assert sha==archive.with_suffix('.gz.sha256').read_text().split()[0]
    rows=[]
    with tempfile.TemporaryDirectory() as folder:
        with tarfile.open(archive) as tar:tar.extractall(folder,filter='data')
        package=Path(folder)/'CoreGeek'
        source=json.loads((package/'submission-manifest.json').read_text())['commit']
        for side in ('challenger','defender'):
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            env={k:v for k,v in os.environ.items() if not k.endswith('_API_KEY')}
            env.update(PYTHONIOENCODING='utf-8',COMPETITION_HW_TRACE='off',COMPETITION_HW_CONSOLE='compact')
            with (Path(folder)/(side+'.log')).open('wb') as log:
                process=subprocess.Popen([sys.executable,str(package/'main3.py'),str(port)],cwd=folder,env=env,
                    stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                try:
                    url='http://127.0.0.1:'+str(port)+'/'
                    for _ in range(100):
                        try:urlopen(url,timeout=.2).close();break
                        except HTTPError:break
                        except URLError:time.sleep(.05)
                    else:raise RuntimeError('startup timeout')
                    state=board(side);initial_gold=state['teamOur']['goldNum'];events=[];timings=[]
                    for _ in range(42):
                        started=time.perf_counter()
                        with urlopen(Request(url,data=json.dumps(state).encode(),headers={'Content-Type':'application/json'}),timeout=5) as reply:
                            response=json.load(reply)
                        timings.append(1000*(time.perf_counter()-started))
                        assert set(response)<= {'roleCommandMap','prompt','executeCmd'}
                        assert not response.get('prompt') and not response.get('executeCmd')
                        turn=Turn.load(state);used=False
                        for uid,command in response['roleCommandMap'].items():
                            role=next(r for r in state['teamOur']['roles'] if str(r['id'])==uid)
                            action=command['action']
                            if action=='move':
                                target=Pos.load(command['targetPos'][0]);unit=next(u for u in turn.ours if str(u.unit_id)==uid)
                                assert distance(unit.pos,target)==1 and target not in turn.blocked(unit) and turn.land(target)
                                role['pos']=target.dump()
                            elif action=='buy':
                                turnactions.buy(state,role,item=command['name'],amount=command.get('num',1))
                                events.append({'round':state['roundNo'],'action':action,'item':command['name'],'gold':state['teamOur']['goldNum']})
                            elif action=='use':
                                turnactions.use(state,role,item=command['name'],target=Pos.load(command['targetPos'][0]),effects={})
                                events.append({'round':state['roundNo'],'action':action,'item':command['name']});used=True
                            else:raise AssertionError('Unexpected fixture action '+action)
                        if used:break
                        state['roundNo']+=1
                    assert [e['action'] for e in events]==['buy','use']
                    assert state['teamOur']['goldNum']==initial_gold-100
                    assert any(r['level']==2 and r['health']==1500 for r in state['teamOur']['roles'])
                    rows.append({'side':side,'requests':len(timings),'events':events,'max_http_ms':max(timings),'passed':True})
                finally:
                    process.terminate()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:process.kill();process.wait()
            log_text=(Path(folder)/(side+'.log')).read_text(encoding='utf-8')
            assert 'upgrade_itinerary' in log_text and 'weapon_readiness' in log_text
            catalogs=[json.loads(line.split('task_event ',1)[1]) for line in log_text.splitlines()
                      if 'task_event ' in line and '"kind":"trading_catalog"' in line]
            assert len(catalogs)==1
            catalog=json.loads(catalogs[0]['content']['text'])
            assert catalog['vendorShopList']['items']==board(side)['vendorShopList']
            assert catalog['weaponShopList']['items']==board(side)['weaponShopList']
            assert catalog['weaponShopList']['positions']==[board(side)['mapInfo']['zones'][0]['pos']]
            rows[-1]['trading_catalog']=catalog
    report={'source_commit':source,'archive_sha256':sha,'passed':True,'cases':rows,
            'scope':'Actual archive main3 HTTP; independent synthetic mirrored board and local buy/use settlement, not official platform PASS'}
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report))


if __name__=='__main__':main()
