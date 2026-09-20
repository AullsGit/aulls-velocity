"""
Configurações centrais do pipeline.
Ajusta estes valores conforme os teus testes.
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Pastas ---
DIR_REFERENCE = os.path.join(BASE_DIR, "input", "reference")
DIR_USER_VIDEOS = os.path.join(BASE_DIR, "input", "user_videos")
DIR_USER_PHOTOS = os.path.join(BASE_DIR, "input", "user_photos")
DIR_MUSIC = os.path.join(BASE_DIR, "input", "music")
DIR_OUTPUT = os.path.join(BASE_DIR, "output")
DIR_TEMP = os.path.join(BASE_DIR, "temp")

# --- Parâmetros de scene detection ---
# Sensibilidade do corte de cena. Menor = mais sensível (mais cortes detectados).
SCENE_DETECT_THRESHOLD = 27.0

# --- Parâmetros de classificação de corte ---
# Duração mínima de um plano (segundos) — planos menores que isto são fundidos ao vizinho
MIN_SHOT_DURATION = 0.35
# Diferença de escala mínima entre frame inicial e final do plano para considerar "zoom"
ZOOM_SCALE_THRESHOLD = 0.08
# Similaridade mínima entre último frame de um plano e primeiro do próximo para considerar "fade"
FADE_SIMILARITY_THRESHOLD = 0.75

# --- Parâmetros de fotos ---
PHOTO_DURATION_SEC = 1.5
PHOTO_ZOOM_FACTOR = 1.15  # Ken Burns: zoom de 1.0 -> 1.15 ao longo da duração

# --- Parâmetros de render ---
OUTPUT_RESOLUTION = (1080, 1920)  # vertical (Reels/TikTok). Muda para (1920,1080) se for horizontal.
OUTPUT_FPS = 30
TRANSITION_DURATION = 0.3  # segundos, usado nos fades (xfade)
CRF = 18  # qualidade do H.264 (menor = melhor qualidade, ficheiro maior). 18 é "visualmente quase sem perdas"
FFMPEG_PRESET = "medium"  # velocidade vs. compressão: ultrafast..veryslow. 'medium' dá bem melhor qualidade/tamanho que 'veryfast' ao custo de mais tempo de render

# --- Matching de conteúdo (Fase 4) ---
# Se True, usa embeddings CLIP (local, grátis) para escolher qual clip do
# utilizador combina visualmente com cada plano da referência. Requer
# `pip install torch open_clip_torch`. Se não estiver instalado, o
# pipeline cai automaticamente para o matching sequencial (sem travar).
USE_SEMANTIC_MATCHING = True

# Modelo CLIP a usar. ViT-L-14 é mais preciso que ViT-B-32 (padrão mais
# leve), mas maior download (~900MB) e mais lento por imagem — dado que
# processamos poucas imagens (não vídeo em tempo real), a troca compensa
# se tiveres espaço/paciência. Alternativa mais leve: "ViT-B-32".
CLIP_MODEL_NAME = "ViT-L-14"
CLIP_PRETRAINED = "openai"

# --- Color grading (aproximação do tom/luz/saturação da referência) ---
# Analisa o brilho, contraste e saturação médios do vídeo de referência
# e ajusta cada clip do utilizador na mesma direção via filtro eq do
# FFmpeg. Ver src/color_match.py para a lógica e simplificações assumidas.
USE_COLOR_MATCHING = False

# --- Legendas automáticas (Fase 8, opcional) ---
# NÃO TESTADO no ambiente de desenvolvimento (ver src/captions.py para
# detalhes). Desativado por padrão — ativa só depois de confirmar que
# `ffmpeg -h filter=whisper` funciona na tua máquina e de baixares um
# modelo Whisper (.bin) de https://huggingface.co/ggerganov/whisper.cpp
USE_AUTO_CAPTIONS = False
WHISPER_MODEL_PATH = ""  # ex: r"C:\whisper\ggml-small.bin"
WHISPER_LANGUAGE = "pt"

# --- Zoom em vídeos (distinto do zoom em fotos) ---
VIDEO_ZOOM_FACTOR = 1.2  # quanto o zoom aproxima/afasta num plano com zoom detectado

# --- Motion Effects (boomerang, bounce, time warp — ver
# src/motion_effects.py) ---
EFFECT_POOL = []            # lista de efeitos a ciclar; vazio = nenhum efeito
EFFECT_COVERAGE_RATIO = 0.0  # fração dos planos de vídeo que recebem um efeito (0-1)
BOUNCE_REPS = 2               # nº de ciclos ida-e-volta no efeito "bounce"
TIMEWARP_SLOW_SPEED = 0.6    # velocidade da parte lenta do "time_warp" (<1 = mais lento)
TIMEWARP_FAST_SPEED = 1.8    # velocidade da parte rápida do "time_warp" (>1 = mais rápido)

# Se True, os planos com efeito são escolhidos pela ENERGIA da música
# no momento (picos primeiro) em vez de por posição — ver
# src/audio_analysis.py. Isto analisa intensidade/volume (RMS), NÃO
# reconhece a música (nome, artista, género). Cai para distribuição por
# posição se a análise falhar ou não houver fonte de áudio.
USE_MUSIC_ENERGY_ASSIGNMENT = True

# --- AI Subject Boost (deteção de pessoa + realce localizado) ---
# Requer `pip install mediapipe`. NÃO TESTADO com MediaPipe real (ver
# src/subject_detect.py — foi testada e confirmada com uma máscara
# sintética; a deteção em si precisa de um modelo .tflite (ver
# instruções em src/subject_detect.py) que ainda não foi confirmado
# nesta máquina (download bloqueado no ambiente de desenvolvimento).
USE_SUBJECT_BOOST = True
SUBJECT_SEGMENTER_MODEL_PATH = r"C:\Users\Aulls\Downloads\selfie_segmenter.tflite" # ex: r"C:\mediapipe\selfie_segmenter.tflite"

# --- Zoom "subject-locked" (o centro do Ken Burns SEGUE o sujeito) ---
# Usa o mesmo MediaPipe Image Segmenter do AI Subject Boost (precisa das
# MESMAS dependências — SUBJECT_SEGMENTER_MODEL_PATH acima) para amostrar
# a posição do centróide da pessoa detectada em vários pontos do plano e
# interpolar linearmente entre eles como o centro do zoom (em vez de
# ficar sempre fixo no meio do frame — ver src/subject_detect.py função
# estimate_subject_centroid_track e src/zoom.py).
#
# HONESTIDADE (importante): isto segue o CENTRÓIDE da máscara de
# segmentação por AMOSTRAGEM + interpolação linear — não é tracking de
# objeto frame-a-frame verdadeiro (não distingue pessoas se houver mais
# que uma, nem persiste uma "identidade"). Se a deteção for inconsistente
# ao longo do plano (pessoa sai/entra de vista, < SUBJECT_ZOOM_MIN_PRESENCE_RATIO
# das amostras), cai automaticamente para o zoom centrado normal, sem
# travar o pipeline. Só se aplica a planos de VÍDEO (não fotos) e é
# ignorado nos planos onde o Speed Ramp real (abaixo) está ativo, para
# não combinar dois efeitos dinâmicos no mesmo clip nesta versão.
USE_SUBJECT_LOCKED_ZOOM = False
SUBJECT_ZOOM_N_SAMPLES = 5
SUBJECT_ZOOM_MIN_PRESENCE_RATIO = 0.6  # fração mínima das amostras com pessoa detectada para confiar no tracking

# --- Speed Ramp real (src/speed_ramp.py) ---
# Mede a curva de energia de movimento de CADA plano da referência
# (optical flow denso) e classifica a sua FORMA (flat/ramp_up/ramp_down/
# peak_middle/valley_middle). Planos com forma real (não "flat") recebem,
# no vídeo final, uma curva de velocidade por segmentos que aproxima essa
# forma (aplicada ao clip do UTILIZADOR correspondente) — em vez do
# time_warp genérico de 2 velocidades fixas escolhido por regra/pool.
#
# HONESTIDADE (ver docstring completa em src/speed_ramp.py): a análise é
# um sinal INDIRETO (energia de movimento visual), não deteção certa de
# "isto foi acelerado em pós-produção" — pode confundir movimento de
# conteúdo real (ex: alguém a correr) com uma curva de edição. A
# reconstrução é uma aproximação em DEGRAUS (piecewise-constant), não uma
# curva suave verdadeira. Se o clip do utilizador não tiver footage
# suficiente disponível a partir do in_point escolhido para cobrir a
# curva completa, cai silenciosamente para sem-ramp (fica "flat") em vez
# de falhar — ver aviso "[Speed Ramp] ... a saltar" no log do render.
USE_SPEED_RAMP = False
SPEED_RAMP_N_SAMPLES = 12         # nº de amostras de optical flow por plano na ANÁLISE da referência
SPEED_RAMP_MIN_ENERGY_VARIATION = 0.35  # coeficiente de variação mínimo para considerar o plano "dinâmico o suficiente" (ver src/speed_ramp.py)
SPEED_RAMP_INTENSITY = 1.0        # 0 = sem efeito (tudo a 1x), 1 = curva completa como definida em speed_ramp._SHAPE_SEGMENT_SPEEDS
SPEED_RAMP_BEAT_SNAP_WINDOW = 0.2  # segundos: janela de tolerância para alinhar fronteiras de segmento a um beat próximo

# --- VFX (biblioteca em src/vfx.py) ---
USE_VFX = False
VFX_LIST = ["film_grain"]  # opções: "film_grain", "light_leak" (ver src/vfx.py)

# --- Estilos de edição (presets) ---
# Cada preset ajusta os parâmetros acima (zoom, transições, VFX, reverse)
# e o estilo visual das legendas, mantendo o vídeo de referência do
# utilizador como input central — os presets mudam COMO o sistema
# interpreta/aplica o que detecta na referência, não substituem a análise.
STYLE_PRESETS = {
    "velocity": {
        "SCENE_DETECT_THRESHOLD": 18.0,        # ainda mais sensível -> mais cortes, ritmo mais denso
        "MIN_SHOT_DURATION": 0.40,               # ritmo rápido mas ainda apreciável a olho nu (não "piscar de olhos")
        "VIDEO_ZOOM_FACTOR": 1.5,               # zoom mais agressivo/impactante (agora suave, ver src/zoom.py)
        "FORCE_ZOOM": True,                     # zoom em TODOS os planos, não só onde a referência tinha
        "FORCE_ZOOM_PATTERN": ["in", "out"],    # alterna in/out para dar variedade e impacto
        "TRANSITION_DURATION": 0.08,            # cortes bem secos, sem "amaciar"
        "TRANSITION_XFADE_TYPE": "fadewhite",   # flash de luz branco, rápido -> impacto energético
        "USE_VFX": False,                       # desligado -> mantém as cores/aparência originais do vídeo
        "VFX_LIST": ["film_grain"],
        "VFX_STRENGTH": {"film_grain": 12.0},
        # "time_warp" saiu do pool aqui: agora que existe deteção REAL de
        # curva de velocidade (src/speed_ramp.py, ligado abaixo), planos com
        # uma curva detectada na referência usam essa reconstrução real em
        # vez do time_warp genérico de 2 velocidades fixas — o pool só
        # continua a cobrir os planos "flat" (sem curva detectada), com os
        # efeitos que não fingem reproduzir um padrão específico da referência.
        "EFFECT_POOL": ["boomerang", "bounce"],
        "EFFECT_COVERAGE_RATIO": 0.2,            # no máximo ~1 em cada 5 planos de vídeo (sem speed ramp) recebem um efeito
        "USE_COLOR_MATCHING": False,            # desligado -> sem forçar cor da referência, look natural
        "COLOR_MATCH_INTENSITY": 0.6,
        "USE_SPEED_RAMP": True,                 # traço definidor do "Velocity Edit" — ver bloco de config acima
        "USE_SUBJECT_LOCKED_ZOOM": True,        # zoom segue o sujeito quando detectado, em vez de ficar centrado
        "captions": {
            "FontName": "Arial Black",
            "FontSize": 22,
            "PrimaryColour": "&H0000FFFF",     # amarelo vibrante (BGR ASS)
            "OutlineColour": "&H00000000",
            "Bold": 1,
            "Alignment": 2,                     # centro-baixo
            "MarginV": 90,
        },
    },
    "memory_rewind": {
        "SCENE_DETECT_THRESHOLD": 30.0,        # menos sensível -> planos mais longos
        "MIN_SHOT_DURATION": 0.6,               # ritmo mais lento e contemplativo, condiz com o estilo
        "VIDEO_ZOOM_FACTOR": 1.25,
        "FORCE_ZOOM": True,
        "FORCE_ZOOM_PATTERN": ["out"],          # zoom-out constante -> sensação de "afastar da memória"
        "TRANSITION_DURATION": 0.65,            # fades ainda mais longos e sonhadores
        "TRANSITION_XFADE_TYPE": "fade",        # dissolve suave normal, sem flash (mantém o tom nostálgico)
        "USE_VFX": False,                       # desligado -> mantém as cores/aparência originais do vídeo
        "VFX_LIST": ["film_grain", "light_leak"],
        "VFX_STRENGTH": {"film_grain": 14.0, "light_leak": 0.35},
        "EFFECT_POOL": ["bounce", "boomerang"],  # mais suaves/lentos, sem time_warp (energia rápida demais para o tom)
        "EFFECT_COVERAGE_RATIO": 0.2,
        "USE_COLOR_MATCHING": False,            # desligado -> sem forçar cor da referência, look natural
        "COLOR_MATCH_INTENSITY": 0.7,
        "USE_SPEED_RAMP": False,                # ritmo lento/nostálgico -> curvas de velocidade não fazem sentido aqui
        "USE_SUBJECT_LOCKED_ZOOM": False,
        "captions": {
            "FontName": "Georgia",
            "FontSize": 16,
            "PrimaryColour": "&H00E8E8E8",      # branco suave, não puro
            "OutlineColour": "&H00202020",
            "Bold": 0,
            "Alignment": 2,
            "MarginV": 70,
        },
    },
    "clean_minimal": {
        "SCENE_DETECT_THRESHOLD": 27.0,
        "MIN_SHOT_DURATION": 0.45,
        "VIDEO_ZOOM_FACTOR": 1.02,              # zoom quase inexistente -> ainda mais "limpo"
        "FORCE_ZOOM": False,                    # zoom raro é intencional neste estilo
        "TRANSITION_DURATION": 0.05,            # corte cirúrgico, o mais seco de todos
        "TRANSITION_XFADE_TYPE": "fade",        # sem flash — mantém o "clean" do estilo
        "USE_VFX": False,
        "VFX_LIST": [],
        # NOTA: mantive a cobertura BAIXA aqui de propósito, mesmo tendo
        # pedido 70% para o sistema — "clean minimal" é literalmente
        # definido pela ausência de efeitos chamativos; aplicar isto a
        # 70% dos cortes destrói o conceito do próprio estilo. Se
        # quiseres mesmo 70% aqui também, muda para 0.7 e diz-me.
        "EFFECT_POOL": ["boomerang"],
        "EFFECT_COVERAGE_RATIO": 0.15,
        "USE_COLOR_MATCHING": False,            # desligado -> look natural do conteúdo do utilizador
        "COLOR_MATCH_INTENSITY": 0.4,
        "USE_SPEED_RAMP": False,
        "USE_SUBJECT_LOCKED_ZOOM": False,
        "captions": {
            "FontName": "Helvetica",
            "FontSize": 14,
            "PrimaryColour": "&H00FFFFFF",
            "OutlineColour": "&H00000000",
            "Bold": 0,
            "Alignment": 2,
            "MarginV": 60,
        },
    },
}
DEFAULT_STYLE = "velocity"

# --- Deteção automática de estilo (opcional) ---
# Se True, o sistema tenta classificar o "mood" da referência via CLIP
# (zero-shot texto-imagem) + tempo (BPM) e escolhe automaticamente qual
# preset de STYLE_PRESETS aplicar, em vez de exigir escolha manual.
# Requer as mesmas dependências do matching semântico (torch, open_clip).
# Se indisponível ou a classificação falhar, cai para DEFAULT_STYLE.
USE_AUTO_STYLE_DETECTION = True

MOOD_LABEL_DESCRIPTIONS = {
    "velocity": "fast paced energetic aggressive action packed video with quick cuts",
    "memory_rewind": "calm nostalgic romantic emotional slow sentimental video",
    "clean_minimal": "simple clean minimal neutral straightforward video with plain cuts",
}

# BPM acima disto reforça a favor de 'velocity'; abaixo disto reforça
# 'memory_rewind'. Entre os dois, o BPM não pesa a decisão.
BPM_FAST_THRESHOLD = 120
BPM_SLOW_THRESHOLD = 85

# --- Áudio ---
USE_REFERENCE_AUDIO_IF_NO_MUSIC = True  # se True e não houver música em input/music, usa o áudio da referência

# --- Edit DNA (src/edit_dna.py) ---
# Movimento de câmera (pan/rotação/zoom, via src/camera_motion.py) é
# analisado por padrão — custo baixo (feature matching ORB em poucos
# frames por plano), sem dependências extra.
ENABLE_CAMERA_MOTION_ANALYSIS = True

# Deteção de texto/legendas na referência (src/overlay_detect.py) via
# OCR — DESLIGADO por padrão porque exige o motor Tesseract-OCR
# instalado no sistema (não só o pacote pip `pytesseract`), tal como o
# FFmpeg/Whisper/MediaPipe também exigem binários/modelos externos neste
# projeto. Instalação: https://github.com/UB-Mannheim/tesseract/wiki
# Se o tesseract.exe não estiver no PATH, aponta TESSERACT_CMD_PATH para lá.
USE_OVERLAY_DETECTION = False
TESSERACT_CMD_PATH = ""  # ex: r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# --- Refinamento Iterativo (src/compare_refine.py) ---
# Depois de renderizar, o pipeline analisa o próprio resultado (mesma
# Edit DNA usada na referência) e compara com a referência. Se a
# ENABLE_ITERATIVE_REFINEMENT estiver ligada, ajusta automaticamente
# parâmetros de RENDER (duração de transições, magnitude do zoom, color
# matching — ver compare_refine.suggest_adjustments) e renderiza de
# novo, repetindo até estagnar, atingir REFINE_TARGET_SCORE, ou atingir
# MAX_REFINE_ITERATIONS. Ver src/compare_refine.py para o âmbito exato
# (só afeta a fase de render, não repete a deteção/matching de conteúdo).
ENABLE_ITERATIVE_REFINEMENT = True
MAX_REFINE_ITERATIONS = 2          # nº de RE-renders extra após o primeiro (0 = só o render normal + 1 análise, sem re-render)
REFINE_IMPROVEMENT_THRESHOLD = 0.01  # para de iterar se o score melhorar menos que isto entre iterações
REFINE_TARGET_SCORE = 0.92          # para de iterar se o score atingir isto (0-1)
REFINE_MIN_TRANSITION_DURATION = 0.05
REFINE_MAX_ZOOM_FACTOR = 2.0

# Peso de cada métrica no similarity_score (0-1, devem somar ~1.0, mas
# são normalizados internamente por segurança mesmo que não somem).
# NOTA: "speed_curve" só entra de facto no score quando USE_SPEED_RAMP
# está ligado (senão fica sempre 1.0 — neutro, ver src/compare_refine.py).
REFINE_METRIC_WEIGHTS = {
    "duration": 0.15,
    "cut_count": 0.15,
    "zoom": 0.15,
    "transition": 0.15,
    "color": 0.20,
    "camera_motion": 0.10,
    "speed_curve": 0.10,
}

# --- AI Analyzer (src/ai_analyzer.py) — opcional, DESLIGADO por padrão ---
# Troca as heurísticas locais (ORB/histograma) por um modelo Claude com
# visão a julgar diretamente os frames de cada plano (movimento de
# câmera, cor, ritmo). Corrige o cenário em que a heurística local fica
# "presa" num valor baixo mesmo ajustando parâmetros (ex: zoom_match_ratio
# não melhora ao aumentar VIDEO_ZOOM_FACTOR porque o problema real é a
# DETEÇÃO de direção, não a magnitude).
#
# CUSTA DINHEIRO: cada plano avaliado é uma chamada de API à tua conta
# Anthropic (2 imagens + texto por chamada), repetida em CADA iteração
# do refinamento. Para ~10-12 planos e as 3 iterações por defeito, conta
# poucas dezenas de chamadas — tipicamente cêntimos de dólar com
# claude-sonnet-5, mas depende do teu plano/preços atuais em
# https://docs.claude.com/en/docs/about-claude/pricing. Vem DESLIGADO de propósito.
#
# Para ativar:
#   1. pip install anthropic
#   2. define a variável de ambiente ANTHROPIC_API_KEY (uma API key da
#      tua conta Anthropic, criada em https://console.anthropic.com/settings/keys
#      — isto é DIFERENTE de teres a app/site Claude, é uma chave de API)
#   3. USE_AI_ANALYZER = True
USE_AI_ANALYZER = False
AI_ANALYZER_MODEL = "claude-sonnet-5"  # alternativas: "claude-haiku-4-5-20251001" (mais barato/rápido), "claude-opus-5" (mais preciso, mais caro)
AI_ANALYZER_MAX_SHOTS = 12  # limite de planos avaliados por vídeo/iteração (amostra distribuída se houver mais)
AI_ANALYZER_MATCH_THRESHOLD = 0.6  # score 0-1 do modelo acima disto conta como "bateu certo"
