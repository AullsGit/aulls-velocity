# Fase 1 — prova técnica de motores (medida)

Pergunta: que motor deve executar os planos do AullsVelocity? Resposta curta, com os números
abaixo: **o compositor próprio continua a ser o principal** (o mais exato e o único com os efeitos
medidos da referência) e **o MLT entra como segundo motor**, escolhido automaticamente quando o
plano pede o que só ele faz (dissoluções entre itens sobrepostos; várias pistas). GES e
libopenshot ficaram para trás nesta prova.

## Como se mediu

- Contentor `aulls-engines` (`bench/engines/Dockerfile`, Debian trixie, pacotes oficiais):
  **MLT 7.30.0**, **GStreamer/GES 1.26.2**, **libopenshot 0.3.3**, FFmpeg 7.1.5. Docker Desktop 4.97
  (WSL2) nesta máquina (Ryzen 7 7735HS, 15 GB RAM). Sem GPU dentro do contentor.
- Fontes com o **número do frame desenhado** (`velocity/evaluate/frame_probe.py`): cada frame de
  saída diz exatamente que frame da fonte mostra. Imagens esperadas calculadas das fontes perfeitas.
- A mesma especificação de teste é traduzida para cada motor (`bench/engines/poc_runner.py`,
  `velocity/evaluate/engine_poc.py`). Regra de referência: "hold" (o frame que está no ecrã no
  instante pedido) — a mesma do alinhamento e do gerador de verdade.
- Reproduzir: `docker build -t aulls-engines bench/engines` e
  `python -m velocity.evaluate.engine_poc --engines compositor,mlt,mlt_strict,ges,openshot --perf`.

## Resultados (1 nov 2026)

| prova | compositor | MLT (nativo) | MLT estrito | GES | libopenshot |
|---|---|---|---|---|---|
| T1 cortes e trims | 100% | 100% | 100% | 98% | 58% |
| T1 fonte 24 → saída 30 fps | 100% | 60% | 100% | 60% | 40% |
| T1 fonte 30 → saída 24 fps | 100% | 50% | 100% | 75% | 100% |
| T1 fonte VFR (telemóvel) | 100% | 77% | 77% → **100%** no adaptador | 100% | 100% |
| T2 velocidade 2× | 100% | 100% | 100% | 3% | 100% |
| T2 velocidade 0,5× (hold) | 100% | 50% | 100% | 0% | 50% |
| T2 mapa de tempo por frame (rampa, congelamento, reverse) | 100% | 100% | 100% | 15% | 100% |
| T3 zoom por keyframes (PSNR vs modelo) | 35,5 dB | 34,6 dB | 33,2 dB | 12,1 dB | 11,9 dB |
| T5 dissolução 16 frames | **não suporta** (recusado antes do render) | alfa linear 0→0,94, 37,7 dB | igual | 22,3 dB | falhou (alfa ~1) |
| T6 30 s a 1080×1920, 10 cortes | 36,0 s | 31,5 s | 32,2 s | 36,7 s | 31,3 s (mpeg4) |
| T4 plano de reconstrução real (cenário A, r1) — SSIM vs referência | **0,985** | — | **0,955** | não suporta (sem mapa por frame) | não testado |

"Exato" = % de frames de saída com exatamente o frame da fonte esperado.

**MLT estrito** = o adaptador decide o tempo (regra hold) e pede ao MLT, pelo link `timeremap`, o
instante do MEIO do frame escolhido. Medições que levaram a este desenho:
- o `timeremap` mostra o frame que está no ecrã no instante pedido (hold); pedir o instante exato
  do início do frame falha por arredondamento (60%), o meio do frame é robusto (100%);
- os keyframes de um filtro ligado a um produtor contam a partir do frame ABSOLUTO da fonte (sem
  esse desvio o zoom saía ao contrário: 11,6 dB → 34,6 dB depois de corrigido);
- em VFR, num instante sem frame o MLT mostra o frame SEGUINTE; o adaptador lê os instantes reais
  da fonte (ffprobe) e pede sempre um frame que existe → 100%.

**T4** (o teste decisivo para o cenário A): o plano medido pelo alinhamento (frame da fonte,
retângulo e matriz de cor por frame) executado pelo MLT (`timeremap` + `affine` + `colorchannelmixer`
/`lutrgb`) dá SSIM 0,955 contra 0,985 do compositor, nos 205 frames fora de transições. Viável,
com perda de 0,03 ainda não explicada (candidatos: interpolação do `affine`, conversões YUV↔RGB
dos filtros de cor). O termo de rotação medido era desprezável (0,001).

## Limitações honestas

- **GES e libopenshot podem estar penalizados pela MINHA integração**, não só pelos motores: no
  GES não consegui pôr a funcionar os time effects (`videorate`) nem os keyframes de posição no
  tempo da prova; o libopenshot 0.3.3 do Debian (a versão upstream é a 1.1.0) só gravou vídeo
  com `mpeg4` (com `libx264` o ficheiro saía vazio; com áudio, SIGFPE).
- MLT testado na versão do Debian (7.30); a upstream vai na 7.43.
- Sem GPU no contentor: T6 é só CPU. NVENC/NVDEC dentro do Docker não foram testados.
- O adaptador MLT ainda não traduz: transições flash/dip/blur/glitch e efeitos próprios (só o
  compositor os tem), texto, look global, flip, rotação e fontes com aspeto diferente da saída —
  tudo isso é **recusado antes do render** (`MltEngine.validate`), nunca simulado.

## Decisão aplicada

- `velocity/engine/mlt.py`: adaptador MLT real atrás da mesma interface (`RenderEngine`).
  Passa as 6 provas de precisão ao frame (`tests/test_engine_mlt.py`, 7 testes; saltados onde
  não há melt, como no CI).
- `choose_engine(plan)`: o primeiro motor disponível cujos limites aceitam o plano (preferência:
  `VELOCITY_ENGINE`, depois compositor, depois MLT). Hoje os planos da IA nunca têm sobreposições,
  por isso continua tudo no compositor; quando a análise passar a detetar cross-fades (Fase 3/4),
  esses planos vão para o MLT automaticamente.
- Para o MLT correr em produção falta acrescentar o pacote `melt` à imagem do worker
  (`worker/Dockerfile`) e publicar uma nova versão no RunPod.
