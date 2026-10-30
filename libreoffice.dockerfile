FROM python:3.13-slim

# Keep apt lean; avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# --- System dependencies ---
# Install LibreOffice (headless) + Firefox for rendering or conversions
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libreoffice \
        libreoffice-writer \
        libreoffice-calc \
        libreoffice-impress \
        libreoffice-draw \
        libreoffice-base \
        libreoffice-common \
        fonts-dejavu \
        fonts-liberation && \
    rm -rf /var/lib/apt/lists/*

# --- Python dependencies ---
RUN pip install --no-cache-dir ddtrace

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /home/jetoslabs/autobots

# Copy uv metadata
COPY ./pyproject.toml ./uv.lock ./README.md ./

# Install dependencies (not in separate venv concept — uv manages .venv)
RUN uv sync --frozen --no-dev --no-install-project

ENV PATH="/home/jetoslabs/autobots/.venv/bin:$PATH"

# Copy application source
COPY ./src/autobots ./src/autobots

## DataDog setup
ARG DD_ENV=default
ENV DD_ENV=$DD_ENV
ARG DD_VERSION=default
ENV DD_VERSION=$DD_VERSION

ENV DD_SERVICE=autobots-libreoffice
# DD_AGENT_HOST should match datadog-agent container name
ENV DD_AGENT_HOST=datadog-agent
ENV DD_TRACE_AGENT_PORT=8126

# Datadog autodiscovery labels
LABEL "com.datadoghq.ad.check_names"='["autobots-libreoffice"]'
LABEL "com.datadoghq.ad.init_configs"='[{}]'
LABEL "com.datadoghq.ad.instances"='[{"autobots_status_url": "https://%%host%%:%%port%%/v1/hello"}]'
LABEL "com.datadoghq.ad.logs"='[{"source": "autobots-libreoffice", "service": "autobots-libreoffice"}]'

# --- Entrypoint ---
ENTRYPOINT ["ddtrace-run", "uvicorn", "--loop", "asyncio", "src.autobots.libreoffice:app", "--host", "0.0.0.0", "--port", "8003", "--workers", "5", "--lifespan", "on"]
