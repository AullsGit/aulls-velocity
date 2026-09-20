"""
AI Analyzer (opcional, DESLIGADO por padrão) — usa um modelo Claude com
visão (API da Anthropic) como "segundo par de olhos" na comparação entre
a referência e o vídeo gerado, em vez de só as heurísticas locais (ORB,
histograma de cor) de src/camera_motion.py e src/color_match.py.

PORQUÊ ISTO EXISTE: as heurísticas locais são frágeis em planos curtos
e perto de transições — na prática, isso fez o `zoom_match_ratio` ficar
preso num valor baixo mesmo depois do Refinamento Iterativo aumentar
VIDEO_ZOOM_FACTOR (o ajuste certo para "zoom subtil demais", mas não
para "a deteção de direção do zoom está a falhar"). Um modelo de visão
a olhar de facto para os frames tende a julgar "isto está a fazer o
mesmo movimento que a referência?" de forma mais robusta do que feature
matching 2D.

REQUER (instala e ativa por tua conta — ver README):
  pip install anthropic
  variável de ambiente ANTHROPIC_API_KEY (uma CHAVE DE API da tua conta
  Anthropic — diferente da app Claude que usaste para gerar este código;
  cria-se em https://console.anthropic.com/settings/keys)
  config.USE_AI_ANALYZER = True

HONESTIDADE:
- Isto CUSTA DINHEIRO (chamadas de API, cobradas à tua conta) e tempo
  (latência de rede), por cada plano avaliado, em CADA iteração do
  refinamento — por isso vem desligado por padrão, com um limite de
  planos por vídeo (config.AI_ANALYZER_MAX_SHOTS, amostra distribuída
  se houver mais planos que isso) para o custo não escalar sem controlo
  com vídeos longos.
- Não é determinístico bit-a-bit (o mesmo par de frames pode receber
  notas ligeiramente diferentes em chamadas diferentes) — por isso os
  vereditos entram no mesmo pipeline de agregação e nos mesmos clamps
  deterministas de compare_refine.suggest_adjustments, nunca aplicando
  um número "cru" do modelo sem validação.
- Continua a NÃO ser cópia pixel-a-pixel (o teu conteúdo é sempre
  diferente do da referência) — isto melhora o julgamento de ESTILO
  (movimento de câmera, cor, ritmo), não elimina a diferença de
  conteúdo, que é inerente ao próprio conceito do projeto.
"""
import os
import sys
import base64
import json
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

import config


def is_available() -> bool:
    """Verifica se o SDK `anthropic` está instalado E há uma API key configurada."""
    try:
        import anthropic  # noqa: F401
    except Exception:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()  # lê ANTHROPIC_API_KEY do ambiente
    return _client


def _frame_to_jpeg_b64(frame, max_dim: int = 512, quality: int = 80) -> str:
    """Reduz o frame antes de enviar — mantém o custo/latência da chamada
    baixos; para julgar movimento/cor/ritmo não é preciso resolução total."""
    h, w = frame.shape[:2]
    scale = min(1.0, max_dim / max(h, w))
    if scale < 1.0:
        frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("Falha ao codificar frame para JPEG.")
    return base64.b64encode(buf.tobytes()).decode("ascii")


def _get_frame_at(video_path: str, timestamp_sec: float):
    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_MSEC, timestamp_sec * 1000)
    ok, frame = cap.read()
    cap.release()
    return frame if ok else None


def _extract_json(text: str) -> dict:
    """Extrai o primeiro objeto JSON do texto — o modelo por vezes escreve
    uma frase antes/depois apesar do pedido de responder só com JSON."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"Nenhum JSON na resposta do modelo: {text[:200]!r}")
    return json.loads(match.group(0))


_PROMPT_TEMPLATE = """Estás a avaliar se um plano de vídeo GERADO replica o ESTILO de edição \
de um plano de REFERÊNCIA — não o conteúdo (as pessoas/objetos serão diferentes, isso é \
esperado e correto, ignora completamente essa diferença). Avalia especificamente:

1. Movimento de câmera/enquadramento: o GERADO tem o mesmo tipo de movimento que a \
REFERÊNCIA (estático, zoom in, zoom out, pan, rotação, handheld/tremido)? A análise \
automática da referência classificou este plano como: zoom={ref_has_zoom} \
direção={ref_zoom_dir}, movimento={ref_motion_type} (esta classificação pode estar errada — \
usa-a como pista, não como verdade absoluta; confia mais no que vês nas duas imagens).
2. Cor/luz: o tom geral (brilho, contraste, saturação, temperatura de cor) do GERADO vai na \
mesma direção que o da REFERÊNCIA?
3. Ritmo: o plano do GERADO dura {out_duration:.2f}s e o da REFERÊNCIA {ref_duration:.2f}s — \
essa diferença de duração é percetível/relevante para a sensação de ritmo do plano?

