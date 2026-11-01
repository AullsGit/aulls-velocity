"""Adaptador MLT (melt): o plano v1 é traduzido para MLT XML e executado pelo `melt`.

Escolhido na Fase 1 (bench/out/engines, docs/ENGINES.md): em modo "estrito" passa todas as provas
de precisão ao frame que o compositor passa, faz o que o compositor não faz (itens sobrepostos:
dissoluções; várias pistas) e foi ~12% mais rápido a 1080x1920.

Modo estrito (o único usado aqui): o MLT NÃO decide o tempo. Cada frame de saída pede, pelo link
`timeremap`, o instante do MEIO do frame da fonte que o plano escolheu (regra "hold" calculada por
nós) — medido: assim sai exatamente esse frame, incluindo conversões 24<->30 fps e câmara lenta.
A geometria vai como retângulo por frame (filtro `affine`) e a cor medida como matriz 3x4
(`avfilter.colorchannelmixer` + `avfilter.lutrgb`).

O que este adaptador ainda NÃO traduz é recusado antes do render (`validate`): transições
flash/dip/blur/glitch e os efeitos próprios (só o compositor os tem), texto, look global, flip,
rotação e fontes com aspeto diferente da saída (o enquadramento do MLT nesse caso não foi medido).

Execução: `VELOCITY_MELT` = comando do melt (por defeito `melt`). Em desenvolvimento no Windows,
`VELOCITY_MELT=docker:aulls-engines` corre o melt dentro da imagem de bench/engines.
"""
from __future__ import annotations

import math
import os
import subprocess
import tempfile
import time
from fractions import Fraction
from xml.sax.saxutils import escape

import numpy as np

from velocity.engine.base import Capabilities, PlanRejected, RenderReport, check_capabilities
from velocity.plan.schema import Plan, VideoItem
from velocity.plan.validate import Issue, errors, validate_plan

CAPS = Capabilities(
    name="mlt",
    max_video_tracks=1,
    gaps_allowed=False,
    overlapping_items=True,  # dissolução entre itens sobrepostos (pistas A/B + transição luma)
    max_audio_items=1,
    transitions=frozenset({"cut"}),
    effects=frozenset(),
    per_frame_time=True,
    per_frame_transform=True,
    color_matrix=True,
    subject_masks=False,
    text=False,
)


def _src_info(uri: str):
    from velocity.io.probe import probe
    info = probe(uri)
    return info, float(info.fps_nominal or info.fps_avg or 30.0)


_VFR_CACHE: dict = {}


def frame_times(uri: str, src_fps: float) -> dict[int, float] | None:
    """Fonte VFR: índice na grelha nominal -> instante REAL do frame (s). O MLT mostra, num instante
    em que a fonte não tem frame, o frame SEGUINTE (medido); para manter a regra "hold" pede-se
    sempre o instante de um frame que existe. CFR: None (os instantes são f/fps)."""
    info, _ = _src_info(uri)
    if not info.is_vfr:
        return None
    key = (uri, os.path.getmtime(uri))
    if key not in _VFR_CACHE:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=best_effort_timestamp_time",
                            "-of", "csv=p=0", uri], capture_output=True, text=True)
        ts = [float(x.strip(",")) for x in r.stdout.split() if x.strip(",").strip() not in ("", "N/A")]
        _VFR_CACHE[key] = {int(round(t * src_fps)): t for t in ts}
    return _VFR_CACHE[key]


def hold_frames(it: VideoItem, src_fps: float, out_fps: float) -> list[int]:
    """Frame da fonte em cada frame do item (regra "hold"), igual ao compositor."""
    if it.time.src_frames is not None:
        return list(it.time.src_frames)
    L = it.duration
    tau = (np.arange(L) + 0.5) / L
    rate = np.interp(tau, it.time.speed.t, it.time.speed.v)
    cum = np.concatenate([[0.0], np.cumsum(rate)[:-1]])  # posição em frames de saída
    f0 = math.floor(it.time.src_start_s * src_fps + 1e-6)
    return [f0 + math.floor(c * src_fps / out_fps + 1e-9) for c in cum]


def item_warps(it: VideoItem, src_aspect: float, out_aspect: float, zoom_gain: float) -> list[tuple]:
    """Afim normalizada saída->fonte por frame. Rasto medido: tal e qual. Paramétrico: o mesmo
    enquadramento do compositor (cover ao aspeto da saída, zoom, centro + pan, limitado à fonte)."""
    if it.transform.warps is not None:
        return list(it.transform.warps)
    cw, ch = (out_aspect / src_aspect, 1.0) if src_aspect > out_aspect else (1.0, src_aspect / out_aspect)
    wx0, wy0 = (1 - cw) / 2, (1 - ch) / 2
    L = it.duration
    tau = (np.arange(L) + 0.5) / L
    z = np.maximum(np.interp(tau, it.transform.zoom.t, it.transform.zoom.v) * zoom_gain, 0.0)
    out = []
    for k in range(L):
        s = math.exp(z[k])
        cx = it.transform.center[0] + it.transform.pan[0] * tau[k]
        cy = it.transform.center[1] + it.transform.pan[1] * tau[k]
        x0 = min(max(cx - 0.5 / s, 0.0), 1 - 1 / s)
        y0 = min(max(cy - 0.5 / s, 0.0), 1 - 1 / s)
        out.append((cw / s, 0.0, wx0 + cw * x0, 0.0, ch / s, wy0 + ch * y0))
    return out


