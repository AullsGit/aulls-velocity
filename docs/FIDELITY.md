# Fidelidade à referência — diagnóstico, medições e correções

Missão: reproduzir uma edição de referência com a maior exatidão possível. Dois cenários:

- **A — mesmo material**: o utilizador tem os vídeos originais usados na referência → reconstruir
  a edição original (que trecho, que instante, que enquadramento, que cor, que transição).
- **B — material diferente**: preservar a estrutura e o estilo (cortes, ritmo, movimento,
  transições), adaptando ao conteúdo novo.

Tudo o que está aqui foi medido com o banco descrito abaixo. Nenhum número é estimado.

## 1. Como se mede (banco com verdade conhecida)

`velocity/evaluate/synth.py` gera referências sintéticas em que **cada frame é conhecido**: de que
fonte veio, frame da fonte, zoom/centro (com easing), velocidade (0.5×, 1.5×, 2×), cor por plano,
grade global e transição (corte, flash, dip-to-black, zoom-blur, cross-fade). O gerador é
independente do renderer do motor (transformações e transições com fórmulas próprias).

`velocity/evaluate/fidelity_bench.py` mede SEPARADAMENTE:

| Nível | Métricas |
|---|---|
| Analisador vs verdade | precisão/recall/F1 dos cortes, erro em ms, tipo de transição, zoom/pan por plano (no mesmo intervalo de frames que o analisador usa), BPM |
| Reprodução — estrutura (A e B) | geometria (tamanho, aspeto, fps), duração, F1 dos cortes planeados (total e na janela da referência), tipos de transição, movimento observado, alinhamento à batida, áudio |
| Reprodução — visual (só A) | SSIM e PSNR por frame aos instantes da referência, ΔE Lab, % de planos com a fonte certa e com o instante certo (±2 frames), erro de velocidade |
| Linhas de base | **SSIM "ao acaso"** (a referência contra si própria desfasada meio vídeo: mesmo tipo de imagem, conteúdo errado) e **teto do codec** (a própria referência recodificada com as definições do perfil — o máximo que qualquer reprodução perfeita consegue) |

Casos: **r1** Velocity 9:16 30 fps 120 BPM (punches, zoom lento, 2×/0.5×/1.5×, pans, flash,
zoom-blur, dip, cor por plano) · **r2** cinematográfico 16:9 **24 fps** com fontes a 30 fps
(zooms com easing, pans, 2 cross-fades, dips, grade quente) · **r3** social 1:1 de fontes 9:16,
100 BPM (tilt, speed ramps, flashes, plano quase P&B).

```
venv\Scripts\python.exe -m velocity.evaluate.fidelity_bench --tag minha-medicao
```
Resultados em `bench/out/fidelity/<tag>/` (JSON por caso + `summary.md`).

## 2. Diagnóstico — as 5 causas de imprecisão (medidas antes de qualquer correção)

Linha de base (`--tag baseline`), perfil rápido, motor tal como estava:

| | r1 A | r2 A | r3 A |
|---|---|---|---|
| SSIM (acaso) | 0.578 (0.536) | 0.576 (0.538) | 0.613 (0.566) |
| PSNR | 11.0 dB | 10.0 dB | 10.5 dB |
| ΔE Lab | 26.6 | 32.2 | 18.8 |
| fonte certa / instante certo | 13% / 0% | 33% / 17% | 44% / 0% |
| duração × referência | 3.0 | 2.0 | 5.0 |
| F1 dos cortes (vídeo inteiro) | 0.48 | 0.50 | 0.31 |
| fidelidade **auto-reportada** pelo motor | 0.966 | 0.952 | 0.978 |

1. **Não existia reconstrução no cenário A** (maior impacto). O motor nunca compara píxeis com as
   fontes: escolhe trechos por energia/papel. Mesmo com o material exato, o SSIM ficava a 0.04 do
   acaso, com 0% de instantes certos.
2. **Formato de saída não é o da referência.** Duração esticada para 25–31 s repetindo o padrão
   (×2 a ×5), saída sempre 9:16 (uma referência 16:9 ou 1:1 era recortada), sempre 30 fps (a 24 fps
   da r2 perdia-se). Por isso o F1 dos cortes no vídeo inteiro cai para 0.31–0.50.
3. **Movimento de câmara sub-reproduzido.** O analisador mede só 55–80% do zoom real; o render
   reproduz 25–64% (zoom observado / verdadeiro); pans entre 0.29× e 1.55× do real (o plano
   divide o pan por 2 e corta-o a ±12%).
4. **Velocidade absoluta não é medida nem reproduzida.** O DNA só tem a curva RELATIVA dentro do
   plano; 2×, 1.5× e 0.5× saíam a ~1× (erro médio 0.09–0.22).
