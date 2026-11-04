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

## Atualização — filtro guiado, anti-halo, denoise e super-resolução (2026-09-20, sessão 2, fim)
1) Máscara do sujeito refinada com filtro guiado (cv2.ximgproc) + nitidez com limite anti-overshoot (mín/máx local): o contorno claro
   vinha em boa parte do próprio unsharp; agora desaparece. 2) `--denoise` (hqdn3d à resolução nativa, defeito 0.5): efeito suave, baixo risco.
3) `--sr` EXPERIMENTAL (Real-ESRGAN general x4v3 via spandrel, `bench/models/`): só em planos com ampliação >=1.5x; SR limitada ao mín/máx local
   da bicúbica e misturada 75/25 (sem halo/plástico, validado em A/B visual). Custo: ~2.6 s/frame com a GPU a 10 W (deve ser ~10x menos a 60 W).
Nota: nvidia-smi bloqueia enquanto a GPU está sob carga; benchmark de GPU tem de sincronizar (torch.cuda.synchronize) com nº fixo de operações.

## Atualização — Professional Velocity Engine (2026-09-21)
Novo: `VibeProfile` no DNA v1.3 (agressividade, suavidade, calma, curva de energia a 4 Hz, impactos, descansos, arco, densidades,
vocabulário, relação com a música); detetor v1 (também por efeitos densos sobre filmagem contínua); transições com **direção medida**
(slide_left/right/up/down, zoom_in/out) e **envelopes medidos** (também nas faixas de efeito); matching v2 (papel impacto/descanso
pela curva de energia, direção do sujeito, continuidade em cortes secos); `--text` condicionado à evidência (OCR, sem marcas de água,
texto de cena filtrado por posição estável); log de decisões (`decisions` no relatório); autocorreção pela vibe ("exagerou -> reduz").
Validações: `bench/look_eval.py` (18/18 suave/agressivo, EM AMOSTRA), `bench/detector_eval.py` (26/30 positivos, 5/5 negativos),
`tests/test_transitions_roundtrip.py`, `tests/test_vibe_text.py`.

Problemas abertos (por ordem de impacto):
1. Referência de FILMAGEM ÚNICA com efeitos à batida (ex.: pro_01) é lida como multi-corte -> output alterna entre clips. Precisa de
   embeddings visuais (CLIP/DINO) para decidir 'mesma filmagem'. Heurística de fundo testada e REJEITADA (não separa).
2. Vocabulário de efeitos em falta: rotação, mudança de grade de cor, pixelização, picture-in-picture, faíscas/partículas, glow, cartões com perspetiva.
3. Recorte/isolamento do sujeito como TRANSIÇÃO (máscaras, object wipes, sujeito como máscara) não implementado.
4. Texto que aparece letra a letra e marcas de canal grandes escapam ao filtro do OCR.
5. Impactos da curva de energia do output não alinham ao tempo dos da referência (0 de 5 em aggr_10), apesar de a curva correlacionar 0.74.
6. GPU limitada a 10 W pelo portátil (NitroSense): segmentação ~0.55 s/frame, SR ~2.6 s/frame.

## Atualização — pronto para lançamento: duração-alvo, Studio, sprint de 72h (2026-09-25)

Foco desta sessão: sair de "motor que funciona em bench/" para "produto que um operador consegue
usar para servir clientes reais". Mudanças:

1. **Duração-alvo do produto (25-30s) — a maior lacuna anterior.** `velocity/matching/extend.py`:
   referências curtas (a maioria das reais, 10-20s) têm o seu padrão de corte/efeito REPETIDO
   (com material NOVO do utilizador a cada volta — o matching já penaliza reutilização) até caber
   em 25-31s, com uma transição de costura no ponto de loop escolhida a partir do estilo mais
   representativo já medido na própria referência; referências longas de mais são aparadas no
   limite superior, num limite de plano. Todos os campos derivados (signature/audio/vibe) são
   RECALCULADOS sobre a timeline estendida — essa timeline estendida passa a ser o alvo real
   contra o qual o resultado é avaliado, não a referência curta original. Testado (round-trip +
   render real): duração final sempre dentro do alvo, cortes sem sobreposição, transição de costura
   inspecionada visualmente (parece um corte normal da própria referência).
