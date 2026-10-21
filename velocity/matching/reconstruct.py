"""Cenário A: Timeline que REPRODUZ a referência a partir do alinhamento às fontes.

A timeline sai à taxa NATIVA da referência (24/25/30/60 fps), com a duração e as fronteiras de
plano da referência (frames nativos, cortes afinados — ver source_align.refine_cuts). Cada plano
alinhado leva o rasto exato (frame da fonte + afim + cor por frame de saída); os planos que não
alinharam (o utilizador não tem esse material) usam a escolha do matching normal (cenário B),
convertida para a mesma taxa — e isso fica registado nas decisões.

O que se reaplica por cima: as transições medidas (flash, dip, blur, glitch) e os efeitos que a
geometria/cor medidas NÃO contêm (rgb_split, pulsos). Zoom punches e shakes não se reaplicam nos
planos exatos: já estão na afim medida — reaplicá-los duplicava o efeito.
"""
from __future__ import annotations

import math

import numpy as np

from velocity.io.probe import probe
from velocity.matching.source_align import AlignResult
from velocity.schemas.edit_dna import Curve, EditDNA
from velocity.schemas.timeline import ClipUse, ExactTrack, Timeline, TimelineShot

GEOMETRIC_FX = ("zoom_punch", "shake")


def output_size(ref_wh: tuple[int, int], profile_wh: tuple[int, int]) -> tuple[int, int]:
    """Tamanho com o ASPETO da referência e a mesma área (custo) do perfil escolhido."""
    area = profile_wh[0] * profile_wh[1]
    r = ref_wh[0] / ref_wh[1]
    h = math.sqrt(area / r)
    return max(2, int(round(r * h / 2)) * 2), max(2, int(round(h / 2)) * 2)


def _aspect_fix(out_wh: tuple[int, int], ref_wh: tuple[int, int]) -> np.ndarray:
    """Saída normalizada -> referência normalizada (recorte central se os aspetos diferirem)."""
    ro, rr = out_wh[0] / out_wh[1], ref_wh[0] / ref_wh[1]
    if abs(ro - rr) < 1e-3:
        return np.eye(3)
    if ro < rr:  # saída mais estreita: usa a faixa central da largura da referência
        f = ro / rr
        return np.array([[f, 0, (1 - f) / 2], [0, 1, 0], [0, 0, 1]])
    f = rr / ro
    return np.array([[1, 0, 0], [0, f, (1 - f) / 2], [0, 0, 1]])


def _approx_params(warps: list, src_aspect: float, out_aspect: float) -> tuple[Curve, tuple[float, float], tuple[float, float]]:
    """Aproximação paramétrica (zoom/centro/pan) do rasto exato — só para o editor, se o
    utilizador mudar o plano e o rasto deixar de se aplicar."""
    A = np.array(warps, np.float64)
    cover_w = min(1.0, out_aspect / src_aspect)  # largura do enquadramento "cover" (fração da fonte)
    widths = np.maximum(np.abs(A[:, 0]), 1e-4)
    z = np.log(np.maximum(cover_w / widths, 1.0))
    t = np.linspace(0, 1, len(z))
    sel = np.unique(np.linspace(0, len(z) - 1, min(len(z), 9)).astype(int))
    cx = A[:, 0] * 0.5 + A[:, 1] * 0.5 + A[:, 2]
    cy = A[:, 3] * 0.5 + A[:, 4] * 0.5 + A[:, 5]
    cover_h = min(1.0, src_aspect / out_aspect)
    to_win = lambda u, c: float(np.clip((u - (1 - c) / 2) / c, 0, 1))  # noqa: E731
    center = (to_win(cx[0], cover_w), to_win(cy[0], cover_h))
    pan = (to_win(cx[-1], cover_w) - center[0], to_win(cy[-1], cover_h) - center[1])
    return Curve(t=[round(float(v), 4) for v in t[sel]], v=[round(float(v), 4) for v in z[sel]]), center, pan


def _rescale_transition(t, new_cut: int, k: float, n: int):
    if t is None:
        return None
    before, after = round((t.cut_frame - t.start_frame) * k), round((t.end_frame - t.cut_frame) * k)
    return t.model_copy(update={"start_frame": max(0, new_cut - before), "end_frame": min(n, new_cut + max(after, 1)),
                                "cut_frame": new_cut})


