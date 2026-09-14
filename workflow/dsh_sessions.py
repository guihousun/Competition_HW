"""Route follow-ups to live DSH sessions; DSH owns all conversation context.

An ordinary send targets a live worker and never replaces one. A failed or
stopped worker is recovered only through the explicit `resume` flow, which asks
the native runtime to resume the persisted session under its original id; this
module never copies messages, edits the log, or creates a fresh session with the
same id.
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

# Worker status heartbeat cadence. Independent of any waiter/turn deadline.
HEARTBEAT_SECONDS = 15.0


class Heartbeat:
    """Throttle for tick-driven liveness writes during a native turn.

    Liveness only: it never imposes or triggers a turn deadline. It is driven
    by the SDK receive window on the worker thread, so there is exactly one
    writer for the status file (no concurrent thread mutation).
    """
    def __init__(self, interval=HEARTBEAT_SECONDS):
        self.interval = interval
        self.last = None
    def due(self, now=None):
        now = time.monotonic() if now is None else now
        if self.last is not None and now-self.last < self.interval: return False
        self.last = now
        return True


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
    """One native SDK runtime, reusable for multiple sequential prompts.

    A prompt waits for the native turn to end and the session to go idle for as
    long as the native runtime needs. Waiting in bounded receive windows keeps
    steering and status heartbeats flowing; it is not an execution deadline.
    Only real transport loss marks this runtime broken.

    Passing ``resume`` re-attaches this runtime to the persisted native session
    before any prompt: the project bridge calls the native
    ``ctx.agents.resume({resumeSessionId})`` path, which cold-reads the existing
    log. No message, summary, or history is copied, and a missing log or missing
    persistence is reported instead of silently starting a fresh session.
    """
    def __init__(self, cwd, stderr, patch=None, window=.2, resume=None):
        if not math.isfinite(window) or window <= 0:
            raise ValueError('Finite positive receive window required')
        self.window = window
        self.process = launch(cwd, stderr, patch) if patch else launch(cwd, stderr)
        self.steering = False
        self.resumed = None
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
                capabilities = self.reply(self.send('competition/capabilities'),time.monotonic()+10)
                self.steering = capabilities.get('nativeSteer') is True
                if not self.steering: raise RuntimeError('Native steering bridge unavailable')
                if resume is not None:
                    if capabilities.get('nativeResume') is not True:
                        raise RuntimeError('Bridge does not expose native resume; refusing to start a fresh session')
                    self.resumed = self.resume(resume)
        except Exception:
            self.broken = True
            self.close()
            raise

    def resume(self, session_id):
        """Ask the native runtime to resume the persisted session under this id.

        This is a bounded reattachment handshake (log open + cold read, no model
        turn), not an execution deadline: prompts still wait without a deadline.
        Returns the runtime's confirmation. The confirmation is required before
        any prompt: a runtime that answers with an error (missing persisted log,
        no persistence backend, model mismatch) leaves this runtime broken.
        """
        reply = self.reply(self.send('competition/resume', {'resumeSessionId':session_id}), time.monotonic()+120)
        if not isinstance(reply,dict) or reply.get('resumed') is not True or reply.get('sessionId') != session_id or reply.get('created'):
            raise RuntimeError('Native runtime did not confirm a native resume of '+session_id)
        return reply

    def send(self, method, params=None):
        self.rid += 1
        frame = {'jsonrpc':'2.0', 'id':self.rid, 'method':method}
        if params is not None: frame['params'] = params
        self.process.stdin.write(json.dumps(frame, ensure_ascii=False)+'\n')
        self.process.stdin.flush()
        return self.rid

    def receive(self, deadline=None, tick=None):
        """Receive one frame, working in bounded windows so ticks keep running.

        deadline=None waits for the real transport instead of a turn deadline;
        loss of the native process is reported as a transport failure.
        """
        while True:
            if tick:
                try: tick()
                except Exception as error:
                    raise RuntimeError('Steering dispatch failed: '+str(error)) from error
            remaining = None if deadline is None else deadline-time.monotonic()
            if remaining is not None and remaining <= 0: raise TimeoutError('DSH deadline exceeded')
            if tick or remaining is None: wait = self.window
            else: wait = remaining
            try: frame = self.inbox.get(timeout=wait); break
            except queue.Empty:
                if deadline is not None and not tick: raise TimeoutError('DSH deadline exceeded')
                lost = self.lost()
                if lost: raise RuntimeError(lost)
        if 'transport_error' in frame:
            # Prefer the concrete process report when both signal the same loss.
            raise RuntimeError(self.lost() or frame['transport_error'])
        # A JSON-RPC error frame is returned as-is: prompt() can tell a rejected
        # steering receipt from a fatal protocol failure, reply() raises it.
        params = frame.get('params', {})
        if frame.get('method') == 'session.event':
            sid, seq = params.get('sessionId'), params.get('event',{}).get('seq',-1)
            self.seq[sid] = max(seq, self.seq.get(sid,-1))
        return frame

    def lost(self):
        """Return a transport-loss report when the native process is really gone."""
        code = self.process.poll()
        if code is None: return None
        return f'DSH transport lost: runtime exited (exit code {code})'

    def reply(self, rid, deadline):
        while True:
            frame = self.receive(deadline)
            if 'error' in frame: raise RuntimeError(str(frame['error']))
            if frame.get('id') == rid: return frame.get('result',{})

    def prompt(self, session, task, interventions=None, progress=None):
        """Run one native turn and wait for its native turn/end + idle.

        There is no execution deadline here. A caller-side deadline belongs to
        wait_result/CLI --timeout, which only stops observing the job; it never
        closes the runtime, cancels the turn, or resubmits session/prompt. So a
        second prompt() call is always a new turn, never a retry of this one.
        Only real transport loss (or a JSON-RPC protocol error) marks the
        runtime broken.
        """
        if self.broken: raise RuntimeError('SDK session unavailable')
        # Discard transport notifications already delivered after a prior idle.
        # This does not touch DSH's persisted messages or model context.
        while not self.inbox.empty(): self.receive()
        watermark = self.seq.get(session,-1)
        rid = self.send('session/prompt', {'sessionId':session, 'contentBlocks':[{'type':'text','text':task}]})
        ack, answer, reason, turn, idle = None, '', None, None, False
        pending, sent = {}, {}
        def tick():
            # Steering and the liveness heartbeat both ride the receive window.
            if progress: progress(turn)
            if not interventions or turn is None or reason is not None: return
            for request in interventions(turn):
                if request['requestId'] in sent: continue
                request_id = self.send('competition/steer', {'sessionId':session,'expectedTurn':turn,
                    'requestId':request['requestId'],'text':request['text']})
                pending[request_id]=request; sent[request['requestId']]=request
        try:
            while True:
                frame = self.receive(tick=tick)
                if frame.get('id') in pending:
                    # A rejected steer is a receipt, not a transport failure.
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
            # Real transport or protocol failure only; never a caller timeout.
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


def _spawn_worker(pool, role, slot):
    """Start the fixed supervisor process for one specialist slot."""
    with (slot/'worker.log').open('ab') as log:
        return subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'worker','--pool',str(pool),'--specialist',role],
                                cwd=str(pool.parent),stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,
                                start_new_session=os.name!='nt')


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
            # A long turn is not invalid while the worker process is alive and the
            # status file keeps its heartbeat. Only real process loss is reported.
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
            process = _spawn_worker(pool,role,slot)
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


def historical_session(previous, role):
    """Resolve the native session a previous run belonged to, from its own evidence.

    Older runs predate `session_id` in request.json, so the association is read
    from started.json/result.json (both written by the worker) and only then from
    request.json. Every session id an evidence file does carry must be the same
    one; a file that exists without one is not treated as disagreement. The
    files are read, never rewritten.
    """
    job = json.loads((previous/'request.json').read_text(encoding='utf-8'))
    if job.get('specialist') != role:
        raise ValueError('Previous output belongs to another specialist')
    if not job.get('job_id'):
        raise ValueError('Previous output has no job identity; inspect it before resuming')
    if not job.get('worktree') or not job.get('base_sha'):
        raise ValueError('Previous output has no worktree baseline; inspect it before resuming')
    seen = {}
    for name in ('started.json','result.json','request.json'):
        path = previous/name
        if not path.exists(): continue
        try: value = json.loads(path.read_text(encoding='utf-8')).get('session_id')
        except (json.JSONDecodeError,AttributeError): value = None
        if isinstance(value,str) and value: seen[name] = value
    if not seen:
        raise ValueError('Previous output carries no session identity; cannot verify a native resume')
    if len(set(seen.values())) != 1:
        raise ValueError('Previous output session evidence conflicts; inspect it before resuming')
    if not ({'started.json','result.json'} & set(seen)):
        # request.json alone is not enough: only the worker-written files prove
        # that this native session actually executed the previous run.
        raise ValueError('Previous output has no worker-written session evidence; inspect it before resuming')
    return job, next(iter(seen.values())), sorted(seen)


def resume_worker(pool, manifest, previous, expect_session, output):
    """Recover a failed/stopped worker on its own persisted native session.

    Requires a verified dead worker PID, the unchanged session id, worktree,
    provider and model. The old output directory is evidence and is only read
    for the original job identity: its result is never overwritten and the
    original job is never re-queued. A fresh output directory carries the new
    approved follow-up manifest; the worker asks the native runtime to resume
    the persisted session before it prompts.

    A queued result only means the resume was requested: the native confirmation
    is written by the worker to the new output's native-resume.json before any
    follow-up prompt.
    """
    pool = Path(pool).resolve()
    previous, output = Path(previous).resolve(), Path(output).resolve()
    if not isinstance(expect_session,str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,160}',expect_session):
        raise ValueError('Expected session ID is required')
    if manifest.get('approved_by') != 'codex': raise ValueError('Codex-approved Spec required')
    role = manifest.get('specialist')
    if role not in SPECIALISTS: raise ValueError('Unknown specialist')
    slot = pool/role
    with lock(pool/'manager'):
        identity = slot/'identity.json'
        if not identity.exists(): raise ValueError('Unknown session: no identity for this specialist')
        record = json.loads(identity.read_text(encoding='utf-8'))
        status = json.loads((slot/'status.json').read_text(encoding='utf-8'))
        request, original, evidence = historical_session(previous,role)
        # Evidence of the interrupted run, never a replay source.
        if original != expect_session:
            raise ValueError('Previous output belongs to another native session; inspect it before resuming')
        if record.get('session_id') != expect_session:
            raise ValueError('Native session changed; resume keeps the original identity or fails')
        if status.get('session_id') not in (None,expect_session):
            raise ValueError('Status file belongs to another native session; inspect it before resuming')
        if status.get('status') not in ('failed','stopped'):
            raise ValueError('Session is not failed or stopped; ordinary send keeps the live session')
        for field in ('specialist','worktree','provider','model','reasoningEffort'):
            if manifest.get(field) != record.get(field):
                raise ValueError(field+' changed; resume keeps the original identity or fails')
        for source in (record,status):
            pid = source.get('worker_pid')
            if pid is not None and alive(pid):
                raise ValueError(f'Worker pid {pid} is still alive; refusing to resume or replace a live session')
        if (output/'result.json').exists(): raise ValueError('New output already has a result; inspect it instead of resuming')
        if output.exists(): raise FileExistsError('Output already exists; inspect its result instead of replaying')
        cwd = validate(manifest)
        if Path(request['worktree']).resolve() != cwd:
            raise ValueError('Previous output belongs to another worktree; inspect before resuming')
        identity_path = slot/'identity.json'
        # Recorded before the supervisor starts so the worker it spawns knows
        # this is a native resume; confirmed status is written by that worker
        # only after the native runtime answers.
        record.update(native_resume=True,resumed_from=str(previous),
                      resume_evidence={name:expect_session for name in evidence})
        save(identity_path,record)
        save(slot/'status.json',{'status':'starting','heartbeat':time.time(),'session_id':expect_session})
        output.mkdir(parents=True,exist_ok=False)
        job = dict(manifest,output=str(output),worktree=str(cwd),spec_path=str(Path(manifest['spec_path']).resolve()),
                   job_id=str(time.time_ns())+'-'+uuid.uuid4().hex,session_id=expect_session,
                   resumed_from=str(previous),previous_job_id=request.get('job_id'),previous_base_sha=request.get('base_sha'))
        save(output/'request.json',job)
        save(output/'status.json',{'status':'queued','session_id':expect_session})
        (slot/'jobs').mkdir(parents=True,exist_ok=True)
        save(slot/'jobs'/(job['job_id']+'.json'),job)
        try:
            process = _spawn_worker(pool,role,slot)
            # Re-read: the worker may already have updated its own record.
            current = json.loads(identity_path.read_text(encoding='utf-8'))
            current['worker_pid'] = process.pid
            save(identity_path,current)
            # Only a successfully started supervisor may clear an earlier stop
            # request; otherwise it would exit before running the follow-up.
            if (slot/'STOP').exists(): (slot/'STOP').unlink()
        except Exception:
            save(slot/'status.json',{'status':'failed','heartbeat':time.time(),'session_id':expect_session})
            raise
        return {'output':str(output),'session_id':expect_session,'status':'queued','resume_requested':True,
                'previous':str(previous),'evidence':evidence}


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
    live={}
    def status(value, **extra):
        # Idle must not inherit a previous turn's active_turn/output.
        if value!='running': extra.setdefault('active_turn',None); extra.setdefault('output',None)
        live.update(status=value,heartbeat=time.time(),worker_pid=os.getpid(),session_id=record['session_id'],**extra)
        save(slot/'status.json',dict(live))
    sdk=None
    try:
        status('starting')
        with (slot/'sdk.stderr.log').open('a',encoding='utf-8') as stderr:
            patch=slot/'steering.patch.yml'
            bridge=str(Path(__file__).with_name('dsh_steering_bridge.mjs').resolve()).replace('\\','/')
            patch.write_text('- id: sdk-jsonrpc-server\n  disabled: true\n- insert:\n    - id: competition-steering\n      name: '+json.dumps(bridge)+'\n      inject: [sdkAppStartup, loader]\n',encoding='utf-8')
            sdk=SDK(record['worktree'],stderr,patch=patch,
                    resume=record['session_id'] if record.get('native_resume') and not record.get('native_resumed') else None)
            record.update(json.loads((slot/'identity.json').read_text(encoding='utf-8')))
            record['native_steer']=sdk.steering
            if sdk.resumed is not None:
                # Native confirmation precedes the follow-up prompt. A runtime
                # that could not resume fails here, before any new model turn.
                record['native_resumed']=sdk.resumed
                save(slot/'status.json',{'status':'resumed','heartbeat':time.time(),'session_id':record['session_id'],
                                         'worker_pid':os.getpid(),'native_resumed':sdk.resumed})
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
                            heartbeat=Heartbeat()
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
                                    """Liveness + active turn; idle clears active_turn/output."""
                                    if turn is None: status('idle')
                                    elif heartbeat.due(): status('running',output=str(out),active_turn=turn,native_steer=sdk.steering)
                                if sdk.resumed is not None and not (out/'native-resume.json').exists():
                                    save(out/'native-resume.json',sdk.resumed)
                                # No execution timeout: wait for the native turn/end + idle
                                # however long the turn takes. Only a real transport loss
                                # (or explicit `stop`) ends this session.
                                result=sdk.prompt(record['session_id'],task,interventions=interventions,progress=progress)
                                (out/'answer.md').write_text(result.pop('answer'),encoding='utf-8')
                                result['workspace_after']=fingerprint(job['worktree'])
                                result['base_after']=git(job['worktree'],'rev-parse','HEAD')
                            except Exception as error:
                                result=dict(MODEL,status='failed',error=str(error),session_id=record['session_id'],review_required=True)
                            finally:
                                progress(None)
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
        live['done']=True
        if sdk: sdk.close()


def main():
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['send','resume','steer','wait','status','stop','worker','fingerprint'])
    p.add_argument('--pool',type=Path,default=Path('.workflow/dsh-sessions'))
    p.add_argument('--specialist',choices=SPECIALISTS)
    p.add_argument('--manifest',type=Path); p.add_argument('--output',type=Path)
    p.add_argument('--previous',type=Path,help='Resume only: output directory of the interrupted run (evidence, never replayed)')
    p.add_argument('--expect-session',help='Resume only: the original native session ID that must be unchanged')
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
    if args.command=='resume':
        if not args.manifest or not args.previous or not args.output or not args.expect_session:
            p.error('--manifest, --previous, --expect-session and --output required')
        print(json.dumps(resume_worker(args.pool,json.loads(args.manifest.read_text(encoding='utf-8')),
                                       args.previous,args.expect_session,args.output),ensure_ascii=False),flush=True)
        if not args.wait: return
    if args.command=='send':
        if not args.manifest or not args.output: p.error('--manifest and --output required')
        print(json.dumps(submit(args.pool,json.loads(args.manifest.read_text(encoding='utf-8')),args.output)),flush=True)
        if not args.wait: return
    if args.command=='wait' or (args.command in ('send','resume') and args.wait):
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