2. **Perfis de qualidade/velocidade** (`--profile fast|standard|hd`, também no Studio): tempo de
   geração do perfil "standard" (720x1280, 1 iteração de correção) caiu de ~7min24s para ~3min28s
   por vídeo de 30s, sem perda de fidelidade mensurável — as duas maiores poupanças foram (a) o
   OCR (EasyOCR) estava a correr em CADA reanálise do próprio output, onde nunca é usado — removido;
   (b) `-preset fast` do libx264 em vez de `medium` nos perfis não-HD.
3. **`velocity/pipeline.py`**: a lógica de geração (antes só dentro de `cli.cmd_make`) foi extraída
   para ser chamada tanto pela CLI como pelo Studio NO MESMO PROCESSO — evita repagar o arranque de
   modelos (EasyOCR, segmentador de pessoas) a cada encomenda, o maior custo fixo por geração.
4. **Velocity Studio** (`studio/`, `run_studio.py`): interface web local (FastAPI, um único
   operador, um job de cada vez) — upload de referência/clips/música, verificação prévia da
   referência (aprovada/recusada, com motivo), perfis, progresso em direto, pré-visualização e
   download do resultado. Testado ponta a ponta via API real (não só a UI): geração completa
   (vídeo válido, duração no alvo) e o caminho de rejeição (referência não-Velocity).
5. **Robustez**: `cli.main` nunca mais rebenta com stack trace (captura e devolve erro limpo,
   com relatório de erro se `--report` for passado); corrigido um crash real do matching quando
   TODAS as janelas de um clip estão marcadas como instáveis (ficheiro de teste do próprio
   utilizador, um output anterior usado por engano como input); áudio de referência/música agora
   faz LOOP automático quando o vídeo fica mais longo que a faixa disponível (antes a 2ª metade
   de um vídeo estendido saía muda); higiene de git — `teste/` (vídeos pessoais) deixou de ser
   versionado.
6. **Material de vendas**: `marketing/landing.html` (publicada como Artifact privado) e
   `marketing/mensagens.md` (mensagens WhatsApp/Instagram, checklist de encomenda). Demos gerados
   com o material de bench foram enviados ao utilizador para revisão PRIVADA, não publicados —
   a referência usada nos testes tem proveniência de terceiros (edições encontradas online);
   para material de marketing público, gerar demos novos com conteúdo próprio via o Studio.

Testes: 17 (todos os anteriores + `tests/test_extend.py`, 4 casos: referência curta repetida,
referência já no alvo mantida intacta, referência longa aparada, render real da timeline
estendida com duração exata). Falta cobertura de teste automático para `studio/server.py`
em si (validado manualmente via curl contra o servidor real, não com pytest).

### Ainda por fazer para uma v2 (não bloqueia o lançamento)
- WhatsApp/M-Pesa/e-Mola na landing page e em `mensagens.md` são placeholders — substituir pelos
  reais antes de divulgar.
- Nenhum mecanismo de pagamento automático (deliberado, por pedido do utilizador — fluxo manual
  via WhatsApp + confirmação de M-Pesa/e-Mola).
- `studio/server.py` não tem autenticação nem limite de tamanho de upload reforçado — aceitável
  para uso local por um único operador; NÃO expor à Internet sem adicionar isso primeiro.
- Os 6 problemas em aberto listados acima (filmagem única, vocabulário de efeitos, transições por
  máscara, texto letra-a-letra, alinhamento de impactos, GPU a 10W) continuam válidos.

## Atualização — público global, i18n e pagamento internacional (2026-09-25, tarde)

Pedido do utilizador: alvo deixa de ser só Moçambique, passa a ser global; site em inglês por
defeito com alternância de idioma; forma de pagamento internacional viável já.