Responde APENAS com um objeto JSON, sem nenhum texto antes ou depois, exatamente neste \
formato:
{{"camera_motion_match": <inteiro 0-10>, "color_match": <inteiro 0-10>, \
"pacing_match": <inteiro 0-10>, "overall": <inteiro 0-10>, \
"note": "<uma frase curta em português só se houver algo relevante e específico a apontar, senão string vazia>"}}
"""


def judge_shot_pair(reference_path: str, output_path: str, ref_shot: dict, out_shot: dict, model: str = None) -> dict:
    """
    Pede a um modelo Claude com visão para julgar um par de planos
    (frame do meio de cada um) e devolve um veredito estruturado (0-1,
    já normalizado) em movimento de câmera, cor e ritmo. Lança exceção
    se a API falhar, os frames não puderem ser extraídos, ou a resposta
    não for parseável — o chamador (judge_all_shots) trata isso como
    "sem veredito de IA para este plano" e não derruba o resto.
    """
    client = _get_client()
    model = model or getattr(config, "AI_ANALYZER_MODEL", "claude-sonnet-5")

    ref_frame = _get_frame_at(reference_path, (ref_shot["start"] + ref_shot["end"]) / 2)
    out_frame = _get_frame_at(output_path, (out_shot["start"] + out_shot["end"]) / 2)
    if ref_frame is None or out_frame is None:
        raise RuntimeError("Não foi possível extrair frames para julgamento.")

    ref_b64 = _frame_to_jpeg_b64(ref_frame)
    out_b64 = _frame_to_jpeg_b64(out_frame)

    rm = ref_shot.get("camera_motion") or {}
    prompt = _PROMPT_TEMPLATE.format(
        ref_has_zoom=ref_shot["zoom"]["has_zoom"],
        ref_zoom_dir=ref_shot["zoom"]["direction"],
        ref_motion_type=rm.get("motion_type", "desconhecido"),
        out_duration=out_shot["duration"],
        ref_duration=ref_shot["duration"],
    )

    message = client.messages.create(
        model=model,
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "Frame de REFERÊNCIA (plano a replicar):"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": ref_b64}},
                {"type": "text", "text": "Frame GERADO (tentativa de replicar o estilo acima):"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": out_b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )

    text = "".join(block.text for block in message.content if getattr(block, "type", None) == "text")
    verdict = _extract_json(text)

    return {
        "camera_motion_match": float(verdict.get("camera_motion_match", 5)) / 10.0,
        "color_match": float(verdict.get("color_match", 5)) / 10.0,
        "pacing_match": float(verdict.get("pacing_match", 5)) / 10.0,
        "overall": float(verdict.get("overall", 5)) / 10.0,
        "note": str(verdict.get("note", "")).strip(),
    }


def judge_all_shots(reference_path: str, output_path: str, aligned_pairs: list, max_shots: int = None, model: str = None) -> list:
    """
    Corre judge_shot_pair sobre até max_shots pares alinhados
    (ver compare_refine._align_shots — pares com out=None são ignorados,
    nada para comparar). Devolve uma lista de vereditos, cada um com
    "index_ref"/"index_out" adicionados, ou {"index_ref","index_out",
    "error"} se essa chamada falhou — NUNCA levanta exceção para o
    chamador, para uma falha de rede/API não derrubar o refinamento.

    max_shots limita o custo: em vídeos com muitos planos, amostra
    distribuído ao longo do vídeo (não só os primeiros N).
    """
    max_shots = max_shots if max_shots is not None else getattr(config, "AI_ANALYZER_MAX_SHOTS", 12)

    usable = [(r, o) for r, o in aligned_pairs if o is not None]
    if max_shots and len(usable) > max_shots:
        indices = sorted(set(np.linspace(0, len(usable) - 1, num=max_shots, dtype=int).tolist()))
        usable = [usable[i] for i in indices]

    results = []
    for ref, out in usable:
        try:
            verdict = judge_shot_pair(reference_path, output_path, ref, out, model=model)
            verdict["index_ref"] = ref["index"]
            verdict["index_out"] = out["index"]
            results.append(verdict)
        except Exception as e:
            results.append({"index_ref": ref["index"], "index_out": out["index"], "error": str(e)})

    return results


if __name__ == "__main__":
    if not is_available():
        print(
            "AI Analyzer indisponível: instala com 'pip install anthropic' e define a "
            "variável de ambiente ANTHROPIC_API_KEY (ver docstring do módulo)."
        )
        sys.exit(0)
    print(
        "AI Analyzer disponível. É usado automaticamente por src/compare_refine.py "
        "quando config.USE_AI_ANALYZER = True."
    )
