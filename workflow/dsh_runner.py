"""Supervise dsh SDK with an explicit model/effort; never edit global settings.

Uses the installed dsh CLI's documented newline JSON-RPC SDK profile.
Worktrees and diff checks are review hygiene, not an OS security sandbox.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import uuid

MODEL = {'provider': 'deepseek-official', 'model': 'deepseek-flash', 'reasoningEffort': 'max'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git(cwd, *args):
    return subprocess.check_output(['git', '-C', str(cwd), *args], text=True, encoding='utf-8').strip()


def launch(cwd, stderr):
    env = os.environ.copy()
    if os.name == 'nt':
        exe = shutil.which('dsh.ps1') or shutil.which('dsh')
        if not exe:
            raise RuntimeError('dsh not installed')
        env['COMPETITION_DSH_EXE'] = exe
        cmd = [shutil.which('pwsh') or 'powershell', '-NoProfile', '-NonInteractive',
               '-Command', '& $env:COMPETITION_DSH_EXE --profile sdk']
    else:
        cmd = ['dsh', '--profile', 'sdk']
    return subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=stderr, text=True,
                            encoding='utf-8', bufsize=1)


def run_task(cwd, task, output, timeout=1200):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    session = 'competition-' + uuid.uuid4().hex
    result = dict(MODEL, session_id=session, status='starting')
    # Do not publish stderr or raw model stream to GitHub automatically.
    with (output / 'stderr.log').open('w', encoding='utf-8') as errors:
        process = launch(cwd, errors)
        (output / 'process.json').write_text(json.dumps({'pid': process.pid, 'session': session}), encoding='utf-8')
        inbox = queue.Queue()
        def reader():
            try:
                for line in process.stdout:
                    try: inbox.put(json.loads(line))
                    except json.JSONDecodeError: inbox.put({'transport_error': 'Non-JSON SDK stdout'})
            finally:
                inbox.put({'transport_error': 'SDK stdout closed'})
        threading.Thread(target=reader, daemon=True).start()
        deadline = time.monotonic() + timeout
        def send(rid, method, params=None):
            frame = {'jsonrpc':'2.0','id':rid,'method':method}
            if params is not None: frame['params'] = params
            process.stdin.write(json.dumps(frame, ensure_ascii=False)+'\n')
            process.stdin.flush()
        def receive():
            try: frame = inbox.get(timeout=max(.01, deadline-time.monotonic()))
            except queue.Empty: raise TimeoutError('dsh task timed out')
            if 'transport_error' in frame: raise RuntimeError(frame['transport_error'])
            if 'error' in frame: raise RuntimeError(str(frame['error']))
            return frame
        try:
            send(1,'initialize', dict(MODEL, cwd=str(Path(cwd).resolve())))
            frame = receive()
            while frame.get('id') != 1: frame = receive()
            if frame.get('result',{}).get('serverInfo',{}).get('name') != 'deepseek-harness-sdk-runtime':
                raise RuntimeError('Unexpected SDK server identity')
            result['initialized'] = True
            send(2, 'session/prompt', {'sessionId':session, 'contentBlocks':[{'type':'text','text':task}]})
            answer, end_reason, accepted = '', None, False
            while True:
                frame = receive()
                if frame.get('id') == 2:
                    accepted = True
                    result['message_id'] = frame['result']['messageId']
                params = frame.get('params',{})
                if params.get('sessionId') != session: continue
                if frame.get('method') == 'session.event':
                    event = params.get('event',{})
                    data = event.get('data',{})
                    if event.get('type') == 'assistant/message':
                        text = '\n'.join(b.get('text','') for b in data.get('message',{}).get('content',[]) if b.get('type')=='text')
                        if text.strip(): answer = text
                    elif event.get('type') == 'turn/end':
                        end_reason = data.get('reason')
                if frame.get('method') == 'session.status' and params.get('status') == 'idle' and accepted and end_reason is not None:
                    break
            result.update(status='completed' if isinstance(end_reason,dict) and end_reason.get('kind')=='completed' and answer.strip() else 'failed', end_reason=end_reason)
            (output/'answer.md').write_text(answer, encoding='utf-8')
            # A transport success is not implementation/review approval.
            send(3,'shutdown')
            while receive().get('id') != 3: pass
            process.wait(timeout=20)
            result['exit_code'] = process.returncode
        except Exception as error:
            result.update(status='failed', error=str(error))
        finally:
            if process.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True)
                else: process.terminate()
                process.wait(timeout=20)
            process.stdin.close(); process.stdout.close()
            (output/'result.json').write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args=parser.parse_args()
    if args.smoke:
        cwd=args.output.resolve().parent
        cwd.mkdir(parents=True,exist_ok=True)
        task='Reply exactly DSH_READY. Do not use tools, inspect files, or make changes.'
    else:
        if not args.manifest: parser.error('--manifest is required')
        spec=json.loads(args.manifest.read_text(encoding='utf-8'))
        cwd=Path(spec['worktree']).resolve()
        if not (cwd/'.git').is_file(): raise RuntimeError('Use an isolated Git worktree')
        if git(cwd,'rev-parse','HEAD') != spec['base_sha']: raise RuntimeError('Base SHA mismatch')
        if git(cwd,'status','--porcelain'): raise RuntimeError('Worktree is dirty; review before resuming')
        if spec.get('approved_by') != 'codex': raise RuntimeError('Spec must be approved by the orchestrator')
        if sha(spec['spec_path']) != spec['spec_sha256']: raise RuntimeError('Spec changed after approval')
        task=Path(spec['spec_path']).read_text(encoding='utf-8')
        task+='\n\nExecutor contract: implement only this Spec. Do not change official source documents, global settings, or files outside the worktree. Do not create subagents, commit, push, merge, contact services beyond the task, or post GitHub comments. Return changed paths, test commands and actual results, unresolved questions. Do not claim intranet verification. Model is deepseek-flash with max effort.'
    result=run_task(cwd,task,args.output,120 if args.smoke else 1200)
    if not args.smoke:
        result['base_after']=git(cwd,'rev-parse','HEAD')
        result['worktree_status']=git(cwd,'status','--porcelain')
        result['review_required']=True
        (args.output/'result.json').write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({k:result.get(k) for k in ['status','model','reasoningEffort','end_reason','exit_code']},ensure_ascii=False))
    if result['status']!='completed': raise SystemExit(1)


if __name__=='__main__': main()