- **Pagamento internacional: PayPal.** Verificado na documentação oficial do PayPal
  (developer.paypal.com/payouts/supported-features, atualizada 10/set/2026): Moçambique está
  na categoria "Send, receive, and withdraw" — sem restrições para receber/levantar. É a opção
  mais rápida de ativar sem espera de aprovação (basta um link `paypal.me/<utilizador>`).
  PayPal.me ainda **não está criado** — `PAYPAL_ME` em `landing.html` é placeholder
  (`paypal.me/AullsVelocity`); ver `marketing/mensagens.md` para os passos.
- **`marketing/landing.html` reescrita com i18n cliente** (EN/PT/ES/FR, dicionário JS,
  deteção automática por `navigator.language` com fallback para inglês, preferência guardada em
  localStorage) e um alternador de região (Internacional/USD/PayPal vs Moçambique/MT/e-Mola-
  WhatsApp) independente do idioma — também com deteção automática (fuso horário
  `Africa/Maputo`). Testado interagindo com o DOM real (`form_input`/`click`), não só visualmente:
  troca de idioma e de região confirmadas a atualizar todo o texto, preços e método de pagamento.
- Preços internacionais fixados em USD (5/8/12) independentes da conversão MT — preço mais alto
  para mercados com maior poder de compra é uma escolha deliberada, não um erro de câmbio.
- Marca atualizada para "Aulls Velocity" com slogan ("Steal the rhythm. Not the video." / PT
  "Rouba o ritmo. Não o vídeo.", traduzido nas 4 línguas).

## Atualização — deploy da app real na nuvem (2026-09-25, noite)

Pedido do utilizador: a landing page (`marketing/landing.html`) é só divulgação — o pedido foi
para publicar a APLICAÇÃO (Velocity Studio) num link público, usável por qualquer pessoa via
navegador, sem precisar de instalar nada nem de correr localmente.

Preparado (tudo o que dá para preparar sem uma conta pessoal do utilizador):
- `Dockerfile` — Python 3.11-slim + ffmpeg + libs de sistema para o OpenCV headless; PyTorch
  instalado à parte a partir do índice CPU-only oficial (evita puxar pacotes CUDA, inúteis sem
  GPU); `VELOCITY_NO_TEXT=1` por defeito na imagem (EasyOCR exige download de modelos e RAM
  extra — inaceitável num free tier de 512MB; o texto sobreposto é um extra, não o núcleo).
- `.dockerignore`, `requirements-cloud.txt` (lista mínima real de dependências, obtida por
  grep aos imports de `velocity/`/`studio/` — confirmou que `mediapipe`, `anthropic`,
  `matplotlib`, `pytesseract`, `open_clip_torch`, `scenedetect` não são usados em runtime).
- `render.yaml` — blueprint do Render.com (plano free) já com as variáveis de ambiente de
  segurança abaixo pré-configuradas para os limites apertados do free tier.
- **`studio/server.py` reforçado para exposição pública** (antes só previsto para um operador
  local, sem proteção nenhuma):
  - `/health` — endpoint de vida para o healthcheck do Render.
  - limite de tamanho de upload REALMENTE aplicado por streaming/contagem de bytes
    (`VELOCITY_MAX_UPLOAD_MB`; antes existia só a constante `MAX_UPLOAD_MB`, nunca verificada —
    lacuna identificada e agora fechada).
  - limite de nº de clipes por pedido (`VELOCITY_MAX_CLIPS`).
  - fila com tamanho máximo (`VELOCITY_MAX_QUEUE`) — acima disso, resposta 503 imediata em vez
    de deixar acumular pedidos que a CPU fraca do free tier nunca despacharia a tempo.
  - limite de pedidos por IP por hora (`VELOCITY_RATE_LIMIT_JOBS`/`_WINDOW_S`, janela deslizante
    em memória) — protege contra um único visitante (ou bot) monopolizar a única instância.
  - Verificado com `fastapi.testclient.TestClient` (sem servidor real): `/health` responde,
    upload acima do limite devolve 413 e limpa o diretório do job, 3ª tentativa dentro da janela
    de rate-limit devolve 429. Não há teste automatizado (`pytest`) dedicado — smoke test manual
    documentado aqui.
