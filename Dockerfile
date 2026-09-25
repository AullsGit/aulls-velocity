# Imagem para o deploy do Velocity Studio na nuvem (Render.com ou qualquer host Docker).
# Base slim + ffmpeg (dependência externa, não instalável via pip) + libs de sistema que o
# OpenCV precisa mesmo na variante "headless". PyTorch é instalado à parte, a partir do
# índice CPU-only oficial, para não puxar os pacotes CUDA (vários GB, inúteis sem GPU).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Sem isto o arranque tenta carregar o EasyOCR (modelos pesados, exigem download na
    # primeira execução) — em hosts de baixo custo (RAM/disco/rede limitados) isto pode
    # falhar ou esgotar a memória. O texto sobreposto é um extra, não o núcleo do produto.
    VELOCITY_NO_TEXT=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-cloud.txt .
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision \
    && pip install -r requirements-cloud.txt

COPY velocity/ velocity/
COPY studio/ studio/

EXPOSE 8000
CMD ["sh", "-c", "uvicorn studio.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
