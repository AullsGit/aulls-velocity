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
