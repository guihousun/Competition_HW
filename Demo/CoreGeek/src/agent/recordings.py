"""Bounded, cancellable recording jobs. Local UI only; uses debug.series_payload."""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from .debug import series_payload


class RecordingJobs:
    def __init__(self, producer=series_payload):
        self.producer = producer
        self.lock = threading.RLock()
        self.jobs: dict[str, dict[str, Any]] = {}

    def start(self, seed=1, side='challenger', pressure=1, limit=1300):
        seed, pressure, limit = int(seed), int(pressure), int(limit or 1300)
        if side not in ('challenger', 'defender') or not 1 <= pressure <= 3 or not 1 <= limit <= 1300:
            raise ValueError('阵营、压力或录制回合数无效（回合范围 1–1300）')
        with self.lock:
            if any(j['state'] in ('running', 'stopping') for j in self.jobs.values()):
                raise RuntimeError('已有录制正在运行，请等待完成或取消该录制')
            while len(self.jobs) >= 3:
                self.jobs.pop(next(iter(self.jobs)))
            job_id = uuid.uuid4().hex
            job = dict(id=job_id, seed=seed, side=side, pressure=pressure,
                       limit=limit, round=0, state='running', started=time.monotonic(),
                       ended=None, cancel=threading.Event(), result=None, error=None)
            self.jobs[job_id] = job
            initial = self.snapshot(job_id)
            threading.Thread(target=self._run, args=(job,), daemon=True).start()
            return initial

    def _run(self, job):
        def progress(count, limit):
            with self.lock:
                job['round'] = count
        try:
            result = self.producer(job['seed'], job['side'], job['pressure'], job['limit'],
                                   progress=progress, cancelled=job['cancel'].is_set)
            with self.lock:
                job['result'] = result
                job['state'] = 'cancelled' if job['cancel'].is_set() else 'done'
                result['metadata'] = dict(result.get('metadata', {}),
                                          recordingStatus=job['state'], requestedRounds=job['limit'])
        except Exception as error:
            with self.lock:
                job['state'], job['error'] = 'failed', f'{type(error).__name__}: {error}'
        finally:
            with self.lock:
                job['ended'] = time.monotonic()

    def _job(self, job_id):
        if job_id not in self.jobs:
            raise KeyError('录制不存在或已过期；服务重启后请重新录制')
        return self.jobs[job_id]

    def snapshot(self, job_id):
        with self.lock:
            job = self._job(job_id)
            elapsed = (job['ended'] or time.monotonic()) - job['started']
            return {**{k: job[k] for k in ('id', 'seed', 'side', 'pressure', 'limit', 'round', 'state', 'error')},
                    'elapsed': round(elapsed, 2), 'progress': job['round'] / job['limit'],
                    'local': True}

    def cancel(self, job_id):
        with self.lock:
            job = self._job(job_id)
            if job['state'] == 'running':
                job['cancel'].set()
                job['state'] = 'stopping'
            return self.snapshot(job_id)

    def result(self, job_id):
        with self.lock:
            job = self._job(job_id)
            if job['state'] not in ('done', 'cancelled'):
                raise RuntimeError(job['error'] or '录制尚未完成')
            return job['result']


JOBS = RecordingJobs()
