"""
Overlay/Caption Detection (opcional) — deteção de texto na tela (legendas,
overlays, textos gravados) do vídeo de referência, via OCR (Tesseract,
através do binding `pytesseract`).

Requer o MOTOR Tesseract-OCR instalado no sistema (não basta o pacote
pip, tal como o FFmpeg/Whisper/MediaPipe também exigem binários/modelos
externos neste projeto):
  https://github.com/tesseract-ocr/tesseract
  Windows: instalador em https://github.com/UB-Mannheim/tesseract/wiki
Depois de instalado, se o tesseract.exe não estiver no PATH do sistema,
aponta config.TESSERACT_CMD_PATH para o caminho completo do executável.

O QUE ISTO FAZ: deteta REGIÕES com texto (bounding box normalizado) e o
texto reconhecido, amostrando alguns frames de cada plano e mantendo só
as regiões "estáveis" — as que aparecem, na mesma posição aproximada, em
vários dos frames amostrados desse plano. Isto filtra boa parte do ruído
típico de OCR sobre vídeo (motion blur num frame isolado, reflexos,
texturas confundidas com letras).

O QUE ISTO NÃO FAZ (âmbito, por agora):
- Não extrai fonte, cor, animação de entrada/saída, nem o instante exato
  de aparição/desaparição da legenda DENTRO do plano — regista texto,
  posição aproximada e o plano onde aparece, para entrar no Edit DNA
  (fase de análise/diagnóstico).
- Não reproduz automaticamente o overlay no vídeo final do utilizador.
  Copiar o TEXTO da referência literalmente para o vídeo do utilizador
  raramente faz sentido (o texto é conteúdo, não estilo) — decidir que
  texto novo colocar aí é um problema de geração de conteúdo, não de
  deteção, e fica fora do escopo deste módulo. A posição/estilo
  detectados ficam disponíveis no Edit DNA para um passo futuro que
  queira usá-los com texto fornecido pelo utilizador.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

import config


def is_available() -> bool:
    """Verifica se pytesseract está instalado E o motor Tesseract responde."""
    try:
        import pytesseract
    except Exception:
        return False

    cmd_path = getattr(config, "TESSERACT_CMD_PATH", "")
    if cmd_path:
        pytesseract.pytesseract.tesseract_cmd = cmd_path

    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def _get_frame_at(video_path: str, timestamp_sec: float):
    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_MSEC, timestamp_sec * 1000)
    ok, frame = cap.read()
    cap.release()
    return frame if ok else None


def _detect_text_boxes(frame, min_confidence: float = 60.0) -> list:
    """Corre o Tesseract sobre um frame; devolve [{"text","bbox","bbox_norm","conf"}, ...]."""
    import pytesseract

    h, w = frame.shape[:2]
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    data = pytesseract.image_to_data(rgb, output_type=pytesseract.Output.DICT)

    boxes = []
    for i in range(len(data["text"])):
        text = data["text"][i].strip()
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1.0
        if not text or conf < min_confidence:
            continue
        bw, bh = data["width"][i], data["height"][i]
        bx, by = data["left"][i], data["top"][i]
        boxes.append({
            "text": text,
            "bbox": [bx, by, bw, bh],
            "bbox_norm": [bx / w, by / h, bw / w, bh / h],
            "conf": conf,
        })
    return boxes


def _iou(box_a: list, box_b: list) -> float:
    ax, ay, aw, ah = box_a
    bx, by, bw, bh = box_b
    ix1, iy1 = max(ax, bx), max(ay, by)
    ix2, iy2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def detect_overlays_for_shot(
    video_path: str, start: float, end: float,
    n_samples: int = 3, min_stable_frac: float = 0.5, min_confidence: float = 60.0,
) -> list:
    """
    Amostra n_samples frames dentro de [start, end], deteta texto em cada
    um via OCR, agrupa deteções sobrepostas (IoU) entre frames num
    "track" por região, e devolve só os tracks ESTÁVEIS: os que
    aparecem em pelo menos min_stable_frac dos frames amostrados.

    Devolve lista de {"text", "bbox_norm" [x,y,w,h normalizados 0-1],
    "conf", "frames_seen", "frames_sampled"}. Lista vazia se o OCR não
    estiver disponível, se não houver texto, ou se nada for estável o
    suficiente.
    """
    if not is_available():
        return []

    n_samples = max(2, n_samples)
    end_safe = max(start, end - 0.02)
    timestamps = np.linspace(start, end_safe, num=n_samples)

    per_frame_boxes = []
    for t in timestamps:
        frame = _get_frame_at(video_path, float(t))
        if frame is None:
            continue
        try:
            per_frame_boxes.append(_detect_text_boxes(frame, min_confidence=min_confidence))
        except Exception:
            continue

    if not per_frame_boxes:
        return []

    clusters = []  # {"last_bbox","bbox_norm","texts":[...],"confs":[...],"frames_seen": int}
    for boxes in per_frame_boxes:
        used_this_frame = set()
        for box in boxes:
            best_idx, best_iou = None, 0.0
            for idx, cluster in enumerate(clusters):
                if idx in used_this_frame:
                    continue
                iou = _iou(box["bbox"], cluster["last_bbox"])
                if iou > best_iou:
                    best_iou, best_idx = iou, idx
            if best_idx is not None and best_iou >= 0.3:
                c = clusters[best_idx]
                c["last_bbox"] = box["bbox"]
                c["bbox_norm"] = box["bbox_norm"]
                c["texts"].append(box["text"])
                c["confs"].append(box["conf"])
                c["frames_seen"] += 1
                used_this_frame.add(best_idx)
            else:
                clusters.append({
                    "last_bbox": box["bbox"],
                    "bbox_norm": box["bbox_norm"],
                    "texts": [box["text"]],
                    "confs": [box["conf"]],
                    "frames_seen": 1,
                })

    n_frames = len(per_frame_boxes)
    stable = []
    for c in clusters:
        if c["frames_seen"] / n_frames < min_stable_frac:
            continue
        texts = c["texts"]
        best_text = max(set(texts), key=texts.count)  # texto mais frequente entre os vistos
        stable.append({
            "text": best_text,
            "bbox_norm": [round(v, 4) for v in c["bbox_norm"]],
            "conf": round(float(np.mean(c["confs"])), 1),
            "frames_seen": c["frames_seen"],
            "frames_sampled": n_frames,
        })

    return stable


if __name__ == "__main__":
    import json
    from scene_detect import detect_scenes

    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print("Uso: python overlay_detect.py <video>")
        sys.exit(1)

    if not is_available():
        print("Tesseract OCR não disponível (pip install pytesseract + instalar o motor Tesseract). A sair.")
        sys.exit(0)

    shots = detect_scenes(path)
    for i, shot in enumerate(shots):
        overlays = detect_overlays_for_shot(path, shot["start"], shot["end"])
        print(f"Plano {i}: {json.dumps(overlays, ensure_ascii=False)}")
