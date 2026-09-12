"""Route follow-ups to live DSH sessions; DSH owns all conversation context.

The SDK currently supports follow-ups in a live process, not native resume RPC.
Never reconstruct history, silently replace a stopped session, or alter DSH settings.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

from dsh_runner import MODEL, git, launch, sha
from state import lock, save

SPECIALISTS = {
    'rules-engine': 'Implement precisely specified protocol or engine fixes; escalate rule ambiguity.',
    'strategy': 'Implement bounded strategy/task algorithms with explicit acceptance cases.',
    'frontend': 'Implement rendering and interaction without changing game behavior.',
    'qa-tooling': 'Implement independent tests, diagnostics, workflow and documentation tasks.',
}


def alive(pid):
    if not isinstance(pid,int) or pid <= 0: return False
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.restype=ctypes.c_void_p
        kernel.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_bool,ctypes.c_ulong]
        kernel.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle: return False
        try:
            code=ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value==259
        finally: kernel.CloseHandle(handle)
    try: os.kill(pid,0); return True
    except ProcessLookupError: return False
    except PermissionError: return True


class SDK:
    """One native SDK runtime, reusable for multiple sequential prompts."""
    def __init__(self, cwd, stderr, patch=None):
        self.process = launch(cwd, stderr, patch) if patch else launch(cwd, stderr)
        self.steering = False
        self.inbox = queue.Queue()
        self.rid = 0
        self.seq = {}
        self.broken = False
        def read():
            for line in self.process.stdout:
                try: self.inbox.put(json.loads(line))
                except json.JSONDecodeError: self.inbox.put({'transport_error': 'Invalid SDK JSON'})
            self.inbox.put({'transport_error': 'SDK connection closed'})
        threading.Thread(target=read, daemon=True).start()
        try:
            rid = self.send('initialize', dict(MODEL, cwd=str(cwd)))
            reply = self.reply(rid, time.monotonic()+60)
            if reply.get('serverInfo',{}).get('name') != 'deepseek-harness-sdk-runtime':
                raise RuntimeError('Unexpected SDK identity')
            if patch:
                self.steering = self.reply(self.send('competition/capabilities'),time.monotonic()+10).get('nativeSteer') is True
                if not self.steering: raise RuntimeError('Native steering bridge unavailable')
        except Exception:
            self.broken = True
            self.close()
            raise

    def send(self, method, params=None):
        self.rid += 1
        frame = {'jsonrpc':'2.0', 'id':self.rid, 'method':method}
        if params is not None: frame['params'] = params
        self.process.stdin.write(json.dumps(frame, ensure_ascii=False)+'\n')
        self.process.stdin.flush()
        return self.rid

    def receive(self, deadline, tick=None):
        while True:
            if tick: tick()
            remaining = deadline-time.monotonic()
            if remaining <= 0: raise TimeoutError('DSH deadline exceeded')
            try: frame = self.inbox.get(timeout=min(.2,remaining) if tick else remaining); break
            except queue.Empty:
                if not tick: raise TimeoutError('DSH deadline exceeded')
        if 'transport_error' in frame: raise RuntimeError(frame['transport_error'])
        if 'error' in frame and not tick: raise RuntimeError(str(frame['error']))
        params = frame.get('params', {})
        if frame.get('method') == 'session.event':
            sid, seq = params.get('sessionId'), params.get('event',{}).get('seq',-1)
            self.seq[sid] = max(seq, self.seq.get(sid,-1))
        return frame

    def reply(self, rid, deadline):
        while True:
            frame = self.receive(deadline)
            if frame.get('id') == rid: return frame.get('result',{})

    def prompt(self, session, task, timeout=1200, interventions=None, progress=None):
        if self.broken: raise RuntimeError('SDK session unavailable')
        deadline = time.monotonic()+timeout
        # Discard transport notifications already delivered after a prior idle.
        # This does not touch DSH's persisted messages or model context.
        while not self.inbox.empty(): self.receive(deadline)
        watermark = self.seq.get(session,-1)
        rid = self.send('session/prompt', {'sessionId':session, 'contentBlocks':[{'type':'text','text':task}]})
        ack, answer, reason, turn, idle = None, '', None, None, False
        pending, sent = {}, {}
        def tick():
            if not interventions or turn is None or reason is not None: return
            for request in interventions(turn):
                if request['requestId'] in sent: continue
                request_id = self.send('competition/steer', {'sessionId':session,'expectedTurn':turn,
                    'requestId':request['requestId'],'text':request['text']})
                pending[request_id]=request; sent[request['requestId']]=request
        try:
            while True:
                frame = self.receive(deadline,tick=tick)
                if frame.get('id') in pending:
                    request=pending.pop(frame['id'])
                    receipt=frame.get('result') or {'accepted':False,'error':frame.get('error')}
                    save(Path(request['receipt']),receipt)
                    frame={}
                if 'error' in frame: raise RuntimeError(str(frame['error']))
                if frame.get('id') == rid: ack = frame.get('result',{}).get('messageId')
                p = frame.get('params', {})
                if p.get('sessionId') == session:
                    if frame.get('method') == 'session.event':
                        event = p.get('event',{})
                        data = event.get('data',{})
                        if event.get('seq',-1) > watermark:
                            if event.get('type') == 'turn/start':
                                turn, idle = data['turn'], False
                                if progress: progress(turn)
                            elif turn is not None and data.get('turn') == turn:
                                if event.get('type') == 'assistant/message':
                                    value = '\n'.join(b.get('text','') for b in data.get('message',{}).get('content',[]) if b.get('type')=='text')
                                    if value.strip(): answer = value
                                elif event.get('type') == 'turn/end': reason = data.get('reason')
                    if frame.get('method') == 'session.status' and p.get('status') == 'idle' and reason is not None:
                        idle = True
                    if frame.get('method') == 'competition.steer_consumed' and p.get('requestId') in sent:
                        request=sent[p['requestId']]
                        save(Path(request['receipt']).with_suffix('.consumed.json'),p)
                if ack and idle and reason is not None and not pending:
                    return dict(MODEL, session_id=session, message_id=ack, turn=turn,
                                status='completed' if reason.get('kind')=='completed' and answer.strip() else 'failed',
                                end_reason=reason, answer=answer, review_required=True,
                                steering_requests=list(sent))
        except Exception:
            self.broken = True
            raise

    def close(self):
        if self.process.poll() is None and not self.broken:
            try:
                rid = self.send('shutdown')
                self.reply(rid,time.monotonic()+10)
                self.process.wait(timeout=10)
            except Exception: pass
        if self.process.poll() is None:
            if os.name == 'nt':
                subprocess.run(['taskkill','/PID',str(self.process.pid),'/T','/F'],capture_output=True)
            else: self.process.terminate()
            self.process.wait(timeout=20)
        self.process.stdin.close(); self.process.stdout.close()


def fingerprint(worktree):
    """Bind a review to exact current files, including uncommitted corrections."""
    worktree = Path(worktree).resolve()
    raw = subprocess.check_output(['git','-C',str(worktree),'ls-files','-co','--exclude-standard','-z'])
    digest = hashlib.sha256(git(worktree,'rev-parse','HEAD').encode())
    for name in sorted(set(raw.decode('utf-8').split('\0'))- {''}):
        p = worktree/name
        if p.is_symlink(): raise ValueError('Symlink needs separate review')
        digest.update(name.encode()+b'\0')
        digest.update(p.read_bytes() if p.is_file() else b'<deleted>')
        digest.update(b'\0')
    return digest.hexdigest()


def validate(manifest):
    cwd = Path(manifest['worktree']).resolve()
    if manifest.get('approved_by') != 'codex': raise ValueError('Codex-approved Spec required')
    if manifest.get('specialist') not in SPECIALISTS: raise ValueError('Unknown specialist')
    if not (cwd/'.git').is_file(): raise ValueError('Isolated worktree required')
    if git(cwd,'rev-parse','HEAD') != manifest['base_sha']: raise ValueError('Base SHA changed')
    if sha(manifest['spec_path']) != manifest['spec_sha256']: raise ValueError('Spec changed')
    if fingerprint(cwd) != manifest['workspace_sha256']: raise ValueError('Worktree changed since review')
    return cwd


def start(pool, role, cwd):
    pool, cwd = Path(pool).resolve(), Path(cwd).resolve()
    slot = pool/role
    slot.mkdir(parents=True,exist_ok=True)
    with lock(pool/'manager'):
        identity = slot/'identity.json'
        if identity.exists():
            record = json.loads(identity.read_text(encoding='utf-8'))
            if Path(record['worktree']) != cwd: raise ValueError('Session is bound to another worktree')
            status = json.loads((slot/'status.json').read_text(encoding='utf-8'))
            if status['status'] in ('stopped','failed'): raise ValueError('Session stopped: use DSH native recovery; do not silently replace it')
            pid = record.get('worker_pid')
            if pid is None and status.get('session_id') == record['session_id']:
                pid = status.get('worker_pid')
            if not alive(pid): raise ValueError('Session worker is no longer running; inspect native DSH session')
            if status['status'] == 'running' and time.time()-status.get('heartbeat',0) > 1290:
                raise ValueError('Session turn exceeded its deadline; inspect before retrying')
            if 'worker_pid' not in record:
                record['worker_pid'] = pid
                save(identity,record)
            if time.time()-status.get('heartbeat',0) > 90 and status['status'] != 'running':
                raise ValueError('Worker unresponsive: inspect PID before retrying')
            return record
        for other in pool.glob('*/identity.json'):
            existing=json.loads(other.read_text(encoding='utf-8'))
            if Path(existing['worktree']) == cwd:
                raise ValueError('Another specialist owns this worktree')
        record = dict(MODEL, specialist=role, worktree=str(cwd), session_id='competition-'+role+'-'+uuid.uuid4().hex)
        save(identity,record)
        save(slot/'status.json',{'status':'starting','heartbeat':time.time()})
        (slot/'jobs').mkdir()
        try:
            with (slot/'worker.log').open('ab') as log:
                process = subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'worker','--pool',str(pool),'--specialist',role],
                                           stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,
                                           start_new_session=os.name!='nt')
            record['worker_pid']=process.pid
            save(identity,record)
        except Exception:
            save(slot/'status.json',{'status':'failed','heartbeat':time.time()})
            raise
        return record


def submit(pool, manifest, output):
    cwd = validate(manifest)
    output = Path(output).resolve()
    if output.exists(): raise FileExistsError('Output already exists; inspect its result instead of replaying')
    record = start(pool,manifest['specialist'],cwd)
    output.mkdir(parents=True,exist_ok=False)
    job = dict(manifest,output=str(output),worktree=str(cwd),spec_path=str(Path(manifest['spec_path']).resolve()),job_id=str(time.time_ns())+'-'+uuid.uuid4().hex)
    save(output/'request.json',job)
    save(output/'status.json',{'status':'queued','session_id':record['session_id']})
    save(Path(pool).resolve()/manifest['specialist']/'jobs'/(job['job_id']+'.json'),job)
    return {'output':str(output),'session_id':record['session_id'],'status':'queued'}


def wait_result(pool, output, timeout=1250, interval=.2):
    """Wait in this caller process, leaving the native session worker alive.

    A waiter timeout neither cancels nor resubmits the existing job. Call wait
    again with the same output directory to collect its eventual result.
    """
    if not math.isfinite(timeout) or not math.isfinite(interval) or timeout <= 0 or interval <= 0:
        raise ValueError('Finite positive wait timeout/interval required')
    output=Path(output).resolve()
    request=json.loads((output/'request.json').read_text(encoding='utf-8'))
    receipt=json.loads((output/'status.json').read_text(encoding='utf-8'))
    expected=receipt['session_id']
    if Path(request['output']).resolve() != output: raise ValueError('Output does not match submitted job')
    role=request['specialist']
    if role not in SPECIALISTS: raise ValueError('Unknown specialist')
    slot=Path(pool).resolve()/role
    deadline=time.monotonic()+timeout
    while True:
        if (output/'result.json').exists():
            result=json.loads((output/'result.json').read_text(encoding='utf-8'))
            if result.get('session_id') != expected: raise ValueError('Result belongs to another native session')
            answer=output/'answer.md'
            if result.get('status')=='completed' and not answer.exists():
                raise RuntimeError('Completed result is missing its answer')
            result.update(answer=answer.read_text(encoding='utf-8') if answer.exists() else '',
                          output=str(output),waiter_pid=os.getpid())
            return result
        state=json.loads((slot/'status.json').read_text(encoding='utf-8'))
        identity=json.loads((slot/'identity.json').read_text(encoding='utf-8'))
        if identity['session_id'] != expected: raise RuntimeError('Native session changed while waiting')
        if state['status'] in ('failed','stopped') and not (output/'result.json').exists():
            raise RuntimeError('Worker stopped before result; inspect job, do not resubmit blindly')
        pid=identity.get('worker_pid') or state.get('worker_pid')
        if pid and not alive(pid) and not (output/'result.json').exists():
            raise RuntimeError('Worker exited before result; inspect existing job')
        remaining=deadline-time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Wait timed out; job/session left unchanged. Use wait with the same output directory.')
        time.sleep(min(interval,remaining))


def request_steer(pool, manifest):
    """Side-channel intervention bound to a live job; never a new queued job."""
    if manifest.get('approved_by')!='codex': raise ValueError('Codex-approved steering Spec required')
    request_id=manifest['request_id']
    if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',request_id):
        raise ValueError('Invalid steering request ID')
    role=manifest['specialist']
    if role not in SPECIALISTS: raise ValueError('Unknown specialist')
    out=Path(manifest['job_output']).resolve()
    job=json.loads((out/'request.json').read_text(encoding='utf-8'))
    if manifest['job_id']!=job['job_id'] or role!=job['specialist']: raise ValueError('Wrong target job')
    if sha(manifest['spec_path'])!=manifest['spec_sha256']: raise ValueError('Steering Spec changed')
    item={k:manifest[k] for k in ('request_id','session_id','job_id','base_sha','spec_sha256')}
    item.update(turn=manifest['expected_turn'],spec_path=str(Path(manifest['spec_path']).resolve()))
    path=out/'steering'/(request_id+'.request.json')
    receipt=path.with_suffix('.ack.json')
    if path.exists():
        if json.loads(path.read_text(encoding='utf-8'))!=item: raise ValueError('Steering ID already used for different content')
        return {'receipt':str(receipt),'consumed':str(receipt.with_suffix('.consumed.json')),'request_id':request_id}
    slot=Path(pool).resolve()/role
    identity=json.loads((slot/'identity.json').read_text(encoding='utf-8'))
    state=json.loads((slot/'status.json').read_text(encoding='utf-8'))
    if not identity.get('native_steer'): raise ValueError('This native session has no steering bridge; not queued or restarted')
    if identity['session_id']!=manifest['session_id']: raise ValueError('Native session mismatch')
    if (out/'result.json').exists() or state['status']!='running' or Path(state.get('output','')).resolve()!=out or state.get('active_turn')!=manifest['expected_turn']:
        raise ValueError('Target turn is not active; steering was not queued')
    if git(job['worktree'],'rev-parse','HEAD')!=manifest['base_sha']: raise ValueError('Target HEAD changed')
    path.parent.mkdir(exist_ok=True)
    with lock(out/'steering-submit'):
        if path.exists(): raise ValueError('Concurrent duplicate intervention; inspect receipt')
        save(path,item)
    return {'receipt':str(receipt),'consumed':str(receipt.with_suffix('.consumed.json')),'request_id':request_id}


def wait_steer(paths, timeout=30):
    deadline=time.monotonic()+timeout; receipt=Path(paths['receipt'])
    while not receipt.exists():
        if (receipt.parent.parent/'result.json').exists():
            result={'accepted':False,'error':'Target turn finished before receipt; not queued'}
            save(receipt,result)
            return result
        if time.monotonic()>=deadline: raise TimeoutError('Steering receipt pending; inspect same request, do not duplicate it')
        time.sleep(.1)
    return json.loads(receipt.read_text(encoding='utf-8'))


def worker(pool, role):
    pool = Path(pool).resolve(); slot=pool/role
    record=json.loads((slot/'identity.json').read_text(encoding='utf-8'))
    def status(value, **extra):
        save(slot/'status.json',dict(status=value,heartbeat=time.time(),worker_pid=os.getpid(),session_id=record['session_id'],**extra))
    sdk=None
    try:
        status('starting')
        with (slot/'sdk.stderr.log').open('a',encoding='utf-8') as stderr:
            patch=slot/'steering.patch.yml'
            bridge=str(Path(__file__).with_name('dsh_steering_bridge.mjs').resolve()).replace('\\','/')
            patch.write_text('- id: sdk-jsonrpc-server\n  disabled: true\n- insert:\n    - id: competition-steering\n      name: '+json.dumps(bridge)+'\n      inject: [sdkAppStartup, loader]\n',encoding='utf-8')
            sdk=SDK(record['worktree'],stderr,patch=patch)
            record.update(json.loads((slot/'identity.json').read_text(encoding='utf-8')))
            record['native_steer']=sdk.steering
            save(slot/'identity.json',record)
            while not (slot/'STOP').exists():
                status('idle')
                for path in sorted((slot/'jobs').glob('*.json')):
                    job=json.loads(path.read_text(encoding='utf-8')); out=Path(job['output'])
                    if (out/'result.json').exists(): continue
                    if (out/'started.json').exists():
                        raise RuntimeError('Interrupted job requires review; refusing replay')
                    try:
                        with lock(pool/'active'):
                            result=None
                            try:
                                validate(job)
                                save(out/'started.json',{'session_id':record['session_id'],'worker_pid':os.getpid(),'time':time.time()})
                                status('running',output=str(out))
                                save(out/'status.json',{'status':'running','session_id':record['session_id']})
                                task=Path(job['spec_path']).read_text(encoding='utf-8')
                                task+='\n\nCurrent assignment (supersedes old task scope): '+SPECIALISTS[role]
                                task+=f"\nWorktree: {job['worktree']}\nHEAD: {job['base_sha']}\nSpec SHA256: {job['spec_sha256']}"
                                task+='\nImplement only the current Spec. Do not modify official sources or files outside this worktree. Do not commit, push, merge, create subagents or change global settings. Report actual tests and limitations. Codex owns review and decisions. DSH owns context management; do not manually rewrite history.'
                                control_dir=out/'steering'
                                def interventions(turn):
                                    if not control_dir.exists(): return []
                                    requests=[]
                                    for control in sorted(control_dir.glob('*.request.json')):
                                        item=json.loads(control.read_text(encoding='utf-8'))
                                        receipt=control.with_suffix('.ack.json')
                                        if receipt.exists(): continue
                                        if item['session_id']!=record['session_id'] or item['job_id']!=job['job_id'] or item['turn']!=turn or item['base_sha']!=git(job['worktree'],'rev-parse','HEAD'):
                                            save(receipt,{'accepted':False,'error':'Active job/turn/base changed'}); continue
                                        if sha(item['spec_path'])!=item['spec_sha256']:
                                            save(receipt,{'accepted':False,'error':'Steering Spec changed'}); continue
                                        requests.append({'requestId':item['request_id'],'text':Path(item['spec_path']).read_text(encoding='utf-8'),'receipt':str(receipt)})
                                    return requests
                                def progress(turn):
                                    status('running',output=str(out),active_turn=turn,native_steer=sdk.steering)
                                result=sdk.prompt(record['session_id'],task,interventions=interventions,progress=progress)
                                (out/'answer.md').write_text(result.pop('answer'),encoding='utf-8')
                                result['workspace_after']=fingerprint(job['worktree'])
                                result['base_after']=git(job['worktree'],'rev-parse','HEAD')
                            except Exception as error:
                                result=dict(MODEL,status='failed',error=str(error),session_id=record['session_id'],review_required=True)
                            save(out/'result.json',result)
                            for control in (out/'steering').glob('*.request.json'):
                                receipt=control.with_suffix('.ack.json')
                                if not receipt.exists(): save(receipt,{'accepted':False,'error':'Turn finished before steering receipt; not queued'})
                            if sdk.broken:
                                sdk.close()
                                raise RuntimeError('Native session interrupted; not reconstructing context')
                    except FileExistsError:
                        # Another specialist is working. Keep this job pending.
                        break
                    if (slot/'STOP').exists(): break
                time.sleep(1)
        status('stopped')
    except Exception as error:
        status('failed',error=str(error))
    finally:
        if sdk: sdk.close()


def main():
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['send','steer','wait','status','stop','worker','fingerprint'])
    p.add_argument('--pool',type=Path,default=Path('.workflow/dsh-sessions'))
    p.add_argument('--specialist',choices=SPECIALISTS)
    p.add_argument('--manifest',type=Path); p.add_argument('--output',type=Path)
    p.add_argument('--worktree',type=Path)
    p.add_argument('--wait',action='store_true',help='Keep this caller alive until the submitted turn returns')
    p.add_argument('--timeout',type=float,default=1250,help='Waiter deadline only; does not cancel the DSH job')
    args=p.parse_args()
    if (args.command=='wait' or args.wait) and (not math.isfinite(args.timeout) or args.timeout <= 0):
        p.error('--timeout must be finite and positive')
    if args.command=='fingerprint':
        if not args.worktree: p.error('--worktree required')
        print(fingerprint(args.worktree)); return
    if args.command=='steer':
        if not args.manifest: p.error('--manifest required')
        paths=request_steer(args.pool,json.loads(args.manifest.read_text(encoding='utf-8')))
        print(json.dumps(paths),flush=True)
        if args.wait:
            result=wait_steer(paths,min(args.timeout,30))
            print(json.dumps(result,ensure_ascii=False),flush=True)
            if not result.get('accepted'): raise SystemExit(1)
        return
    if args.command=='send':
        if not args.manifest or not args.output: p.error('--manifest and --output required')
        print(json.dumps(submit(args.pool,json.loads(args.manifest.read_text(encoding='utf-8')),args.output)),flush=True)
        if not args.wait: return
    if args.command=='wait' or (args.command=='send' and args.wait):
        if not args.output: p.error('--output required')
        try:
            result=wait_result(args.pool,args.output,args.timeout)
            print(json.dumps(result,ensure_ascii=False),flush=True)
            if result.get('status')!='completed': raise SystemExit(1)
        except TimeoutError as error:
            print(json.dumps({'status':'wait_timeout','error':str(error),'output':str(args.output)},ensure_ascii=False),flush=True)
            raise SystemExit(2)
        return
    if not args.specialist: p.error('--specialist required')
    if args.command=='worker': worker(args.pool,args.specialist)
    elif args.command=='stop':
        slot=args.pool/args.specialist
        if not (slot/'identity.json').exists(): p.error('Unknown session')
        (slot/'STOP').touch()
        print('Stop requested after current turn; DSH history is not deleted.')
    else:
        print((args.pool/args.specialist/'status.json').read_text(encoding='utf-8'))


if __name__=='__main__': main()
