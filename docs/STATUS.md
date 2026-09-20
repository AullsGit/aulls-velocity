# Estado do projeto (2026-09-20)

Pacote novo: `velocity/`. MVP original preservado em `legacy/` (só consulta).

## Pipeline atual
`python -m velocity make --ref R.mp4 --clips a.mp4 b.mp4 [--music m.mp3] --out out.mp4`
1. `reference/` : 1 decode (30 fps CFR) -> letterbox -> sinais por frame -> segmentador de transições
   tipadas (cut/flash/dip_black/blur/glitch) -> features por plano -> áudio/beats -> **EditDNA v1** (`schemas/edit_dna.py`).
2. `detector/`  : Velocity Detector v0 (regras interpretáveis sobre a assinatura do DNA).
3. `media/`     : ClipProfile por vídeo do utilizador (energia, sujeito por subtração de fundo, frames instáveis).
4. `matching/`  : custo por (plano, janela): ação (rank), forma temporal, brilho, sujeito, reutilização; atribuição gulosa.
   `plan.py` -> Timeline (retime às batidas da música se a referência estiver presa ao beat; reenquadramento por punch-in).
5. `render/`    : compositor frame a frame (efeitos sintetizados) + UMA codificação FFmpeg. Duração exata, sem drift.
6. `evaluate/`  : DNA(ref) vs DNA(output re-analisado) + verificação de execução contra o plano.

## Testes
`pytest` (6 testes; 2 precisam de bench/data). Round-trip: transições com posição/tipo conhecidos são recuperadas 4/4.

## Limitações conhecidas (ordenadas por impacto)
1. Sem dataset anotado nem negativos reais: o Velocity Detector NÃO está calibrado (6/7 positivos; ref_02 é FN).
2. Vocabulário de transições sem `dissolve`/fade (dissolves passam invisíveis).
3. Cortes secos entre planos de conteúdo parecido não são detetáveis pelo medidor (subestima a fidelidade).
4. Matching guloso; sem embeddings semânticos (CLIP) ainda; sem direção de movimento; localizador de sujeito só p/ câmara fixa.
5. Speed ramp: curva indireta (energia de movimento), sem interpolação de frames real (só mistura).
6. Referências reais têm 10-19 s; alvo do produto é 25-30 s (ainda não há estratégia de extensão).
7. Áudio da referência NUNCA entra no output (só como "música de teste" manual via --music).
8. Torch instalado é CPU-only.


## Atualização — sistema de efeitos e look (2026-09-20, sessão 2)
Novo: `EffectTrack` + `LookProfile` no DNA v1.1; `render/fxstack.py` (zoom_punch, shake, rgb_split, pulsos);
`matching/fx_plan.py` (regra da evidência); `evaluate/effects_fidelity.py` (protetor); ajuste de tom por plano;
ciclo fechado de look no `cli make` (`--fx-iters`, `--tone`, `--no-fx`, `--subject-enhance` EXPERIMENTAL).
Validação: `bench/look_eval.py` (suave/agressivo vs etiquetas do utilizador: 16/18, otimista), `tests/test_effects_roundtrip.py`.

Achados: (1) zoom medido por fluxo óptico é ~50% subestimado em zonas sem textura -> comparar sempre no domínio da
medição; (2) igualar só a luma média cria silhuetas quando o sujeito está em contraluz; (3) realce do sujeito com máscara
de subtração de fundo cria halos -> desligado por defeito; (4) energia de movimento vem do conteúdo: clips calmos não
chegam à agressividade da referência sem "inventar" efeitos (decisão do utilizador).
Pendente: segmentador de pessoas em GPU (torch CUDA a instalar), depois relight/denoise/super-resolução com A/B.

## Atualização — realce do sujeito e nitidez (2026-09-20, sessão 2, fim)
Segmentador de pessoas DeepLabV3 em GPU com cache (`media/segment.py`); `--subject-enhance` (opt-in) levanta a luz do sujeito e
escurece só o fundo (máscara erodida). A/B em 4 refs: brilho mais perto da referência em todas, fidelidade ±0.03, ganho visual modesto,
resta contorno claro ténue (próximo: refinar a máscara com filtro guiado). Nitidez guiada pela referência no ciclo fechado
(`post_sharp`, só nos contornos, só se o output for mais mole): 0.40->0.51 e 0.41->0.59, sem ruído amplificado a 200%.
Bloqueio de hardware: GPU limitada a 10 W (limite 60 W) pelo modo do portátil (NitroSense); segmentação ~550 ms/frame até resolver.
