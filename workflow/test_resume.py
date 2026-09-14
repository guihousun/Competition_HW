"""Native resume tests: fake native protocol, no model calls, no real worker.

They pin the contract in workflow/STEERING.md: a resume keeps the original
session identity and asks the installed DSH runtime to resume the persisted
session (never `agents.create`, never a copied history); the follow-up prompt
rides the ordinary session/prompt channel with no lifetime deadline; the
interrupted run stays as evidence.
"""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import dsh_sessions as ds
from test_sessions import FakeProcess, LongTurnProcess, SESSION_ID


class ResumeProcess(FakeProcess):
    """Fake native runtime that answers the resume method before prompts."""
    def __init__(self, confirm=True):
        super().__init__()
        self.confirm = confirm
    def write(self, line):
        frame = json.loads(line)
        if frame['method'] == 'competition/capabilities':
            self.sent.append(frame)
            self.emit({'id':frame['id'],'result':{'nativeSteer':True,'nativeResume':True,'delivery':'next-step'}})
            return
        if frame['method'] != 'competition/resume': return super().write(line)
        self.sent.append(frame)
        if not self.confirm:
            self.emit({'id':frame['id'],'error':{'message':'session not found: '+frame['params']['resumeSessionId']}})
            return
        self.emit({'id':frame['id'],'result':{'resumed':True,'sessionId':frame['params']['resumeSessionId'],
                   'agentId':frame['params']['resumeSessionId'],'created':False,
                   'deliveredBy':'ctx.agents.resume','provider':'deepseek-official','model':'deepseek-flash'}})


def confirmation(session=SESSION_ID):
    return {'resumed':True,'sessionId':session,'agentId':session,'created':False,
            'deliveredBy':'ctx.agents.resume','provider':'deepseek-official','model':'deepseek-flash'}


class ResumeProtocolTests(unittest.TestCase):
    """The bridge/RPC side of a resume, driven through the real SDK client code."""

    def test_sdk_asks_the_native_runtime_to_resume_the_original_id(self):
        process = ResumeProcess()
        with patch.object(ds,'launch',return_value=process):
            sdk = ds.SDK('/worktree',None,patch=Path('bridge.patch.yml'),resume=SESSION_ID)
            try:
                self.assertEqual(sdk.resumed['sessionId'],SESSION_ID)
                self.assertFalse(sdk.resumed['created'])
                self.assertTrue(sdk.steering)
            finally: sdk.close()
            methods = [f['method'] for f in process.sent]
            self.assertEqual(methods,['initialize','competition/capabilities','competition/resume','shutdown'])
            # No prompt may be sent by the resume step itself.
            self.assertNotIn('session/prompt',methods)

    def test_followup_after_resume_uses_the_same_native_prompt_channel(self):
        process = ResumeProcess()
        with patch.object(ds,'launch',return_value=process):
            sdk = ds.SDK('/worktree',None,patch=Path('bridge.patch.yml'),resume=SESSION_ID)
            try:
                result = sdk.prompt(SESSION_ID,'follow-up manifest applies to the resumed session')
            finally: sdk.close()
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['session_id'],SESSION_ID)
        self.assertEqual([f['method'] for f in process.sent],
                         ['initialize','competition/capabilities','competition/resume','session/prompt','shutdown'])
        prompt = [f for f in process.sent if f['method']=='session/prompt'][0]
        self.assertEqual(prompt['params']['sessionId'],SESSION_ID)

    def test_missing_persisted_session_fails_before_any_prompt(self):
        process = ResumeProcess(confirm=False)
        with patch.object(ds,'launch',return_value=process):
            with self.assertRaisesRegex(RuntimeError,'session not found'):
                ds.SDK('/worktree',None,patch=Path('bridge.patch.yml'),resume=SESSION_ID)
        self.assertNotIn('session/prompt',[f['method'] for f in process.sent])

    def test_unconfirmed_resume_is_rejected_instead_of_prompting(self):
        for reply in (None,{'resumed':False,'sessionId':SESSION_ID},{'resumed':True,'sessionId':SESSION_ID,'created':True},
                      {'resumed':True,'sessionId':'other'}):
            with self.subTest(reply=reply):
                sdk = ds.SDK.__new__(ds.SDK)
                sdk.send = lambda method,params=None: 1
                sdk.reply = lambda rid,deadline: reply
                with self.assertRaisesRegex(RuntimeError,'did not confirm a native resume'):
                    sdk.resume(SESSION_ID)

    def test_resume_has_no_turn_deadline_and_waits_for_the_native_turn_end(self):
        process = LatentResumeProcess()
        with patch.object(ds,'launch',return_value=process):
            sdk = ds.SDK('/worktree',None,patch=Path('bridge.patch.yml'),resume=SESSION_ID,window=.01)
            self.assertEqual(sdk.prompt.__defaults__[:1],(None,),
                             'resume must not introduce an execution deadline')
            threading.Timer(.05,process.finish,(SESSION_ID,)).start()
            try:
                result = sdk.prompt(SESSION_ID,'long follow-up')
            finally: sdk.close()
        self.assertEqual(result['status'],'completed')
        self.assertEqual([f['method'] for f in process.sent].count('session/prompt'),1)


