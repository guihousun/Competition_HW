"""Run the real flat archive entry with mirrored, hand-authored night cases."""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_front_wall_interior import battle
from test_nightwork import board as quiet_board


def cases(side):
    outside = battle()
    next(r for r in outside['teamOur']['roles'] if r['id'] == 1)['pos'] = {'x':12,'y':23}
    repair = battle()
    next(r for r in repair['teamOur']['roles'] if r['id'] == 1)['backpack'] = ['WallFixer']
    quiet = quiet_board()
    quiet['teamOur']['roles'][1]['pos'] = {'x':8,'y':5}
    unknown = deepcopy(quiet)
    unknown.pop('robot')
    rows = [('outside', outside, '1', 'move'), ('repair', repair, '1', 'use'),
            ('quiet', quiet, '2', 'collect'), ('unknown', unknown, '2', 'move'),
            ('quiet_fourth_night', deepcopy(quiet), '2', 'move')]
    for i, (name, state, uid, action) in enumerate(rows):
        state['roundNo'] = 462 if name == 'quiet_fourth_night' else 90 + i
        state['teamOur']['type'] = side
        if side == 'defender':
            for role in state['teamOur']['roles']:
                role['pos']['x'] = 40 - role['pos']['x'] - (role['roleType'] == 'station')
            for role in state.get('robot', {}).get('roles', []):
                role['pos']['x'] = 40 - role['pos']['x']
            for zone in state['mapInfo']['zones']:
                zone['pos']['x'] = 40 - zone['pos']['x']
        yield name, state, uid, action


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    archive = Path(args.archive).resolve()
    rows = []
    with tempfile.TemporaryDirectory(prefix='front-package-') as folder:
        with tarfile.open(archive, 'r:gz') as handle:
            handle.extractall(folder, filter='data')
        root = Path(folder) / 'CoreGeek'
        manifest = json.loads((root / 'submission-manifest.json').read_text())
        for side in ('challenger', 'defender'):
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            env = dict(os.environ, PYTHONIOENCODING='utf-8', COMPETITION_HW_TRACE='off',
                       COMPETITION_HW_TASK_AGENT='off', COMPETITION_HW_WORLD_AGENT='off',
                       COMPETITION_HW_LLM_ROUTER='off')
            with (Path(folder) / 'service.log').open('wb') as log:
                process = subprocess.Popen([sys.executable, str(root / 'main3.py'), str(port)],
                                           cwd=folder, env=env, stdout=log, stderr=subprocess.STDOUT,
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                try:
                    for _ in range(100):
                        try:
                            with socket.create_connection(('127.0.0.1',port),timeout=.1):break
                        except OSError:time.sleep(.05)
                    else:raise RuntimeError('startup timeout')
                    for name, state, uid, expected in cases(side):
                        start = time.perf_counter()
                        with urlopen(Request(f'http://127.0.0.1:{port}/',
                                             data=json.dumps(state).encode(),
                                             headers={'Content-Type':'application/json'}),timeout=5) as response:
                            result=json.load(response)
                        elapsed = round((time.perf_counter()-start)*1000, 3)
                        commands=result['roleCommandMap']
                        assert commands[uid]['action']==expected, (side,name,result)
                        assert not any(c.get('controllerId')==uid for c in commands.values())
                        if name=='repair':assert commands[uid]['name']=='WallFixer'
                        rows.append(dict(side=side,case=name,commands=commands,ms=elapsed))
                finally:
                    process.terminate(); process.wait(timeout=10)
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(dict(source_commit=manifest['commit'],
        archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        python=sys.version,scope='Synthetic HTTP cases, no intranet/official PASS',cases=rows),
        ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'PASS {len(rows)} real-package POSTs; max {max(r["ms"] for r in rows)} ms')


if __name__ == '__main__':
    main()