5. **Vocabulário de transições e cor incompletos.** Cross-fade não existe (nem no analisador — a r2
   perde 2 de 5 cortes, recall 0.6 — nem no renderer, que não sobrepõe dois planos). O look só tem
   luma/contraste/saturação/nitidez: sem balanço de cor (r2: b\* +5.8 na referência → −16.9 na saída).

Agravante transversal: **a auto-avaliação do motor é enganadora** (0.95–0.98 com SSIM ao nível do
acaso), porque compara o DNA da referência com o DNA do resultado medidos pelo mesmo analisador —
os erros do analisador cancelam-se dos dois lados.

### Sequência de implementação proposta
1. Cenário A: alinhamento às fontes + reconstrução exata, no formato da referência. **(feito)**
2. Formato da referência também no cenário B (aspeto, fps, duração; extensão a 25–30 s passa a opção).
3. Movimento no cenário B: curvas densas do analisador, sem o fator 0.5/±12% no pan, calibração do
   zoom subestimado.
4. Velocidade absoluta: estimar a partir do movimento da própria fonte e reproduzir com remapeamento
   de tempo.
5. Cross-fade (deteção + composição A/B no renderer) e transferência de cor (matriz 3x4/LUT) no B.

## 3. Correção nº 1 — reconstrução do cenário A (implementada)

`velocity/matching/source_align.py` (novo), `velocity/matching/reconstruct.py` (novo),
caminho "exato" no renderer, integração automática no pipeline (`MakeParams.reconstruct="auto"`).

Para cada plano da referência:
1. **Sonda barata** (índice SIFT a 1.5 keyframes/s, 3 planos mais longos): se nada corresponde, é o
   cenário B e o motor segue como antes (custo medido: ~2.5 s).
2. **Recuperação**: SIFT + votação + verificação RANSAC; vários keyframes candidatos por amostra.
3. **Instante exato** de 3 amostras por plano: registo afim por fluxo óptico (Lucas-Kanade +
   RANSAC) a cada frame candidato; escolha **conjunta e temporalmente coerente** (resíduo local +
   correlação, velocidade positiva, tolerância de ±1 frame à quantização da câmara lenta) com
   propagação de hipóteses entre amostras.
4. **Seguimento frame a frame** (para a frente e para trás): frame da fonte e transformação afim
   MEDIDOS em cada frame → speed ramps, freezes, zoom/pan/shake com a curva real. Frames sob flash
   e dip são medidos (o registo normaliza a intensidade); sob blur/glitch a geometria é interpolada.
5. **Cor**: matriz afim RGB 3×4 por plano (brilho, contraste, saturação e balanço).
6. **Cortes**: afinados à taxa nativa (24/25/30/60 fps); cortes suaves decididos pelo conteúdo.
7. **Reanálise de segmento**: se o seguimento perde o conteúdo numa ponta do plano (corte que o
   analisador não viu — p.ex. cross-fade), essa parte é alinhada como plano próprio.
8. Se ≥ 50% da duração alinhar com confiança ≥ 0.8, a saída tem a **duração, aspeto e fps da
   referência** e os planos não alinhados usam o matching normal (cenário B) — registado nas
   decisões.

A timeline guarda o rasto medido (`ClipUse.exact`: frame da fonte + afim + cor por frame); o editor
preserva-o em cortes e divisões e volta ao modo paramétrico se o utilizador mudar velocidade,
zoom, enquadramento ou ficheiro. A app mostra o resultado no formato real (16:9, 1:1…).

### Antes / depois (cenário A)

| | r1 antes → depois | r2 antes → depois | r3 antes → depois |
|---|---|---|---|
| SSIM | 0.578 → **0.947** | 0.576 → **0.957** | 0.613 → **0.953** |
| teto do codec (SSIM) | 0.973 | 0.976 | 0.968 |
| PSNR | 11.0 → **35.7 dB** | 10.0 → **36.2 dB** | 10.5 → **37.5 dB** |
| ΔE Lab | 26.6 → **1.95** | 32.2 → **1.66** | 18.8 → **0.85** |
| fonte certa / instante certo | 13%/0% → **100%/100%** | 33%/17% → **100%/100%** | 44%/0% → **100%/100%** |
| erro de velocidade | 0.137 → **0.001** | 0.090 → **0.000** | 0.222 → **0.001** |
| duração × ref. | 3.0 → **1.0** | 2.0 → **1.0** | 5.0 → **1.0** |
| geometria | 9:16@30 → 9:16@30 | 9:16@30 → **16:9@24** | 9:16@30 → **1:1@30** |
| F1 cortes (vídeo inteiro) | 0.48 → **1.00** (erro 0 ms) | 0.50 → **0.80** | 0.31 → **1.00** (erro 0 ms) |
| tempo total de geração | 31.6 → 37.6 s | 25.9 → 24.1 s | 33.8 → 22.7 s |

