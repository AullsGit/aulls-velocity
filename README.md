# AwlsVelocity — AI reference video editor

**Give AI a reference. It creates your edit.** O utilizador envia os próprios vídeos e um
vídeo de referência; a IA mede a linguagem de edição da referência (cortes, ritmo, zooms,
transições, efeitos, batida) e cria um vídeo vertical novo nesse estilo com o conteúdo do
utilizador. Depois, um editor leve permite corrigir o resultado sem gerar tudo de novo.

- **Web/PWA** (instalável em Android e iPhone), interface mobile-first com versão desktop.
- **Créditos** pagos (PayPal; e-Mola em Moçambique; códigos promo), preço configurável no Admin.
- **Processamento assíncrono** em worker GPU sob demanda (RunPod, escala a zero) ou local.

Deploy: ver **[docs/DEPLOY.md](docs/DEPLOY.md)**. Estado técnico: [docs/STATUS.md](docs/STATUS.md).

## Correr localmente

```powershell
venv\Scripts\python.exe run_studio.py
```
Abre `http://127.0.0.1:8811`. Sem Supabase configurado, contas/projetos ficam em `studio/data/`
e os vídeos são gerados neste PC (cada conta nova recebe 50 créditos de teste).

## Estrutura

```
velocity/          motor de edição por IA (análise da referência, matching, render, avaliação)
  edit/            editor pós-geração: aplica as edições do utilizador à timeline da IA
  pipeline.py      geração completa (usada pelo worker e pela CLI)
worker/            worker de processamento (runner.py; handler.py + Dockerfile para o RunPod)
studio/            API FastAPI + app web
  api.py           rotas /api (contas, projetos, uploads assinados, jobs, créditos, pagamentos, admin)
  jobs.py          ciclo de vida dos jobs (créditos reservados antes, reembolso automático)
  store/           dados: Supabase (produção) ou memória (dev/testes), mesmo contrato
  compute/         fornecedores de computação: local ou RunPod (trocável)
  payments/        PayPal (e e-Mola manual)
  static/          app (app.html + js/), landing page, PWA (manifest, service worker, ícones)
supabase-schema.sql  esquema completo e idempotente da base de dados
tests/             pytest (motor, editor, API, ponta-a-ponta) + tests/sql (migração em Postgres real)
```

## Testes

```powershell
venv\Scripts\python.exe -m pytest
cd tests\sql; npm install; npm test
```

## Linha de comandos (motor)

```powershell
venv\Scripts\python.exe testar.py --profile standard
venv\Scripts\python.exe -m velocity make --help
```
