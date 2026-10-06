# AwlsVelocity — imagem COMPLETA para o Render (API + motor de vídeo na CPU).
# Funciona sozinha (COMPUTE_PROVIDER=local): o vídeo é gerado na CPU do próprio Render.
# Quando o RunPod estiver configurado (COMPUTE_PROVIDER=runpod), usar Dockerfile.api —
# muito mais leve, porque o servidor do Render deixa de processar vídeo.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VELOCITY_NO_TEXT=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-cloud.txt .
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision \
    && pip install -r requirements-cloud.txt

COPY velocity/ velocity/
COPY worker/ worker/
COPY studio/ studio/

EXPOSE 8000
CMD ["sh", "-c", "uvicorn studio.server:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
