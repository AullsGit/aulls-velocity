# AwlsVelocity — worker de vídeo para o RunPod Serverless (GPU sob demanda).
#
# O RunPod constrói esta imagem diretamente do GitHub (Serverless → New Endpoint → Import Git
# Repository → Dockerfile Path: worker/Dockerfile). Contexto de build = raiz do repositório.
#
# Onde a GPU ajuda (medido): segmentação de pessoas e OCR de texto (PyTorch/EasyOCR). O render
# em si é ffmpeg + OpenCV em CPU — por isso a pré-descodificação usa vários núcleos
# (VELOCITY_DECODE_WORKERS): as máquinas GPU do RunPod têm 8-16+ vCPU, ao contrário do Render.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VELOCITY_DECODE_WORKERS=6 \
    VELOCITY_DECODE_MEM_MB=2048

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# PyTorch com CUDA (as rodas trazem as bibliotecas CUDA; o RunPod fornece o driver)
ARG TORCH_INDEX=https://download.pytorch.org/whl/cu126
COPY requirements-worker.txt .
RUN pip install --index-url ${TORCH_INDEX} torch torchvision \
    && pip install -r requirements-worker.txt

# Modelos do OCR incluídos na imagem: sem download no arranque a frio (cold start mais curto)
RUN python -c "import easyocr; easyocr.Reader(['en', 'pt'], gpu=False, verbose=False)"

COPY velocity/ velocity/
COPY worker/ worker/

CMD ["python", "-u", "-m", "worker.handler"]
