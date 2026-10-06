"""Editor leve pós-geração: aplica as alterações do utilizador à timeline que a IA gerou.

O browser NÃO envia uma timeline arbitrária. Envia um `EditDoc` (lista simples de planos
com cortes, velocidade, transição, zoom...) e é aqui que se reconstrói a `Timeline` a partir
da ORIGINAL — assim:
  * o utilizador só consegue usar ficheiros do próprio projeto (o mapa `files` vem do backend);
  * os valores ficam sempre dentro de limites seguros (sem vídeos de 10 horas);
  * transições e efeitos que a IA tirou da referência acompanham o plano onde estavam
    quando ele é cortado, dividido ou mudado de sítio (em vez de ficarem presos a frames
    absolutos que já não correspondem a nada).
"""
from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, Field

from velocity.schemas.edit_dna import Curve, EffectTrack, FxEvent, Transition
from velocity.schemas.timeline import ClipUse, TextOverlay, Timeline, TimelineShot

MAX_TOTAL_S = 90.0
MIN_SHOT_FRAMES = 3
TRANSITION_TYPES = ("cut", "flash", "dip_black", "blur", "glitch")
FILTERS = {
    "none": {},
    "vivid": {"sat": 1.3, "contrast": 1.1},
    "mono": {"sat": 0.0},
    "punch": {"sat": 1.15, "contrast": 1.2},
    "fade": {"contrast": 0.85, "gamma": 0.9},
}


class EditShot(BaseModel):
    orig: int | None = None  # índice do plano na timeline gerada (None = plano novo, exige file + src_start_s)
    file: str | None = None  # id do ficheiro-fonte (substituir clip); None = o do plano original
    src_start_s: float | None = None  # início explícito na fonte (substituir/dividir); None = original + trim_in_s
    trim_in_s: float = 0.0  # segundos cortados no início do plano original
    duration_s: float = Field(..., gt=0)
    speed: float | None = None  # None = curva de velocidade original da IA; número = velocidade constante
    transition: Literal["keep", "cut", "flash", "dip_black", "blur", "glitch"] = "keep"
    zoom: float | None = None  # None = zoom original; número = zoom constante (1 = enquadramento inteiro)
    center: tuple[float, float] | None = None
    flip: bool | None = None


class EditText(BaseModel):
    text: str = Field(..., min_length=1, max_length=80)
    start_s: float = Field(..., ge=0)
    end_s: float = Field(..., gt=0)
    x: float = 0.5
    y: float = 0.8
    size: float = 0.06  # altura da letra (fração da altura do vídeo)
    entry: Literal["pop", "fade", "none"] = "pop"


class EditLook(BaseModel):
    brightness: float = 0.0  # -1..1
    contrast: float = 0.0  # -1..1
    saturation: float = 0.0  # -1..1
    filter: Literal["none", "vivid", "mono", "punch", "fade"] = "none"


class EditDoc(BaseModel):
    shots: list[EditShot] = Field(..., min_length=1, max_length=400)
    texts: list[EditText] = Field(default_factory=list, max_length=20)
    look: EditLook = Field(default_factory=EditLook)
    music_file: str | None = None  # None = música/áudio da geração original
    music_volume: float = 1.0
    keep_effects: bool = True


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def doc_from_timeline(tl: Timeline) -> EditDoc:
    """Estado inicial do editor (o que a IA gerou), em segundos e sem nada alterado."""
    shots = [EditShot(orig=i, duration_s=round(s.n_frames / tl.fps, 3)) for i, s in enumerate(tl.shots)]
    texts = [EditText(text=t.text, start_s=round(t.start_frame / tl.fps, 3), end_s=round(t.end_frame / tl.fps, 3),
                      x=t.x, y=t.y, size=t.height, entry=t.entry if t.entry in ("pop", "fade") else "none")
             for t in tl.texts]
    return EditDoc(shots=shots, texts=texts, music_volume=tl.music_volume)


def _owned(items, shots: list[TimelineShot]) -> list[list]:
    """Para cada plano original, os efeitos que COMEÇAM dentro dele (offset relativo ao início)."""
    out: list[list] = [[] for _ in shots]
    for it in items:
        for i, s in enumerate(shots):
            if s.start_frame <= it.start_frame < s.end_frame:
                out[i].append(it)
                break
    return out


def _mean_speed(curve: Curve) -> float:
    if len(curve.t) < 2:
        return float(curve.v[0]) if curve.v else 1.0
    area = sum((curve.t[k + 1] - curve.t[k]) * (curve.v[k + 1] + curve.v[k]) / 2 for k in range(len(curve.t) - 1))
    span = curve.t[-1] - curve.t[0]
    return area / span if span > 0 else 1.0


def _new_transition(t_type: str, cut: int, fps: float) -> Transition | None:
    if t_type == "cut":
        return Transition(type="cut", start_frame=cut, end_frame=cut + 1, cut_frame=cut, intensity=0.0)
    half = max(2, int(round(0.1 * fps)))
    return Transition(type=t_type, start_frame=cut - half, end_frame=cut + half, cut_frame=cut, intensity=0.7,
                      blur_kind="radial" if t_type == "blur" else "none")


