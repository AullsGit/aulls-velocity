"""
Matching semântico via CLIP (open-source, roda localmente, sem custo).

Em vez de preencher os slots do EDL sequencialmente (clip 1 -> slot 1,
clip 2 -> slot 2, ...), este módulo usa embeddings visuais CLIP para
escolher, para cada slot do padrão de referência, qual dos teus clips
tem o conteúdo mais parecido visualmente com o plano correspondente
da referência.

Não entende "narrativa" ou "intenção" como um LLM multimodal entenderia,
mas resolve bem o problema de "que clip combina com que plano" a nível
visual (cor, composição, tipo de cena, presença de pessoas/objetos
semelhantes).

Requer: pip install open_clip_torch torch
(download do modelo ~350MB na primeira execução, depois fica em cache)
"""
import os
import sys
import cv2
import numpy as np
from PIL import Image

_model = None
_preprocess = None
_tokenizer = None
_device = None


def _lazy_load_model():
    """Carrega o modelo CLIP uma única vez (custoso, por isso é 'lazy')."""
    global _model, _preprocess, _tokenizer, _device
    if _model is not None:
        return

    import torch
    import open_clip

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import config
    model_name = getattr(config, "CLIP_MODEL_NAME", "ViT-B-32")
    pretrained = getattr(config, "CLIP_PRETRAINED", "openai")

    _device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"  [CLIP] Carregando modelo {model_name} ({pretrained}) em {_device}...")
    _model, _, _preprocess = open_clip.create_model_and_transforms(
        model_name, pretrained=pretrained
    )
    _model = _model.to(_device).eval()
    _tokenizer = open_clip.get_tokenizer(model_name)


def is_available() -> bool:
    """Verifica se as dependências (torch, open_clip) estão instaladas e utilizáveis."""
    try:
        import torch  # noqa: F401
        import open_clip  # noqa: F401
        return True
    except Exception:
        # Cobre ImportError (não instalado) e também falhas de carregamento
        # de bibliotecas nativas (ex: instalação incompleta/corrompida) —
        # em qualquer caso, o pipeline deve cair para o fallback sequencial
        # em vez de rebentar.
        return False


def _embed_text(text: str) -> np.ndarray:
    import torch
    _lazy_load_model()
    tokens = _tokenizer([text]).to(_device)
    with torch.no_grad():
        features = _model.encode_text(tokens)
        features = features / features.norm(dim=-1, keepdim=True)
    return features.cpu().numpy()[0]


def classify_mood(video_path: str, candidate_labels: dict, n_samples: int = 6) -> dict:
    """
    Classificação zero-shot do 'mood'/estilo do vídeo via CLIP: compara
    frames amostrados da referência contra descrições textuais candidatas
    (ex: {"velocity": "fast energetic aggressive action video editing",
    "memory_rewind": "calm nostalgic romantic emotional video"}).

    Retorna {"scores": {label: float}, "best_label": str} — 'scores' são
    similaridades médias (cosine, 0-1 aprox.) entre os frames e cada
    descrição; 'best_label' é a chave com maior score.

    Isto não é "compreensão" ao nível de um LLM multimodal — é uma
    aproximação por similaridade texto-imagem, gratuita e local via CLIP.
    """
    cap = cv2.VideoCapture(video_path)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total_frames <= 0:
        cap.release()
        raise RuntimeError(f"Não foi possível ler frames de {video_path}")

    indices = np.linspace(0, max(total_frames - 1, 0), num=min(n_samples, total_frames), dtype=int)
    frame_embeddings = []
    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
        ok, frame = cap.read()
        if ok:
            frame_embeddings.append(_embed_image(_frame_to_pil(frame)))
    cap.release()

    if not frame_embeddings:
        raise RuntimeError(f"Nenhum frame válido extraído de {video_path}")

    text_embeddings = {label: _embed_text(desc) for label, desc in candidate_labels.items()}

    scores = {}
    for label, text_emb in text_embeddings.items():
        sims = [_cosine_sim(frame_emb, text_emb) for frame_emb in frame_embeddings]
        scores[label] = float(np.mean(sims))

    best_label = max(scores, key=scores.get)
    return {"scores": scores, "best_label": best_label}


def _frame_to_pil(frame_bgr) -> Image.Image:
    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    return Image.fromarray(frame_rgb)


def _get_middle_frame_video(video_path: str, start: float = None, end: float = None):
    """Extrai o frame do meio de um vídeo (ou de um trecho start->end)."""
    cap = cv2.VideoCapture(video_path)
    if start is None or end is None:
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        mid_time = (total_frames / fps) / 2.0
    else:
        mid_time = (start + end) / 2.0
    cap.set(cv2.CAP_PROP_POS_MSEC, mid_time * 1000)
    ok, frame = cap.read()
    cap.release()
    return frame if ok else None


def _embed_image(pil_image: Image.Image) -> np.ndarray:
    import torch
    _lazy_load_model()
    image_input = _preprocess(pil_image).unsqueeze(0).to(_device)
    with torch.no_grad():
        features = _model.encode_image(image_input)
        features = features / features.norm(dim=-1, keepdim=True)
    return features.cpu().numpy()[0]


def embed_reference_shots(reference_path: str, shots: list) -> list:
    """Retorna um embedding CLIP por shot (usando o frame do meio de cada plano)."""
    embeddings = []
    for shot in shots:
        frame = _get_middle_frame_video(reference_path, shot["start"], shot["end"])
        if frame is None:
            embeddings.append(None)
            continue
        embeddings.append(_embed_image(_frame_to_pil(frame)))
    return embeddings


def embed_user_media(videos: list, photos: list) -> dict:
    """
    Retorna um dict {caminho_do_ficheiro: embedding} para todos os vídeos
    e fotos do utilizador.
    """
    embeddings = {}
    for v in videos:
        frame = _get_middle_frame_video(v)
        if frame is not None:
            embeddings[v] = _embed_image(_frame_to_pil(frame))
    for p in photos:
        try:
            img = Image.open(p).convert("RGB")
            embeddings[p] = _embed_image(img)
        except Exception:
            continue
    return embeddings


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))


def match_shots_to_media(shot_embeddings: list, media_embeddings: dict) -> list:
    """
    Para cada shot (na ordem), escolhe o ficheiro do utilizador mais
    parecido visualmente que ainda não foi usado. Quando os ficheiros
    disponíveis se esgotam, reinicia o ciclo (permite reutilização).

    Retorna uma lista paralela a shot_embeddings:
    [{"source_path": str, "similarity": float}, ...]
    """
    all_paths = list(media_embeddings.keys())
    if not all_paths:
        raise RuntimeError("Nenhum embedding de conteúdo do utilizador disponível.")

    available = set(all_paths)
    results = []

    for shot_emb in shot_embeddings:
        if not available:
            available = set(all_paths)  # reinicia o ciclo

        if shot_emb is None:
            # Não foi possível extrair frame do plano da referência -> escolhe qualquer disponível
            chosen = next(iter(available))
            similarity = 0.0
        else:
            best_path = None
            best_sim = -1.0
            for path in available:
                sim = _cosine_sim(shot_emb, media_embeddings[path])
                if sim > best_sim:
                    best_sim = sim
                    best_path = path
            chosen = best_path
            similarity = best_sim

        available.discard(chosen)
        results.append({"source_path": chosen, "similarity": similarity})

    return results