- `docs/DEPLOY.md` — guia passo a passo para o utilizador (os 2 únicos passos que só ele pode
  fazer: criar o repositório no GitHub e ligar conta Render ao blueprint), incluindo limitações
  honestas do plano gratuito (512MB RAM, adormece ao fim de 15min, armazenamento não-persistente,
  um vídeo de cada vez) e o caminho de upgrade (~$7/mês) quando houver clientes reais.

Por fazer (depende de ações do utilizador, fora do meu alcance): criar o repositório GitHub e
fazer push; criar conta Render e aplicar o blueprint; depois do primeiro deploy, testar uma
geração real ponta-a-ponta no link público antes de divulgar.

**Concluído nesta sessão** (feito via `gh` CLI portátil, autenticado pelo próprio utilizador
por device-code — nunca vi a password): repositório `AullsGit/aulls-velocity` criado (privado)
e código enviado; deploy no Render confirmado a funcionar em produção
(`https://aulls-velocity-studio.onrender.com`, `/health` respondeu OK).

## Atualização — contas, paywall e pagamentos (2026-09-25, noite)

Pedido do utilizador: 1ª edição grátis, depois exigir pagamento; pagamento por declaração do
valor (sem verificar PIN do e-Mola, número fixo 868116236, fluxo em que o cliente se sinta
seguro); login formal ("estilo Instagram.web"); quem quiser pode publicar os vídeos gerados.
Também perguntou que base de dados está a ser usada, e sugeriu Firebase para "acelerar o
processamento".

- **Resposta à pergunta**: antes desta atualização, nenhuma — `studio/server.py` guardava tudo
  num dicionário Python em memória, perdido a cada reinício (crítico no Render free, que não
  tem disco persistente). Corrigido agora com Supabase.
- **Correção ao pedido do utilizador**: uma base de dados não acelera o processamento de vídeo
  (isso é CPU/GPU, não armazenamento) — expliquei isto diretamente em vez de implementar algo
  que não resolveria o problema. Fica registado para quando fizer sentido: mover só a geração
  para um serviço com mais CPU/GPU (ex.: Modal.com, tier grátis generoso) é o próximo passo real
  de velocidade, não feito agora.
- **Escolhido Supabase em vez de Firebase**: Postgres relacional (mais adequado a
  utilizador→pagamentos→vídeos do que o NoSQL do Firestore), Auth com hash de password gerido
  pelo próprio Supabase, e Storage grátis que resolve também a perda de vídeos ao reiniciar.
- **`supabase-schema.sql`** (novo, raiz do repo) — tabelas `profiles`/`jobs`/`payments`, trigger
  de criação automática de perfil, e funções Postgres atómicas `can_edit`/`consume_edit`/
  `confirm_payment` (lógica de créditos na BD, não em Python, para não haver corrida entre
  pedidos simultâneos).
- **`studio/db.py`** (novo) — cliente Supabase (service_role key, nunca exposta ao browser).
  `db.enabled()` é False sem `SUPABASE_URL`/`SUPABASE_SERVICE_KEY` — nesse caso o site volta ao
  comportamento original (sem login, sem limite), para não obrigar quem só quer correr
  `run_studio.py` localmente a ter conta Supabase.
- **`studio/auth.py`** (novo) — sessão via cookie httponly com o access/refresh token do
  Supabase Auth (nunca guardamos nem vemos passwords em claro — isso é todo tratado pelo
  Supabase). `require_owner` restringe `/admin` ao email em `VELOCITY_OWNER_EMAIL`.
- **`studio/server.py`**: gate de login+paywall em `POST /api/jobs` (401 sem sessão, 402 sem
  crédito); persiste jobs na BD e sobe o vídeo final para o Supabase Storage; endpoints de
  auth (`/api/auth/*`), pagamentos (`/api/payments/*`, `/api/admin/payments/*`), publicação
  opcional (`/api/jobs/{id}/public`, `/api/gallery`) e páginas novas (`/login`, `/signup`,
  `/conta`, `/explorar`, `/admin`).
