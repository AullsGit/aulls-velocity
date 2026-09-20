# Video Edit AI — MVP (edição por referência)

Protótipo local que analisa o padrão de edição de um vídeo de referência
e aplica esse padrão ao teu próprio conteúdo (vídeos/fotos), gerando
automaticamente um vídeo editado.

## Instalação

```bash
cd video_edit_ai
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Precisas também do **FFmpeg** instalado no sistema (não é um pacote pip):
- Mac: `brew install ffmpeg`
- Ubuntu/Debian: `sudo apt install ffmpeg`
- Windows: baixa de https://ffmpeg.org/download.html e adiciona ao PATH

Confirma que está tudo certo:
```bash
ffmpeg -version
python3 -c "import cv2, scenedetect, librosa; print('OK')"
```

## Como usar

1. Coloca o vídeo de referência em `input/reference/` (só 1 ficheiro)
2. Coloca os teus vídeos em `input/user_videos/`
3. Coloca as tuas fotos em `input/user_photos/`
4. (Opcional) Coloca uma música em `input/music/`
5. Roda:

```bash
python3 main.py
```

O vídeo final aparece em `output/video_final.mp4` — ou, se o
Refinamento Iterativo (ativo por padrão, ver secção "Edit DNA" abaixo)
concluir que uma iteração posterior ficou mais próxima da referência,
em `output/video_final_v1.mp4`/`_v2.mp4` etc.; o caminho exato do
melhor resultado é sempre impresso no fim da execução ("CONCLUÍDO: ...").

## Testar cada fase isoladamente (útil para debug)

```bash
# Fase 1 — ver os cortes detectados na referência
python3 src/scene_detect.py input/reference/meu_video.mp4

# Fase 2 — ver zoom/transições classificados
python3 src/classify_cuts.py input/reference/meu_video.mp4

# Fase 2b — ver pan/rotação/zoom estimados por plano (camera motion)
python3 src/camera_motion.py input/reference/meu_video.mp4

# Fase 3 — ver beats detectados na música
python3 src/beat_sync.py input/music/minha_musica.mp3

# Fase 4 — conferir que os teus ficheiros estão a ser lidos
python3 src/content_mapper.py

# Edit DNA — ver a representação estruturada completa da referência
python3 src/edit_dna.py input/reference/meu_video.mp4

# Deteção de overlays/legendas (requer Tesseract instalado — ver secção abaixo)
python3 src/overlay_detect.py input/reference/meu_video.mp4

# AI Analyzer — confirma se está disponível (requer 'pip install anthropic' + ANTHROPIC_API_KEY)
python3 src/ai_analyzer.py

# Comparar duas Edit DNA já guardadas em temp/ (ex: referência vs. um output)
python3 src/compare_refine.py temp/edit_dna_reference.json temp/refine_iteration_0.json

