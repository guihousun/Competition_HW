"""Run a supplied, verified archive without needing Git inside the guest OS."""
import argparse, hashlib, json, os, socket, subprocess, sys, tarfile, tempfile, time
from pathlib import Path
from urllib.request import Request, urlopen

p = argparse.ArgumentParser()
p.add_argument('--archive', required=True)
p.add_argument('--request', required=True)
p.add_argument('--output', required=True)
a = p.parse_args()
archive, out = Path(a.archive).resolve(), Path(a.output).resolve()
out.mkdir(parents=True, exist_ok=False)
expected = archive.with_suffix('.gz.sha256').read_text().split()[0]
assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected
payload = json.loads(Path(a.request).read_text(encoding='utf-8-sig'))
cases = ['main3', 'main', 'blocked_trace', 'absent_manifest'] + ([] if os.name == 'nt' else ['run_sh'])
rows = []
for case in cases:
    row = {'case': case, 'passed': False, 'requests': 0}
    with tempfile.TemporaryDirectory(prefix='competition-package-probe-') as tmp:
        folder = Path(tmp)
        with tarfile.open(archive) as bundle:
            bundle.extractall(folder, filter='data')
        package = folder / 'CoreGeek'
        manifest = json.loads((package / 'submission-manifest.json').read_text(encoding='utf-8'))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = {k:v for k,v in os.environ.items() if not k.endswith('_API_KEY')
               and k not in ('COMPETITION_HW_TASK_AGENT', 'COMPETITION_HW_WORLD_AGENT', 'COMPETITION_HW_LLM_ROUTER')}
        env.update(PYTHONIOENCODING='utf-8', PATH=str(Path(sys.executable).parent)+os.pathsep+env.get('PATH',''),
                   COMPETITION_HW_TRACE_DIR=str(out / (case+'-traces')))
        if case == 'blocked_trace':
            blocked = folder / 'not-a-directory'
            blocked.write_text('test-only fault')
            env['COMPETITION_HW_TRACE_DIR'] = str(blocked / 'child')
        if case == 'absent_manifest':
            (package / 'submission-manifest.json').rename(package / 'submission-manifest.saved')
        command = ['bash', str(package / 'run.sh'), str(port)] if case == 'run_sh' else [sys.executable,
            str(package / ('main.py' if case == 'main' else 'main3.py')), str(port)]
        with (out / (case+'.log')).open('wb') as log:
            process = subprocess.Popen(command, cwd=folder, env=env, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                started = time.monotonic()
                url = f'http://127.0.0.1:{port}/'
                while time.monotonic() - started < 10:
                    if process.poll() is not None:
                        raise RuntimeError('exited_before_ready')
                    try:
                        with urlopen(url, timeout=.5) as response:
                            assert response.status == 200
                        break
                    except OSError:
                        time.sleep(.05)
                else:
                    raise RuntimeError('startup_deadline')
                row['startup_seconds'] = time.monotonic() - started
                times = []
                for n in range(5):
                    request = dict(payload, roundNo=int(payload.get('roundNo') or 1)+n)
                    started = time.monotonic()
                    with urlopen(Request(url, data=json.dumps(request).encode('utf-8'),
                        headers={'Content-Type':'application/json'}), timeout=5) as response:
                        value = json.load(response)
                    times.append((time.monotonic()-started)*1000)
                    assert isinstance(value.get('roleCommandMap'), dict)
                    assert set(value) <= {'roleCommandMap','prompt','executeCmd'}
                    assert times[-1] < 5000
                    row['requests'] += 1
                row.update(passed=True, max_ms=max(times))
            except Exception as error:
                row['error'] = type(error).__name__+': '+str(error)[:180]
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    rows.append(row)
    (out/'progress.json').write_text(json.dumps(rows, indent=2), encoding='utf-8')
report = {'scope':'Actual archive entry and logging fallback probes; not full-game or intranet PASS',
    'archive_sha256':expected, 'python':sys.version.split()[0], 'os':os.name,
    'cases':rows, 'passed':all(r['passed'] for r in rows)}
(out/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report),flush=True)
raise SystemExit(0 if report['passed'] else 1)
