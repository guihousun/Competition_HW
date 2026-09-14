import json
import threading
import time
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from test_baseline import fixture
from agent.deepseek_client import DeepSeekClient, NoRedirect
from agent.local_llm import LocalLLM
from agent import local_llm, debug
from agent.planner import PlannerState
from agent.sandbox import PendingRequest, parse_command_result
from agent.server import Handler


class FakeClient:
    configured = True
    def __init__(self):
        self.prompts = []
    def complete(self, prompt):
        self.prompts.append(prompt)
        return {'answer': 'result=42', 'usage': {'total_tokens': 8}}


class LocalLLMTests(unittest.TestCase):
    def test_full_task_chain_uses_model_answer_and_no_private_answer_key(self):
        client = FakeClient()
        service = LocalLLM(client)
        with patch.object(local_llm, 'SERVICE', service):
            state = debug.llm_scenario_payload()['state']
            rewards = None
            for _ in range(18):
                result = debug.step_payload(json.loads(json.dumps(state)))
                if result.get('pending'):
                    time.sleep(.01)
                    continue
                state = result['state']
                rewards = state['_demo'].get('task_report', {}).get('rewards')
                if rewards:
                    break
            self.assertIsNotNone(rewards)
            self.assertEqual(1.0, rewards['rate'])
            self.assertEqual('result=42', rewards['answer'])
            self.assertEqual(1, service.calls)
            self.assertNotIn('42', client.prompts[0])
            self.assertNotIn('_demo', client.prompts[0])
            self.assertGreaterEqual(state['_demo']['planner']['judge']['llmResponses'], 1)

    def test_pending_poll_does_not_advance_or_duplicate_request(self):
        entered, release = threading.Event(), threading.Event()
        class Slow(FakeClient):
            def complete(self, prompt):
                entered.set(); release.wait(3)
                return super().complete(prompt)
        service = LocalLLM(Slow())
        with patch.object(local_llm, 'SERVICE', service):
            identity = service.submit('run', 10, 'test')
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(identity, service.submit('run', 10, 'test'))
                state = fixture(); state['_demo'] = {'llm_pending': identity}
                for _ in range(3):
                    result = debug.step_payload(state)
                    self.assertTrue(result['pending'])
                    self.assertEqual(state, result['state'])
                self.assertEqual(1, service.calls)
            finally:
                release.set()

    def test_budget_failure_and_provider_exception_are_not_answers(self):
        service = LocalLLM(FakeClient(), max_calls=0)
        result = service.poll(service.submit('run', 1, 'question'))
        self.assertEqual('local_call_budget_reached', result['error'])
        self.assertNotIn('answer', result)
        class Failing(FakeClient):
            def complete(self, prompt): raise ValueError('sensitive-provider-diagnostics')
        service = LocalLLM(Failing())
        key = service.submit('run', 1, 'question')
        for _ in range(100):
            result = service.poll(key)
            if result['status'] != 'running': break
            time.sleep(.01)
        self.assertEqual('provider_failed', result['error'])
        self.assertNotIn('sensitive', json.dumps(result))

    def test_pending_and_command_result_survive_json_roundtrip(self):
        state = PlannerState()
        state.judge.pending_prompt = PendingRequest('prompt', 10, 'question')
        state.judge.pending_cmd = PendingRequest('cmd', 10, 'pwd')
        state.judge.last_result = parse_command_result('[exitCode:7]\noutput')
        restored = PlannerState.load(json.loads(json.dumps(state.dump())))
        self.assertEqual(state.judge.pending_prompt, restored.judge.pending_prompt)
        self.assertEqual(state.judge.pending_cmd, restored.judge.pending_cmd)
        self.assertEqual(7, restored.judge.last_result.exit_code)
        restored.note_results({'llmResp': 'answer'}, 10)
        self.assertIsNotNone(restored.judge.pending_prompt)
        restored.note_results({'llmResp': 'answer'}, 11)
        self.assertIsNone(restored.judge.pending_prompt)
        self.assertEqual(1, restored.judge.llm_responses)

    def test_api_shape_discards_reasoning_and_credentials(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit):
                return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'result=42','reasoning_content':'private-reasoning'}}],
                                   'usage':{'total_tokens':9}}).encode()
        class Opener:
            def open(self, request, timeout):
                self.request = request
                return Response()
        opener = Opener()
        client = DeepSeekClient(key='fake-unit-test-key', opener=opener)
        result = client.complete('math')
        body = json.loads(opener.request.data)
        self.assertEqual('deepseek-flash', body['model'])
        self.assertEqual('max', body['reasoning_effort'])
        self.assertNotIn('fake-unit-test-key', json.dumps(result))
        self.assertNotIn('private-reasoning', json.dumps(result))
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'https://other.invalid'))

    def test_official_http_keeps_optional_channels_without_calling_api(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        expected = {'roleCommandMap':{}, 'prompt':'approved prompt', 'executeCmd':'pwd'}
        try:
            with patch('agent.server.respond', return_value=expected), patch.object(local_llm.SERVICE, 'submit') as submit:
                req = Request(f'http://127.0.0.1:{server.server_port}/', json.dumps(fixture()).encode(),
                              {'Content-Type':'application/json'})
                with urlopen(req, timeout=5) as response:
                    self.assertEqual(expected, json.load(response))
                submit.assert_not_called()
        finally:
            server.shutdown(); server.server_close(); thread.join(2)


if __name__ == '__main__': unittest.main()
