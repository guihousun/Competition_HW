"""Nonblocking local judge channel, with bounded calls and repeat-request dedup."""
from copy import deepcopy
import hashlib
import threading
import time
import uuid
from .deepseek_client import DeepSeekClient, MODEL, EFFORT


class LocalLLM:
    def __init__(self, client=None, max_calls=20):
        self.client = client
        self.max_calls = max_calls
        self.calls = 0
        self.jobs = {}
        self.lock = threading.RLock()

    def _client(self):
        if self.client is None:
            self.client = DeepSeekClient()
        return self.client

    def status(self):
        with self.lock:
            return {'provider': 'DeepSeek', 'model': MODEL, 'effort': EFFORT,
                    'configured': self._client().configured, 'calls': self.calls,
                    'maxCalls': self.max_calls, 'note': '仅显式启用的本地场景调用，额度是本地费用保护，不是官方规则'}

    def submit(self, run_id, round_no, prompt):
        identity = hashlib.sha256((run_id + '\0' + str(round_no) + '\0' + prompt).encode()).hexdigest()
        with self.lock:
            if identity in self.jobs:
                return identity
            if len(self.jobs) >= 128:
                raise RuntimeError('local_request_history_full')
            job = {'status': 'running', 'started': time.monotonic(), 'usage': {}}
            self.jobs[identity] = job
            if not self._client().configured:
                job.update(status='failed', error='missing_credential')
            elif self.calls >= self.max_calls:
                job.update(status='failed', error='local_call_budget_reached')
            else:
                self.calls += 1
                threading.Thread(target=self._run, args=(job, prompt), daemon=True).start()
            return identity

    def _run(self, job, prompt):
        try:
            result = self._client().complete(prompt)
            with self.lock:
                job.update(result, status='done')
        except Exception as error:
            with self.lock:
                # Even custom providers may include secrets in exception messages.
                reason = str(error)
                allowed = reason.startswith('provider_http_') and reason[14:].isdigit()
                if not allowed and reason not in ('missing_credential', 'provider_timeout_or_network', 'invalid_provider_response'):
                    reason = 'provider_failed'
                job.update(status='failed', error=reason)
        finally:
            with self.lock:
                job['elapsed'] = round(time.monotonic() - job['started'], 2)

    def poll(self, identity):
        with self.lock:
            job = self.jobs.get(identity)
            if job is None:
                return {'status': 'failed', 'error': 'request_expired_after_restart'}
            return {k: deepcopy(v) for k, v in job.items() if k != 'started'}


SERVICE = LocalLLM()


def before_step(payload):
    """Return pending response without advancing, or a state with the prior answer."""
    state = deepcopy(payload)
    meta = state.get('_demo') or {}
    pending = meta.pop('llm_pending', None)
    if pending:
        result = SERVICE.poll(pending)
        if result['status'] == 'running':
            return payload, True
        state['llmResp'] = result.get('answer', '')
        meta['llm_status'] = {k: v for k, v in result.items() if k != 'answer'}
        meta['llm_status']['model'] = MODEL
    return state, False


def after_step(result):
    state = result['state']
    meta = state.get('_demo') or {}
    request = result.get('judgeRequest') or {}
    if request.get('executeCmd'):
        from .local_task_sandbox import active_task_fixture, execute
        fixture = active_task_fixture(state)
        state['lastCmdResult'] = (execute(request['executeCmd'], fixture, active=True)
                                  if fixture is not None else
                                  '[JUDGER_ERROR]\n本地沙盒执行器未接入：此场景没有虚拟沙盒 fixture')
    if not request.get('prompt'):
        return result
    if not meta.get('llm_enabled'):
        meta['llm_status'] = {'status': 'disabled', 'model': MODEL}
        return result
    run_id = meta.setdefault('llm_run_id', uuid.uuid4().hex)
    identity = SERVICE.submit(run_id, result['frame']['round'], request['prompt'])
    meta['llm_pending'] = identity
    meta['llm_status'] = {'status': 'running', 'model': MODEL}
    return result