- **Pagamento por declaração, não automático** — não existe (nem seria seguro pedir) qualquer
  ligação ao PIN da conta e-Mola. O cliente transfere pelo próprio telemóvel para 868116236,
  declara o valor na app (fica "pending"), e só o operador confirma manualmente em `/admin`
  depois de ver o dinheiro a entrar — é aí que o crédito é atribuído (função `confirm_payment`).
- **`studio/static/brand.css`** (novo) — identidade visual partilhada (mesma paleta/tipografia
  da landing page: Bebas Neue + Manrope, ciano/magenta) entre todas as páginas do Studio, para
  a marca ficar coerente ("extremamente formal" pedido pelo utilizador).
- **Bug real encontrado e corrigido durante o teste**: `/api/jobs/mine` colidia com a rota
  já existente `/api/jobs/{job_id}` (declarada antes — `{job_id}` capturava literalmente
  "mine"), devolvendo sempre 404. Renomeado para `/api/my-jobs`. Confirmado com
  `fastapi.testclient.TestClient` + mocks do `db`/`auth` (sem precisar de projeto Supabase
  real): gates 401/402, admin 403 para não-donos, endpoints de pagamento/publicação — todos
  verificados a devolver o que deviam antes deste bug ser corrigido.
- Testado visualmente no browser (modo local, sem Supabase configurado): nav responsiva em
  ecrã estreito corrigida (quebrava mal no telemóvel), páginas login/conta/explorar renderizam
  consistentes com a marca. Suite `pytest -m "not bench"` (5 testes rápidos) continua a passar.

### Por fazer (só o utilizador consegue)
- Criar projeto Supabase, correr `supabase-schema.sql`, e definir `SUPABASE_URL` /
  `SUPABASE_SERVICE_KEY` / `VELOCITY_OWNER_EMAIL` no Render — passo a passo em `docs/DEPLOY.md`.
  Sem isto, o site em produção continua no modo antigo (sem login/paywall).
- Testar o fluxo completo uma vez com Supabase real: criar conta, gerar a edição grátis,
  tentar gerar uma 2ª (deve bloquear), declarar um pagamento fictício, confirmar em `/admin`,
  confirmar que desbloqueia.

## Atualização — AwlsVelocity: sem plano grátis, PayPal automático (2026-09-26)

Pedido do utilizador (pasted_content extenso, 25 secções): rename para "AwlsVelocity";
investigar e otimizar a velocidade real do pipeline; remover o plano grátis — só $0.50 por
geração, nada mais; pagamento online automático e seguro (frontend nunca decide preço nem
credita nada — só um webhook verificado no backend); preservar tudo o que já funciona.

- **Rename** para AwlsVelocity em toda a UI do Studio (título, navbar, login/registo, conta,
  admin, explorar). Não tocado: nome do repo GitHub, nome do serviço/domínio no Render.
- **Velocidade — profiling real primeiro** (`cProfile` + timing por etapa, sem cache, com a
  config do Docker): render = 82% do tempo total; dentro do render, `decode_window()`
  (chamadas ffmpeg por plano, não o compositing OpenCV) = 75%. Duas otimizações medidas,
  sem perda de qualidade (mesma fidelidade final 0.8253 antes/depois no mesmo vídeo):
  1. `pipeline.py`: a 2ª renderização do "protetor de fidelidade" só acontece se a 1ª ainda
     não atingir fidelidade ≥ 0.85 (`GOOD_ENOUGH_FIDELITY`) — antes disparava sempre que
     havia qualquer ajuste, pagando ~metade do tempo total por um ganho por vezes marginal.
  2. `pipeline.py`: análise dos clips paralelizada (`ThreadPoolExecutor`) quando não há
     segmentador partilhado — o custo é ffmpeg em subprocesso (liberta o GIL), por isso
     paraleliza de verdade. Ganho medido: 203s → 180s no vídeo de teste (a otimização #1
     não disparou nesse caso em concreto — só poupa quando a 1ª passagem já é boa).
  - **Não feito** (maior esforço, adiado): cache dos frames descodificados entre a 1ª e a
    2ª passagem eliminaria quase os ~94s da 2ª por completo, mas guardar todos os planos
    descodificados em RAM pode passar de 1-2GB num vídeo de 30s — arriscado numa instância
    Render pequena. Precisa de ser em disco, não em RAM, para ser seguro. Próximo passo de
    maior impacto, não implementado ainda.
