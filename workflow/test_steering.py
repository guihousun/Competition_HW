import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import dsh_sessions as ds
from test_sessions import FakeProcess


class SteeringProcess(FakeProcess):
    def __init__(self, reject=False): super().__init__(); self.reject=reject
    def write(self,line):
        f=json.loads(line)
        if f['method'] not in ('session/prompt','competition/steer'): return super().write(line)
        self.sent.append(f); p=f['params']; sid=p['sessionId']
        def event(kind,seq,data):
            self.emit({'method':'session.event','params':{'sessionId':sid,'event':{'type':kind,'seq':seq,'data':data}}})
        if f['method']=='session/prompt':
            self.emit({'id':f['id'],'result':{'messageId':'initial'}})
            event('turn/start',1,{'turn':1})
        else:
            r={'requestId':p['requestId'],'sessionId':sid,'messageId':'steer-msg','turn':1,'accepted':True,'delivery':'next-step'}
            # Ack deliberately arrives after turn-end/idle.
            if not self.reject:
                self.emit({'method':'competition.steer_consumed','params':dict(r,consumed=True)})
            event('assistant/message',3,{'turn':1,'message':{'content':[{'type':'text','text':'BEFORE' if self.reject else 'AFTER'}]}})
            event('turn/end',4,{'turn':1,'reason':{'kind':'completed'}})
            self.emit({'method':'session.status','params':{'sessionId':sid,'status':'idle'}})
            self.emit({'id':f['id'],'error':{'message':'inactive; not queued'}} if self.reject else {'id':f['id'],'result':r})


class SteeringTests(unittest.TestCase):
    def test_steer_uses_native_method_same_turn_and_waits_for_ack(self):
        for rejected in (False,True):
            with self.subTest(rejected=rejected), tempfile.TemporaryDirectory() as temp:
                receipt=Path(temp)/'ack.json'; process=SteeringProcess(rejected)
                with patch.object(ds,'launch',return_value=process):
                    sdk=ds.SDK('/tree',None)
                    try:
                        result=sdk.prompt('native','original',3,interventions=lambda turn:[{'requestId':'r1','text':'change','receipt':str(receipt)}])
                        self.assertEqual(result['turn'],1)
                        self.assertEqual(result['answer'],'BEFORE' if rejected else 'AFTER')
                        self.assertFalse(sdk.broken)
                        self.assertEqual([f['method'] for f in process.sent].count('session/prompt'),1)
                        self.assertEqual([f['method'] for f in process.sent].count('competition/steer'),1)
                        self.assertEqual(json.loads(receipt.read_text())['accepted'],not rejected)
                    finally: sdk.close()

    def setup_request(self,temp):
        root=Path(temp); out=root/'out'; out.mkdir(); slot=root/'rules-engine'; slot.mkdir()
        ds.save(out/'request.json',{'job_id':'job','specialist':'rules-engine','worktree':'tree'})
        ds.save(slot/'identity.json',{'session_id':'native','native_steer':True})
        ds.save(slot/'status.json',{'status':'running','output':str(out),'active_turn':1})
        spec=root/'spec.md'; spec.write_text('new requirement')
        m={'approved_by':'codex','request_id':'r1','specialist':'rules-engine','job_output':str(out),'job_id':'job','session_id':'native','expected_turn':1,'base_sha':'head','spec_path':str(spec),'spec_sha256':ds.sha(spec)}
        return root,out,slot,m

    def test_duplicate_id_is_idempotent_and_conflicting_reuse_rejected(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(ds,'git',return_value='head'):
            root,out,slot,m=self.setup_request(temp)
            first=ds.request_steer(root,m)
            self.assertEqual(ds.request_steer(root,m),first)
            self.assertEqual(len(list((out/'steering').glob('*.request.json'))),1)
            with self.assertRaisesRegex(ValueError,'different content'):
                ds.request_steer(root,dict(m,expected_turn=2))

    def test_inactive_or_legacy_session_does_not_queue(self):
        for mode in ('legacy','ended','wrong-turn','wrong-session'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                root,out,slot,m=self.setup_request(temp)
                if mode=='legacy': ds.save(slot/'identity.json',{'session_id':'native'})
                if mode=='ended': ds.save(out/'result.json',{'status':'completed'})
                if mode=='wrong-turn': m['expected_turn']=2
                if mode=='wrong-session': m['session_id']='other'
                with self.assertRaises(ValueError): ds.request_steer(root,m)
                self.assertFalse((out/'steering').exists())

    def test_late_request_receipt_rejects_without_dispatching_new_turn(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'steering').mkdir(); ds.save(root/'result.json',{'status':'completed'})
            result=ds.wait_steer({'receipt':str(root/'steering/r1.ack.json')},1)
            self.assertFalse(result['accepted'])
