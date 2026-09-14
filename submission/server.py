"""Competition-only server, packaged as src/agent/server.py.

R01: same direct main3.py -> agent.server entry and HTTP/1.0 as the working
official sample. The local web server remains a separate repository entry.
"""
import json
import logging
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .brain import respond
from .scenarios import observation
from . import diagnostics, telemetry

LOGGER = logging.getLogger(__name__)


class Handler(BaseHTTPRequestHandler):
    # Deliberately inherit BaseHTTPRequestHandler's HTTP/1.0, as the sample does.
    def do_POST(self):
        try:
            ticket = telemetry.begin()
        except Exception:
            ticket = None
        raw = b''
        payload = None
        response = {'roleCommandMap': {}}
        invalid = False
        fault = None
        started = time.perf_counter()
        try:
            length = int(self.headers.get('Content-Length') or 0)
            if length < 0:
                raise ValueError('negative content length')
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode('utf-8'))
            if not isinstance(payload, dict):
                raise ValueError('request must be an object')
        except (ValueError, UnicodeError):
            invalid = True
        if not invalid:
            try:
                response = respond(observation(payload))
            except Exception as error:
                LOGGER.error('decision failed (%s)', type(error).__name__)
                try:
                    fault = {'category': 'decision_exception', 'type': type(error).__name__,
                             'message': telemetry.clean(str(error))[:500],
                             'frames': [{'file': Path(f.filename).name, 'function': f.name, 'line': f.lineno}
                                        for f in traceback.extract_tb(error.__traceback__)[-12:]]}
                except Exception:
                    fault = {'category': 'decision_exception'}
        elapsed = (time.perf_counter()-started)*1000
        body = json.dumps(response, ensure_ascii=False).encode('utf-8')
        sent = False
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            sent = True
        finally:
            try:
                telemetry.submit(ticket, raw, body, sent=sent, plan_ms=elapsed,
                                 invalid_input=invalid, fault=fault)
            except Exception:
                pass
        try:
            diagnostics.response_summary(payload, response, plan_ms=elapsed, invalid_input=invalid,
                                         decision_exception='internal_error' if fault else None,
                                         event_id=getattr(ticket, 'event_id', None))
        except Exception:
            pass

    def do_GET(self):
        body = b'{"status":"ready","mode":"competition"}'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def serve(port):
    # Bind before optional logging work. No local viewer or simulator is imported.
    http = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    trace = None
    try:
        root = Path(__file__).resolve().parents[2]
        diagnostics.utf8_stdout()
        identity = diagnostics.emit_startup_identity(entry='main3.py', root=root)
        trace = telemetry.configure(root, identity)
        http.serve_forever()
    finally:
        http.server_close()
        if trace is not None:
            trace.close()
