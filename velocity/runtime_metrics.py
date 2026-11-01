"""CPU, RAM e VRAM por etapa de uma geração (amostragem numa thread, a cada 0,25 s).

Mede o processo E os filhos (o ffmpeg corre em processos à parte e é a maior parte do custo do
render). A VRAM é a que o PyTorch reservou (segmentação, OCR, modelos futuros); a usada pelo
ffmpeg/NVDEC não entra. Sem `psutil` instalado, devolve só a VRAM (nunca falha a geração).
"""
from __future__ import annotations

import sys
import threading


class ResourceMeter:
    def __init__(self, interval: float = 0.25):
        self.interval = interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._reset()
        self.stages: dict[str, dict] = {}
        try:
            import psutil
            self._proc = psutil.Process()
            self._psutil = psutil
        except Exception:  # noqa: BLE001
            self._proc = self._psutil = None
        self._known: dict = {}
        self._t = threading.Thread(target=self._run, name="resource-meter", daemon=True)

    def _reset(self):
        self._peak_rss = 0.0
        self._cpu_sum = 0.0
        self._n = 0

    @staticmethod
    def _torch():
        t = sys.modules.get("torch")
        try:
            return t if t is not None and t.cuda.is_available() else None
        except Exception:  # noqa: BLE001
            return None

    def _sample(self):
        if self._proc is None:
            return
        try:
            procs = [self._proc] + self._proc.children(recursive=True)
        except Exception:  # noqa: BLE001
            return
        rss = cpu = 0.0
        alive = {}
        for p in procs:
            q = self._known.get(p.pid, p)
            try:
                with q.oneshot():
                    rss += q.memory_info().rss
                    cpu += q.cpu_percent(None)  # 1ª leitura de um processo novo é 0: normal
                alive[p.pid] = q
            except Exception:  # noqa: BLE001 — o processo acabou entre a listagem e a leitura
                continue
        self._known = alive
        with self._lock:
            self._peak_rss = max(self._peak_rss, rss / 2 ** 20)
            self._cpu_sum += cpu
            self._n += 1

    def _run(self):
        while not self._stop.wait(self.interval):
            self._sample()

    def start(self) -> "ResourceMeter":
        torch = self._torch()
        if torch is not None:
            torch.cuda.reset_peak_memory_stats()
        if self._proc is not None:
            self._t.start()
        return self

    def lap(self, stage: str):
        """Fecha a etapa que acabou de terminar (mesma semântica que os tempos do pipeline)."""
        self._sample()
        with self._lock:
            cpus = self._psutil.cpu_count() if self._psutil else None
            rec = {"peak_rss_mb": round(self._peak_rss, 1),
                   "cpu_mean_pct_of_machine": round(self._cpu_sum / self._n / cpus, 1) if (self._n and cpus) else None}
            self._reset()
        torch = self._torch()
        if torch is not None:
            rec["peak_vram_torch_mb"] = round(torch.cuda.max_memory_reserved() / 2 ** 20, 1)
            torch.cuda.reset_peak_memory_stats()
        prev = self.stages.get(stage)
        if prev:  # etapa repetida (p.ex. 2ª renderização): fica o pior caso
            rec = {k: max(v or 0, prev.get(k) or 0) for k, v in rec.items()}
        self.stages[stage] = rec

    def stop(self) -> dict:
        self._stop.set()
        return self.stages
