"""Local-only DeepSeek adapter. Credentials and reasoning never enter game state."""
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.request

ENDPOINT = 'https://openrouter.ai/api/v1/chat/completions'
PROVIDER = 'OpenRouter'
MODEL = 'deepseek/deepseek-v4.1-flash'
EFFORT = 'max'
API_EFFORT = 'xhigh'  # user-approved local max mapping, same as the DSH worker


def credential():
    key = os.environ.get('OPENROUTER_API_KEY', '').strip()
    if key:
        return key
    path = Path(os.environ.get('LOCALAPPDATA', '')) / 'CompetitionHW/openrouter-key.dpapi'
    if os.name != 'nt' or not path.is_file():
        return ''
    # Windows DPAPI is tied to this OS user. Never print the result or shell errors.
    script = "$s=(Get-Content -Raw -LiteralPath (Join-Path $env:LOCALAPPDATA 'CompetitionHW/openrouter-key.dpapi')).Trim() | ConvertTo-SecureString; $p=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s); try {[Runtime.InteropServices.Marshal]::PtrToStringBSTR($p)} finally {[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p)}"
    try:
        result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, text=True, timeout=10,
                                env={k: v for k, v in os.environ.items() if k.lower() != 'psmodulepath'},
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError):
        return ''
    return result.stdout.strip() if result.returncode == 0 else ''


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DeepSeekClient:
    def __init__(self, key=None, opener=None, timeout=60):
        self._key = credential() if key is None else key
        self._opener = opener or urllib.request.build_opener(NoRedirect())
        self.timeout = timeout

    @property
    def configured(self):
        return bool(self._key)

    def complete(self, prompt):
        if not self._key:
            raise RuntimeError('missing_credential')
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16000:
            raise ValueError('invalid_prompt_length')
        payload = {'model': MODEL, 'messages': [{'role': 'user', 'content': prompt}],
                   'reasoning': {'effort': API_EFFORT, 'exclude': True},
                   'max_tokens': 4096, 'stream': False}
        request = urllib.request.Request(ENDPOINT, json.dumps(payload).encode('utf-8'),
                    {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self._key})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(524289)
            if len(raw) > 524288:
                raise ValueError('response_too_large')
            data = json.loads(raw)
            choice = data['choices'][0]
            answer = choice['message']['content']
            if choice.get('finish_reason') != 'stop' or not isinstance(answer, str) or not answer.strip():
                raise ValueError('incomplete_answer')
            if len(answer.encode('utf-8')) > 65536:
                raise ValueError('answer_too_large')
            # Only counters and final content; never return reasoning_content/raw headers.
            usage = {k: v for k, v in (data.get('usage') or {}).items()
                     if k in ('prompt_tokens', 'completion_tokens', 'total_tokens') and isinstance(v, int)}
            reported_model = data.get('model')
            return {'answer': answer, 'usage': usage,
                    'reported_model': reported_model if isinstance(reported_model, str) else None}
        except urllib.error.HTTPError as error:
            raise RuntimeError('provider_http_' + str(error.code)) from None
        except (TimeoutError, urllib.error.URLError):
            raise RuntimeError('provider_timeout_or_network') from None
        except Exception:
            raise RuntimeError('invalid_provider_response') from None
