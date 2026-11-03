"""Do vídeo de referência ao primeiro plano de trabalho (sem renderizar).

Corre no worker. Usa o mesmo pipeline do fluxo clássico (`run_make`), parando antes do render, com
dois acrescentos: o agente pode corrigir os cortes da referência (viu os frames e sabe que há uma
troca que a deteção não apanhou) e nenhum plano fica mais longo do que o material disponível.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field

from velocity.io.probe import probe
from velocity.pipeline import MakeParams, run_make
from velocity.schemas.edit_dna import EditDNA, Transition

FORMATS = {"9:16": (720, 1280), "16:9": (1280, 720), "1:1": (720, 720), "4:5": (720, 900), "4:3": (960, 720), "3:4": (720, 960)}


@dataclass
class BuildOptions:
    mode: str = "style"  # style: recria a edição (efeitos, zoom, cor); cuts_only: só cortes, imagem intacta
    format: str = "auto"  # auto | reference | 9:16 | 16:9 | 1:1 | 4:5 | 4:3 | 3:4
    extend: bool = False  # repetir o padrão da referência até 25-31 s
    cuts_s: list[float] | None = None  # cortes corrigidos pelo agente; None = os detetados
    tone: float = 0.85
    extra: dict = field(default_factory=dict)


def with_cuts(dna: EditDNA, cuts_s: list[float]) -> EditDNA:
    """DNA com estes cortes. Um corte perto de um já medido mantém a transição medida; cada plano
    novo herda as medições do plano de que veio."""
    fps = dna.fps
    frames = sorted({int(round(c * fps)) for c in cuts_s if 3 <= c * fps <= dna.n_frames - 3})
    known = {t.cut_frame: t for t in dna.transitions()}
    edges = [0] + frames + [dna.n_frames]
    shots = []
    for k, (a, b) in enumerate(zip(edges, edges[1:])):
        mid = (a + b) // 2
        src = next((s for s in dna.shots if s.start_frame <= mid < s.end_frame), dna.shots[-1])
        tr = None
        if k > 0:
            near = min(known, key=lambda c: abs(c - a), default=None)
            if near is not None and abs(near - a) <= 4:
                t0 = known[near]
                d = a - t0.cut_frame
                tr = t0.model_copy(update={"cut_frame": a, "start_frame": t0.start_frame + d, "end_frame": t0.end_frame + d})
            else:
                tr = Transition(type="cut", start_frame=a, end_frame=a + 1, cut_frame=a, intensity=1.0)
        shots.append(src.model_copy(update={"index": k, "start_frame": a, "end_frame": b, "duration_s": round((b - a) / fps, 3), "transition_in": tr}))
    return dna.model_copy(update={"shots": shots})


def split_long_shots(dna: EditDNA, max_frames: int) -> EditDNA:
    """Um plano mais longo do que o clip mais longo acabaria num frame congelado: divide-o."""
    cuts = []
    for s in dna.shots:
        n = s.end_frame - s.start_frame
        if n > max_frames > 0:
            parts = n // max_frames + 1
            cuts += [s.start_frame + n * k // parts for k in range(1, parts)]
    if not cuts:
        return dna
    return with_cuts(dna, [c / dna.fps for c in sorted(set(dna.cut_frames()) | set(cuts))])


def resolve_size(fmt: str, clips: list[str], ref: str) -> tuple[int, int] | None:
    """Tamanho de trabalho (o render final escala). None = deixa o pipeline decidir (reconstrução)."""
    if fmt in FORMATS:
        return FORMATS[fmt]
    if fmt == "reference":
        info = probe(ref)
        ar = info.width / max(info.height, 1)
    elif fmt == "auto":
        ars = [probe(c).width / max(probe(c).height, 1) for c in clips]
        ars.sort()
        ar = ars[len(ars) // 2]
    else:
        raise ValueError(f"formato desconhecido: {fmt}")
    best = min(FORMATS, key=lambda k: abs(FORMATS[k][0] / FORMATS[k][1] - ar))
    return FORMATS[best]


def build_plan(ref: str, clips: list[str], dna: EditDNA, opts: BuildOptions, log=lambda m: None, progress=None):
    """Devolve (Plano com caminhos locais, info). `dna`: a análise guardada do projeto."""
    if opts.cuts_s is not None:
        dna = with_cuts(dna, opts.cuts_s)
    intact = opts.mode == "cuts_only"
    if intact:  # sem repetir nem congelar: nenhum plano maior que o clip mais longo
        longest = max(int(probe(c).duration * dna.fps) for c in clips)
        dna = split_long_shots(dna, max(int(longest * 0.9), 30))
    size = resolve_size(opts.format, clips, ref) if (intact or opts.format != "auto") else None
    with tempfile.TemporaryDirectory(prefix="awls_plan_") as tmp:
        res = run_make(MakeParams(
            ref=ref, clips=clips, out=os.path.join(tmp, "unused.mp4"), dna=dna, force=True, plan_only=True, intact=intact,
            size=size, no_extend=not opts.extend or intact, tone=0.0 if intact else opts.tone, denoise=0.0 if intact else 0.5,
            no_fx=intact, reconstruct="off" if intact else "auto", **opts.extra,
        ), log=log, progress=progress)
    if not res.ok:
        raise RuntimeError(res.error or "não foi possível construir o plano")
    return res.plan, {"decisions": (res.report or {}).get("decisions", []), "scenario": res.plan.meta.get("scenario"),
                      "duration_s": round(res.plan.output.duration_frames / res.plan.output.fps, 2),
                      "reconstruction": (res.report or {}).get("reconstruction"), "dna": dna}
