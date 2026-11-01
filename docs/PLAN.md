# Plano de edição v1 e motores de render

A partir desta versão, a **inteligência** (análise da referência, matching, alinhamento às fontes)
entrega um **Plano** — e só o plano chega ao **motor** que o executa. O motor está atrás de uma
interface (`velocity/engine`), por isso pode ser trocado (compositor próprio hoje; MLT/GES se a
prova técnica o justificar) sem tocar na inteligência nem na plataforma.

```
referência + clips ──► inteligência ──► Plano v1 ──► validar (plano + limites do motor) ──► motor ──► vídeo
                                          │
                                          └─► guardado no projeto (editor, "voltar à versão da IA")
```

## Formato

- Código: `velocity/plan/schema.py` (pydantic). JSON Schema publicado: [`plan.schema.json`](plan.schema.json)
  (gerado com `python -m velocity.plan --schema docs/plan.schema.json`; um teste falha se ficar desatualizado).
- Exemplos reais, gerados pelo pipeline sobre o banco de fidelidade:
  [`examples/plano_cenario_A_reconstrucao.json`](examples/plano_cenario_A_reconstrucao.json) (rasto medido por
  frame) e [`examples/plano_cenario_B_estilo.json`](examples/plano_cenario_B_estilo.json) (edição por estilo).

| Parte | O que contém |
|---|---|
| `plan_version` | `"1.0"`. Um plano com outra versão MAIOR é recusado com mensagem clara. |
| `output` | largura, altura, fps, duração em frames, espaço de cor (`bt709`). |
| `sources` | ficheiros por id (`uri` = caminho local no worker ou chave de armazenamento). |
| `tracks[]` | pistas `video`, `audio`, `text`. Tempo sempre em **frames inteiros** à taxa de saída. |
| item de vídeo | `source`, `at`, `duration`; `time` (início + curva de velocidade, ou **frame da fonte por frame**); `transform` (zoom/centro/pan/flip, ou **afim por frame**); `color` (tom, sujeito/fundo, ou **matriz 3x4 medida**); `transition_in`; `provenance` (quem decidiu: `matcher`, `source_align`, `editor`…, com confiança); `measured` (frames medidos vs extrapolados). |
| `effects`, `fx_events` | efeitos medidos na referência (zoom punch, shake, rgb split, pulsos; flash/glitch dentro de um plano). |
| `look`, `zoom_gain`, `effects_scale` | correção global e escalas com significado. |
| `render_hints` | preferências que um motor pode ignorar (denoise, super-resolução). |
| `meta` | origem do plano (pipeline, cenário A/B, referência, versão do DNA; `migrated_from` nos projetos antigos). |

## Validação antes do render

1. `validate_plan(plan)` — coerência do plano, sem depender do motor: versão, tamanhos e fps, intervalos
   dentro do vídeo, fontes declaradas, listas por frame com o tamanho certo, transformações não
   degeneradas, curvas bem formadas, matriz de cor 3x4, transições coerentes com o início do item,
   limites de cor/look/escala. Efeitos ou transições que passam do fim são **avisos** (inofensivos).
2. `engine.validate(plan)` — o que o motor escolhido não executa (`unsupported`): mais pistas do que
   suporta, itens sobrepostos (cross-fade/PiP), buracos, tipos de transição/efeito desconhecidos.
3. `check_media(plan)` — no worker, depois de descarregar: ficheiros existem e os instantes pedidos
   cabem nas fontes.

Onde corre: no pipeline (antes do 1.º render), na API do editor (**antes de reservar créditos**) e no
worker (antes de renderizar uma edição). Linha de comandos: `python -m velocity.plan --check plano.json`.

## Motores

| Motor | Estado | Limites declarados (`Capabilities`) |
|---|---|---|
| `compositor` (próprio, `velocity/render`) | em uso | 1 pista de vídeo sequencial, sem sobreposição; 1 faixa de áudio; transições cut/flash/dip_black/blur/glitch; efeitos medidos; **tempo, afim e cor por frame** (único que executa a reconstrução do cenário A) |
| `mlt` | prova técnica pendente (ver auditoria, testes T1–T7) | — |

Escolha: `VELOCITY_ENGINE` (por defeito `compositor`).

## Compatibilidade e migração

- A coluna `projects.timeline` passa a guardar o Plano v1. Projetos antigos (Timeline sem
  `plan_version`) abrem sem migração em lote: `load_plan` converte-os ao ler (`meta.migrated_from`).
- A conversão Timeline ↔ Plano é exata (teste `test_conversao_timeline_plano_timeline_e_exata`), e o
  banco de fidelidade deu **resultados idênticos** antes e depois (6/6 casos).
- Deploys desfasados: o worker devolve `plan` **e** `timeline`; a API envia ao worker `plan` **e**
  `timeline`. Uma API ou worker de versão anterior continua a funcionar com o outro lado novo.
- Rollback: reverter os commits; os planos já guardados continuam legíveis pela versão anterior? **Não** —
  a versão anterior só lê Timelines. Antes de reverter em produção, converter de volta com
  `plan_to_timeline` (função disponível) ou repor a base de dados.

## Recursos por etapa

Cada geração mede, por etapa, o pico de RAM (processo + ffmpeg), a CPU média e o pico de VRAM do
PyTorch (`velocity/runtime_metrics.py`), guardados em `metrics.resources` do job. Exemplo medido
(referência de 6 s, perfil rápido): alinhamento às fontes 1,2 GB; análise de clips 1,5 GB; render 0,8–0,9 GB.
