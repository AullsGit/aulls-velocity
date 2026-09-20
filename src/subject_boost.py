"""
AI Subject Boost — usa uma máscara de sujeito (src/subject_detect.py)
para aplicar tratamento DIFERENTE à pessoa e ao fundo, depois recompõe
tudo num único vídeo, via o filtro `maskedmerge` do FFmpeg.

Consolidação honesta dos 10 nomes originalmente pedidos: 'Subject
Lighting', 'Face/Person Enhance' e 'Relight' são todos o MESMO
mecanismo aqui (clarear/realçar a região da pessoa) — não são três
efeitos distintos. 'Background Dim' é o mecanismo complementar
(escurecer/dessaturar o resto). 'HDR/Smart HDR' não está incluído —
não é possível recuperar detalhe que não existe nos pixels originais.
"""
import subprocess


def _run(cmd: list):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Comando FFmpeg falhou (subject_boost):\n{' '.join(cmd)}\n\nSTDERR:\n{result.stderr[-3000:]}"
        )
    return result


def apply_subject_boost(
    clip_path: str,
    mask_path: str,
    output_path: str,
    subject_brightness: float = 0.08,
    subject_contrast: float = 1.15,
    subject_sharpen: float = 0.6,
    bg_brightness: float = -0.05,
    bg_saturation: float = 0.75,
):
    """
    Aplica o AI Subject Boost: clareia/nitidez na região da pessoa
    (mask branca), escurece/dessatura o resto (mask preta), recompõe.
    """
    filter_complex = (
        f"[0:v]split=2[a][b];"
        f"[a]eq=brightness={subject_brightness}:contrast={subject_contrast},"
        f"unsharp=5:5:{subject_sharpen}:5:5:0.0[subject];"
        f"[b]eq=brightness={bg_brightness}:saturation={bg_saturation}[bg];"
        f"[bg][subject][1:v]maskedmerge[out]"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", clip_path,
        "-i", mask_path,
        "-filter_complex", filter_complex,
        "-map", "[out]",
        output_path,
    ]
    _run(cmd)
