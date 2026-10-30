FROM python:3.13-slim

WORKDIR /home/jetoslabs/autobots

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy only the dependency definition files first to leverage Docker's layer caching.
COPY uv.lock pyproject.toml ./

# Install dependencies. We exclude development dependencies to keep the production image lean.
RUN uv sync --frozen --no-dev --no-install-project

ENV PATH="/home/jetoslabs/autobots/.venv/bin:$PATH"

# Copy the application source code into the container.
COPY src ./src

ENV DD_SERVICE=autobots-ingestion-worker

# The ENTRYPOINT instruction starts the Uvicorn web server.
# This makes the worker service's API endpoints available on internal port 8002
# and allows the lifespan manager to start the background Redis polling loop.
ENTRYPOINT ["uvicorn", "--loop", "asyncio", "src.autobots.ingestion_worker:app", "--host", "0.0.0.0", "--port", "8002", "--workers", "4", "--lifespan", "on"]