class LatentResumeProcess(LongTurnProcess):
    """Long-running fake runtime that also answers capabilities/resume."""
    def write(self, line):
        frame = json.loads(line)
        if frame['method'] == 'competition/capabilities':
            self.sent.append(frame)
            self.emit({'id':frame['id'],'result':{'nativeSteer':True,'nativeResume':True,'delivery':'next-step'}})
            return
        if frame['method'] == 'competition/resume':
            self.sent.append(frame)
            self.emit({'id':frame['id'],'result':{'resumed':True,'sessionId':frame['params']['resumeSessionId'],
                       'agentId':frame['params']['resumeSessionId'],'created':False,
                       'deliveredBy':'ctx.agents.resume'}})
            return
        super().write(line)


def tree(root):
    return sorted(p.name for p in root.iterdir())


class ResumeFlowTests(unittest.TestCase):
    """The CLI/worker supervision side: who may be resumed and what is preserved."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.pool = self.root/'pool'; self.slot = self.pool/'qa-tooling'; self.slot.mkdir(parents=True)
        self.worktree = self.root/'worktree'; self.worktree.mkdir()
        (self.worktree/'.git').write_text('gitdir: fixture')
        self.spec = self.root/'followup.md'; self.spec.write_text('regression fixes')
        self.previous = self.root/'run-1'; self.previous.mkdir()
        self.output = self.root/'run-2'
        self.identity = {'provider':'deepseek-official','model':'deepseek-flash','reasoningEffort':'max',
                         'specialist':'qa-tooling','worktree':str(self.worktree),
                         'session_id':SESSION_ID,'worker_pid':51704,'native_steer':True}
        self.manifest = {'approved_by':'codex','specialist':'qa-tooling','worktree':str(self.worktree),
                         'base_sha':'base','workspace_sha256':'snapshot','spec_path':str(self.spec),
                         'spec_sha256':ds.sha(self.spec),'provider':'deepseek-official',
                         'model':'deepseek-flash','reasoningEffort':'max'}
        ds.save(self.slot/'identity.json',self.identity)
        ds.save(self.slot/'status.json',{'status':'failed','heartbeat':time.time(),'worker_pid':51704,
                                         'session_id':SESSION_ID,'error':'Native session interrupted'})
        # The historical shape: the original submit never wrote session_id into
        # request.json, so the association comes from started.json/result.json.
        ds.save(self.previous/'request.json',{'approved_by':'codex','specialist':'qa-tooling','job_id':'job-1',
                                              'worktree':str(self.worktree),'base_sha':'base',
                                              'spec_path':str(self.spec),'output':str(self.previous),
                                              'spec_sha256':ds.sha(self.spec),'workspace_sha256':'snapshot'})
        ds.save(self.previous/'status.json',{'status':'running','session_id':SESSION_ID})
        ds.save(self.previous/'started.json',{'session_id':SESSION_ID,'worker_pid':51704,'time':1.0})
        ds.save(self.previous/'result.json',{'status':'failed','session_id':SESSION_ID,'error':'DSH deadline exceeded'})
        (self.previous/'answer.md').write_text('old failed answer',encoding='utf-8')
        self.before = {name:(self.previous/name).read_bytes() for name in tree(self.previous)}

    def resume(self, spawned=None, **changes):
        manifest = dict(self.manifest,**changes.pop('manifest',{}))
        worker_alive = changes.pop('worker_alive',False)
        with patch.object(ds,'git',return_value='base'), patch.object(ds,'fingerprint',return_value='snapshot'), \
             patch.object(ds,'alive',return_value=worker_alive), \
             patch.object(ds,'_spawn_worker',return_value=spawned or type('P',(),{'pid':60001})()) as spawn:
            result = ds.resume_worker(self.pool,manifest,changes.pop('previous',self.previous),
                                      changes.pop('expect_session',SESSION_ID),changes.pop('output',self.output))
        return result,spawn

    def resume_identity(self):
        return json.loads((self.slot/'identity.json').read_text(encoding='utf-8'))

    def test_resume_queues_a_followup_on_the_same_session_and_keeps_evidence(self):
        result,spawn = self.resume()
        # Queued means requested: the native confirmation is the worker's job.
        self.assertTrue(result['resume_requested'])
        self.assertNotIn('resumed',result)
        self.assertEqual(result['session_id'],SESSION_ID)
        self.assertEqual(spawn.call_count,1)
        job = json.loads((self.output/'request.json').read_text(encoding='utf-8'))
        self.assertEqual(job['session_id'],SESSION_ID)
        self.assertEqual(job['resumed_from'],str(self.previous))
        self.assertEqual(job['previous_job_id'],'job-1')
        self.assertEqual(job['spec_path'],str(self.spec))
        identity = self.resume_identity()
        self.assertTrue(identity['native_resume'])
        self.assertEqual(identity['worker_pid'],60001)
        self.assertEqual(identity['resume_evidence'],{'result.json':SESSION_ID,'started.json':SESSION_ID})
        self.assertNotIn('native_resumed',identity,'the native confirmation is the worker\'s, not the queue\'s')
        self.assertEqual(json.loads((self.slot/'status.json').read_text(encoding='utf-8'))['status'],'starting')
        # Nothing in the interrupted run was overwritten or replayed.
        self.assertEqual({name:(self.previous/name).read_bytes() for name in tree(self.previous)},self.before)
        self.assertEqual(sorted(p.name for p in (self.slot/'jobs').iterdir()),[job['job_id']+'.json'])
        self.assertNotIn('started.json',tree(self.output))

    def test_newer_request_json_session_id_still_matches(self):
        ds.save(self.previous/'request.json',dict(json.loads((self.previous/'request.json').read_text(encoding='utf-8')),
                                                  session_id=SESSION_ID))
        before = (self.previous/'request.json').read_bytes()
        result,_ = self.resume()
        self.assertTrue(result['resume_requested'])
        self.assertEqual((self.previous/'request.json').read_bytes(),before)
        self.assertEqual(self.resume_identity()['resume_evidence'],
                         {'request.json':SESSION_ID,'result.json':SESSION_ID,'started.json':SESSION_ID})

    def fresh(self, mutate):
        """Run one resume attempt against a fresh fixture with one evidence change."""
        case = type(self)('test_resume_queues_a_followup_on_the_same_session_and_keeps_evidence')
        case.setUp(); self.addCleanup(case.doCleanups)
        mutate(case)
        try:
            result,_ = case.resume()
        except Exception as error:
            return error
        return result

    def test_resume_uses_only_consistent_worker_written_evidence(self):
        # A worker-written file without a session id carries no claim, so the
        # other worker file still proves the association ...
        missing = self.fresh(lambda c: ds.save(c.previous/'started.json',{'status':'failed'}))
        self.assertTrue(missing['resume_requested'])
        # ... while a mismatching one is refused outright.
        for name in ('started.json','result.json'):
            with self.subTest(name=name):
                conflict = self.fresh(lambda c,name=name: ds.save(c.previous/name,
                                                                  {'session_id':'competition-qa-tooling-other'}))
                self.assertIsInstance(conflict,ValueError)
        # request.json cannot outvote the worker evidence.
        override = self.fresh(lambda c: ds.save(c.previous/'request.json',
                                                dict(json.loads((c.previous/'request.json').read_text(encoding='utf-8')),
                                                     session_id='competition-qa-tooling-other')))
        self.assertIsInstance(override,ValueError)

    def test_a_run_without_worker_written_session_evidence_is_refused(self):
        # Even a request.json that claims the session cannot prove it: the
        # worker-written started/result files are the association evidence.
        def strip(c):
            ds.save(c.previous/'request.json',dict(json.loads((c.previous/'request.json').read_text(encoding='utf-8')),
                                                   session_id=SESSION_ID))
            (c.previous/'started.json').unlink(); (c.previous/'result.json').unlink()
        refused = self.fresh(strip)
        self.assertIsInstance(refused,ValueError)
        self.assertIn('no worker-written session evidence',str(refused))

    def test_resume_clears_a_stop_marker_only_after_the_supervisor_starts(self):
        (self.slot/'STOP').write_text('')
        self.resume()
        self.assertFalse((self.slot/'STOP').exists())
        # A failed spawn must leave the stop request in place: the slot is still
        # stopped, and an immediate worker exit would hide that from the operator.
        (self.slot/'STOP').write_text('')
        ds.save(self.slot/'status.json',{'status':'stopped','heartbeat':time.time(),'session_id':SESSION_ID,
                                         'worker_pid':51704})
        with patch.object(ds,'git',return_value='base'), patch.object(ds,'fingerprint',return_value='snapshot'), \
             patch.object(ds,'alive',return_value=False), \
             patch.object(ds,'_spawn_worker',side_effect=OSError('cannot spawn')):
            with self.assertRaises(OSError):
                ds.resume_worker(self.pool,self.manifest,self.previous,SESSION_ID,self.root/'run-3')
        self.assertTrue((self.slot/'STOP').exists(),'a failed resume must not clear the stop request')

    def test_resume_refuses_a_live_worker(self):
        with self.assertRaisesRegex(ValueError,'still alive'):
            self.resume(worker_alive=True)
        self.assertFalse((self.slot/'jobs').exists())
        self.assertFalse(self.output.exists())
        self.assertEqual((self.previous/'result.json').read_bytes(),self.before['result.json'])

    def test_resume_refuses_a_non_failed_session_or_a_changed_identity(self):
        ds.save(self.slot/'status.json',{'status':'running','session_id':SESSION_ID,'worker_pid':51704})
        with self.assertRaisesRegex(ValueError,'not failed or stopped'):
            self.resume()
        ds.save(self.slot/'status.json',{'status':'failed','session_id':SESSION_ID,'worker_pid':51704})
        cases = {'specialist':'strategy','worktree':str(self.root/'other'),'provider':'other','model':'other',
                 'reasoningEffort':'low','base_sha':'other','approved_by':'dsh','spec_sha256':'other'}
        for field,value in cases.items():
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.resume(manifest={field:value})
        with self.assertRaisesRegex(ValueError,'Expected session ID'):
            self.resume(expect_session='')
        with self.assertRaisesRegex(ValueError,'another native session'):
            self.resume(expect_session='competition-qa-tooling-deadbeef')
        self.assertFalse(self.output.exists())

    def test_ordinary_send_never_silently_replaces_a_failed_session(self):
        """A failed slot must route to the explicit resume flow, not to a new session."""
        before = (self.slot/'identity.json').read_bytes()
        with patch.object(ds,'git',return_value='base'), patch.object(ds,'fingerprint',return_value='snapshot'), \
             patch.object(ds,'alive',return_value=False), patch.object(ds.subprocess,'Popen') as spawn:
            with self.assertRaisesRegex(ValueError,'Session stopped'):
                ds.submit(self.pool,self.manifest,self.root/'run-new')
            with self.assertRaisesRegex(ValueError,'Session stopped'):
                ds.start(self.pool,'qa-tooling',self.worktree)
            spawn.assert_not_called()
        self.assertEqual((self.slot/'identity.json').read_bytes(),before)
        self.assertFalse((self.root/'run-new').exists())

    def test_resume_never_overwrites_an_existing_output(self):
        self.output.mkdir(); (self.output/'result.json').write_text('{"status":"completed"}',encoding='utf-8')
        before = (self.output/'result.json').read_bytes()
        with self.assertRaisesRegex(ValueError,'already has a result'):
            self.resume()
        self.assertEqual((self.output/'result.json').read_bytes(),before)

    def test_cli_resume_requires_the_original_identity_arguments(self):
        argv = ['dsh_sessions.py','resume','--pool',str(self.pool),'--manifest',str(self.root/'m.json'),
                '--previous',str(self.previous),'--output',str(self.output)]
        with patch.object(ds.sys,'argv',argv):
            with self.assertRaises(SystemExit): ds.main()


class RecordingSDK:
    """SDK stand-in that records how worker() resumes and prompts."""
    def __init__(self):
        self.resumed = None; self.steering = True; self.broken = False
        self.events = []; self.prompts = []
    def resume(self, session_id):
        self.events.append(('resume',session_id))
        self.resumed = confirmation(session_id)
        return self.resumed
    def prompt(self, session, task, interventions=None, progress=None):
        self.events.append(('prompt',session))
        self.prompts.append({'session':session,'task':task})
        progress(1); progress(None)
        return dict(ds.MODEL,session_id=session,message_id='m1',turn=1,status='completed',
                    end_reason={'kind':'completed'},answer='resumed answer',review_required=True,steering_requests=[])
    def close(self): pass


class WorkerResumeTests(unittest.TestCase):
    """worker() must confirm the native resume before it prompts, and never replay."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.pool = self.root/'pool'; self.slot = self.pool/'qa-tooling'
        self.slot.mkdir(parents=True); (self.slot/'jobs').mkdir()
        self.worktree = self.root/'worktree'; self.worktree.mkdir()
        (self.worktree/'.git').write_text('gitdir: fixture')
        self.spec = self.root/'spec.md'; self.spec.write_text('follow-up requirements')
        self.out = self.root/'run-2'; self.out.mkdir()
        self.identity = {'provider':'deepseek-official','model':'deepseek-flash','reasoningEffort':'max',
                         'specialist':'qa-tooling','worktree':str(self.worktree),
                         'session_id':SESSION_ID,'worker_pid':60001,'native_resume':True}
        ds.save(self.slot/'identity.json',self.identity)
        ds.save(self.slot/'status.json',{'status':'starting','session_id':SESSION_ID,'worker_pid':60001})
        ds.save(self.out/'request.json',{'approved_by':'codex','specialist':'qa-tooling','worktree':str(self.worktree),
                                         'output':str(self.out),'spec_path':str(self.spec),'spec_sha256':ds.sha(self.spec),
                                         'base_sha':'base','workspace_sha256':'snapshot','job_id':'job-2','session_id':SESSION_ID})
        ds.save(self.out/'status.json',{'status':'queued','session_id':SESSION_ID})
        ds.save(self.slot/'jobs'/'job-2.json',{'approved_by':'codex','specialist':'qa-tooling','worktree':str(self.worktree),
                                               'output':str(self.out),'spec_path':str(self.spec),
                                               'spec_sha256':ds.sha(self.spec),'base_sha':'base','workspace_sha256':'snapshot',
                                               'job_id':'job-2','session_id':SESSION_ID})
        self.created = {}

    def run_worker(self, sdk):
        """Run one worker loop with a fake SDK that mirrors the real init/resume order.

        The loop terminates after the queued job: nothing clears an existing stop
        request except a successful resume, so the tests own the exit condition.
        """
        def factory(cwd, stderr, patch=None, window=.2, resume=None):
            self.created.update(cwd=str(cwd), init_resume=resume)
            if resume is not None: sdk.resume(resume)
            return sdk
        with patch.object(ds,'SDK',side_effect=factory), patch.object(ds,'git',return_value='base'), \
             patch.object(ds,'fingerprint',return_value='snapshot'):
            def stop_after_job():
                # A resumed worker runs until its stop request; emulate the
                # operator's later `stop` once the follow-up result exists.
                for _ in range(200):
                    if (self.out/'result.json').exists() or not (self.slot/'jobs').exists(): break
                    time.sleep(.01)
                (self.slot/'STOP').write_text('')
            stopper = threading.Thread(target=stop_after_job,daemon=True); stopper.start()
            ds.worker(self.pool,'qa-tooling')
            stopper.join(timeout=5)
        return sdk

    def test_worker_resumes_native_session_then_prompts_and_records_confirmation(self):
        sdk = self.run_worker(RecordingSDK())
        self.assertEqual(self.created['init_resume'],SESSION_ID)
        # Resume precedes the follow-up prompt, on the same original session.
        self.assertEqual(sdk.events,[('resume',SESSION_ID),('prompt',SESSION_ID)])
        self.assertEqual([p['session'] for p in sdk.prompts],[SESSION_ID])
        self.assertIn('follow-up requirements',sdk.prompts[0]['task'])
        result = json.loads((self.out/'result.json').read_text(encoding='utf-8'))
        self.assertEqual(result['session_id'],SESSION_ID)
        self.assertEqual(result['status'],'completed')
        self.assertEqual((self.out/'answer.md').read_text(encoding='utf-8'),'resumed answer')
        saved = json.loads((self.out/'native-resume.json').read_text(encoding='utf-8'))
        self.assertTrue(saved['resumed'])
        self.assertFalse(saved['created'])
        identity = json.loads((self.slot/'identity.json').read_text(encoding='utf-8'))
        self.assertEqual(identity['session_id'],SESSION_ID)
        self.assertEqual(identity['native_resumed']['sessionId'],SESSION_ID)
        self.assertEqual(json.loads((self.slot/'status.json').read_text(encoding='utf-8'))['status'],'stopped')

    def test_worker_without_resume_flag_never_asks_for_resume(self):
        ds.save(self.slot/'identity.json',{k:v for k,v in self.identity.items() if k!='native_resume'})
        sdk = self.run_worker(RecordingSDK())
        self.assertIsNone(self.created['init_resume'])
        # An ordinary worker still prompts, but no resume confirmation is invented.
        self.assertEqual(sdk.events,[('prompt',SESSION_ID)])
        self.assertFalse((self.out/'native-resume.json').exists())

    def test_failed_native_resume_leaves_the_followup_unstarted_and_visible(self):
        def factory(cwd, stderr, patch=None, window=.2, resume=None):
            self.created.update(init_resume=resume)
            raise RuntimeError('Native resume failed for '+str(resume)+': session not found')
        with patch.object(ds,'SDK',side_effect=factory), patch.object(ds,'git',return_value='base'), \
             patch.object(ds,'fingerprint',return_value='snapshot'):
            ds.worker(self.pool,'qa-tooling')
        self.assertEqual(self.created['init_resume'],SESSION_ID)
        status = json.loads((self.slot/'status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['status'],'failed')
        self.assertIn('session not found',status['error'])
        self.assertFalse((self.out/'started.json').exists(),'no turn may start without a native resume')
        self.assertFalse((self.out/'answer.md').exists())
        self.assertFalse((self.out/'native-resume.json').exists())

    def test_interrupted_job_is_never_replayed_by_a_later_worker(self):
        # A job whose turn already started but never produced a result must be
        # reported for review, not silently re-prompted on the resumed session.
        ds.save(self.out/'started.json',{'session_id':SESSION_ID})
        sdk = self.run_worker(RecordingSDK())
        self.assertEqual(sdk.events,[('resume',SESSION_ID)])
        self.assertFalse((self.out/'result.json').exists(),'an interrupted job is never replayed')
        status = json.loads((self.slot/'status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['status'],'failed')
        self.assertIn('refusing replay',status['error'])


if __name__=='__main__': unittest.main()
