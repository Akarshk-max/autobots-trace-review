FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive

# Keep apt lean; no full upgrade in containers
RUN apt-get update \
 && apt-get install -y --no-install-recommends firefox-esr \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir ddtrace

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /home/jetoslabs/autobots

COPY ./pyproject.toml ./uv.lock ./README.md ./

RUN uv sync --frozen --no-dev --no-install-project

ENV PATH="/home/jetoslabs/autobots/.venv/bin:$PATH"

COPY ./src/autobots ./src/autobots

## DataDog setup
ARG DD_ENV=default
ENV DD_ENV=$DD_ENV
ARG DD_VERSION=default
ENV DD_VERSION=$DD_VERSION

ENV DD_SERVICE=autobots-scheduler
# DD_AGENT_HOST should match datadog-agent container name
ENV DD_AGENT_HOST=datadog-agent
ENV DD_TRACE_AGENT_PORT=8126

LABEL "com.datadoghq.ad.check_names"='["autobots-scheduler"]'
LABEL "com.datadoghq.ad.init_configs"='[{}]'
LABEL "com.datadoghq.ad.instances"='[{"autobots_scheduler_status_url": "https://%%host%%:%%port%%/v1/hello"}]'
LABEL "com.datadoghq.ad.logs"='[{"source": "autobots-scheduler", "service": "autobots-scheduler"}]'
##

ENTRYPOINT ["uvicorn", "--loop", "asyncio", "src.autobots.scheduler:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1", "--lifespan", "on"]