Precisão do alinhamento contra a verdade (frame a frame): erro mediano do instante da fonte
**0 frames** em todos os planos dos 3 casos (máximo 1; 2 num plano dentro de um cross-fade),
geometria com erro mediano 0.02–0.2 px (frames extrapolados dentro de zoom-blur: até ~20 px),
velocidades medidas 2.00 / 1.50 / 0.49–0.50 para verdades 2 / 1.5 / 0.5.

Cenário B: **sem alteração de qualidade** (os números B da linha de base mantêm-se), custo extra
medido de ~3–5 s por geração (sonda).

Testes: `tests/test_reconstruct.py` (6, regressão do alinhamento, do render exato, do cenário B
recusado e do editor); suite completa 55/55. Verificado na app real (servidor dev): referência
16:9 a 24 fps + clips originais → vídeo 960×540, 15.5 s, resultado e editor mostrados em 16:9;
corte de um plano reconstruído e re-exportação OK.

## 4. O que falhou / limitações honestas

- **O que sobra até ao teto (0.947 vs 0.973 na r1)** vem de: transições reaplicadas com a
  implementação do motor (não idênticas às da referência), dupla reamostragem (fonte comprimida →
  afim) e frames extrapolados em zoom-blur.
- **Cross-fades** (r2): os cortes são encontrados pelo conteúdo, mas ficam a 1–2 frames do meio da
  dissolução e são renderizados como **corte seco** — o renderer ainda não sobrepõe dois planos
  (causa nº 5). Por isso o F1 da r2 é 0.80 e não 1.0.
- **A auto-avaliação do motor continua enganadora**: na r2 desceu para 0.84 apesar de a fidelidade
  real ter subido de 0.58 para 0.96 (o analisador vê os cortes secos onde a referência tinha
  cross-fades que ele próprio não detetou). Deve passar a reportar métricas de pixel no cenário A.
- Espelhamento (flip) da fonte não é detetado; rotações grandes não foram testadas.
- Custo: o alinhamento leva 9–25 s para referências de 6–15 s (dominado pelo registo fino em
  Python+OpenCV). Aceitável, mas há margem (pirâmide de resolução, menos candidatos).
- O banco é sintético: valida a exatidão com verdade conhecida, mas vídeo real (compressão de redes
  sociais, letterbox, texto sobreposto, VFR) ainda precisa de casos reais com anotação.
- No cenário B nada mudou: as causas 2–5 continuam por resolver (próximas correções).

## 5. Atualização (1 nov 2026) — precisão ao frame e filmagem real

### Precisão ao frame do motor (prova T1/T2)
`velocity/evaluate/frame_probe.py` desenha o número do frame em cada imagem da fonte;
`engine_t1.py` renderiza planos conhecidos por QUALQUER motor (via `velocity/engine`) e lê de volta
que frame aparece em cada frame de saída. Regra: "hold" (o frame que está no ecrã nesse instante).

| caso | antes | depois |
|---|---|---|
| cortes à mesma taxa | 100% | 100% |
| velocidade 2× | **0%** (sempre 1 frame adiantado) | 100% |
| tempo por frame (reconstrução) | 100% | 100% |
| fonte 24 fps → saída 30 fps | 80% | 100% |
| fonte 30 fps → saída 24 fps | 100% | 100% |
| fonte VFR (telemóvel) | 100% | 100% |

Causas corrigidas no compositor: `-ss` arredondado a 4 casas (metade dos planos começava 1 frame
tarde) e o arredondamento do filtro `fps`, que seguia regras diferentes em cada sentido.
Teste de regressão: `tests/test_frame_accuracy.py`.

### Filmagem real com edição conhecida
`synth.FileSource`: a mesma edição com verdade exata, mas sobre clips REAIS (compressão de redes
sociais, ruído, fps de telemóvel ~26,08 VFR, movimento de câmara verdadeiro). Os caminhos ficam
em `bench/real/sources.json` (local, fora do Git: material de terceiros).

| caso (cenário A) | SSIM (teto) | fonte certa | cortes encontrados |
|---|---|---|---|
| r4 — 3 clips da mesma cena (jump cuts), 26 fps VFR → 9:16 30 fps | 0,905 (0,988) | 50% | 3 de 11 |
| r5 — futebol 30 fps → 1:1 | 0,868 (0,986) | 78% | 8 de 8 (2 desviados) |

O que isto mostra (e o banco sintético não mostrava):
- **Jump cuts** (cortes entre planos da mesma cena, câmara fixa): o analisador não os vê (recall
  0,27) porque deteta cortes por mudança de cor/estrutura; até os flashes passam a "efeito dentro
  do plano". No cenário A, o alinhamento vê a descontinuidade da fonte, mas a reanálise de segmento
  só divide uma vez por ponta — é preciso dividir de forma iterativa.
- Planos muito curtos a 2× e planos quase P&B (r5) ainda falham a recuperação.
- A validação do plano apanhou, antes de renderizar, uma velocidade aproximada inválida (reverse
  medido) — corrigido na reconstrução (a aproximação editável fica nos limites; o rasto medido manda).
