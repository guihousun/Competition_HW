"""Native session routing tests; no model calls in unit tests."""
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import sys
import subprocess
import unittest
from unittest.mock import patch

import dsh_sessions as ds

SESSION_ID='competition-native-detected'


def job_record(worktree):
    """Manifest-shaped record for resume tests; never executed by a worker."""
    return {'approved_by':'codex','specialist':'qa-tooling','worktree':str(worktree),
            'base_sha':'base','workspace_sha256':'snapshot','spec_path':'spec.md',
            'spec_sha256':'spec-digest','session_id':SESSION_ID,'job_id':'job-1','output':'out'}


class FakeProcess:
    def __init__(self):
        self.lines=queue.Queue(); self.sent=[]; self.turns={}; self.returncode=None
        self.stdin=self; self.stdout=self; self.pid=0
    def __iter__(self):
        while True:
            value=self.lines.get()
            if value is None: return
            yield json.dumps(value)+'\n'
    def emit(self, frame): self.lines.put(frame)
    def write(self, line):
        frame=json.loads(line); self.sent.append(frame)
        rid, method=frame['id'],frame['method']
        if method=='initialize':
            self.emit({'id':rid,'result':{'serverInfo':{'name':'deepseek-harness-sdk-runtime'}}})
        elif method=='shutdown':
            self.emit({'id':rid,'result':{}}); self.returncode=0; self.lines.put(None)
        else:
            sid=frame['params']['sessionId']; turn=self.turns.get(sid,0)+1; self.turns[sid]=turn
            self.emit({'method':'session.status','params':{'sessionId':sid,'status':'idle'}})
            def event(kind,data):
                self.emit({'method':'session.event','params':{'sessionId':sid,'event':{'seq':turn*10+len(kind),'type':kind,'data':data}}})
            event('turn/start',{'turn':turn})
            event('assistant/message',{'turn':turn,'message':{'content':[{'type':'text','text':f'turn {turn}'}]}})
            event('turn/end',{'turn':turn,'reason':{'kind':'completed'}})
            # Deliberately deliver the admission ack after idle to test ordering.
            self.emit({'method':'session.status','params':{'sessionId':sid,'status':'idle'}})
            self.emit({'id':rid,'result':{'messageId':f'm{turn}'}})
    def flush(self): pass
    def close(self): pass
    def poll(self): return self.returncode
    def wait(self,timeout=None): self.returncode=0; return 0


