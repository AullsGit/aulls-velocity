"""Corre DENTRO do contentor aulls-engines. Lê uma especificação de teste (JSON, independente do
motor) e renderiza-a com MLT, GES ou libopenshot, usando só funcionalidades documentadas de cada
um. O que um motor não suporta é declarado ("unsupported"), nunca simulado.

    python3 /code/poc_runner.py <motor> <spec.json> <saida.mp4>

Especificação:
  {"fps": [30, 1], "width": W, "height": H, "frames": N,
   "sources": {"a": {"path": "/work/a30.mp4", "fps": 30.0}},
   "items": [{"src": "a", "at": 0, "dur": 20, "start": 10,       # frames de saída; start = frame da fonte
              "speed": 1.0,                                        # velocidade constante
              "time_frames": null | [f0, f1, ...],                 # frame da fonte por frame de saída
              "zoom": null | {"z0": 1.0, "z1": 1.5},              # zoom centrado, linear no item
              "dissolve_in": 0}]}                                  # frames de sobreposição com o anterior
Imprime uma linha JSON: {"ok": bool, "unsupported": [...], "seconds": t, "engine_version": "..."}.
"""
import json
import os
import sys
import time
from xml.sax.saxutils import escape


def _sec(src, frame):
    return frame / src["fps"]


# ---------------------------------------------------------------------------- MLT
def mlt_xml(spec):
    fn, fd = spec["fps"]
    W, H = spec["width"], spec["height"]
    fps = fn / fd
    unsupported = []
    prods, entries_by_track = [], [[], []]
    trans = []
    cursor = [0, 0]  # próximo frame livre em cada pista (A/B roll para dissoluções)
    track = 0
    for i, it in enumerate(spec["items"]):
        src = spec["sources"][it["src"]]
        pid = f"p{i}"
        sp = it.get("speed", 1.0)
        # posição do 1.º frame do item na linha de tempo do PRODUTOR: os keyframes de um filtro
        # ligado ao produtor contam a partir daí (medido: sem este desvio o zoom saía errado)
        if it.get("time_frames"):
            inp = 0
        elif abs(sp - 1.0) > 1e-9:
            inp = round(_sec(src, it["start"]) * fps / sp)
        else:
            inp = round(_sec(src, it["start"]) * fps)
        filters = ""
        rect = lambda z: f"{-(z - 1) * W / 2:.4f} {-(z - 1) * H / 2:.4f} {z * W:.4f} {z * H:.4f} 1"  # noqa: E731
        kfs = None
        if it.get("rects"):  # retângulo por frame (px na saída): zoom/pan/shake medidos
            kfs = ";".join(f"{inp + k}={x:.3f} {y:.3f} {w:.3f} {h:.3f} 1" for k, (x, y, w, h) in enumerate(it["rects"]))
        elif it.get("zoom"):
            kfs = f"{inp}={rect(it['zoom']['z0'])};{inp + it['dur'] - 1}={rect(it['zoom']['z1'])}"
        if kfs:
            filters += (f'<filter><property name="mlt_service">affine</property><property name="transition.rect">{kfs}</property>'
                        f'<property name="transition.distort">1</property><property name="transition.fill">1</property></filter>')
        if it.get("color"):  # matriz 3x4 (0..255): parte 3x3 no colorchannelmixer, deslocamento no lutrgb
            m = it["color"]
            mix = "".join(f'<property name="av.{c}{d}">{m[ci][di]:.5f}</property>' for ci, c in enumerate("rgb")
                          for di, d in enumerate("rgb"))
            lut = "".join(f'<property name="av.{c}">clip(val{m[ci][3]:+.3f},0,255)</property>' for ci, c in enumerate("rgb"))
            filters += (f'<filter><property name="mlt_service">avfilter.colorchannelmixer</property>{mix}</filter>'
                        f'<filter><property name="mlt_service">avfilter.lutrgb</property>{lut}</filter>')
        if it.get("time_frames"):
            # link timeremap (MLT 7): instante de SAÍDA -> instante da FONTE (s). Medido: o MLT mostra o
            # frame que está no ecrã nesse instante (hold). O MEIO do frame, (f + 1/2)/fps, dá exatamente
            # f sem depender de arredondamentos, e um frame em falta (VFR) dá o anterior
            tm = ";".join(f"{k}={(f + 0.5) / src['fps']:.6f}" for k, f in enumerate(it["time_frames"]))
            prods.append(f'<chain id="{pid}" out="{it["dur"] - 1}"><property name="resource">{escape(src["path"])}</property>'
                         f'<link><property name="mlt_service">timeremap</property><property name="time_map">{tm}</property>'
                         f'<property name="image_mode">nearest</property></link>{filters}</chain>')
        elif abs(sp - 1.0) > 1e-9:
            prods.append(f'<producer id="{pid}"><property name="mlt_service">timewarp</property>'
                         f'<property name="resource">{sp}:{escape(src["path"])}</property>{filters}</producer>')
        else:
            prods.append(f'<producer id="{pid}"><property name="resource">{escape(src["path"])}</property>{filters}</producer>')
        d = it.get("dissolve_in", 0)
        if d:
            track = 1 - track
            trans.append((it["at"], it["at"] + d - 1, 1 - track, track))
        gap = it["at"] - cursor[track]
        if gap < 0:
            unsupported.append(f"item {i}: sobreposição sem dissolução")
            gap = 0
        if gap:
            entries_by_track[track].append(f'<blank length="{gap}"/>')
        entries_by_track[track].append(f'<entry producer="{pid}" in="{inp}" out="{inp + it["dur"] - 1}"/>')
        cursor[track] = it["at"] + it["dur"]
    pls = "".join(f'<playlist id="t{k}">{"".join(e)}</playlist>' for k, e in enumerate(entries_by_track) if e)
    ntracks = sum(1 for e in entries_by_track if e)
    if ntracks == 1:
        body = pls.replace('<playlist id="t0">', '<playlist id="main">')
    else:
        tr = "".join(f'<transition in="{a}" out="{b}"><property name="mlt_service">luma</property>'
                     f'<property name="a_track">{ta}</property><property name="b_track">{tb}</property>'
                     f'<property name="reverse">{1 if tb < ta else 0}</property></transition>' for a, b, ta, tb in trans)
        body = (f'{pls}<tractor id="main"><multitrack><track producer="t0"/><track producer="t1"/></multitrack>{tr}</tractor>')
    xml = (f'<?xml version="1.0" encoding="utf-8"?><mlt><profile width="{W}" height="{H}" frame_rate_num="{fn}" '
           f'frame_rate_den="{fd}" sample_aspect_num="1" sample_aspect_den="1" display_aspect_num="{W}" '
           f'display_aspect_den="{H}" progressive="1" colorspace="709"/>{"".join(prods)}{body}</mlt>')
    return xml, unsupported


