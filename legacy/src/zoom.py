"""
Zoom (Ken Burns) — módulo dedicado.

PROBLEMA TÉCNICO REAL que este módulo resolve:
O filtro `zoompan` do FFmpeg, quando aplicado diretamente sobre um frame
já na resolução final (ex: 1080x1920), sofre de um artefacto conhecido
de "tremor"/instabilidade (o cálculo de x/y/zoom por frame perde precisão
sub-pixel, fazendo o zoom parecer nervoso ou pouco percetível em vez de
um movimento suave). Isto é um problema documentado do próprio filtro,
não um bug nosso.

SOLUÇÃO (supersampling): escalar o clip para uma resolução muito maior
(ex: 3x) ANTES do zoompan, e deixar o próprio zoompan reduzir para o
tamanho final via o parâmetro `s=`. Isto dá ao cálculo margem suficiente
para computar o movimento com precisão, resultando num zoom visivelmente
suave e real — tanto para dentro como para fora.

ZOOM "SUBJECT-LOCKED" (config.USE_SUBJECT_LOCKED_ZOOM, ver
src/subject_detect.py estimate_subject_centroid_track): em vez do centro
do zoom ficar sempre fixo no meio do frame, pode seguir uma sequência de
pontos (t_frac, cx, cy) amostrados ao longo do plano, interpolados
linearmente frame a frame via uma expressão FFmpeg construída em
_piecewise_linear_expr. É uma aproximação por interpolação entre
amostras, não tracking contínuo frame-a-frame.
"""

DEFAULT_SUPERSAMPLE = 4  # escala interna = resolução final * este fator


def _piecewise_linear_expr(points: list, total_frames: int) -> str:
    """
    Constrói uma expressão FFmpeg que interpola linearmente entre os
    pontos (frac_0..1, valor_0..1) dados, em função da variável `on`
    (índice do frame de output, 0-based, disponível dentro de zoompan).

    points deve estar ordenado por fração crescente e cobrir pelo menos
    frac=0.0 e frac=1.0 (ver estimate_subject_centroid_track). Devolve
    uma expressão aninhada if(lte(on,f1), segmento_linear, ...).
    """
    frames_vals = [(f * (total_frames - 1), v) for f, v in points]
    if frames_vals[0][0] > 0.0:
        frames_vals.insert(0, (0.0, frames_vals[0][1]))
    if frames_vals[-1][0] < total_frames - 1:
        frames_vals.append((float(total_frames - 1), frames_vals[-1][1]))

    expr = f"{frames_vals[-1][1]:.5f}"
    for i in range(len(frames_vals) - 2, -1, -1):
        f0, v0 = frames_vals[i]
        f1, v1 = frames_vals[i + 1]
        if f1 - f0 < 1e-6:
            seg = f"{v1:.5f}"
        else:
            slope = (v1 - v0) / (f1 - f0)
            seg = f"({v0:.5f}+({slope:.6f})*(on-{f0:.3f}))"
        expr = f"if(lte(on,{f1:.3f}),{seg},{expr})"
    return expr


def build_zoom_chain(w: int, h: int, fps: int, duration: float, zoom_factor: float,
                      direction: str = "in", supersample: int = None,
                      center_track: list = None) -> dict:
    """
    Constrói os componentes do zoom Ken Burns de alta qualidade:

    Retorna um dict com:
      - "prep_scale_crop": filtro scale+crop para a resolução SUPERAMOSTRADA
        (deve substituir o scale+crop normal quando há zoom)
      - "zoompan": o filtro zoompan em si, já a devolver a resolução final

    direction="in":  começa em 1.0x, termina em zoom_factor (aproxima)
    direction="out": começa em zoom_factor, termina em 1.0x (afasta)

    center_track (opcional): lista de (t_frac, cx, cy), frações 0-1 do
    frame, ordenada e cobrindo t_frac=0.0 e 1.0 — ver
    subject_detect.estimate_subject_centroid_track. Se fornecida, o
    centro do zoom interpola linearmente entre estes pontos em vez de
    ficar fixo no meio do frame (iw/2, ih/2).
    """
    supersample = supersample or DEFAULT_SUPERSAMPLE
    big_w, big_h = w * supersample, h * supersample

    total_frames = max(int(round(duration * fps)), 1)
    rate = (zoom_factor - 1.0) / total_frames

    if direction == "out":
        z_expr = f"if(eq(on,1),{zoom_factor},max(zoom-{rate:.6f},1.0))"
    else:
        z_expr = f"min(zoom+{rate:.6f},{zoom_factor})"

    if center_track:
        cx_points = [(pt[0], pt[1]) for pt in center_track]
        cy_points = [(pt[0], pt[2]) for pt in center_track]
        cx_expr = _piecewise_linear_expr(cx_points, total_frames)
        cy_expr = _piecewise_linear_expr(cy_points, total_frames)
        x_center = f"(iw*({cx_expr}))"
        y_center = f"(ih*({cy_expr}))"
    else:
        x_center = "iw/2"
        y_center = "ih/2"

    # Clamp para a janela de crop nunca sair do frame, mesmo quando o
    # centro seguido está perto de uma borda.
    x_expr = f"max(0,min({x_center}-(iw/zoom/2),iw-iw/zoom))"
    y_expr = f"max(0,min({y_center}-(ih/zoom/2),ih-ih/zoom))"

    prep_scale_crop = f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase,crop={big_w}:{big_h}"
    zoompan = (
        f"zoompan=z='{z_expr}':d=1:x='{x_expr}':y='{y_expr}':s={w}x{h}:fps={fps}"
    )

    return {"prep_scale_crop": prep_scale_crop, "zoompan": zoompan}