# Speed Ramp — ver a forma da curva de velocidade detectada por plano na referência
python3 src/speed_ramp.py input/reference/meu_video.mp4
```

## Ajustar o comportamento

Todos os parâmetros relevantes estão em `config.py`:
- `SCENE_DETECT_THRESHOLD` — menor valor = mais cortes detectados (mais sensível)
- `ZOOM_SCALE_THRESHOLD` — menor valor = mais planos marcados como "zoom"
- `FADE_SIMILARITY_THRESHOLD` — maior valor = mais exigente para marcar como "fade"
- `OUTPUT_RESOLUTION` — muda para `(1920, 1080)` se quiseres saída horizontal
- `TRANSITION_DURATION` — duração dos fades em segundos
- `USE_SPEED_RAMP` / `SPEED_RAMP_*` — Speed Ramp real (ver secção dedicada abaixo)
- `USE_SUBJECT_LOCKED_ZOOM` / `SUBJECT_ZOOM_*` — zoom que segue o sujeito (ver secção dedicada abaixo)

## Edit DNA — a "receita digital de edição"

Além das fases que já existiam (cortes, zoom, transições, beat sync), o
pipeline agora junta tudo isso — mais movimento de câmera e cor por
plano — numa única estrutura JSON chamada **Edit DNA**, guardada em
`temp/edit_dna_reference.json` sempre que corres `main.py`. É a
representação explícita da "lógica de edição" da referência: uma lista
de planos, cada um com o seu timing, tipo de transição para o seguinte,
zoom (se houver, com direção), movimento de câmera estimado (pan
horizontal/vertical, rotação, escala — ver `src/camera_motion.py`) e
perfil de cor (brilho/contraste/saturação — ver `src/color_match.py`),
mais um resumo do vídeo inteiro (`style_signature`: nº de planos,
cortes/minuto, % de planos com zoom, % com fade) e, se houver fonte de
áudio, o tempo (BPM) e os beats.

Também é usada para o **próprio vídeo que o pipeline gera** — depois de
renderizar, o sistema constrói a Edit DNA do resultado exatamente da
mesma forma e compara-a com a da referência (`src/compare_refine.py`).
Isto é o **Refinamento Iterativo**: gerar → analisar o resultado →
comparar com a referência → ajustar parâmetros → renderizar de novo,
até a melhoria estagnar ou um score-alvo ser atingido
(`config.MAX_REFINE_ITERATIONS`, `config.REFINE_TARGET_SCORE`). Fica
ativo por padrão (`config.ENABLE_ITERATIVE_REFINEMENT = True`); cada
iteração fica registada em `temp/refine_iteration_N.json` (a Edit DNA
do output dessa iteração + o diff medido contra a referência), e o
ficheiro final entregue (`output/video_final.mp4`, possivelmente vindo
de uma iteração `_v1`/`_v2` internamente) é sempre o de **maior
similarity_score visto**, nunca pior que a primeira tentativa — se uma
iteração piorar o resultado, é descartada.

**O que o refinamento ajusta, e o que NÃO ajusta (importante):** só
mexe em parâmetros da fase de RENDER — duração das transições
(`TRANSITION_DURATION`), magnitude do zoom (`VIDEO_ZOOM_FACTOR`), e
liga/reforça o color matching (`USE_COLOR_MATCHING`,
`COLOR_MATCH_INTENSITY`). NÃO repete a deteção de cortes nem o matching
de conteúdo (Fase 1/2/4) — mudar esses exigiria re-detectar tudo e
potencialmente re-correr o CLIP (caro a cada iteração), o que tornaria
o loop lento e instável nesta versão. Ou seja: o refinamento otimiza
**como** o padrão já detectado é renderizado, não muda a estrutura de
cortes escolhida. Ver `src/compare_refine.py` para as regras exatas
(`suggest_adjustments`) — são deterministas e limitadas por clamps, não
há nenhum modelo "a decidir".

**Deteção de overlays/legendas (opcional, desligada por padrão):**
`src/overlay_detect.py` deteta texto na tela da referência via OCR
(Tesseract) e entra na Edit DNA (posição, texto, plano onde aparece).
Requer o motor Tesseract-OCR instalado no sistema, não só o pacote pip:
- Windows: instalador em https://github.com/UB-Mannheim/tesseract/wiki
- Mac: `brew install tesseract`
- Ubuntu/Debian: `sudo apt install tesseract-ocr`

Depois de instalado, ativa com `config.USE_OVERLAY_DETECTION = True`
(e, se o executável não estiver no PATH, aponta
`config.TESSERACT_CMD_PATH` para ele). Isto deteta e regista os
overlays da referência — **não os reproduz automaticamente** no vídeo
final: decidir que texto novo pôr lá é um problema de conteúdo, não de
deteção (ver docstring do módulo).

### AI Analyzer — julgamento por IA no Refinamento Iterativo (opcional, desligado por padrão)

As heurísticas locais (ORB/histograma) que julgam se o render bateu
certo com a referência são frágeis em planos curtos e perto de
transições — na prática, isso pode deixar métricas como
`zoom_match_ratio` presas num valor baixo mesmo depois do refinamento
ajustar `VIDEO_ZOOM_FACTOR` (o ajuste certo para "zoom subtil demais",
não para "a deteção de direção está a falhar"). `src/ai_analyzer.py`
troca esse julgamento, plano a plano, por um modelo Claude com visão
(API da Anthropic) a olhar de facto para o frame da referência e o do
teu vídeo lado a lado.

**Isto CUSTA DINHEIRO** — cada plano avaliado é uma chamada de API à
tua conta, repetida em cada iteração do refinamento (para ~10-12 planos
e as 3 iterações por defeito, tipicamente poucas dezenas de chamadas —
conta cêntimos de dólar com `claude-sonnet-5`, mas confirma preços
atuais em https://docs.claude.com/en/docs/about-claude/pricing). Por
isso vem **desligado por padrão** e limitado a
`config.AI_ANALYZER_MAX_SHOTS` planos por vídeo (amostra distribuída se
houver mais planos que isso).

Para ativar:
```bash
pip install anthropic
# Windows (PowerShell):
$env:ANTHROPIC_API_KEY = "sk-ant-..."
# Mac/Linux:
export ANTHROPIC_API_KEY="sk-ant-..."
```
A chave cria-se em https://console.anthropic.com/settings/keys — é uma
**API key**, diferente de teres conta na app/site Claude. Depois, em
`config.py`: `USE_AI_ANALYZER = True`.

Os vereditos da IA (0-10 em movimento de câmera, cor e ritmo, mais uma
nota curta) substituem as heurísticas locais **só nos planos avaliados**
e entram no MESMO cálculo agregado e nas MESMAS regras deterministas de
`suggest_adjustments` — não há um modelo "a decidir" ajustes numéricos
sozinho, só a julgar se o plano bateu certo ou não. As notas de texto
de cada iteração aparecem na consola e ficam guardadas em
`temp/refine_iteration_N.json`.

**Importante sobre "99.9% de fidelidade":** mesmo com isto ligado, não
esperes uma cópia pixel-a-pixel da referência — o teu conteúdo
(pessoas, objetos, cenários) é sempre diferente do dela, por definição
do próprio projeto. O que este analisador melhora é o julgamento de
**estilo** (o movimento/cor/ritmo bateu certo, apesar do conteúdo ser
diferente), que é o que as heurísticas locais falhavam a medir bem.

### Speed Ramp real + Zoom Subject-Locked — o traço definidor do "Velocity Edit" (`config.USE_SPEED_RAMP` / `config.USE_SUBJECT_LOCKED_ZOOM`, ligados por padrão no preset `velocity`)

Isto substitui o que antes era só um efeito genérico de 2 velocidades
fixas (`time_warp`, escolhido por regra/pool sem olhar para a
referência) por uma reconstrução que parte de uma **análise real** de
cada plano da referência:

- **Análise (`src/speed_ramp.py`):** mede a energia de movimento visual
  (optical flow denso) ao longo de cada plano e classifica a FORMA da
  curva — `flat`, `ramp_up`, `ramp_down`, `peak_middle` (lento-rápido-lento,
  o padrão mais clássico de velocity edit) ou `valley_middle`. Um plano só
  é considerado "dinâmico o suficiente" para valer a pena reconstruir se
  a variação relativa de energia ultrapassar `config.SPEED_RAMP_MIN_ENERGY_VARIATION` —
  senão fica `flat` (sem ramp), para não "inventar" curvas em planos
  uniformes. Isto entra na Edit DNA de cada plano (`shots[i].speed_curve`).
- **Reconstrução:** o plano correspondente do TEU vídeo recebe uma curva
  de velocidade por segmentos (não só 2, como o `time_warp` antigo — ver
  `speed_ramp._SHAPE_SEGMENT_SPEEDS`) que aproxima a forma detectada,
  aplicada via `trim+setpts+concat` no FFmpeg. As fronteiras entre
  segmentos tentam alinhar-se a um beat próximo da música
  (`config.SPEED_RAMP_BEAT_SNAP_WINDOW`) — o mecanismo de "sincronizar a
  mudança de velocidade com a música".
- **Fallback honesto:** se o teu clip não tiver footage suficiente a
  partir do `in_point` escolhido para cobrir a curva completa, o
  renderer cai silenciosamente para sem-ramp nesse plano (fica visível
  no log como `[Speed Ramp] ... a saltar`) em vez de falhar ou cortar o
  clip a meio.
- **Zoom não combina com Speed Ramp no mesmo clip nesta versão** —
  planos com uma curva real aplicada não recebem zoom Ken Burns (ver
  `renderer.py::normalize_clip`), para não sobrepor dois efeitos
  dinâmicos ao mesmo tempo.

**Zoom "subject-locked" (`config.USE_SUBJECT_LOCKED_ZOOM`):** nos planos
que continuam a ter zoom (os que não entraram no Speed Ramp), o centro
do Ken Burns pode seguir o sujeito detectado em vez de ficar sempre
fixo no meio do frame — amostra a posição do centróide da pessoa
(mesmo MediaPipe Image Segmenter do AI Subject Boost, precisa do mesmo
`config.SUBJECT_SEGMENTER_MODEL_PATH`, ver secção acima) em vários
pontos do plano e interpola linearmente entre eles (`src/zoom.py`). Se
o modelo não estiver disponível ou a deteção for inconsistente,
cai automaticamente para o zoom centrado normal.

**HONESTIDADE, sem rodeios — o que isto NÃO é:** nem a análise de
velocidade nem o zoom subject-locked são um "objeto que a IA entende e
segue com precisão". A análise de velocidade é um sinal indireto
(energia de movimento visual medida, não uma leitura de metadata de
edição — pode confundir movimento de conteúdo real com uma curva
editada). O zoom subject-locked segue o CENTRÓIDE de uma máscara de
segmentação por amostragem + interpolação linear, não um tracking de
objeto/identidade frame-a-frame — com mais de uma pessoa no frame, ou
entre amostras muito espaçadas, o ponto seguido pode saltar. Nenhum dos
dois foi confirmado com o modelo `.tflite` real nesta máquina de
desenvolvimento (o download continua bloqueado na rede — ver secção AI
Subject Boost); ambos foram validados end-to-end (sem crashar, com
fallback correto quando o modelo não está disponível) contra footage
real neste ambiente, mas a QUALIDADE do resultado com o modelo real só
pode ser confirmada na tua máquina.

## Simplificações assumidas nesta versão MVP (documentadas de propósito)

1. **Zoom out tratado igual a zoom in.** Zoom out com controlo fino não foi
   implementado — ver `renderer.py::normalize_clip`.
2. **Matching de conteúdo é semântico (CLIP) com fallback sequencial.**
   Por padrão (`config.USE_SEMANTIC_MATCHING = True`) cada plano da
   referência é comparado por embedding visual com os teus clips/fotos
   e recebe o mais parecido; se `torch`/`open_clip_torch` não estiverem
   instalados, cai automaticamente para o mapeamento sequencial (clip 1
   → slot 1, e por aí adiante). Continua a não ser "compreensão"
   semântica ao nível de um LLM multimodal — é similaridade visual.
3. **Classificação de transição é heurística (histograma de cor),
   não perfeita.** Vídeos com movimento de câmera rápido podem gerar
   falsos positivos de "fade".
4. **Cortes "cut" usam um xfade de ~2 frames**, não um corte 100% abrupto
   via concat demuxer — visualmente quase idêntico, mas simplifica o
   código ao unificar toda a lógica de transição num único mecanismo.
   Isto tem uma consequência real medida pelo Refinamento Iterativo: em
   planos muito curtos, esse xfade por vezes suaviza o corte o
   suficiente para a segunda análise não o detectar como um corte
   distinto — é exatamente o tipo de coisa que o refinamento tenta
   compensar encurtando `TRANSITION_DURATION`.
5. **Movimento de câmera (pan/rotação) é analisado mas ainda não
   reproduzido.** `src/camera_motion.py` estima pan/rotação/zoom por
   plano (feature matching 2D, não pose 3D — ver docstring do módulo) e
   isso entra na Edit DNA e no relatório de comparação, mas o renderer
   só sabe reproduzir zoom (Ken Burns, incluindo agora a versão
   subject-locked — ver secção acima) — pan e rotação da referência
   ainda não são aplicados aos teus clips.
6. **Refinamento Iterativo só ajusta parâmetros de render**, não a
   deteção de cortes/matching de conteúdo (ver secção "Edit DNA" acima
   para o porquê). E os ajustes são heurísticos: por vezes uma
   iteração piora o score — por isso o sistema guarda sempre o melhor
   resultado visto, nunca entrega algo pior que a primeira tentativa.
7. **Deteção de overlays (OCR) deteta, não reproduz.** Ver secção
   "Edit DNA" acima.
8. **"99.9% de fidelidade" não é um alvo alcançável nem bem definido
   aqui.** O teu conteúdo é sempre pixels diferentes dos da referência
   — o que se pode aproximar é a estrutura (timing/zoom/movimento/cor),
   não os pixels. O AI Analyzer (opcional, ver secção "Edit DNA")
   melhora o JULGAMENTO dessa estrutura (substitui heurísticas frágeis
   por um modelo de visão), não elimina a diferença de conteúdo.
9. **Speed Ramp e zoom subject-locked são aproximações por amostragem,
   não tracking/curvas contínuas verdadeiras.** Ver a secção dedicada
   acima para os detalhes — resumindo: a curva de velocidade é um sinal
   indireto (energia de movimento) com deteção "vale a pena ou não" por
   limiar, reconstruída em DEGRAUS (não uma curva suave); o zoom
   subject-locked segue o centróide de uma máscara de segmentação por
   amostras interpoladas, não uma identidade rastreada frame-a-frame.
   Nenhum dos dois é "o efeito segue precisamente o objeto/pessoa certo
   em qualquer footage" — é a aproximação mais honesta que dava para
   construir sem um modelo de tracking de objetos dedicado (ver
   "Próximos passos").
10. **Não existe transferência de efeito por-objeto entre vídeos
    diferentes.** Mesmo com Speed Ramp + zoom subject-locked, o sistema
    não identifica "este é o mesmo tipo de objeto/pessoa que na
    referência" para decidir COMO um efeito se comporta de forma
    específica a esse objeto — aplica a mesma curva/mesmo seguimento de
    centróide a qualquer plano de vídeo do utilizador correspondente,
    independentemente do que está a filmar. Um sistema que "entende" o
    conteúdo o suficiente para decidir isso por objeto é um problema de
    visão computacional bem mais amplo do que este MVP cobre — é a
    principal razão pela qual "seguir 100% a ideia" descrita não é algo
    que se possa marcar como verdadeiramente concluído (ver também o
    resumo no topo deste README).

## Próximos passos depois do MVP validado

- Reproduzir pan/rotação da referência no renderer (hoje só o zoom é
  reproduzido, incluindo a versão subject-locked), usando os dados que
  `camera_motion.py` já extrai — continua a ser a maior lacuna
  estrutural conhecida (item 5 acima).
- Tracking de objeto/pessoa verdadeiro (frame-a-frame, com identidade
  persistente) para o zoom subject-locked, em vez de amostragem +
  interpolação linear entre poucos pontos — reduziria o risco de o
  centro "saltar" entre pessoas diferentes ou perder o sujeito entre
  amostras.
- Curva de velocidade suave (spline) em vez de degraus (piecewise-constant)
  no Speed Ramp, e mais segmentos configuráveis por forma detectada.
- O passo mais ambicioso do pedido original — efeitos que "seguem o
  objeto certo" com comportamento específico por tipo de objeto, e se
  adaptam de forma inteligente quando aplicados a footage diferente da
  referência — precisaria de deteção+tracking de objetos genérico (não
  só "há uma pessoa ou não", como hoje) e uma camada de decisão que
  associe cada objeto detectado ao comportamento que teve na
  referência. Isto é um projeto de visão computacional à parte, bem
  maior que este MVP — os dois itens acima (tracking real, spline) são
  passos incrementais válidos nessa direção, não a mesma coisa.
- Alargar o Refinamento Iterativo para também poder reajustar a
  deteção de cortes (`SCENE_DETECT_THRESHOLD`) e o beat-snap, não só
  parâmetros de render — precisa de uma forma barata de re-mapear o
  conteúdo do utilizador sem repetir o CLIP a cada iteração.
- Deixar o AI Analyzer também sugerir ajustes de parâmetros em texto
  livre (hoje só julga match/no-match por plano; `suggest_adjustments`
  continua 100% determinista) — precisa de validação/clamps extra para
  não aplicar um valor não confiável vindo do modelo.
- Usar os overlays detectados (`overlay_detect.py`) como base de
  POSIÇÃO/ESTILO para legendas com texto novo fornecido pelo
  utilizador, em vez de só diagnóstico.
- Expor tudo isto como API (FastAPI) + fila de jobs, e construir a
  interface no Flutter
