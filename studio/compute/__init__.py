"""Fornecedores de computação (onde corre o processamento pesado).

Interface (todas as implementações):
    name: str
    push_updates: bool        # True = chama os callbacks; False = a API faz polling/webhook
    submit(job_id, payload) -> provider_job_id
    poll(provider_job_id) -> ProviderStatus
    cancel(provider_job_id) -> None

Trocar de fornecedor = escrever outra classe com estes 3 métodos e escolher pelo env
`COMPUTE_PROVIDER`. O payload é o contrato de `worker/runner.py`, igual para todos.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from studio import config


@dataclass
class ProviderStatus:
    status: str  # QUEUED | PROCESSING | COMPLETED | FAILED | CANCELLED
    stage: str | None = None
    progress: float | None = None
    output: dict | None = None
    error: str | None = None
    queue_ms: int | None = None
    exec_ms: int | None = None
    worker_id: str | None = None
    raw: dict = field(default_factory=dict)


_provider = None


def get_provider():
    global _provider
    if _provider is None:
        if config.COMPUTE_PROVIDER == "runpod":
            from studio.compute.runpod import RunPodProvider
            _provider = RunPodProvider()
        else:
            from studio.compute.local import LocalProvider
            _provider = LocalProvider()
    return _provider


def set_provider(p) -> None:
    global _provider
    _provider = p