- **Sem plano grátis**: removida a lógica de "1ª edição grátis" (`free_edits_used` deixou de
  ser lida por `can_edit`/`consume_edit`, coluna mantida na tabela por compatibilidade, não
  apagada — "não destruir o schema existente"). Só há edição com créditos pagos.
- **Gateway de pagamento — decisão baseada em números verificados, não inventados**:
  - Stripe: **eliminado**. Moçambique não está na lista de países suportados para conta de
    vendedor (confirmado via pesquisa, não assumido).
  - PayPal: viável, mas a taxa PADRÃO (2,99% + $0,49) sozinha já excede $0,50 — cobrar $0,50
    isolado dá prejuízo. A taxa "Micropayments" (~4,99%+$0,09, ~77% margem) resolveria, mas
    exige pedir aprovação direta ao PayPal — não é algo que se resolva por código.
  - **Decisão** (pedida ao utilizador via AskUserQuestion, resposta: "o mais baixo possível
    aceitável, sem complicações"): carteira de créditos por PACOTE em vez de $0,50 isolado.
    O preço ao cliente continua $0,50/geração, mas a cobrança PayPal é maior (4 gerações por
    $2, 10 por $5) — dilui a taxa fixa para ~72-87% de margem já com a taxa padrão, sem
    esperar por ninguém. Ajustável em `studio/payments_paypal.py::PACKS`.
- **`studio/payments_paypal.py`** (novo) — Orders API v2 (sandbox/live por `PAYPAL_ENV`).
  `create_order` usa sempre o preço de `PACKS` (nunca um valor vindo do cliente); `custom_id`
  guarda `"user_id:pack_id"` (única forma de o webhook saber a quem creditar). Arquitetura
  exatamente como pedida: frontend cria order → cliente aprova → backend captura → só o
  WEBHOOK (assinatura verificada pela própria API do PayPal via
  `verify_webhook_signature`) concede créditos, nunca a resposta síncrona da captura.
- **`supabase-schema.sql`**: migração idempotente (`alter table ... add column if not
  exists`, segura de re-correr) acrescenta `provider`/`provider_payment_id`/`currency`/
  `amount_usd`/`pack_id`/`credits` a `payments`; índice único em
  `(provider, provider_payment_id)` — protege contra o PayPal reenviar o mesmo webhook e
  creditar 2x. Nova função `grant_paypal_credits` (idempotente, chamada só pelo webhook).
- Testado com mocks (sem credenciais PayPal reais ainda): `/api/me` só mostra a secção PayPal
  quando configurado; `create-order` rejeita pack_id inválido; `capture` não credita nada por
  si só; `webhook` rejeita assinatura inválida (400), credita corretamente num evento válido,
  ignora eventos irrelevantes sem tocar em créditos. `pytest -m "not bench"` continua a passar.
- `studio/static/conta.html` ganhou os botões PayPal (SDK oficial `paypal.com/sdk/js`,
  carregado só quando `paypal.enabled`) lado a lado com o fluxo e-Mola (mantido, não quebrado).

### Por fazer (a maior parte do pedido original de 25 secções — priorizado o que era mais
### urgente/bloqueante primeiro: rename, velocidade real, viabilidade do pagamento)
- **Só o utilizador consegue**: criar app PayPal Developer (sandbox) e o webhook, testar um
  pagamento sandbox real — passo a passo em `docs/DEPLOY.md`, secção "Passo 4".
- Arquitetura de job assíncrono com etapas nomeadas na UI (Uploading/Analyzing/Rendering/...)
  — hoje já há polling+log, falta a barra de progresso por etapa pedida.
- Limites explícitos de duração/resolução na interface (upload size/clips já existiam).
- Redesenho visual completo (landing page, dashboard, mobile) — não feito nesta fase.
- Sanitização de mensagens de erro técnicas para o utilizador final.
- Cache de frames entre passagens de render (maior otimização de velocidade ainda por fazer).

## Atualização — produto comercial (app v2): RunPod, créditos, editor, PWA (2026-10-06)

Briefing de 33 secções: transformar o motor num produto (web/Android/iOS), processamento em
GPU sob demanda (RunPod Serverless, escala a zero), créditos, editor leve pós-geração, admin,
analytics, landing. Feito por fases, cada uma testada antes da seguinte.

**Motor (só o necessário, medido):**
- Profiling real: render = 75% descodificação ffmpeg por plano. NVDEC (GPU) medido e **mais
  lento** (107s vs 75s). Causa real: cada janela era descodificada até 1440x2560 com clips de
  480x864 (ampliar ~9x píxeis sem detalhe novo). Corrigido: decode à resolução nativa + 1
  ampliação bicúbica no warpAffine (com mapeamento de centros de pixel) + pré-descodificação
  paralela com orçamento de memória + probe memorizado. Render 103.6s -> 39.1s, PSNR 39.4 dB vs
  anterior, ~0 px de deslocamento, 21% mais nítido. Suite de round-trip passa.
- run_make: callback de progresso por etapa, tempos por etapa no relatório, reutiliza a análise
  da referência (MakeParams.dna), devolve a Timeline; OCR só quando há texto pedido.
- Timeline.music_volume (editor). velocity/edit/timeline_ops.py: o browser envia um EditDoc
  simples; o backend reconstrói a timeline reancorando transições/efeitos (8 testes).

**Plataforma:** store (Supabase | memória, mesmo contrato), storage (Supabase | disco, mesmo
protocolo PUT multipart assinado — confirmado na fonte do storage3), compute (local | RunPod —
API verificada em docs.runpod.io), JobService (créditos reservados ANTES, reembolso automático,
idempotência, monitor + webhook, limpeza automática), PaymentService (PayPal: captura
servidor-a-servidor + webhook, idempotentes, recusa valor ≠ preço do pacote), auth (Supabase |
memória; Google só se configurado). supabase-schema.sql v3 testado em Postgres real (PGlite),
incluindo atualização a partir da v2; fecha o buraco das funções security definer executáveis
pelo papel anon.

**App:** SPA sem build (ES modules), PWA (manifest, service worker, ícones), EN por defeito +
PT. Home, criar (4 passos com upload direto/progresso/galeria/câmara), análise da IA visível,
geração com etapas reais e cancelamento com reembolso, resultado (editar/descarregar/partilhar
ficheiro nativo/gerar de novo), projetos, editor (dividir, cortar, reordenar por arrastar/toque
longo, velocidade, transição, zoom/flip, substituir, duplicar, apagar, texto, música, filtros,
desfazer/refazer, voltar à versão da IA, atalhos), perfil/créditos, admin (negócio + custo +
filas + falhas + preço/pacotes/promo), landing, onboarding de 3 ecrãs, página de partilha com
Open Graph.

**Testes:** 49 pytest (motor + editor + 22 API + 1 ponta-a-ponta com o motor real) + 41
verificações SQL. Testado no browser: registo -> onboarding -> upload -> análise real -> geração
real (31s de vídeo em 33s, perfil rápido) -> editor (apagar/dividir/desfazer/texto) ->
re-export com texto queimado -> créditos/admin; layout mobile e desktop medidos no DOM.
Handler RunPod testado com o SDK oficial (modo local): geração completa, 12 atualizações de
progresso, ok=True.

### Por fazer / limitações
- RunPod real, Supabase migração, PayPal e Google dependem de contas/ações do utilizador
  (docs/DEPLOY.md, passos marcados "VOCÊ PRECISA FAZER ISTO").
- Pré-visualização do editor é "rápida" (sem efeitos); o final só ao exportar.
- Rotação, pista de áudio dos clips do utilizador e transições por máscara não existem no motor.
- Landing page só em inglês (a app é EN/PT).
- Lojas Android/iOS: PWA pronta; publicação exige contas de programador.

## Atualização — fidelidade à referência: banco com verdade conhecida + reconstrução do cenário A (2026-10-21)

Detalhe completo, métricas e números em **[docs/FIDELITY.md](FIDELITY.md)**.

- Banco novo (`velocity/evaluate/synth.py`, `fidelity_bench.py`): referências sintéticas em que cada
  frame é conhecido; mede o analisador e a reprodução em separado, com SSIM "ao acaso" e teto do
  codec como linhas de base. Linha de base: no cenário A (mesmo material) o SSIM ficava 0.04 acima
  do acaso, enquanto o motor se auto-avaliava com 0.95–0.98.
- Correção nº 1: alinhamento às fontes (`matching/source_align.py`) + timeline exata
  (`matching/reconstruct.py`, `ClipUse.exact`) + caminho exato no renderer. Automático: só entra
  quando ≥ 50% da referência alinha às fontes. SSIM 0.58–0.61 → 0.947–0.957 (teto 0.968–0.976),
  fonte e instante certos em 100% dos planos, duração/aspeto/fps da referência.
- App: resultado e editor mostram o formato real do vídeo (16:9, 1:1…), não sempre 9:16.
- Testes: 55/55 (6 novos em `tests/test_reconstruct.py`). O teste ponta-a-ponta passou a usar
  clips de material diferente da referência (com o mesmo material, o motor agora reconstrói).
- Por resolver (cenário B): formato da referência, movimento, velocidade absoluta, cross-fade e
  cor — sequência em docs/FIDELITY.md §2.

## Atualização — nova arquitetura: Plano v1 + adaptador de motor (2026-11-01)

Auditoria completa (página "AullsVelocity — Auditoria e Plano"). Decisões do utilizador: produto
web/PWA global; Docker Desktop instalado (falta o WSL para arrancar); implementar já.
- **Plano de edição v1** (`velocity/plan`, docs/PLAN.md): contrato versionado entre inteligência e
  motor, validado antes do render (e no editor antes de reservar créditos); projetos antigos abrem
  sem migração; conversão exata (banco idêntico antes/depois, 6/6).
- **Adaptador de motor** (`velocity/engine`): compositor atual atrás de `RenderEngine` +
  `Capabilities`; MLT/GES entram aqui depois da prova técnica.
- **Medição de recursos** por etapa (RAM, CPU, VRAM do PyTorch) nas métricas de cada job.
- **CI** no GitHub (Python 3.11 = imagens Docker; SQL em PGlite).
- **Precisão ao frame** medida e corrigida (2 bugs no compositor) — docs/FIDELITY.md §5.
- **Filmagem real** no banco de fidelidade (r4, r5) — revela jump cuts e planos curtos como as
  próximas falhas a corrigir.
- Testes: 78/78.

## Atualização — agente de edição (2026-11-04)

O produto passa a ser uma conversa: `docs/AGENT.md`.
- **Agente** (`studio/agent`, `velocity/agent`): 16 ferramentas reais sobre o Plano v1; ciclo com
  histórico só por acréscimo, retoma depois de um reinício sem repetir nem cobrar 2x; memória de
  preferências; verificação objetiva do resultado (`verify_output`) e comparação visual com a
  referência; interface de chat na PWA (`/agent`) como entrada principal.
- **Motor**: dissolução verdadeira, som original por plano (acompanha os cortes), marca de água,
  grade de cor 3x4 medida, modo "imagem intacta".
- **Referência**: deteta dissoluções e cortes dentro de movimento forte (`reference/soft_cuts.py`).
- **Matching**: só repete material quando não há outro.
- Falta provar com o modelo verdadeiro: `python bench/agent_live.py` com `ANTHROPIC_API_KEY`.
