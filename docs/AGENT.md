# Agente de edição

O utilizador conversa, envia uma referência e os seus vídeos, e o agente faz a edição: estuda a
referência, planeia, renderiza, verifica, corrige e apresenta. Depois recebe pedidos de alteração
na mesma conversa.

## Como está montado

```
App (PWA)  ──  /api/agent/*  ──  studio/agent  ──  modelo (API da Anthropic)
                                      │
                                      │ ferramentas
                                      ▼
                              velocity/agent/planops, report   (Python puro, na API)
                                      │
                                      │ jobs de computação (o mesmo mecanismo das gerações)
                                      ▼
                              worker/runner.py → velocity/agent/compute, sheets, builder, verify
                                                 velocity/engine → compositor (MLT quando preciso)
```

- **A API só orquestra.** O ciclo do agente corre na API (imagem leve, sem numpy/OpenCV/ffmpeg;
  `tests/test_agent_planops.py` prova que importa sem eles). Tudo o que toca em vídeo é um job, que
  em produção corre no worker GPU do RunPod.
- **O Plano v1 é o contrato.** O agente nunca fala com o motor: edita o plano com operações
  (`velocity/agent/planops.py`), em segundos e com ids de ficheiro. Fora de limites é limitado ou
  recusado com uma mensagem que o modelo consegue corrigir; um lote de operações aplica-se por inteiro ou
  não se aplica.
- **O modelo vê.** As ferramentas devolvem folhas de frames com o instante em cada um (Files API: o
  histórico guarda só o id). É assim que ele confirma cortes, lê a composição e compara o
  resultado com a referência.

## Ferramentas

| Ferramenta | O que faz |
|---|---|
| `analyze_reference` | Tabela por plano, efeitos, cor, música, energia + frames |
| `analyze_clips` | O que há em cada clip, quando mexe, cortes próprios + frames |
| `extract_captions` | Legenda por OCR: texto, tempos, posição, tamanho, cores |
| `view_frames` / `compare_frames` | Ver qualquer ficheiro; versão e referência lado a lado |
| `build_edit` | Primeiro plano: `style` (recria a edição) ou `cuts_only` (imagem intacta); aceita cortes corrigidos |
| `get_plan` / `update_plan` | Ler e editar o plano |
| `render` | `preview` grátis (540p), `standard` (720p) e `hd` (1080p) pagos; variante `no_music` |
| `verify_output` | Duração, resolução, pretos, imagem parada, material repetido, cortes, volume, cor, fidelidade |
| `present_version` | Mostra à pessoa; nada chega lá sem isto |
| `restore_version` | Volta ao plano de uma versão |
| `remember` / `forget` | Preferências duradouras da pessoa, para todos os projetos |
| `set_file_role` | Corrige o papel de um ficheiro |

## Garantias do ciclo (`studio/agent/loop.py`)

- O histórico só cresce e é reenviado como veio; prompt e ferramentas são fixos. O contexto de cada
  mensagem (ficheiros, versões, créditos, preferências) vai na própria mensagem do utilizador.
- Cada passo é guardado antes do seguinte. Se o servidor reiniciar, o turno continua
  (`AgentService.recover`) e os jobs não se repetem nem cobram duas vezes (idempotentes pelo id da chamada).
- Cada `tool_use` tem sempre `tool_result`, mesmo em erro ou cancelamento. Respostas cortadas ou
  recusadas nunca executam ferramentas.
- Entradas das ferramentas são validadas com pydantic (o streaming da API não as valida).

## Configuração

| Variável | Defeito | Para quê |
|---|---|---|
| `ANTHROPIC_API_KEY` | (nenhuma) | Sem ela o agente fica desligado e a app explica; o resto funciona |
| `AGENT_MODEL` | `claude-opus-5-5` | Modelo |
| `AGENT_EFFORT` | `high` | Quanto o modelo pensa (e custa) |
| `AGENT_MAX_ITERATIONS` | 40 | Chamadas ao modelo por mensagem |
| `AGENT_MAX_TURN_INPUT_TOKENS` | 400000 | Trava de custo por mensagem |
| `AGENT_FREE_TURNS_PER_DAY` | 6 | Mensagens por dia de quem ainda não tem créditos |
| `VELOCITY_RATE_LIMIT_AGENT` | 30 | Mensagens por hora, por pessoa |

Preço: a 1.ª versão final de um projeto custa uma geração; cada correção final custa
`credits_per_agent_revision` (1 por defeito, muda-se no admin). Pré-visualizações são grátis.

## Provar com o modelo verdadeiro

```bash
python bench/agent_live.py --ref R.mp4 --clips a.mp4 b.mp4 --ask "Recria esta edição" --ask "Mais dinâmica"
```

Escreve a conversa, os vídeos e o custo em `bench/out/agent_live/`.

## Medições

Deteção de trocas de cena na referência (`velocity/reference/soft_cuts.py`), acertos / detetadas / reais,
tolerância de 6 frames:

| Caso | Antes | Agora |
|---|---|---|
| Referência real com 16 trocas anotadas à mão (dissoluções e cortes em movimento) | 7 / 7 / 16 | 16 / 19 / 16 |
| r1 velocity (verdade conhecida) | 14 / 14 / 14 | 14 / 14 / 14 |
| r2 cinematic | 1 / 3 / 5 | 1 / 5 / 5 |
| r3 social | 8 / 8 / 8 | 8 / 8 / 8 |
| r4 BMW real, cortes no mesmo cenário | 3 / 3 / 11 | 3 / 3 / 11 |
| r5 futebol real | 7 / 7 / 8 | 7 / 7 / 8 |

Continua fraco em cortes dentro do mesmo cenário (r4) e em dissoluções lentas (r2). É por isso que o agente
tem `compare_frames`/`view_frames` e pode corrigir a lista de cortes em `build_edit(cuts_s=...)`.

## Limites conhecidos

- O motor ainda não faz: máscaras, picture-in-picture, tracking, rotação paramétrica, interpolação de
  frames em câmara lenta (mistura frames ou repete-os). O agente deve dizê-lo em vez de fingir.
- O modelo não ouve: conhece a música por medidas (tempo, batidas, energia) e pela legenda lida por OCR.
- Sem GPU a análise e o render são lentos; o agente espera, mas a pessoa vê o progresso.
