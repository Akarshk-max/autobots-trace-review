# syntax=docker/dockerfile:1.6
# --- STAGE 1: The Builder ---
FROM python:3.13-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV NUITKA_CACHE_DIR=/root/.cache/nuitka
ENV CCACHE_DIR=/root/.cache/ccache
ENV CCACHE_MAXSIZE=5G
ENV PATH="/usr/lib/ccache:${PATH}"
ENV MAKEFLAGS="-j16"

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    ccache \
    libpython3-dev \
    binutils \
    patchelf

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /home/jetoslabs/autobots
COPY ./autobots/pyproject.toml ./autobots/uv.lock ./autobots/README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

ENV PATH="/home/jetoslabs/autobots/.venv/bin:$PATH"

COPY ./Nuitka-commercial /tmp/Nuitka-commercial
RUN --mount=type=cache,target=/root/.cache/pip \
    uv pip install /tmp/Nuitka-commercial
RUN python -m nuitka --version

COPY ./autobots/src/autobots ./src/autobots

ARG NUITKA_MODE=standalone
RUN --mount=type=cache,target=/root/.cache/ccache \
    --mount=type=cache,target=/root/.cache/nuitka \
    uv run python -m nuitka \
    --mode=${NUITKA_MODE} \
    --remove-output \
    --include-package=unstructured_client \
    --include-package=deepgram \
    --include-package=weaviate \
    --include-distribution-metadata=protobuf \
    --include-distribution-metadata=grpcio \
    --include-distribution-metadata=anthropic \
    --output-filename=autobots_playwright \
    src/autobots/autobots_playwright.py

RUN mkdir -p /output && \
    if [ -d autobots_playwright.dist ]; then cp -a autobots_playwright.dist/. /output/; \
    else cp autobots_playwright.build/autobots_playwright /output/; fi

# --- STAGE 2: Runtime ---
FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV MAX_JOBS_PER_BROWSER=200
ENV BROWSER_IDLE_RESTART_SECONDS=900

WORKDIR /home/jetoslabs/autobots

COPY --from=builder /output/ ./
COPY --from=builder /home/jetoslabs/autobots/src/autobots/ui ./src/autobots/ui

EXPOSE 8010

ENTRYPOINT ["./autobots_playwright"]
