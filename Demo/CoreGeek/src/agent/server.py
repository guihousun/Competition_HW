"""Competition HTTP server plus the local web/debug extensions.

Contract kept intact (AGENTS.md item 7, R01):
  * ``bash run.sh <port>`` / ``python main.py <port>`` starts this server on 0.0.0.0
  * the root path POST is the competition entry and answers ``roleCommandMap``
  * a failing decision still answers ``{"roleCommandMap": {}}`` with HTTP 200, so a
    viewer problem can never change what a judge receives

Local extensions used only by web/: the ``/debug/*`` JSON endpoints and the static
page. They are optional; removing them does not affect the judge path.
"""
import json
import logging
import os
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import debug, diagnostics, telemetry, twomatch, recordings
from .brain import decide, respond, decision_report
from .scenarios import observation

LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[4]
WEB_ROOT = ROOT / "web"

DEBUG_STEP = "/debug/step"
DEBUG_SERIES = "/debug/series"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CompetitionHW"

    # ---- shared helpers --------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.path).query)

    # ---- GET: static page, official sample, local debug API --------------
    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        try:
            if path.startswith("/debug/"):
                self._debug_get(path)
                return
            self._static(path)
        except Exception:  # pragma: no cover - keeps the server alive
            LOGGER.exception("GET %s failed", path)
            self._json(500, {"error": "internal error"})

    def _debug_get(self, path: str) -> None:
        query = self._query()
        if path == '/debug/llm/status':
            from .local_llm import SERVICE
            self._json(200, SERVICE.status())
            return
        if path in ('/debug/recording', '/debug/recording/result'):
            try:
                job_id = query.get('id', [''])[0]
                self._json(200, recordings.JOBS.result(job_id) if path.endswith('/result')
                           else recordings.JOBS.snapshot(job_id))
            except KeyError as error:
                self._json(404, {'error': str(error)})
            except RuntimeError as error:
                self._json(409, {'error': str(error)})
            return
        if path == "/debug/stats":
            self._json(200, debug.stats_payload())
            return
        if path == "/debug/rules":
            self._json(200, {"rows": debug.RULE_ROWS, "notes": debug.mismatch_notes()})
            return
        if path == "/debug/strategy":
            from . import strategy_config
            self._json(200, strategy_config.public_view())
            return
        if path == "/debug/scenario":
            try:
                self._json(200, debug.scenario_payload(
                    query.get("seed", ["1"])[0],
                    query.get("side", ["challenger"])[0],
                    query.get("pressure", ["1"])[0],
                    query.get("profile", [None])[0],
                    map_layout=query.get("map_layout", [None])[0],
                ))
            except ValueError as error:
                self._json(400, {"error": str(error)})
            return
        if path == "/debug/series":
            try:
                self._json(200, debug.series_payload(
                    query.get("seed", ["1"])[0],
                    query.get("side", ["challenger"])[0],
                    query.get("pressure", ["1"])[0],
                    query.get("limit", [None])[0],
                    profile=query.get("profile", [None])[0],
                    map_layout=query.get("map_layout", [None])[0],
                ))
            except ValueError as error:
                self._json(400, {"error": str(error)})
            return
        if path == "/debug/twomatch":
            self._json(200, twomatch.snapshot())
            return
        if path == "/debug/twomatch/start":
            try:
                self._json(200, twomatch.start(
                    query.get("seed", ["1"])[0],
                    query.get("pressure", ["1"])[0],
                    query.get("rounds", ["1300"])[0],
                    query.get("profile", [None])[0],
                ))
            except ValueError as error:
                self._json(400, {"error": str(error)})
            return
        if path == "/debug/twomatch/stop":
            self._json(200, twomatch.stop())
            return
        self._json(404, {"error": "unknown debug endpoint"})

    def _static(self, path: str) -> None:
        if path == "/sample":
            self._send(200, (ROOT / "docs/request.txt").read_bytes(),
                       "application/json; charset=utf-8")
            return
        if path == "/health":
            self._json(200, {"ok": True})
            return
        relative = "index.html" if path == "/" else path.lstrip("/")
        asset = debug.load_asset(str(WEB_ROOT), relative)
        if asset is None:
            self.send_error(404)
            return
        body, content_type = asset
        self._send(200, body, content_type)

    # ---- POST: competition decision + local step/series -------------------
    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        local = path.startswith("/debug/")
        try:
            ticket = None if local else telemetry.begin()
        except Exception:
            ticket = None
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            if local:
                self._json(400, {"error": f"请求体不是合法 JSON：{error}"})
            else:
                # Official contract: answer a usable (empty) command map, never an error.
                self._judge_response(ticket, raw, None, {"roleCommandMap": {}}, 0.0, invalid_input=True)
            return
        if not isinstance(payload, dict):
            if local:
                self._json(400, {"error": "请求体必须是 JSON 对象"})
            else:
                self._judge_response(ticket, raw, None, {"roleCommandMap": {}}, 0.0, invalid_input=True)
            return
        if path.startswith("/debug/"):
            self._debug_post(path, payload)
            return
        # Competition path: never surface an error page or a hang to a judge.
        started = time.perf_counter()
        decision_error: str | None = None
        fault = None
        decision = None
        try:
            response = respond(observation(payload))
            try:
                decision = decision_report()
            except Exception:
                decision = None
        except Exception as error:
            LOGGER.error("decision failed (%s); details are in the local trace", type(error).__name__)
            response = {"roleCommandMap": {}}
            decision_error = "internal_error"
            try:
                fault = {'category': 'decision_exception', 'type': type(error).__name__,
                         'message': telemetry.clean(str(error))[:500],
                         'frames': [{'file': Path(frame.filename).name, 'function': frame.name, 'line': frame.lineno}
                                    for frame in traceback.extract_tb(error.__traceback__)[-12:]]}
            except Exception:
                fault = {'category': 'decision_exception'}
        # Planning duration covers only this request handling region: startup time
        # is never reported as a request timeout.
        plan_ms = (time.perf_counter() - started) * 1000.0
        LOGGER.debug("round %s -> %d commands", payload.get("roundNo"),
                    len(response["roleCommandMap"]))
        self._judge_response(ticket, raw, payload, response, plan_ms,
                             decision_exception=decision_error, fault=fault, decision=decision)

    def _judge_response(self, ticket, raw, payload, response, plan_ms, *,
                        invalid_input=False, decision_exception=None, fault=None, decision=None):
        body = json.dumps(response, ensure_ascii=False).encode('utf-8')
        sent = False
        from . import ordered_diagnostics
        summary_ticket = ordered_diagnostics.reserve(lambda: self._diagnose(
            payload, response if sent else {}, plan_ms, invalid_input=invalid_input,
            decision_exception=decision_exception if sent else 'response_write_failed',
            event_id=getattr(ticket, 'event_id', None), decision=decision))
        try:
            self._send(200, body, 'application/json; charset=utf-8')
            sent = True
        finally:
            ordered_diagnostics.complete(summary_ticket)
            # Immutable wire bytes go to a bounded queue AFTER the HTTP write.
            # Redaction, diffing and all file I/O happen on the writer thread.
            try:
                telemetry.submit(ticket, raw, body, sent=sent, plan_ms=plan_ms,
                                 invalid_input=invalid_input, fault=fault, decision=decision)
            except Exception:
                pass

    def _diagnose(self, payload, response, plan_ms: float, *, invalid_input: bool = False,
                  decision_exception: str | None = None, event_id: str | None = None, decision=None) -> None:
        """Local engineering metadata only; never alters the judge response.

        Called after the response bytes are written, and fully fail-open: a
        diagnostics error can never change the answer or the gameplay.
        """
        try:
            diagnostics.response_summary(payload, response, plan_ms=plan_ms,
                                         invalid_input=invalid_input,
                                         decision_exception=decision_exception, event_id=event_id, decision=decision)
        except Exception:  # pragma: no cover - defence in depth
            LOGGER.debug("diagnostics summary skipped")

    def _debug_post(self, path: str, payload: dict[str, Any]) -> None:
        try:
            if path == '/debug/strategy':
                if self.client_address[0] not in ('127.0.0.1', '::1'):
                    self._json(403, {'error': '策略写入仅允许本机访问'})
                    return
                from . import strategy_config
                config = payload.get('config', payload)
                identity = strategy_config.save(config)
                self._json(200, {'saved': True, 'restart_required': True, 'identity': identity,
                                 'message': '策略已保存；请重启服务后让所有策略模块重新加载。'})
                return
            if (path.startswith('/debug/llm') or (payload.get('_demo') or {}).get('llm_enabled') or (payload.get('_demo') or {}).get('llm_pending')) and self.client_address[0] not in ('127.0.0.1', '::1'):
                self._json(403, {'error': '真实 LLM 调用仅允许本机访问'})
                return
            if path == '/debug/llm/scenario':
                self._json(200, debug.llm_scenario_payload(payload.get('seed', 1), payload.get('side', 'challenger'),
                    payload.get('kind', 'arithmetic'), payload.get('backend', 'openrouter'), payload.get('max_calls'),
                    payload.get('profile'), payload.get('pressure', 1), payload.get('map_layout')))
                return
            if path == '/debug/recording/start':
                try:
                    self._json(202, recordings.JOBS.start(payload.get('seed', 1),
                        payload.get('side', 'challenger'), payload.get('pressure', 1),
                        payload.get('limit', 1300), payload.get('profile'), payload.get('map_layout')))
                except RuntimeError as error:
                    self._json(409, {'error': str(error)})
                return
            if path == '/debug/recording/cancel':
                try:
                    self._json(200, recordings.JOBS.cancel(payload.get('id', '')))
                except KeyError as error:
                    self._json(404, {'error': str(error)})
                return
            if path == DEBUG_SERIES:
                self._json(200, debug.series_payload(
                    payload.get("seed", 1),
                    payload.get("side", "challenger"),
                    payload.get("pressure", 1),
                    payload.get("limit"),
                    profile=payload.get("profile"),
                    map_layout=payload.get("map_layout"),
                ))
                return
            if path == "/debug/screenshot":
                self._json(200, debug.save_screenshot(
                    payload.get("name"), payload.get("dataUrl")))
                return
            if path != DEBUG_STEP:
                self._json(404, {"error": f"unknown debug endpoint {path}"})
                return
            self._json(200, debug.step_payload(payload))
        except Exception as error:  # debug endpoints must explain their failures
            LOGGER.info("debug request failed: %s", error)
            self._json(400, {"error": f"{type(error).__name__}: {error}"})

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve(port: int) -> None:
    os.environ.setdefault('COMPETITION_HW_TASK_AGENT', 'on')
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
