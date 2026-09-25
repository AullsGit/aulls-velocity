# Velocity Edits — motor de edição por IA (Velocity Edit Engine)

Recebe um vídeo de referência no estilo "Velocity" (cortes rápidos, zooms, speed ramps,
transições ao ritmo da música) e recria essa **linguagem de edição** usando os vídeos do
utilizador. O objetivo é a edição, não o conteúdo: o sistema mede a referência (Edit DNA),
escolhe os melhores trechos do utilizador para cada papel na edição, e produz um vídeo
vertical de ~25-30s no mesmo estilo.

Estado do produto: **núcleo funcional, em validação com utilizadores reais.** Ver
`docs/STATUS.md` para o detalhe técnico completo (o que funciona, limitações conhecidas,
métricas medidas).

## Como usar (operador) — Velocity Studio

A forma mais simples de gerar vídeos é a interface web local:

```powershell
venv\Scripts\python.exe run_studio.py
```

Abre `http://127.0.0.1:8811` no browser (abre sozinho). Carrega a referência, os teus
vídeos e, opcionalmente, uma música; escolhe a qualidade (Rápido/Normal/HD); gera e
descarrega. Processa uma encomenda de cada vez. Os ficheiros ficam em `studio/data/`
(nunca saem do computador).

## Como usar (linha de comandos)

```powershell
venv\Scripts\python.exe testar.py --profile standard
```

Lê `teste/1_reference` (1 vídeo), `teste/2_inputs` (os teus vídeos) e `teste/3_music`
(opcional), escreve em `teste/4_output`. Ver `testar.py --help`-style docstring no topo do
ficheiro para todas as opções. Para uso avançado, `python -m velocity make --help`.

## Estrutura do projeto

```
velocity/           núcleo do produto
  reference/         análise da referência -> Edit DNA (cortes, transições, vibe, áudio, texto)
  media/             análise dos vídeos do utilizador (energia, sujeito, segmentação)
  matching/          escolhe o trecho certo para cada plano; extensão de duração (25-30s)
  render/            Edit DNA + matches -> vídeo final (compositor, efeitos, super-resolução)
  detector/          decide se uma referência é um Velocity suportado
  evaluate/          compara o resultado com a referência (fidelidade, vibe, efeitos)
  pipeline.py        orquestra tudo (usado pela CLI e pelo Studio)
  cli.py             `python -m velocity ...`
studio/              interface web local (FastAPI) — ver server.py e static/index.html
bench/               material de teste, scripts de avaliação, cache de decodificação
tests/               suite de testes automáticos (pytest)
legacy/              MVP original (Python solto, mantido só para consulta)
marketing/           página de vendas (landing.html) e textos de divulgação
```

## Testes

```powershell
venv\Scripts\python.exe -m pytest
```

## Documentação

- `docs/STATUS.md` — estado técnico detalhado, decisões de arquitetura, limitações conhecidas.
- `marketing/landing.html` — página de vendas (publicada como Artifact; edita e republica a
  partir daqui se mudares preços/contacto).
- `marketing/mensagens.md` — mensagens prontas para WhatsApp/Instagram e checklist de encomenda.
