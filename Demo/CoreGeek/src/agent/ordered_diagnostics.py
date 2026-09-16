"""Reserve console order before exposing HTTP bytes, process after the write.

Only logging uses this bounded queue. No game actions or model work run here.
"""
import atexit
import logging
import queue
import threading
import time


class OrderedDiagnostics:
    def __init__(self, capacity=64):
        self.queue = queue.Queue(maxsize=capacity)
        self.lock = threading.Lock()
        self.dropped = 0
        self.worker = threading.Thread(target=self._run, name='ordered-console', daemon=True)
        self.worker.start()

    def reserve(self, callback):
        ticket = (threading.Event(), callback)
        try:
            self.queue.put_nowait(ticket)
            return ticket
        except queue.Full:
            with self.lock:
                self.dropped += 1
            return None

    @staticmethod
    def complete(ticket):
        if ticket is not None:
            ticket[0].set()

    def _run(self):
        while True:
            ready, callback = self.queue.get()
            try:
                ready.wait()  # worker only; never blocks an HTTP request
                with self.lock:
                    dropped, self.dropped = self.dropped, 0
                if dropped:
                    logging.getLogger(__name__).warning('diagnostics_queue_dropped count=%d', dropped)
                callback()
            except Exception:
                logging.getLogger(__name__).warning('diagnostics_summary_failed')
            finally:
                self.queue.task_done()

    def drain(self, timeout=1.0):
        deadline = time.monotonic() + timeout
        while self.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(.005)
        return self.queue.unfinished_tasks == 0


_sink = None
_lock = threading.Lock()


def reserve(callback):
    global _sink
    try:
        with _lock:
            if _sink is None:
                _sink = OrderedDiagnostics()
                atexit.register(_sink.drain)
        return _sink.reserve(callback)
    except Exception:
        return None  # optional telemetry must never change a game response


def complete(ticket):
    try:
        OrderedDiagnostics.complete(ticket)
    except Exception:
        pass
