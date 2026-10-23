# Prizolov Agent OS 0.3.0 | Author: Dm.Andreyanov | Brand: Prizolov Lab | © 2026
# SPDX-FileCopyrightText: 2026 Dm.Andreyanov / Prizolov Lab
# SPDX-License-Identifier: Apache-2.0

# Образ Prizolov Agent OS: веб-интерфейс и HTTP API (по умолчанию), Telegram-бот,
# отчёты. Данные (база, документы, отчёты, журналы) - в томе /data.
#
#   docker build -t prizolov-os .
#   docker run --env-file .env -p 8800:8800 -v prizolov-data:/data prizolov-os
#   docker run --env-file .env -v prizolov-data:/data prizolov-os prizolov telegram

# Базовый образ можно взять из зеркала, если Docker Hub ограничивает число загрузок:
#   docker build --build-arg BASE_IMAGE=mirror.gcr.io/library/python:3.12-slim .
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

LABEL org.opencontainers.image.title="Prizolov Agent OS" \
      org.opencontainers.image.version="0.3.0" \
      org.opencontainers.image.authors="Dm.Andreyanov / Prizolov Lab" \
      org.opencontainers.image.vendor="Prizolov Lab" \
      org.opencontainers.image.url="https://prizolov.ru" \
      org.opencontainers.image.source="https://github.com/prizolov-lab/prizolov-agent-os" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLBACKEND=Agg \
    PRIZOLOV_WORKSPACE=/data/workspace \
    PRIZOLOV_DB_PATH=/data/prizolov.db \
    PRIZOLOV_TRACE_DIR=/data/logs \
    PRIZOLOV_API_HOST=0.0.0.0

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE AUTHORS ./
COPY prizolov_os ./prizolov_os
COPY cli ./cli
RUN pip install ".[telegram,api]" \
    && useradd --create-home --uid 10001 prizolov \
    && mkdir -p /data \
    && chown prizolov:prizolov /data

USER prizolov
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8800

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.getenv('PRIZOLOV_API_PORT', '8800'), timeout=4)" || exit 1

CMD ["prizolov", "api", "--scheduler"]
