# Prova técnica de motores NLE (Fase 1): MLT, GES e libopenshot dos repositórios oficiais do
# Debian, no mesmo contentor, para correr os mesmos testes (T1-T7) em todos.
#   docker build -t aulls-engines bench/engines
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg python3 python3-numpy \
        melt python3-mlt \
        gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav ges1.0-tools gir1.2-ges-1.0 python3-gi python3-gst-1.0 \
        python3-openshot \
    && rm -rf /var/lib/apt/lists/*

# versões instaladas (para o relatório)
RUN (melt --version 2>&1 | head -1; python3 -c "import gi; gi.require_version('GES','1.0'); from gi.repository import GES, Gst; Gst.init(None); print('GStreamer', Gst.version_string(), 'GES', GES.version())"; \
     python3 -c "import openshot; print('libopenshot', openshot.OPENSHOT_VERSION_FULL)"; \
     python3 -c "import mlt7 as m; print('mlt python ok')" 2>&1 | tail -1; \
     ffmpeg -hide_banner -version | head -1) > /versions.txt; cat /versions.txt

WORKDIR /work
