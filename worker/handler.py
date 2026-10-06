"""Ponto de entrada do RunPod Serverless (imagem: worker/Dockerfile).

Contrato verificado na documentação do RunPod: `handler(job)` recebe job["input"] e job["id"];
`runpod.serverless.progress_update(job, msg)` publica progresso visível ao consultar /status;
o resultado tem de ser pequeno (<10 MB em /run) — por isso o vídeo vai direto para o Storage
por URL assinada e aqui só se devolvem metadados.

Teste local sem conta RunPod (o SDK suporta):
    python -m worker.handler --test_input '{"input": {...payload...}}'
"""
from __future__ import annotations

import time

import runpod

from worker.runner import run


def handler(job):
    payload = job["input"]
    last = {"t": 0.0, "stage": None}

    def progress(stage: str, frac: float):
        now = time.time()
        # cada atualização é um pedido HTTP ao RunPod: no máximo de 2 em 2 s (ou ao mudar de etapa)
        if stage != last["stage"] or now - last["t"] >= 2.0 or frac >= 1.0:
            last.update(t=now, stage=stage)
            runpod.serverless.progress_update(job, f"{stage}:{frac:.3f}")

    return run(payload, progress)


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
