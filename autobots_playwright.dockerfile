FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

ENV DEBIAN_FRONTEND=noninteractive

#  Install Python 3.13
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        software-properties-common \
        gnupg \
        ca-certificates \
        curl && \
    add-apt-repository -y ppa:deadsnakes/ppa && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        python3.13 \
        python3.13-venv && \
    ln -sf /usr/bin/python3.13 /usr/bin/python && \
    ln -sf /usr/bin/python3.13 /usr/bin/python3 && \
    rm -rf /var/lib/apt/lists/*

#  Install pip for Python 3.13
RUN curl -sS https://bootstrap.pypa.io/get-pip.py | python

# App setup
WORKDIR /home/jetoslabs/autobots

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY uv.lock pyproject.toml ./

RUN uv sync --frozen --no-dev --no-install-project

ENV PATH="/home/jetoslabs/autobots/.venv/bin:$PATH"

COPY src ./src

#  Playwright
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

#  Runtime tuning
ENV MAX_JOBS_PER_BROWSER=200
ENV BROWSER_IDLE_RESTART_SECONDS=900
ENV DD_SERVICE=autobots-playwright

# Uvicorn configuration
ENV UVICORN_HOST=0.0.0.0
ENV UVICORN_PORT=8010
ENV UVICORN_WORKERS=1
ENV UVICORN_LOOP=asyncio
ENV UVICORN_LIFESPAN=on

# Entrypoint
EXPOSE 8010
ENTRYPOINT ["uvicorn", "--loop", "asyncio", "src.autobots.autobots_playwright:app", "--host", "0.0.0.0", "--port", "8010", "--workers", "1", "--lifespan", "on"]