def apply_edit(original: Timeline, doc: EditDoc, files: dict[str, str]) -> Timeline:
    """Constrói a Timeline editada. `files`: id do ficheiro -> caminho/chave que o renderer
    vai receber (só ficheiros do projeto; um id fora do mapa é recusado)."""
    fps = original.fps
    orig_shots = original.shots
    fx_owned = _owned(original.effects, orig_shots) if doc.keep_effects else [[] for _ in orig_shots]
    ev_owned = _owned(original.fx_events, orig_shots) if doc.keep_effects else [[] for _ in orig_shots]

    shots: list[TimelineShot] = []
    effects: list[EffectTrack] = []
    fx_events: list[FxEvent] = []
    cursor = 0
    for j, es in enumerate(doc.shots):
        n = max(MIN_SHOT_FRAMES, int(round(es.duration_s * fps)))
        if cursor + n > MAX_TOTAL_S * fps:
            raise ValueError(f"o vídeo editado não pode passar de {MAX_TOTAL_S:.0f}s")
        if es.orig is not None:
            if not 0 <= es.orig < len(orig_shots):
                raise ValueError(f"plano original inexistente: {es.orig}")
            base = orig_shots[es.orig]
            clip = base.clip.model_copy(deep=True)
        else:
            if es.file is None or es.src_start_s is None:
                raise ValueError("plano novo precisa de ficheiro e início")
            base = None
            clip = ClipUse(source_path="", src_start_s=0.0)
        if es.file is not None:
            if es.file not in files:
                raise ValueError("ficheiro não pertence a este projeto")
            clip.source_path = files[es.file]
        if es.speed is not None:
            s = _clamp(es.speed, 0.25, 4.0)
            clip.speed = Curve(t=[0.0, 1.0], v=[s, s])
        if es.src_start_s is not None:
            clip.src_start_s = max(0.0, es.src_start_s)
        elif es.trim_in_s:
            clip.src_start_s = max(0.0, clip.src_start_s + es.trim_in_s)
        if es.zoom is not None:
            z = math.log(_clamp(es.zoom, 1.0, 3.0))
            clip.zoom = Curve(t=[0.0, 1.0], v=[z, z])
        if es.center is not None:
            clip.center = (_clamp(es.center[0], 0.0, 1.0), _clamp(es.center[1], 0.0, 1.0))
        if es.flip is not None:
            clip.flip = bool(es.flip)

        trans = None
        continues_split = j > 0 and es.orig is not None and doc.shots[j - 1].orig == es.orig
        if j > 0:
            if es.transition == "keep" and continues_split:
                # 2ª metade de um plano dividido: a transição original pertence à 1ª metade
                trans = _new_transition("cut", cursor, fps)
            elif es.transition == "keep":
                if base is not None and base.transition_in is not None:
                    t0 = base.transition_in
                    trans = t0.model_copy(update={
                        "start_frame": cursor - (t0.cut_frame - t0.start_frame),
                        "end_frame": cursor + (t0.end_frame - t0.cut_frame),
                        "cut_frame": cursor,
                    })
                else:
                    trans = _new_transition("cut", cursor, fps)
            else:
                trans = _new_transition(es.transition, cursor, fps)
        shots.append(TimelineShot(index=j, start_frame=cursor, end_frame=cursor + n, clip=clip, transition_in=trans))

        if base is not None:
            # frames de SAÍDA cortados no início deste plano (o trim é em segundos de fonte)
            trim_out = int(round(es.trim_in_s * fps / max(_mean_speed(base.clip.speed), 1e-3))) if es.src_start_s is None else 0
            if es.src_start_s is not None and es.orig is not None:
                # plano dividido: a 2ª metade começa onde a fonte avançou
                src_off = es.src_start_s - base.clip.src_start_s
                trim_out = int(round(src_off * fps / max(_mean_speed(base.clip.speed), 1e-3)))
            shift = cursor - base.start_frame - trim_out
            for t in fx_owned[es.orig]:
                rel = t.start_frame - base.start_frame - trim_out
                if 0 <= rel < n:
                    effects.append(t.model_copy(update={"start_frame": t.start_frame + shift,
                                                        "end_frame": t.end_frame + shift,
                                                        "peak_frame": t.peak_frame + shift}))
            for ev in ev_owned[es.orig]:
                rel = ev.start_frame - base.start_frame - trim_out
                if 0 <= rel < n:
                    fx_events.append(ev.model_copy(update={"start_frame": ev.start_frame + shift,
                                                           "end_frame": ev.end_frame + shift}))
        cursor += n

    total = cursor
    texts = []
    for t in doc.texts:
        a, b = int(round(t.start_s * fps)), int(round(t.end_s * fps))
        a, b = max(0, min(a, total - 1)), max(1, min(b, total))
        if b > a:
            texts.append(TextOverlay(text=t.text, start_frame=a, end_frame=b, x=_clamp(t.x, 0.05, 0.95),
                                     y=_clamp(t.y, 0.05, 0.95), width=0.9, height=_clamp(t.size, 0.02, 0.15),
                                     entry=t.entry))

    lk = doc.look
    f = FILTERS[lk.filter]
    post_gamma = original.post_gamma * f.get("gamma", 1.0) * (1.0 - 0.35 * _clamp(lk.brightness, -1, 1))
    post_contrast = original.post_contrast * f.get("contrast", 1.0) * (1.0 + 0.4 * _clamp(lk.contrast, -1, 1))
    post_sat = original.post_sat * f.get("sat", 1.0) * (1.0 + _clamp(lk.saturation, -1, 1))

    music_path, music_offset = original.music_path, original.music_offset_s
    if doc.music_file is not None:
        if doc.music_file not in files:
            raise ValueError("música não pertence a este projeto")
        music_path, music_offset = files[doc.music_file], 0.0

    return original.model_copy(update={
        "n_frames": total, "shots": shots, "effects": effects, "fx_events": fx_events, "texts": texts,
        "post_gamma": round(_clamp(post_gamma, 0.5, 1.6), 3),
        "post_contrast": round(_clamp(post_contrast, 0.5, 1.6), 3),
        "post_sat": round(_clamp(post_sat, 0.0, 2.0), 3),
        "music_path": music_path, "music_offset_s": music_offset,
        "music_volume": _clamp(doc.music_volume, 0.0, 2.0),
    })
