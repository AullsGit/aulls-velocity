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
