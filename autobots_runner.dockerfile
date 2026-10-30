FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive

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

ARG DD_ENV=default
ENV DD_ENV=$DD_ENV
ARG DD_VERSION=default
ENV DD_VERSION=$DD_VERSION

ENV DD_SERVICE=autobots-runner
ENV DD_AGENT_HOST=datadog-agent
ENV DD_TRACE_AGENT_PORT=8126

ENV DD_TRACE_OPENAI_ENABLED=false

EXPOSE 8005

LABEL "com.datadoghq.ad.check_names"='["autobots-runner"]'
LABEL "com.datadoghq.ad.init_configs"='[{}]'
LABEL "com.datadoghq.ad.instances"='[{"autobots_runner_status_url": "http://%%host%%:%%port%%/health"}]'
LABEL "com.datadoghq.ad.logs"='[{"source": "autobots-runner", "service": "autobots-runner"}]'

# --workers 1 is required, not a tuning choice: each uvicorn worker runs the
# lifespan, so more workers means more Temporal pollers on the same task queue.
ENTRYPOINT ["ddtrace-run", "uvicorn", "--loop", "asyncio", "src.autobots.runner:app", "--host", "0.0.0.0", "--port", "8005", "--workers", "1", "--lifespan", "on"]
