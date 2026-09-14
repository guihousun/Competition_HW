"""Protocol supervision checks without invoking a model or network."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import dsh_runner


class Buffer(io.StringIO):
    def close(self):
        self.saved = self.getvalue()
        super().close()


class Process:
    pid=0
    returncode=0
    def __init__(self, frames):
        self.stdin=Buffer()
        self.stdout=io.StringIO(''.join(json.dumps(f)+'\n' for f in frames))
    def poll(self): return 0
    def wait(self, timeout=None): return 0


class RunnerTests(unittest.TestCase):
    def execute(self, kind='completed', text='done', initialize_error=False):
        session='competition-fixed'
        frames=[{'id':1,'result':{'serverInfo':{'name':'deepseek-harness-sdk-runtime'}}},
                {'id':2,'result':{'messageId':'m'}}]
        for etype,data in [('assistant/message',{'message':{'content':[{'type':'text','text':text}]}}),
                           ('turn/end',{'reason':{'kind':kind}})]:
            frames.append({'method':'session.event','params':{'sessionId':session,'event':{'type':etype,'data':data}}})
        frames += [{'method':'session.status','params':{'sessionId':session,'status':'idle'}}, {'id':3,'result':{}}]
        if initialize_error: frames=[{'id':1,'error':{'message':'unsupported model/effort'}}]
        proc=Process(frames)
        with tempfile.TemporaryDirectory() as temp, patch.object(dsh_runner,'launch',return_value=proc), patch.object(dsh_runner.uuid,'uuid4') as uuid:
            uuid.return_value.hex='fixed'
            result=dsh_runner.run_task(temp,'task',Path(temp)/'result',timeout=3)
        sent=[json.loads(line) for line in proc.stdin.saved.splitlines()]
        return result,sent

    def test_exact_model_and_effort_on_handshake(self):
        result,sent=self.execute()
        self.assertEqual(result['status'],'completed')
        self.assertEqual(sent[0]['params']['model'],'deepseek-flash')
        self.assertEqual(sent[0]['params']['reasoningEffort'],'max')
        self.assertEqual(sent[-1]['method'],'shutdown')

    def test_error_end_is_not_success(self):
        self.assertEqual(self.execute(kind='error')[0]['status'],'failed')

    def test_empty_answer_is_not_success(self):
        self.assertEqual(self.execute(text='')[0]['status'],'failed')

    def test_rejected_model_does_not_fallback_or_send_task(self):
        result,sent=self.execute(initialize_error=True)
        self.assertEqual(result['status'],'failed')
        self.assertEqual([f['method'] for f in sent],['initialize'])
