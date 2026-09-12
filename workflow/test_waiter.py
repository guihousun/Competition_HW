import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import dsh_sessions as ds


class WaiterTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root=Path(tmp.name); self.pool=root/'pool'; self.out=root/'out'; self.out.mkdir()
        self.slot=self.pool/'qa-tooling'; self.slot.mkdir(parents=True)
        ds.save(self.out/'request.json',{'output':str(self.out),'specialist':'qa-tooling'})
        ds.save(self.out/'status.json',{'session_id':'native','status':'running'})
        ds.save(self.slot/'identity.json',{'session_id':'native','worker_pid':123})
        ds.save(self.slot/'status.json',{'session_id':'native','status':'running'})

    def finish(self, status='completed', session='native'):
        (self.out/'answer.md').write_text('DSH response',encoding='utf-8')
        ds.save(self.out/'result.json',{'status':status,'session_id':session})

    def test_returns_answer_when_current_turn_finishes_without_stopping_worker(self):
        before=(self.slot/'identity.json').read_bytes()
        def delayed(): time.sleep(.03); self.finish()
        thread=threading.Thread(target=delayed); thread.start()
        with patch.object(ds,'alive',return_value=True), patch.object(ds,'submit') as submit, patch.object(ds.subprocess,'run') as kill:
            result=ds.wait_result(self.pool,self.out,2,.005)
            submit.assert_not_called(); kill.assert_not_called()
        thread.join()
        self.assertEqual(result['answer'],'DSH response')
        self.assertEqual(result['session_id'],'native')
        self.assertEqual((self.slot/'identity.json').read_bytes(),before)
        self.assertFalse((self.slot/'STOP').exists())

    def test_timeout_keeps_job_and_can_wait_again(self):
        before=(self.out/'request.json').read_bytes()
        with patch.object(ds,'alive',return_value=True), self.assertRaises(TimeoutError):
            ds.wait_result(self.pool,self.out,.01,.005)
        self.assertEqual((self.out/'request.json').read_bytes(),before)
        self.assertFalse((self.slot/'STOP').exists())
        self.finish()
        self.assertEqual(ds.wait_result(self.pool,self.out,1)['status'],'completed')

    def test_failure_is_returned_as_failure(self):
        self.finish('failed')
        self.assertEqual(ds.wait_result(self.pool,self.out,1)['status'],'failed')

    def test_wrong_session_result_is_rejected(self):
        self.finish(session='different')
        with self.assertRaisesRegex(ValueError,'another native session'):
            ds.wait_result(self.pool,self.out,1)

    def test_dead_worker_is_reported_without_waiting_for_deadline(self):
        with patch.object(ds,'alive',return_value=False), self.assertRaisesRegex(RuntimeError,'Worker exited'):
            ds.wait_result(self.pool,self.out,10)
