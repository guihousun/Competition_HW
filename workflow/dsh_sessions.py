"""Route follow-ups to live DSH sessions; DSH owns all conversation context.

The SDK currently supports follow-ups in a live process, not native resume RPC.
Never reconstruct history, silently replace a stopped session, or alter DSH settings.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
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
    def __init__(self, cwd, stderr):
        self.process = launch(cwd, stderr)
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

    def receive(self, deadline):
        remaining = deadline-time.monotonic()
        if remaining <= 0: raise TimeoutError('DSH deadline exceeded')
        try: frame = self.inbox.get(timeout=remaining)
        except queue.Empty: raise TimeoutError('DSH deadline exceeded')
        if 'transport_error' in frame: raise RuntimeError(frame['transport_error'])
        if 'error' in frame: raise RuntimeError(str(frame['error']))
        params = frame.get('params', {})
        if frame.get('method') == 'session.event':
            sid, seq = params.get('sessionId'), params.get('event',{}).get('seq',-1)
            self.seq[sid] = max(seq, self.seq.get(sid,-1))
        return frame

    def reply(self, rid, deadline):
        while True:
            frame = self.receive(deadline)
            if frame.get('id') == rid: return frame.get('result',{})

    def prompt(self, session, task, timeout=1200):
        if self.broken: raise RuntimeError('SDK session unavailable')
        deadline = time.monotonic()+timeout
        # Discard transport notifications already delivered after a prior idle.
        # This does not touch DSH's persisted messages or model context.
        while not self.inbox.empty(): self.receive(deadline)
        watermark = self.seq.get(session,-1)
        rid = self.send('session/prompt', {'sessionId':session, 'contentBlocks':[{'type':'text','text':task}]})
        ack, answer, reason, turn, idle = None, '', None, None, False
        try:
            while True:
                frame = self.receive(deadline)
                if frame.get('id') == rid: ack = frame.get('result',{}).get('messageId')
                p = frame.get('params', {})
                if p.get('sessionId') == session:
                    if frame.get('method') == 'session.event':
                        event = p.get('event',{})
                        data = event.get('data',{})
                        if event.get('seq',-1) > watermark:
                            if event.get('type') == 'turn/start':
                                turn, idle = data['turn'], False
                            elif turn is not None and data.get('turn') == turn:
                                if event.get('type') == 'assistant/message':
                                    value = '\n'.join(b.get('text','') for b in data.get('message',{}).get('content',[]) if b.get('type')=='text')
                                    if value.strip(): answer = value
                                elif event.get('type') == 'turn/end': reason = data.get('reason')
                    if frame.get('method') == 'session.status' and p.get('status') == 'idle' and reason is not None:
                        idle = True
                if ack and idle and reason is not None:
                    return dict(MODEL, session_id=session, message_id=ack, turn=turn,
                                status='completed' if reason.get('kind')=='completed' and answer.strip() else 'failed',
                                end_reason=reason, answer=answer, review_required=True)
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


def worker(pool, role):
    pool = Path(pool).resolve(); slot=pool/role
    record=json.loads((slot/'identity.json').read_text(encoding='utf-8'))
    def status(value, **extra):
        save(slot/'status.json',dict(status=value,heartbeat=time.time(),worker_pid=os.getpid(),session_id=record['session_id'],**extra))
    sdk=None
    try:
        status('starting')
        with (slot/'sdk.stderr.log').open('a',encoding='utf-8') as stderr:
            sdk=SDK(record['worktree'],stderr)
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
                                result=sdk.prompt(record['session_id'],task)
                                (out/'answer.md').write_text(result.pop('answer'),encoding='utf-8')
                                result['workspace_after']=fingerprint(job['worktree'])
                                result['base_after']=git(job['worktree'],'rev-parse','HEAD')
                            except Exception as error:
                                result=dict(MODEL,status='failed',error=str(error),session_id=record['session_id'],review_required=True)
                            save(out/'result.json',result)
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
    p.add_argument('command',choices=['send','status','stop','worker','fingerprint'])
    p.add_argument('--pool',type=Path,default=Path('.workflow/dsh-sessions'))
    p.add_argument('--specialist',choices=SPECIALISTS)
    p.add_argument('--manifest',type=Path); p.add_argument('--output',type=Path)
    p.add_argument('--worktree',type=Path)
    args=p.parse_args()
    if args.command=='fingerprint':
        if not args.worktree: p.error('--worktree required')
        print(fingerprint(args.worktree)); return
    if args.command=='send':
        if not args.manifest or not args.output: p.error('--manifest and --output required')
        print(json.dumps(submit(args.pool,json.loads(args.manifest.read_text(encoding='utf-8')),args.output))); return
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
