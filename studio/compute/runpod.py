"""Fornecedor RunPod Serverless (GPU sob demanda, escala a zero).

API usada (verificada em docs.runpod.io): POST /v2/{endpoint}/run com {"input", "webhook",
"policy"}; GET /v2/{endpoint}/status/{id}; POST /v2/{endpoint}/cancel/{id};
header "authorization: Bearer <RUNPOD_API_KEY>". Estados: IN_QUEUE, IN_PROGRESS, COMPLETED,
FAILED, CANCELLED, TIMED_OUT. O resultado de /run fica disponível só 30 min — por isso o
JobService grava tudo na base de dados assim que o vê, e o vídeo vai direto para o Storage.

O formato exato do JSON de /status (delayTime/executionTime, onde aparece o progresso) não está
na documentação que consultei; é lido de forma defensiva e, se faltar, as métricas de tempo
vêm das medições do próprio worker.
"""
from __future__ import annotations

import hashlib
import hmac

import requests

from studio import config
from studio.compute import ProviderStatus

_MAP = {"IN_QUEUE": "QUEUED", "IN_PROGRESS": "PROCESSING", "COMPLETED": "COMPLETED", "FAILED": "FAILED",
        "CANCELLED": "CANCELLED", "TIMED_OUT": "FAILED"}


def webhook_token(job_id: str) -> str:
    return hmac.new(config.APP_SECRET.encode(), f"runpod:{job_id}".encode(), hashlib.sha256).hexdigest()[:32]


class RunPodProvider:
    name = "runpod"
    push_updates = False

    def __init__(self):
        if not (config.RUNPOD_API_KEY and config.RUNPOD_ENDPOINT_ID):
            raise RuntimeError("COMPUTE_PROVIDER=runpod exige RUNPOD_API_KEY e RUNPOD_ENDPOINT_ID")
        if not config.USE_SUPABASE:
            raise RuntimeError("COMPUTE_PROVIDER=runpod exige Supabase (o worker remoto lê/escreve no Storage)")
        self.base = f"https://api.runpod.ai/v2/{config.RUNPOD_ENDPOINT_ID}"
        self.h = {"authorization": f"Bearer {config.RUNPOD_API_KEY}", "content-type": "application/json"}

    def submit(self, job_id: str, payload: dict) -> str:
        body = {"input": payload, "policy": {"executionTimeout": config.RUNPOD_EXECUTION_TIMEOUT_S * 1000,
                                             "ttl": 2 * 3600 * 1000}}
        if config.PUBLIC_BASE_URL:
            body["webhook"] = (f"{config.PUBLIC_BASE_URL}/api/compute/runpod/webhook"
                               f"?job={job_id}&t={webhook_token(job_id)}")
        r = requests.post(f"{self.base}/run", json=body, headers=self.h, timeout=30)
        r.raise_for_status()
        return r.json()["id"]

    def poll(self, provider_job_id: str) -> ProviderStatus:
        r = requests.get(f"{self.base}/status/{provider_job_id}", headers=self.h, timeout=20)
        if r.status_code == 404:
            return ProviderStatus(status="FAILED", error="job expirou no RunPod (resultado guardado só 30 min)")
        r.raise_for_status()
        j = r.json()
        st = _MAP.get(j.get("status", ""), "PROCESSING")
        out = j.get("output")
        stage = frac = None
        if st == "PROCESSING" and isinstance(out, str) and ":" in out:  # progress_update("stage:frac")
            stage, _, f = out.rpartition(":")
            try:
                frac = float(f)
            except ValueError:
                stage = None
        return ProviderStatus(
            status=st, stage=stage, progress=frac,
            output=out if (st == "COMPLETED" and isinstance(out, dict)) else None,
            error=str(j.get("error")) if j.get("error") else None,
            queue_ms=j.get("delayTime") if isinstance(j.get("delayTime"), (int, float)) else None,
            exec_ms=j.get("executionTime") if isinstance(j.get("executionTime"), (int, float)) else None,
            worker_id=j.get("workerId"), raw={k: j.get(k) for k in ("status", "delayTime", "executionTime", "workerId")},
        )

    def cancel(self, provider_job_id: str) -> None:
        requests.post(f"{self.base}/cancel/{provider_job_id}", headers=self.h, timeout=20)
