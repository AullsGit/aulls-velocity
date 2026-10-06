# Deploy do AwlsVelocity (Render + Supabase + RunPod)

```
Browser / app (PWA)
   │  upload DIRETO por URL assinada ───────────────►  Supabase Storage (vídeos)
   ▼                                                          ▲
Render — API (contas, projetos, jobs, créditos, pagamentos)    │ worker lê/escreve
   │  cria o job e responde logo com job_id                    │ por URLs assinadas
   ▼                                                          │
RunPod Serverless — worker GPU (worker/) ─────────────────────┘
   escala a zero quando não há pedidos; cobra por segundo de uso
```

A ordem importa. Cada passo diz se é teu ou se já está feito.

---

## Passo 1 — Base de dados (Supabase) — VOCÊ PRECISA FAZER ISTO, ANTES do deploy

1. Supabase → **SQL Editor** → New query → cola **todo** o ficheiro `supabase-schema.sql` → **Run**.
   - É seguro correr por cima da base atual: só acrescenta tabelas/colunas e substitui funções;
     não apaga dados (testado num Postgres real, incluindo a atualização a partir da versão que
     tens em produção — ver `tests/sql/`).
   - Também fecha um buraco de segurança da versão anterior (funções de créditos executáveis
     por qualquer pessoa com a chave pública).
2. Supabase → **Authentication → URL Configuration**:
   - *Site URL*: `https://aulls-velocity-studio.onrender.com`
   - *Redirect URLs*: acrescenta `https://aulls-velocity-studio.onrender.com/auth/callback`

> Se fizeres o deploy do código novo sem este passo, a API entra em "manutenção" (mostra uma
> mensagem amigável) até a base estar atualizada — não parte, mas não funciona.

## Passo 2 — Publicar o código novo no Render — VOCÊ PRECISA FAZER ISTO (1 clique)

O código está no branch `app-v2` do GitHub (não no `main`, para não mexer em produção antes do
passo 1). Depois do passo 1:

1. GitHub → repositório `aulls-velocity` → **Pull requests** → o PR de `app-v2` → **Merge**.
2. O Render faz o deploy sozinho (o serviço segue o `main`).
3. Abre `https://aulls-velocity-studio.onrender.com/health` → deve dizer `"status": "ok"`.

Até aqui o vídeo continua a ser gerado na CPU do Render (`COMPUTE_PROVIDER=local`), como hoje.

## Passo 3 — GPU sob demanda no RunPod — VOCÊ PRECISA FAZER ISTO

1. Cria conta em https://www.runpod.io e adiciona saldo (pré-pago, cobra por segundo).
2. **Settings → Connections → GitHub**: liga a tua conta GitHub.
3. **Serverless → New Endpoint → Import Git Repository** → `aulls-velocity`:
   - Branch: `main` · **Dockerfile Path: `worker/Dockerfile`**
4. Configuração do endpoint (escala a zero, nada ligado 24/7):
   - **Active workers: 0** (sem custo quando ninguém usa)
   - **Max workers: 1** (sobe mais tarde, quando houver fila)
   - **Idle timeout: 10 s** (mantém o worker quente uns segundos entre a análise e a geração)
   - **Execution timeout: 900 s**
   - **FlashBoot: ligado** (arranques a frio mais rápidos)
   - GPU: escolhe uma das mais baratas disponíveis. Ver "Custos" abaixo — o que mais pesa no
     tempo é a CPU da máquina, não o modelo da GPU.
5. Depois de criado, copia o **Endpoint ID**; em **Settings → API Keys** cria uma chave.
6. O primeiro build demora (~10-20 min: instala PyTorch com CUDA e os modelos de OCR).

Atualizar o worker no futuro: no GitHub, cria uma *Release* nova — o RunPod reconstrói a
partir dela (não reconstrói sozinho a cada push).

## Passo 4 — Ligar o Render ao RunPod — VOCÊ PRECISA FAZER ISTO

Render → serviço `aulls-velocity-studio` → **Environment**:

| Variável | Valor |
|---|---|
| `COMPUTE_PROVIDER` | `runpod` |
| `RUNPOD_API_KEY` | a chave do passo 3 |
| `RUNPOD_ENDPOINT_ID` | o ID do passo 3 |
| `PUBLIC_BASE_URL` | `https://aulls-velocity-studio.onrender.com` (já vem do blueprint) |

Depois, em **Settings → Dockerfile Path**, muda para `./Dockerfile.api` — imagem leve, sem
PyTorch/ffmpeg: o Render passa a só orquestrar, cabe nos 512 MB e o deploy demora segundos.

Teste: cria um vídeo. No painel **Admin** → Jobs, a coluna *Provider* deve dizer `runpod`.

