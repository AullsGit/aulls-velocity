"""
Deteção de sujeito (pessoa) via MediaPipe Image Segmenter (Tasks API) —
modelo local, grátis, roda em CPU. Gera uma "máscara" (vídeo em tons de
cinza: branco = pessoa, preto = fundo) usada depois pelo
subject_boost.py para aplicar tratamento diferente a cada região.

*** DESCOBERTA DURANTE OS TESTES ***
A versão instalada do MediaPipe (0.10.33) já não usa a API antiga
`mp.solutions.selfie_segmentation` (populada automaticamente com
modelo embutido) — usa a nova "Tasks API"
(`mediapipe.tasks.python.vision.ImageSegmenter`), que exige um ficheiro
de modelo `.tflite` explícito, tal como o Whisper exigia um `.bin`.
Confirmado nesta máquina: os imports e a assinatura da API abaixo estão
corretos (testado); a segmentação em si com um modelo real ainda não
foi confirmada porque o download do modelo está bloqueado na rede deste
ambiente de desenvolvimento.

Modelo necessário (download manual):
  https://storage.googleapis.com/mediapipe-models/image_segmenter/selfie_segmenter/float16/latest/selfie_segmenter.tflite
Guarda em qualquer pasta e aponta config.SUBJECT_SEGMENTER_MODEL_PATH para lá.
"""
import os
import cv2
import numpy as np

sys_path_added = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
if sys_path_added not in sys.path:
    sys.path.insert(0, sys_path_added)
import config


def is_available() -> bool:
    """Verifica se o MediaPipe está instalado E se o modelo .tflite existe."""
    try:
        import mediapipe  # noqa: F401
    except Exception:
        return False
    model_path = getattr(config, "SUBJECT_SEGMENTER_MODEL_PATH", "")
    return bool(model_path) and os.path.exists(model_path)


def generate_subject_mask(video_path: str, mask_out_path: str, threshold: float = 0.5) -> bool:
    """
    Lê video_path frame a frame, corre a segmentação de pessoa em cada
    frame (modo VIDEO, com timestamps corretos por frame), e escreve um
    vídeo em tons de cinza (mesma resolução/fps/duração) em
    mask_out_path: branco onde há pessoa, preto onde não há.
    Retorna True se conseguiu gerar a máscara com pelo menos alguma
    deteção, False caso contrário (o chamador deve desistir do subject
    boost para este clip, sem travar o pipeline).
    """
    import mediapipe as mp
    from mediapipe.tasks.python import vision
    from mediapipe.tasks.python.core.base_options import BaseOptions

    model_path = getattr(config, "SUBJECT_SEGMENTER_MODEL_PATH", "")
    if not model_path or not os.path.exists(model_path):
        return False

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return False

    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frame_duration_ms = 1000.0 / fps

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(mask_out_path, fourcc, fps, (w, h), isColor=False)

    options = vision.ImageSegmenterOptions(
        base_options=BaseOptions(model_asset_path=model_path),
        running_mode=vision.RunningMode.VIDEO,
        output_category_mask=True,
        output_confidence_masks=False,
    )

    ok_any = False
    try:
        with vision.ImageSegmenter.create_from_options(options) as segmenter:
            frame_idx = 0
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                timestamp_ms = int(frame_idx * frame_duration_ms)
                result = segmenter.segment_for_video(mp_image, timestamp_ms)
                category_mask = result.category_mask.numpy_view()
                # No modelo selfie_segmenter, categoria 0 = fundo, 1 = pessoa
                mask = (category_mask == 1).astype(np.uint8) * 255
                writer.write(mask)
                ok_any = True
                frame_idx += 1
        return ok_any
    except Exception:
        return False
    finally:
        cap.release()
        writer.release()


def estimate_subject_presence(video_path: str, start: float, end: float, n_samples: int = 3) -> dict:
    """
    Versão leve, usada pelo Edit DNA (src/edit_dna.py) para RESUMIR a
    presença de sujeito por plano, sem escrever um vídeo de máscara
    inteiro: amostra n_samples frames dentro de [start, end] e usa o
    ImageSegmenter em modo IMAGE (frame a frame, sem gestão de
    timestamps de vídeo) para estimar:
      - present_ratio: fração dos frames amostrados onde há pessoa detectada
      - mean_area_ratio: fração média da imagem ocupada pela pessoa

    Isto NÃO substitui generate_subject_mask (que continua a ser usado
    para o AI Subject Boost em si, sobre o clip final, em modo VIDEO
    para máscaras temporalmente consistentes) — é só um resumo para
    diagnóstico/comparação.
    """
    if not is_available():
        return {"available": False}

    import mediapipe as mp
    from mediapipe.tasks.python import vision
    from mediapipe.tasks.python.core.base_options import BaseOptions

    model_path = getattr(config, "SUBJECT_SEGMENTER_MODEL_PATH", "")

    n_samples = max(1, n_samples)
    end_safe = max(start, end - 0.02)
    timestamps = np.linspace(start, end_safe, num=n_samples)

    options = vision.ImageSegmenterOptions(
        base_options=BaseOptions(model_asset_path=model_path),
        running_mode=vision.RunningMode.IMAGE,
        output_category_mask=True,
        output_confidence_masks=False,
    )

    present_count = 0
    area_ratios = []
    cap = cv2.VideoCapture(video_path)
    try:
        with vision.ImageSegmenter.create_from_options(options) as segmenter:
            for t in timestamps:
                cap.set(cv2.CAP_PROP_POS_MSEC, float(t) * 1000)
                ok, frame = cap.read()
                if not ok:
                    continue
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                result = segmenter.segment(mp_image)
                category_mask = result.category_mask.numpy_view()
                area_ratio = float(np.mean(category_mask == 1))
                area_ratios.append(area_ratio)
                if area_ratio > 0.02:  # >= ~2% do frame -> considera "presente"
                    present_count += 1
    except Exception as e:
        return {"available": True, "error": str(e)}
    finally:
        cap.release()

    n = len(area_ratios)
    if n == 0:
        return {"available": True, "present_ratio": 0.0, "mean_area_ratio": 0.0, "frames_sampled": 0}
    return {
        "available": True,
        "present_ratio": round(present_count / n, 3),
        "mean_area_ratio": round(float(np.mean(area_ratios)), 4),
        "frames_sampled": n,
    }


