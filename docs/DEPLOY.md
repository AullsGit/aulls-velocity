# Pôr o Velocity Studio na nuvem (para uso público, via web)

Isto publica a APLICAÇÃO real (upload de referência + clipes → gera o vídeo), não a landing
page de marketing. Preparei tudo o que dá para preparar sem uma conta tua; faltam só os dois
passos abaixo, que só tu podes fazer (criar contas/repositórios é uma ação de conta pessoal).

## O que já está pronto no código

- `Dockerfile` — empacota a app (Python + ffmpeg + PyTorch CPU-only + as dependências reais).
- `.dockerignore` — mantém a imagem pequena.
- `requirements-cloud.txt` — só os pacotes que o código realmente usa.
- `render.yaml` — blueprint para o Render criar o serviço com as variáveis certas.
- `studio/server.py` foi reforçado para ficar seguro exposto ao público:
  - `/health` — endpoint de verificação de vida (o Render usa isto para saber se o serviço está de pé).
  - limite de tamanho de upload realmente aplicado (`VELOCITY_MAX_UPLOAD_MB`, antes só existia a constante, sem verificação).
  - limite de vídeos por pedido (`VELOCITY_MAX_CLIPS`).
  - fila com limite (`VELOCITY_MAX_QUEUE`) — se estiver cheia, o utilizador recebe logo "servidor ocupado", em vez de ficar tudo lento para todos.
  - limite de pedidos por IP por hora (`VELOCITY_RATE_LIMIT_JOBS` / `VELOCITY_RATE_LIMIT_WINDOW_S`) — protege contra alguém (ou um bot) enfileirar dezenas de gerações e travar o serviço para os outros.

## Passo 1 — Criar o repositório no GitHub (só tu podes fazer isto)

```bash
git init
git add .
git commit -m "Primeira versão para deploy"
```

(se já correste `git init`/`git add` antes, ignora essa parte). Depois:

1. Vai a https://github.com/new, cria um repositório (pode ser privado), sem README/gitignore
   (já existem aqui).
2. Copia os 2 comandos que o GitHub mostra a seguir a "…or push an existing repository from
   the command line", parecidos com:

```bash
git remote add origin https://github.com/TEU-UTILIZADOR/TEU-REPO.git
git push -u origin main
```

## Passo 2 — Criar conta no Render e ligar o repositório (só tu podes fazer isto)

1. Cria conta grátis em https://render.com (não pede cartão para o plano free).
2. No dashboard, "New" → "Blueprint" → autoriza o Render a aceder ao teu GitHub → escolhe o
   repositório que acabaste de criar.
3. O Render lê o `render.yaml` sozinho e propõe criar o serviço `aulls-velocity-studio` — só
   tens de confirmar "Apply". O build demora uns 10-15 minutos da primeira vez (compila o
   PyTorch/OpenCV).
4. No fim, o Render dá-te um link público tipo `https://aulls-velocity-studio.onrender.com` —
   esse é o link que dás aos clientes. Funciona em qualquer telemóvel/computador, só a abrir
   o link no navegador (é isso que faz de ser "web" em vez de "app de loja" — não precisa de
   instalar nada).

## Passo 3 — Contas, pagamentos e galeria: ligar o Supabase (só tu podes fazer isto)

Desde que o site passou a ter login, 1ª edição grátis + pagamento para as seguintes, e
publicação opcional de vídeos, isto precisa de uma base de dados que sobreviva a reinícios
(o Render grátis não tem disco persistente). Usamos o [Supabase](https://supabase.com)
(Postgres + Auth + Storage, tudo grátis no plano Free).

1. Cria conta grátis em https://supabase.com (login com GitHub, um clique, sem cartão).
2. "New Project" → escolhe um nome e uma password para a base de dados (guarda-a nalgum
   lado, não é preciso para o resto destes passos) → espera ~2 min a ficar pronto.
3. Vai a "SQL Editor" → "New query" → cola todo o conteúdo do ficheiro `supabase-schema.sql`
   (na raiz deste repositório) → "Run". Isto cria as tabelas de perfis, vídeos e pagamentos,
   e o bucket de armazenamento dos vídeos.
4. Vai a "Project Settings" → "API" — vais precisar de 2 valores:
   - **Project URL** (ex.: `https://xxxxx.supabase.co`)
   - **service_role secret** (na secção "Project API keys" — é secreta, nunca a partilhes
     nem a metas no código; só vai para uma variável de ambiente no Render)
5. No dashboard do Render, no serviço `aulls-velocity-studio` → "Environment" → adiciona:
   - `SUPABASE_URL` = o Project URL
   - `SUPABASE_SERVICE_KEY` = a service_role secret
   - `VELOCITY_OWNER_EMAIL` = o email com que te vais registar na app (só esta conta consegue
     abrir `/admin` para confirmar pagamentos)
6. Guarda — o Render reinicia o serviço sozinho com as novas variáveis.

**Sem isto configurado, o site continua a funcionar** exactamente como antes (sem login, sem
limite de edições) — as contas/pagamentos só ficam ativos quando as duas variáveis
`SUPABASE_URL`/`SUPABASE_SERVICE_KEY` existirem.

### Como funciona o pagamento (importante)

Não há nenhuma ligação automática ao e-Mola — não é possível para uma conta pessoal, e não
seria seguro pedir o PIN de ninguém. O fluxo é: o cliente transfere pelo **seu próprio**
telemóvel para 868116236, depois declara na app quanto pagou (fica "a aguardar confirmação"),
e só desbloqueia a próxima edição quando TU confirmares manualmente em `/admin` — depois de
veres o dinheiro a entrar na tua conta e-Mola. Nunca pedimos PIN nem qualquer dado da conta.

## Limitações honestas do plano gratuito (importante saberes)

- **512MB RAM, CPU muito fraca** — uma geração que demora ~3m30s no teu PC pode demorar
  bastantes minutos mais aqui. Alguns pedidos maiores podem falhar por falta de memória
  (o job falha isoladamente, mostra erro ao cliente — não derruba o servidor para os outros).
- **"Adormece" ao fim de 15 min sem visitas** — o primeiro acesso depois disso demora ~1 minuto
  a "acordar". Aceitável para começar, mas um cliente pode achar que está avariado.
- **Armazenamento não é permanente** — se o serviço adormecer/reiniciar, vídeos gerados que
  ainda não foram descarregados perdem-se. Combina com o cliente para descarregar logo a
  seguir a "pronto".
- **Um único vídeo de cada vez** — está assim de propósito (ver `studio/server.py`), para não
  competir por CPU/RAM consigo próprio; com CPU tão fraca, é a única forma de não travar tudo.

Quando começares a ter clientes a sério, o passo natural é o upgrade para um plano pago do
Render (a partir de ~$7/mês — mais RAM/CPU dedicada, sem adormecer, disco persistente). Não é
preciso agora para testar/validar com os primeiros clientes.

## Depois do deploy

- Testa tu mesmo primeiro: abre o link, sobe uma referência + um clip teu, confirma que o
  vídeo final sai correto.
- Atualiza `marketing/landing.html` e `marketing/mensagens.md` para apontarem para o link real
  da app (hoje só descrevem os serviços; com a app publicada, dá para ligar direto lá).