class SessionTests(unittest.TestCase):
    def test_two_followups_reuse_exact_native_session_without_shutdown(self):
        process=FakeProcess()
        with patch.object(ds,'launch',return_value=process):
            sdk=ds.SDK('/worktree',None)
            try:
                a=sdk.prompt('qa-session','first')
                b=sdk.prompt('qa-session','second')
                self.assertEqual(a['answer'],'turn 1')
                self.assertEqual(b['answer'],'turn 2')
                self.assertEqual(a['session_id'],b['session_id'])
                self.assertNotEqual(a['message_id'],b['message_id'])
                self.assertEqual([f['method'] for f in process.sent],['initialize','session/prompt','session/prompt'])
                self.assertEqual(process.sent[0]['params']['model'],'deepseek-flash')
                self.assertEqual(process.sent[0]['params']['reasoningEffort'],'max')
            finally: sdk.close()
            self.assertEqual(process.sent[-1]['method'],'shutdown')

    def test_other_session_has_distinct_turn_stream(self):
        process=FakeProcess()
        with patch.object(ds,'launch',return_value=process):
            sdk=ds.SDK('/worktree',None)
            try:
                sdk.prompt('qa','one')
                other=sdk.prompt('frontend','two')
                self.assertEqual(other['turn'],1)
                self.assertEqual(other['session_id'],'frontend')
            finally: sdk.close()

    def test_manifest_requires_review_and_exact_code_and_spec(self):
        with tempfile.TemporaryDirectory() as temp:
            cwd=Path(temp); (cwd/'.git').write_text('gitdir: fixture')
            spec=cwd/'spec.md'; spec.write_text('requirements')
            manifest={'approved_by':'codex','specialist':'qa-tooling','worktree':str(cwd),
                      'base_sha':'base','workspace_sha256':'snapshot','spec_path':str(spec),'spec_sha256':ds.sha(spec)}
            with patch.object(ds,'git',return_value='base'), patch.object(ds,'fingerprint',return_value='snapshot'):
                self.assertEqual(ds.validate(manifest),cwd.resolve())
                for field,value in [('approved_by','dsh'),('specialist','unknown'),('base_sha','other'),('workspace_sha256','other'),('spec_sha256','other')]:
                    with self.subTest(field=field), self.assertRaises(ValueError):
                        ds.validate(dict(manifest,**{field:value}))

    def test_existing_session_cannot_switch_worktree_or_silently_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            pool=Path(temp); slot=pool/'qa-tooling'; slot.mkdir()
            original=pool/'worktree'
            ds.save(slot/'identity.json',{'worktree':str(original),'session_id':'native','worker_pid':123})
            ds.save(slot/'status.json',{'status':'stopped','heartbeat':0})
            with self.assertRaisesRegex(ValueError,'another worktree'): ds.start(pool,'qa-tooling',pool/'other')
            with self.assertRaisesRegex(ValueError,'Session stopped'): ds.start(pool,'qa-tooling',original)
            self.assertEqual(json.loads((slot/'identity.json').read_text())['session_id'],'native')

    def test_other_specialist_cannot_claim_an_existing_specialist_worktree(self):
        with tempfile.TemporaryDirectory() as temp:
            pool=Path(temp); cwd=pool/'worktree'; cwd.mkdir()
            owner=pool/'rules-engine'; owner.mkdir()
            ds.save(owner/'identity.json',{'specialist':'rules-engine','worktree':str(cwd),'session_id':'native-rules','worker_pid':123})
            with patch.object(ds.subprocess,'Popen') as spawn:
                with self.assertRaisesRegex(ValueError,'Another specialist owns this worktree'):
                    ds.start(pool,'qa-tooling',cwd)
                spawn.assert_not_called()
            # A rejected claim may leave an empty slot directory, never an identity.
            self.assertEqual(list((pool/'qa-tooling').iterdir()),[])
            self.assertFalse((pool/'qa-tooling'/'identity.json').exists())
            self.assertEqual(json.loads((owner/'identity.json').read_text())['session_id'],'native-rules')

    def test_reusing_live_role_returns_same_native_session_without_spawning(self):
        with tempfile.TemporaryDirectory() as temp:
            pool=Path(temp); cwd=pool/'worktree'; cwd.mkdir()
            slot=pool/'qa-tooling'; (slot/'jobs').mkdir(parents=True)
            ds.save(slot/'identity.json',{'specialist':'qa-tooling','worktree':str(cwd),'session_id':SESSION_ID,'worker_pid':4321})
            ds.save(slot/'status.json',{'status':'running','heartbeat':time.time()})
            identity=(slot/'identity.json').read_bytes()
            with patch.object(ds.subprocess,'Popen') as spawn, patch.object(ds,'alive',return_value=True) as alive:
                record=ds.start(pool,'qa-tooling',cwd)
                spawn.assert_not_called()
            alive.assert_called_once_with(4321)
            self.assertEqual(record['session_id'],SESSION_ID)
            self.assertEqual(record['worker_pid'],4321)
            self.assertEqual((slot/'identity.json').read_bytes(),identity)
            self.assertEqual(list((slot/'jobs').iterdir()),[])

    def test_submit_to_existing_output_keeps_evidence_and_enqueues_no_duplicate(self):
        with tempfile.TemporaryDirectory() as temp:
            pool=Path(temp)/'pool'; pool.mkdir()
            cwd=Path(temp)/'worktree'; cwd.mkdir(); (cwd/'.git').write_text('gitdir: fixture')
            spec=cwd/'spec.md'; spec.write_text('requirements')
            manifest={'approved_by':'codex','specialist':'qa-tooling','worktree':str(cwd),
                      'base_sha':'base','workspace_sha256':'snapshot','spec_path':str(spec),
                      'spec_sha256':ds.sha(spec)}
            slot=pool/'qa-tooling'; (slot/'jobs').mkdir(parents=True)
            ds.save(slot/'identity.json',{'specialist':'qa-tooling','worktree':str(cwd),'session_id':SESSION_ID,'worker_pid':4321})
            ds.save(slot/'status.json',{'status':'running','heartbeat':0.0})
            output=Path(temp)/'out'; output.mkdir()
            evidence={'request.json':'{"job_id":"job-1"}','status.json':'{"status":"running"}','answer.md':'prior answer'}
            for name,text in evidence.items(): (output/name).write_text(text,encoding='utf-8')
            job=job_record(cwd)
            job_path=slot/'jobs'/(job['job_id']+'.json'); ds.save(job_path,job)
            jobs_before=sorted(p.name for p in (slot/'jobs').iterdir())
            launches=[]
            def fake_start(pool_arg,role,cwd_arg):
                launches.append((str(pool_arg),role,str(cwd_arg)))
                # Existing evidence is already in place before any queue write.
                self.assertEqual(sorted(p.name for p in output.iterdir()),sorted(evidence))
                self.assertFalse((output/'started.json').exists())
                return job
            with patch.object(ds,'git',return_value='base'), patch.object(ds,'fingerprint',return_value='snapshot'), \
                 patch.object(ds,'alive',return_value=True), patch.object(ds,'start',side_effect=fake_start):
                with self.assertRaises(FileExistsError):
                    ds.submit(pool,manifest,output)
            self.assertEqual(len(launches),0)
            for name,text in evidence.items():
                self.assertEqual((output/name).read_text(encoding='utf-8'),text)
            self.assertEqual(sorted(p.name for p in (slot/'jobs').iterdir()),jobs_before)
            self.assertEqual(json.loads(job_path.read_text(encoding='utf-8')),job)
            self.assertEqual(sorted(p.name for p in output.iterdir()),sorted(evidence))

    def test_global_execution_lock_excludes_another_process(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'active'
            script="import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from state import lock\ntry:\n with lock(Path(sys.argv[2])): print('ENTERED')\nexcept FileExistsError: print('LOCKED')"
            with ds.lock(path):
                child=subprocess.run([sys.executable,'-c',script,str(Path(ds.__file__).parent),str(path)],capture_output=True,text=True,timeout=10)
                self.assertEqual(child.returncode,0)
                self.assertEqual(child.stdout.strip(),'LOCKED')
                self.assertTrue(Path(str(path)+'.lock').exists())

    def test_worker_pid_can_be_recovered_from_matching_status(self):
        with tempfile.TemporaryDirectory() as temp:
            pool=Path(temp); slot=pool/'qa-tooling'; slot.mkdir()
            cwd=pool/'tree'
            ds.save(slot/'identity.json',{'worktree':str(cwd),'session_id':'native'})
            ds.save(slot/'status.json',{'status':'idle','session_id':'native','worker_pid':123,'heartbeat':time.time()})
            with patch.object(ds,'alive',return_value=True):
                result=ds.start(pool,'qa-tooling',cwd)
            self.assertEqual(result['worker_pid'],123)
            self.assertEqual(result['session_id'],'native')


class LongTurnProcess(FakeProcess):
    """Answers initialize, then leaves the turn open until finish()/exit()."""
    def write(self,line):
        frame=json.loads(line)
        if frame['method']=='initialize':
            self.sent.append(frame)
            self.emit({'id':frame['id'],'result':{'serverInfo':{'name':'deepseek-harness-sdk-runtime'}}}); return
        if frame['method']=='shutdown':
            self.sent.append(frame); self.emit({'id':frame['id'],'result':{}}); self.returncode=0; return
        self.sent.append(frame)
        if frame['method']=='session/prompt':
            sid=frame['params']['sessionId']
            self.emit({'method':'session.status','params':{'sessionId':sid,'status':'working'}})
            self.emit({'id':frame['id'],'result':{'messageId':'m1'}})
        if frame['method']=='competition/steer':
            self.emit({'id':frame['id'],'result':{'requestId':frame['params']['requestId'],'turn':1,'accepted':True}})
    def finish(self,sid='qa-session',turn=1,text='late result'):
        self.emit({'method':'session.event','params':{'sessionId':sid,'event':{'seq':turn*10+1,'type':'turn/start','data':{'turn':turn}}}})
        self.emit({'method':'session.event','params':{'sessionId':sid,'event':{'seq':turn*10+2,'type':'assistant/message','data':{'turn':turn,'message':{'content':[{'type':'text','text':text}]}}}}})
        self.emit({'method':'session.event','params':{'sessionId':sid,'event':{'seq':turn*10+4,'type':'turn/end','data':{'turn':turn,'reason':{'kind':'completed'}}}}})
        self.emit({'method':'session.status','params':{'sessionId':sid,'status':'idle'}})
    def exit(self,code=1):
        self.returncode=code; self.lines.put(None)
    def wait(self,timeout=None):
        # A live fake runtime must not look like an exited process to close().
        if self.returncode is None: raise subprocess.TimeoutExpired('fake-dsh',timeout or 0)
        return self.returncode


class SupervisionTests(unittest.TestCase):
    """Regression: waiting must never end, cancel or replay the native session."""

    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root=Path(temp.name); self.out=self.root/'out'; self.out.mkdir()

    def drain(self,sdk):
        while True:
            try: sdk.inbox.get_nowait()
            except queue.Empty: return

    def test_native_wait_has_no_execution_deadline(self):
        """Old wrapper: prompt(...,1200) -> TimeoutError -> broken=True -> killed."""
        process=LongTurnProcess()
        with patch.object(ds,'launch',return_value=process):
            sdk=ds.SDK('/worktree',None,window=.01)
            self.assertEqual(sdk.prompt.__defaults__[:1],(None,),
                             'prompt must not carry a default execution timeout')
            threading.Timer(.05,process.finish).start()
            try:
                result=sdk.prompt('qa-session','long task')
                self.assertEqual(result['answer'],'late result')
                self.assertEqual(result['status'],'completed')
                self.assertFalse(sdk.broken)
            finally: sdk.close()
            self.assertEqual(process.poll(),0)
        self.assertEqual([f['method'] for f in process.sent].count('session/prompt'),1)

    def test_same_session_survives_repeated_waiter_timeouts_with_exactly_one_prompt(self):
        """Old bug: waiter deadline -> SDK TimeoutError -> failed result -> taskkill /T /F."""
        process=LongTurnProcess()
        pool=self.root/'pool'; role='qa-tooling'; slot=pool/role; slot.mkdir(parents=True)
        ds.save(slot/'identity.json',{'specialist':role,'worktree':str(self.root),'session_id':SESSION_ID,'worker_pid':4321})
        ds.save(slot/'status.json',{'status':'running','session_id':SESSION_ID,'worker_pid':4321,'heartbeat':time.time()-1250})
        ds.save(self.out/'request.json',{'specialist':role,'worktree':str(self.root),'output':str(self.out)})
        ds.save(self.out/'status.json',{'status':'running','session_id':SESSION_ID})
        with patch.object(ds,'launch',return_value=process):
            sdk=ds.SDK('/worktree',None,window=.01)
            done=threading.Event(); result={}
            def run():
                try: result.update(sdk.prompt(SESSION_ID,'long task'))
                finally: done.set()
            thread=threading.Thread(target=run,daemon=True); thread.start()
            try:
                for _ in range(200):
                    if any(f['method']=='session/prompt' for f in process.sent): break
                    time.sleep(.005)
                for _ in range(5):
                    with patch.object(ds,'alive',return_value=True), patch.object(ds.subprocess,'run') as kill, \
                         patch.object(ds,'submit') as resubmit:
                        with self.assertRaises(TimeoutError):
                            ds.wait_result(pool,self.out,.01,.005)
                        kill.assert_not_called(); resubmit.assert_not_called()
                    self.assertFalse((slot/'STOP').exists())
                    self.assertFalse((self.out/'result.json').exists())
                    self.assertEqual(json.loads((slot/'identity.json').read_text())['session_id'],SESSION_ID)
                self.assertFalse(done.is_set(),'the native turn is still running, only the waiter expired')
                self.assertEqual([f['method'] for f in process.sent].count('session/prompt'),1,
                                 'no waiter timeout may resubmit session/prompt')
                process.finish(SESSION_ID)
                self.assertTrue(done.wait(2),'native turn finished after the waiter timeouts')
            finally:
                sdk.close()
                thread.join(timeout=2)
        # The same job/output is collected by waiting again, not by re-prompting.
        result.update(session_id=SESSION_ID,turn=1)
        (self.out/'answer.md').write_text(result.pop('answer'),encoding='utf-8')
        ds.save(self.out/'result.json',result)
        with patch.object(ds,'alive',return_value=True):
            collected=ds.wait_result(pool,self.out,1,.005)
        self.assertEqual(collected['status'],'completed')
        self.assertEqual(collected['answer'],'late result')
        self.assertEqual(collected['session_id'],SESSION_ID)
        self.assertEqual([f['method'] for f in process.sent].count('session/prompt'),1)

    def test_steering_continues_while_prompt_waits_indefinitely(self):
        process=LongTurnProcess()
        receipt=self.out/'steer.ack.json'
        with patch.object(ds,'launch',return_value=process):
            sdk=ds.SDK('/worktree',None,window=.01)
            try:
                threading.Timer(.05,process.finish).start()
                result=sdk.prompt('qa-session','long task',
                                  interventions=lambda turn:[{'requestId':'r1','text':'change','receipt':str(receipt)}])
                self.assertEqual([f['method'] for f in process.sent].count('competition/steer'),1)
                self.assertTrue(json.loads(receipt.read_text(encoding='utf-8'))['accepted'])
                self.assertEqual(result['steering_requests'],['r1'])
                self.assertFalse(sdk.broken)
                self.assertNotIn('shutdown',[f['method'] for f in process.sent])
            finally: sdk.close()

    def test_real_process_loss_is_a_transport_failure_not_a_timeout(self):
        process=LongTurnProcess()
        with patch.object(ds,'launch',return_value=process):
            sdk=ds.SDK('/worktree',None,window=.01)
            try:
                threading.Timer(.05,process.exit).start()
                with self.assertRaisesRegex(RuntimeError,'DSH transport lost') as caught:
                    sdk.prompt('qa-session','long task')
                self.assertNotIsInstance(caught.exception,TimeoutError)
                self.assertTrue(sdk.broken,'real transport loss still marks the runtime broken')
            finally:
                process.returncode=1; sdk.broken=True; sdk.close()

    def test_reply_raises_error_frame_for_matching_id(self):
        process=LongTurnProcess()
        with patch.object(ds,'launch',return_value=process):
            sdk=ds.SDK('/worktree',None,window=.01)
            try:
                rid=sdk.send('session/prompt',{'sessionId':'qa-session','contentBlocks':[]})
                for _ in range(200):
                    if sdk.inbox.qsize(): break
                    time.sleep(.005)
                self.drain(sdk)
                process.emit({'id':rid,'error':{'message':'boom'}})
                with self.assertRaisesRegex(RuntimeError,'boom'): sdk.reply(rid,time.monotonic()+1)
            finally: sdk.close()

    def test_stale_long_running_heartbeat_is_not_rejected_when_worker_alive(self):
        pool=self.root/'pool'; cwd=self.root/'tree'; cwd.mkdir()
        slot=pool/'qa-tooling'; (slot/'jobs').mkdir(parents=True)
        ds.save(slot/'identity.json',{'specialist':'qa-tooling','worktree':str(cwd),'session_id':SESSION_ID,'worker_pid':4321})
        # 20 minutes past the last heartbeat, worker process still alive.
        ds.save(slot/'status.json',{'status':'running','session_id':SESSION_ID,'worker_pid':4321,'heartbeat':time.time()-1290})
        with patch.object(ds,'alive',return_value=True):
            record=ds.start(pool,'qa-tooling',cwd)
        self.assertEqual(record['session_id'],SESSION_ID)

    def test_heartbeat_throttle_is_tick_driven_and_idle_clears_turn_fields(self):
        beat=ds.Heartbeat(interval=15)
        self.assertTrue(beat.due(1000.0),'first tick writes immediately')
        self.assertFalse(beat.due(1001.0))
        self.assertFalse(beat.due(1014.9))
        self.assertTrue(beat.due(1015.0),'liveness keeps refreshing past any old 20min limit')
        # The heartbeat rides the SDK receive window on the worker thread: one
        # writer, and idle must not keep a previous turn's scoped fields.
        process=LongTurnProcess()
        with patch.object(ds,'launch',return_value=process):
            sdk=ds.SDK('/worktree',None,window=.01)
            writes=[]
            live={}
            def status(value, **extra):
                if value!='running': extra.setdefault('active_turn',None); extra.setdefault('output',None)
                live.update(status=value,**extra); writes.append(dict(live))
            ticks=[0]
            def progress(turn):
                ticks[0]+=1
                if turn is None: status('idle')
                else: status('running',output='out',active_turn=turn)
            try:
                threading.Timer(.05,process.finish).start()
                with patch.object(ds.time,'monotonic',side_effect=range(0,1000)):
                    sdk.prompt('qa-session','long task',progress=progress)
                progress(None)          # the worker's finally-block scope reset
            finally: sdk.close()
        self.assertGreater(ticks[0],1,'heartbeat ticks run inside the native wait window')
        self.assertEqual(writes[-1]['status'],'idle')
        self.assertIsNone(writes[-1]['active_turn'])
        self.assertIsNone(writes[-1]['output'])


if __name__=='__main__': unittest.main()
