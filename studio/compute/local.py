"""Fornecedor local: corre o worker numa thread da própria API (desenvolvimento / operador).

Em produção no Render isto significa processar na CPU do Render — funciona, mas é lento e
bloqueia a instância. Para produção usar COMPUTE_PROVIDER=runpod.
"""
from __future__ import annotations

import os
import threading
import time
from queue import Queue

from studio.compute import ProviderStatus


class LocalProvider:
    name = "local"
    push_updates = True

    def __init__(self, workers: int | None = None):
        self._q: Queue = Queue()
        self._cancelled: set[str] = set()
        self._status: dict[str, ProviderStatus] = {}
        self._on_progress = lambda job_id, stage, frac: None
        self._on_result = lambda job_id, output, **kw: None
        n = workers or int(os.environ.get("VELOCITY_LOCAL_WORKERS", "1"))
        for _ in range(max(1, n)):
            threading.Thread(target=self._loop, daemon=True, name="local-worker").start()

    def bind(self, on_progress, on_result) -> None:
        self._on_progress, self._on_result = on_progress, on_result

    def submit(self, job_id: str, payload: dict) -> str:
        self._status[job_id] = ProviderStatus(status="QUEUED")
        self._q.put((job_id, payload, time.time()))
        return job_id

    def poll(self, provider_job_id: str) -> ProviderStatus:
        return self._status.get(provider_job_id) or ProviderStatus(status="FAILED", error="unknown job")

    def cancel(self, provider_job_id: str) -> None:
        # um job já em execução não é interrompido a meio (o motor não é interrompível);
        # o resultado é simplesmente ignorado pelo JobService.
        self._cancelled.add(provider_job_id)

    def queue_size(self) -> int:
        return self._q.qsize()

    def _loop(self):
        from worker.runner import run
        while True:
            job_id, payload, t_sub = self._q.get()
            if job_id in self._cancelled:
                continue
            queue_ms = int((time.time() - t_sub) * 1000)
            self._status[job_id] = ProviderStatus(status="PROCESSING")
            self._on_progress(job_id, "started", 0.0)
            t0 = time.time()
            try:
                out = run(payload, progress=lambda stage, frac: self._on_progress(job_id, stage, frac))
            except Exception as e:  # run() não devia levantar; última rede de segurança
                out = {"ok": False, "error_code": "internal", "detail": repr(e)}
            exec_ms = int((time.time() - t0) * 1000)
            self._status[job_id] = ProviderStatus(status="COMPLETED", output=out, exec_ms=exec_ms, queue_ms=queue_ms)
            if job_id not in self._cancelled:
                self._on_result(job_id, out, exec_ms=exec_ms, queue_ms=queue_ms, worker_id="local")