class MltEngine:
    name = "mlt"

    def available(self) -> bool:
        import shutil
        spec = os.environ.get("VELOCITY_MELT", "melt")
        if spec.startswith("docker:"):
            docker = os.environ.get("DOCKER", "docker")
            try:
                return subprocess.run([docker, "image", "inspect", spec.split(":", 1)[1]], capture_output=True).returncode == 0
            except OSError:
                return False
        return shutil.which(spec) is not None

    def capabilities(self) -> Capabilities:
        return CAPS

    def validate(self, plan: Plan) -> list[Issue]:
        out = check_capabilities(plan, CAPS)
        oa = plan.output.width / plan.output.height
        lk = plan.look
        if (abs(lk.gamma - 1) > 1e-3 or abs(lk.saturation - 1) > 1e-3 or abs(lk.contrast - 1) > 1e-3 or lk.sharpen > 1e-3):
            out.append(Issue("error", "unsupported", "look", "mlt: correção global de look ainda não traduzida"))
        for it in plan.video_items():
            where = f"video/{it.id}"
            if it.transform.flip:
                out.append(Issue("error", "unsupported", where, "mlt: flip ainda não traduzido"))
            if it.transform.warps and max(max(abs(w[1]), abs(w[3])) for w in it.transform.warps) > 0.01:
                out.append(Issue("error", "unsupported", where, "mlt: rotação (o retângulo do affine não a exprime)"))
            c = it.color
            if abs(c.gamma - 1) > 1e-3 or abs(c.saturation - 1) > 1e-3 or abs(c.subject_gamma - 1) > 1e-3 \
                    or abs(c.background_gamma - 1) > 1e-3 or c.subject_sharpen > 1e-3:
                out.append(Issue("error", "unsupported", where, "mlt: ajustes de tom paramétricos ainda não traduzidos"))
            uri = plan.sources[it.source].uri
            if os.path.exists(uri):
                info, _ = _src_info(uri)
                if abs(info.aspect - oa) > 0.01:
                    out.append(Issue("error", "unsupported", where,
                                     f"mlt: fonte com aspeto {info.aspect:.3f} != saída {oa:.3f} (não medido)"))
        return out

    # ------------------------------------------------------------------ tradução
    def to_xml(self, plan: Plan, path_map=lambda p: p) -> str:
        W, H = plan.output.width, plan.output.height
        fr = Fraction(plan.output.fps).limit_denominator(1001)
        fps = plan.output.fps
        oa = W / H
        prods, tracks, trans = [], [[], []], []
        cursor, track = [0, 0], 0
        for i, it in enumerate(sorted(plan.video_items(), key=lambda v: v.at)):
            uri = plan.sources[it.source].uri
            info, sfps = _src_info(uri)
            frames = hold_frames(it, sfps, fps)
            times = frame_times(uri, sfps)
            if times:  # VFR: o frame existente mais recente (hold), pelo seu instante real
                have = sorted(times)
                pick = lambda f: times[have[max(0, np.searchsorted(have, f, side="right") - 1)]]  # noqa: E731
                tm = ";".join(f"{k}={pick(f) + 0.5 / sfps:.6f}" for k, f in enumerate(frames))
            else:
                tm = ";".join(f"{k}={(f + 0.5) / sfps:.6f}" for k, f in enumerate(frames))
            rects = []
            for a, _b, c, _d, e, f in item_warps(it, info.aspect, oa, plan.zoom_gain):
                rects.append(f"{len(rects)}={-c / a * W:.3f} {-f / e * H:.3f} {W / a:.3f} {H / e:.3f} 1")
            filters = (f'<filter><property name="mlt_service">affine</property>'
                       f'<property name="transition.rect">{";".join(rects)}</property>'
                       f'<property name="transition.distort">1</property><property name="transition.fill">1</property></filter>')
            m = it.color.matrix
            if m:
                mix = "".join(f'<property name="av.{c}{d}">{m[ci][di]:.6f}</property>' for ci, c in enumerate("rgb")
                              for di, d in enumerate("rgb"))
                lut = "".join(f'<property name="av.{c}">clip(val{m[ci][3]:+.4f},0,255)</property>' for ci, c in enumerate("rgb"))
                filters += (f'<filter><property name="mlt_service">avfilter.colorchannelmixer</property>{mix}</filter>'
                            f'<filter><property name="mlt_service">avfilter.lutrgb</property>{lut}</filter>')
            prods.append(f'<chain id="c{i}" out="{it.duration - 1}"><property name="resource">{escape(path_map(uri))}</property>'
                         f'<link><property name="mlt_service">timeremap</property><property name="time_map">{tm}</property>'
                         f'<property name="image_mode">nearest</property></link>{filters}</chain>')
            overlap = cursor[track] - it.at if i else 0
            if overlap > 0:  # dissolução: o item entra na outra pista e a transição luma cobre a sobreposição
                track = 1 - track
                trans.append((it.at, it.at + overlap - 1, 1 - track, track))
            gap = it.at - cursor[track]
            if gap > 0:
                tracks[track].append(f'<blank length="{gap}"/>')
            tracks[track].append(f'<entry producer="c{i}" in="0" out="{it.duration - 1}"/>')
            cursor[track] = it.at + it.duration
        pls = "".join(f'<playlist id="v{k}">{"".join(e)}</playlist>' for k, e in enumerate(tracks) if e)
        mtracks = "".join(f'<track producer="v{k}"/>' for k, e in enumerate(tracks) if e)
        tr = "".join(f'<transition in="{a}" out="{b}"><property name="mlt_service">luma</property>'
                     f'<property name="a_track">{ta}</property><property name="b_track">{tb}</property>'
                     f'<property name="reverse">{1 if tb < ta else 0}</property></transition>' for a, b, ta, tb in trans)
        audio = [it for t in plan.track("audio") for it in t.items]
        if audio:
            a = audio[0]
            ain = int(round(a.offset_s * fps))
            prods.append(f'<producer id="aud"><property name="resource">{escape(path_map(plan.sources[a.source].uri))}</property>'
                         f'<filter><property name="mlt_service">volume</property><property name="gain">{a.volume:.3f}</property>'
                         f'</filter></producer>')
            pls += f'<playlist id="a0"><entry producer="aud" in="{ain}" out="{ain + a.duration - 1}"/></playlist>'
            mtracks += '<track producer="a0" hide="video"/>'
        return (f'<?xml version="1.0" encoding="utf-8"?><mlt><profile width="{W}" height="{H}" frame_rate_num="{fr.numerator}" '
                f'frame_rate_den="{fr.denominator}" sample_aspect_num="1" sample_aspect_den="1" display_aspect_num="{W}" '
                f'display_aspect_den="{H}" progressive="1" colorspace="709"/>{"".join(prods)}{pls}'
                f'<tractor id="main"><multitrack>{mtracks}</multitrack>{tr}</tractor></mlt>')

    def render(self, plan: Plan, out_path: str, *, crf: int = 18, preset: str = "medium", progress=None,
               masks: dict | None = None) -> RenderReport:
        issues = errors(validate_plan(plan) + self.validate(plan))
        if issues:
            raise PlanRejected(issues)
        t0 = time.time()
        cmd_spec = os.environ.get("VELOCITY_MELT", "melt")
        out_path = os.path.abspath(out_path)
        if cmd_spec.startswith("docker:"):
            # desenvolvimento: melt dentro da imagem; cada pasta usada é montada em /m<n>
            dirs = sorted({os.path.dirname(os.path.abspath(s.uri)) for s in plan.sources.values()} | {os.path.dirname(out_path)})
            mounts = {d: f"/m{i}" for i, d in enumerate(dirs)}
            pm = lambda p: f"{mounts[os.path.dirname(os.path.abspath(p))]}/{os.path.basename(p)}"  # noqa: E731
            xml = self.to_xml(plan, pm)
            xml_path = out_path + ".mlt"
            open(xml_path, "w", encoding="utf-8").write(xml)
            docker = os.environ.get("DOCKER", "docker")
            vol = [x for d, m in mounts.items() for x in ("-v", f"{d}:{m}")]
            cmd = [docker, "run", "--rm", *vol, cmd_spec.split(":", 1)[1], "melt", pm(xml_path)]
            consumer_out = pm(out_path)
            env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
        else:
            xml_path = os.path.join(tempfile.mkdtemp(prefix="mlt_"), "plan.mlt")
            open(xml_path, "w", encoding="utf-8").write(self.to_xml(plan))
            cmd, consumer_out, env = [cmd_spec, xml_path], out_path, None
        cmd += ["-consumer", f"avformat:{consumer_out}", "vcodec=libx264", f"crf={crf}", f"preset={preset}",
                "acodec=aac", f"real_time=-{os.cpu_count() or 4}"]
        if not [it for t in plan.track("audio") for it in t.items]:
            cmd.append("an=1")
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if r.returncode != 0 or not os.path.exists(out_path):
            raise RuntimeError(f"melt falhou: {(r.stderr or r.stdout)[-400:]}")
        return RenderReport(out_path=out_path, frames=plan.output.duration_frames, seconds=round(time.time() - t0, 2),
                            engine=self.name)