def reconstruct_timeline(dna: EditDNA, al: AlignResult, size: tuple[int, int], fallback: Timeline | None = None,
                         music_path: str | None = None, music_offset_s: float = 0.0) -> tuple[Timeline, dict]:
    """`fallback`: timeline do matching normal sobre o MESMO DNA (sem extensão), usada nos planos
    que não alinharam. `size`: tamanho de saída (ver output_size)."""
    fps, n, bounds = al.fps, al.n_frames, al.bounds
    k = fps / dna.fps  # frames do DNA (taxa de análise) -> frames nativos
    fix = _aspect_fix(size, al.size)
    out_aspect = size[0] / size[1]
    shots, decisions = [], []
    exact_ranges = []
    for i, sa in enumerate(al.shots):
        a, b = bounds[i], bounds[i + 1]
        o = sa.shot  # plano do DNA de onde este veio (a reanálise de segmento pode dividir planos)
        fb = fallback.shots[o] if fallback is not None and o < len(fallback.shots) else None
        tin_src = None
        if i > 0 and o > 0 and sa.inherit_transition:
            tin_src = fb.transition_in if fb is not None else dna.shots[o].transition_in
        t_in = _rescale_transition(tin_src, a, k, n) if i > 0 else None
        if i > 0 and not sa.inherit_transition:
            decisions.append({"what": f"corte {i}", "decision": "corte seco no ponto onde o conteúdo muda",
                              "why": "corte encontrado pelo alinhamento (o analisador não o marcou; provável cross-fade, "
                                     "que o renderer ainda não sobrepõe)"})
        if sa.ok:
            path = al.clip_paths[sa.clip]
            src_aspect = probe(path).aspect
            warps = [tuple(round(float(v), 6) for v in (np.vstack([np.array(w_).reshape(2, 3), [0, 0, 1]]) @ fix)[:2].reshape(-1))
                     for w_ in sa.warps]
            zoom, center, pan = _approx_params(warps, src_aspect, out_aspect)
            sp = float(sa.speed or 1.0)
            clip = ClipUse(source_path=path, src_start_s=sa.src_frames[0] / al.clip_fps[sa.clip],
                           speed=Curve(t=[0.0, 1.0], v=[sp, sp]), zoom=zoom, center=center, pan=pan,
                           exact=ExactTrack(src_fps=al.clip_fps[sa.clip], src_frames=list(sa.src_frames), warps=warps,
                                            color=sa.color, confidence=sa.confidence, measured=list(sa.measured)))
            exact_ranges.append((a, b))
            decisions.append({"what": f"plano {i}", "decision": "reconstruído (cenário A)",
                              "why": f"fonte {path.replace(chr(92), '/').rsplit('/', 1)[-1]} frame {sa.src_frames[0]}, "
                                     f"velocidade {sp:.2f}x, confiança {sa.confidence:.3f}"})
        elif fb is not None:
            clip = fb.clip.model_copy(deep=True)
            decisions.append({"what": f"plano {i}", "decision": "matching normal (cenário B)",
                              "why": "; ".join(sa.notes) or f"confiança {sa.confidence:.2f} abaixo do limiar"})
        else:
            raise ValueError(f"plano {i} não alinhou e não há timeline de recurso")
        shots.append(TimelineShot(index=i, start_frame=a, end_frame=b, clip=clip, transition_in=t_in))

    def in_exact(f: int) -> bool:
        return any(a <= f < b for a, b in exact_ranges)

    effects, fx_events, texts = [], [], []
    if fallback is not None:
        for e in fallback.effects:
            pk = int(round(e.peak_frame * k))
            if e.type in GEOMETRIC_FX and in_exact(pk):
                continue
            effects.append(e.model_copy(update={"start_frame": int(round(e.start_frame * k)), "peak_frame": pk,
                                                "end_frame": max(int(round(e.end_frame * k)), pk + 1)}))
        fx_events = [ev.model_copy(update={"start_frame": int(round(ev.start_frame * k)), "end_frame": int(round(ev.end_frame * k))})
                     for ev in fallback.fx_events]
        texts = [tx.model_copy(update={"start_frame": int(round(tx.start_frame * k)), "end_frame": int(round(tx.end_frame * k))})
                 for tx in fallback.texts]
    tl = Timeline(fps=fps, width=size[0], height=size[1], n_frames=n, shots=shots, fx_events=fx_events, texts=texts,
                  effects=effects, music_path=music_path, music_offset_s=music_offset_s)
    info = {"mode": "reconstruction", "coverage": round(al.coverage, 3), "fps": fps, "size": list(size),
            "exact_shots": len(exact_ranges), "shots": len(shots), "align_seconds": al.seconds, "decisions": decisions}
    return tl, info