## Passo 5 — Custo por vídeo no painel Admin — VOCÊ PRECISA FAZER ISTO

Admin → Pricing → **Compute cost per second (USD)**: põe o preço por segundo da GPU que
escolheste (aparece no RunPod ao criar o endpoint). A partir daí o Admin mostra custo por vídeo,
custo total e lucro estimado, com base no tempo real de cada job.

## Passo 6 — Pagamentos com cartão/PayPal

Já configurado no código (ver secção PayPal em baixo, inalterada). Sem as 4 variáveis do PayPal,
a app mostra apenas e-Mola e códigos promocionais — nunca um botão de pagamento falso.

## Passo 7 (opcional) — Login com Google

1. Google Cloud Console → credenciais OAuth (tipo *Web*), com o redirect que o Supabase indica.
2. Supabase → Authentication → Providers → **Google** → ativar e colar o client ID/secret.
3. Render → `GOOGLE_AUTH_ENABLED=1`. Só então o botão "Continuar com Google" aparece.

---

## Custos — o que medi (honesto)

- **Não é a GPU que torna o vídeo rápido.** Medi: o render é ~75% descodificação (ffmpeg) e o
  resto composição OpenCV, ambos em CPU. Descodificar na GPU (NVDEC) foi **mais lento** (107 s vs
  75 s). A GPU só acelera a segmentação de pessoas e o OCR. O que o RunPod dá de verdade: máquinas
  com muitos núcleos (a pré-descodificação usa-os) e tirar o processamento do Render.
- Otimização feita no motor (mesmo vídeo de 30 s, 720×1280): render **103.6 s → 39.1 s**, imagem
  indistinguível (PSNR 39.4 dB contra a versão anterior, até 21% mais nítida).
- No teu PC, perfil Rápido: vídeo de 31 s gerado em **~33-40 s** de ponta a ponta.
- O custo real por vídeo aparece no Admin depois do passo 5 — decide o preço com esse número.
- Supabase Free: 1 GB de armazenamento, 50 MB por ficheiro. A app apaga sozinha rascunhos
  abandonados (3 dias) e os vídeos-fonte de projetos parados (14 dias; o vídeo final fica).

## PayPal (pagamento internacional)

Porquê PayPal: o Stripe não aceita contas de vendedor de Moçambique (verificado). Porquê
pacotes e não $0.50 avulso: à taxa padrão (2.99% + $0.49) uma cobrança de $0.50 dá prejuízo.
O cliente vê "$0.50 por geração" e compra em pacotes (4 por $2, 10 por $5) — pacotes e preços
editáveis no Admin, sem deploy.

1. https://developer.paypal.com → **Apps & Credentials** → Create App (tipo Merchant), em
   **Sandbox** primeiro.
2. Copia *Client ID* e *Secret*. Em **Webhooks** → Add Webhook:
   `https://aulls-velocity-studio.onrender.com/api/payments/paypal/webhook`, evento
   `PAYMENT.CAPTURE.COMPLETED`; copia o *Webhook ID*.
3. Render → Environment: `PAYPAL_ENV=sandbox`, `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`,
   `PAYPAL_WEBHOOK_ID`.
4. Testa com a conta de comprador sandbox; depois repete com credenciais *Live* e
   `PAYPAL_ENV=live`.

Os créditos só entram depois de o **servidor** confirmar com o PayPal (captura servidor-a-servidor
e/ou webhook com assinatura verificada), uma única vez por pagamento, e só se o valor pago
corresponder ao preço do pacote.

## e-Mola (Moçambique, manual)

O cliente transfere pela própria app e-Mola para o número configurado (`EMOLA_NUMBER`, por
defeito 868116236) e declara o pagamento; tu confirmas em **Admin → e-Mola to confirm**, indicando
quantos créditos o valor compra. Nunca se pede PIN.

## Apps Android / iOS

A app é uma PWA: no Android o Chrome oferece "Instalar app"; no iPhone, Safari → Partilhar →
"Adicionar ao ecrã principal". Ícone, ecrã inteiro e barra inferior como uma app nativa.
Publicar nas lojas (Play Store via TWA; App Store via Capacitor) reutiliza este mesmo código, mas
exige contas de programador (Google $25 uma vez; Apple $99/ano) — VOCÊ PRECISA de as criar
quando quiseres dar esse passo.

## Desenvolvimento local

```
venv\Scripts\python.exe run_studio.py
```
Sem Supabase: contas e projetos em `studio/data/` (persistem entre reinícios), vídeos gerados no
teu PC, 50 créditos de teste por conta nova.

Testes: `pytest` (motor, editor, API, ponta-a-ponta) e `cd tests/sql && npm install && npm test`
(migração SQL num Postgres real).