def run_mlt(spec, out):
    import subprocess
    xml, unsup = mlt_xml(spec)
    path = out + ".mlt"
    open(path, "w").write(xml)
    r = subprocess.run(["melt", path, "-consumer", f"avformat:{out}", "vcodec=libx264", "crf=12", "preset=veryfast",
                        "an=1", f"real_time=-{os.cpu_count() or 4}"], capture_output=True, text=True)
    ver = subprocess.run(["melt", "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    return {"ok": r.returncode == 0, "unsupported": unsup, "engine_version": ver, "stderr": r.stderr[-300:] if r.returncode else ""}


# ---------------------------------------------------------------------------- GES
def run_ges(spec, out):
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GES", "1.0")
    gi.require_version("GstPbutils", "1.0")
    from gi.repository import GES, GLib, Gst, GstPbutils
    Gst.init(None)
    GES.init()
    fn, fd = spec["fps"]
    ns = lambda frames: int(round(frames * Gst.SECOND * fd / fn))  # noqa: E731
    unsupported = []
    tl = GES.Timeline.new()
    vt = GES.VideoTrack.new()
    vt.set_restriction_caps(Gst.Caps.from_string(f"video/x-raw,width={spec['width']},height={spec['height']},framerate={fn}/{fd}"))
    tl.add_track(vt)
    tl.set_auto_transition(True)
    layer = tl.append_layer()
    for i, it in enumerate(spec["items"]):
        src = spec["sources"][it["src"]]
        asset = GES.UriClipAsset.request_sync(Gst.filename_to_uri(src["path"]))
        if it.get("time_frames"):
            unsupported.append(f"item {i}: mapa de tempo por frame (GES só tem time effects monótonos)")
        sp = it.get("speed", 1.0)
        inpoint = int(round(_sec(src, it["start"]) * Gst.SECOND))
        clip = layer.add_asset(asset, ns(it["at"]), inpoint if abs(sp - 1) < 1e-9 else 0, ns(it["dur"]), GES.TrackType.VIDEO)
        if clip is None:
            unsupported.append(f"item {i}: add_asset falhou")
            continue
        if abs(sp - 1) > 1e-9:
            eff = GES.Effect.new("videorate")
            clip.add_top_effect(eff, 0)
            ok = eff.set_child_property("rate", sp)
            clip.set_inpoint(inpoint)  # o inpoint é em tempo da FONTE (antes do efeito de tempo)
            clip.set_duration(ns(it["dur"]))
            if not ok:
                unsupported.append(f"item {i}: videorate.rate não aceite")
        if it.get("zoom"):
            src_el = [c for c in clip.get_children(False) if isinstance(c, GES.VideoSource)][0]
            W, H = spec["width"], spec["height"]
            for prop, f in (("width", lambda z: z * W), ("height", lambda z: z * H),
                            ("posx", lambda z: -(z - 1) * W / 2), ("posy", lambda z: -(z - 1) * H / 2)):
                cs = GstController_source()
                cs.set(0, f(it["zoom"]["z0"]))
                cs.set(ns(it["dur"] - 1), f(it["zoom"]["z1"]))
                if not src_el.set_control_source(cs, prop, "direct-absolute"):
                    unsupported.append(f"item {i}: keyframes de {prop}")
    prof = GstPbutils.EncodingContainerProfile.new("mp4", None, Gst.Caps.from_string("video/quicktime,variant=iso"), None)
    vprof = GstPbutils.EncodingVideoProfile.new(Gst.Caps.from_string("video/x-h264,profile=high"), None,
                                                Gst.Caps.from_string(f"video/x-raw,framerate={fn}/{fd}"), 0)
    vprof.set_preset_name(None)
    prof.add_profile(vprof)
    pipe = GES.Pipeline()
    pipe.set_timeline(tl)
    pipe.set_render_settings(Gst.filename_to_uri(out), prof)
    pipe.set_mode(GES.PipelineFlags.RENDER)
    loop = GLib.MainLoop()
    err = []

    def on_msg(bus, msg):
        if msg.type == Gst.MessageType.EOS:
            loop.quit()
        elif msg.type == Gst.MessageType.ERROR:
            err.append(str(msg.parse_error()[0]))
            loop.quit()

    bus = pipe.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_msg)
    pipe.set_state(Gst.State.PLAYING)
    loop.run()
    pipe.set_state(Gst.State.NULL)
    return {"ok": not err, "unsupported": unsupported, "engine_version": f"GES {'.'.join(map(str, GES.version()[:3]))}",
            "stderr": "; ".join(err)}


def GstController_source():
    import gi
    gi.require_version("GstController", "1.0")
    from gi.repository import GstController
    cs = GstController.InterpolationControlSource()
    cs.set_property("mode", GstController.InterpolationMode.LINEAR)
    return cs


# ----------------------------------------------------------------------- libopenshot
def run_openshot(spec, out):
    import openshot
    fn, fd = spec["fps"]
    W, H = spec["width"], spec["height"]
    fps = fn / fd
    unsupported = []
    t = openshot.Timeline(W, H, openshot.Fraction(fn, fd), 44100, 2, openshot.LAYOUT_STEREO)
    keep = []
    for i, it in enumerate(spec["items"]):
        src = spec["sources"][it["src"]]
        r = openshot.FFmpegReader(src["path"])
        r.Open()
        c = openshot.Clip(r)
        c.Layer(1 + (i % 2) if any(x.get("dissolve_in") for x in spec["items"]) else 1)
        c.Position(it["at"] / fps)
        c.scale = openshot.SCALE_STRETCH
        # tempo: curva `time` (frame de saída -> frame da fonte, 1-based) cobre velocidade e mapa por frame
        if it.get("time_frames") or abs(it.get("speed", 1.0) - 1) > 1e-9:
            if it.get("time_frames"):
                pts = [(k + 1, f + 1) for k, f in enumerate(it["time_frames"])]
            else:
                pts = [(1, it["start"] + 1), (it["dur"], it["start"] + 1 + (it["dur"] - 1) * it["speed"] * src["fps"] / fps)]
            for k, f in pts:
                c.time.AddPoint(k, f, openshot.LINEAR)
            c.Start(0)
            c.End(it["dur"] / fps)
        else:
            c.Start(_sec(src, it["start"]))
            c.End(_sec(src, it["start"]) + it["dur"] / fps)
        if it.get("zoom"):
            for prop in (c.scale_x, c.scale_y):
                prop.AddPoint(1, it["zoom"]["z0"], openshot.LINEAR)
                prop.AddPoint(it["dur"], it["zoom"]["z1"], openshot.LINEAR)
        if it.get("dissolve_in"):
            c.alpha.AddPoint(1, 0.0, openshot.LINEAR)
            c.alpha.AddPoint(it["dissolve_in"], 1.0, openshot.LINEAR)
        t.AddClip(c)
        keep += [r, c]
    t.Open()
    w = openshot.FFmpegWriter(out)
    # medido no 0.3.3 do Debian: com libx264 o ficheiro sai sem vídeo e com áudio ligado o processo
    # termina com SIGFPE; com mpeg4 (bitrate alto, para a medição não sofrer) grava corretamente
    w.SetVideoOptions(True, "mpeg4", openshot.Fraction(fn, fd), W, H, openshot.Fraction(1, 1), False, False, 20000000)
    w.Open()  # 0.3.x: Open() prepara os streams e escreve o cabeçalho
    for f in range(1, spec["frames"] + 1):
        w.WriteFrame(t.GetFrame(f))
    w.Close()  # escreve o trailer
    t.Close()
    return {"ok": True, "unsupported": unsupported, "engine_version": f"libopenshot {openshot.OPENSHOT_VERSION_FULL}"}


if __name__ == "__main__":
    engine, spec_path, out = sys.argv[1:4]
    spec = json.load(open(spec_path))
    t0 = time.time()
    try:
        res = {"mlt": run_mlt, "ges": run_ges, "openshot": run_openshot}[engine](spec, out)
    except Exception as e:  # noqa: BLE001 — o relatório regista a falha, não a esconde
        res = {"ok": False, "unsupported": [], "error": f"{type(e).__name__}: {e}"[:400]}
    res["seconds"] = round(time.time() - t0, 2)
    print(json.dumps(res))