def estimate_subject_centroid_track(video_path: str, start: float, end: float, n_samples: int = None):
    """
    Usado pelo zoom "subject-locked" (config.USE_SUBJECT_LOCKED_ZOOM, ver
    src/zoom.py). Amostra n_samples frames dentro de [start, end] e, para
    cada um, calcula o CENTRÓIDE (posição média, em fração 0-1 de
    largura/altura) da região segmentada como pessoa — para que o centro
    do zoom Ken Burns possa seguir o sujeito ao longo do plano em vez de
    ficar sempre fixo no meio do frame.

    Devolve uma lista ordenada de (t_frac, cx, cy), com pontos garantidos
    em t_frac=0.0 e t_frac=1.0 (repete a amostra válida mais próxima
    dessa ponta se faltar), pronta para src/zoom.py interpolar
    linearmente entre elas. Devolve None se o MediaPipe/modelo não
    estiverem disponíveis, OU se a pessoa não foi detectada de forma
    consistente o suficiente (< config.SUBJECT_ZOOM_MIN_PRESENCE_RATIO
    das amostras) — nesses casos o chamador deve cair para o zoom
    centrado normal, sem travar o pipeline.

    LIMITAÇÃO HONESTA: isto segue o CENTRÓIDE da máscara de segmentação,
    não um objeto/identidade específica — com mais de uma pessoa no
    frame, o centróide pode saltar entre elas de amostra para amostra
    (só há interpolação linear entre os pontos amostrados, sem suavização
    adicional). Também assume que a fração cx/cy medida no frame ORIGINAL
    da fonte se mantém razoavelmente válida depois do scale+crop do
    renderer para a resolução final — se a fonte tiver uma proporção
    (aspect ratio) muito diferente da saída, o crop central pode deslocar
    essa correspondência.
    """
    if not is_available():
        return None

    n_samples = n_samples or getattr(config, "SUBJECT_ZOOM_N_SAMPLES", 5)
    n_samples = max(3, n_samples)
    min_presence = getattr(config, "SUBJECT_ZOOM_MIN_PRESENCE_RATIO", 0.6)

    import mediapipe as mp
    from mediapipe.tasks.python import vision
    from mediapipe.tasks.python.core.base_options import BaseOptions

    model_path = getattr(config, "SUBJECT_SEGMENTER_MODEL_PATH", "")
    end_safe = max(start, end - 0.02)
    timestamps = np.linspace(start, end_safe, num=n_samples)

    options = vision.ImageSegmenterOptions(
        base_options=BaseOptions(model_asset_path=model_path),
        running_mode=vision.RunningMode.IMAGE,
        output_category_mask=True,
        output_confidence_masks=False,
    )

    points = []
    cap = cv2.VideoCapture(video_path)
    try:
        with vision.ImageSegmenter.create_from_options(options) as segmenter:
            for t in timestamps:
                cap.set(cv2.CAP_PROP_POS_MSEC, float(t) * 1000)
                ok, frame = cap.read()
                if not ok:
                    continue
                fh, fw = frame.shape[:2]
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                result = segmenter.segment(mp_image)
                category_mask = result.category_mask.numpy_view()
                person_pixels = np.argwhere(category_mask == 1)
                if len(person_pixels) < 0.01 * category_mask.size:
                    continue  # sem deteção relevante nesta amostra
                cy = float(np.mean(person_pixels[:, 0])) / fh
                cx = float(np.mean(person_pixels[:, 1])) / fw
                t_frac = (t - start) / max(end - start, 1e-6)
                points.append((round(float(t_frac), 4), round(cx, 4), round(cy, 4)))
    except Exception:
        return None
    finally:
        cap.release()

    if n_samples == 0 or (len(points) / n_samples) < min_presence or len(points) < 2:
        return None

    points.sort(key=lambda p: p[0])
    if points[0][0] > 0.0:
        points.insert(0, (0.0, points[0][1], points[0][2]))
    if points[-1][0] < 1.0:
        points.append((1.0, points[-1][1], points[-1][2]))
    return points
