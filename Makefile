.PHONY: help lint dry-lint format test build clean openrouter-models precommit run dev run-sch dev-sch run-runner dev-runner \
       nuitka-build nuitka-run nuitka-clean-all \
       nuitka-build-app nuitka-build-dashboard nuitka-build-scheduler \
       nuitka-build-ingestion-worker nuitka-build-playwright nuitka-build-libreoffice nuitka-build-runner \
       nuitka-build-all nuitka-up nuitka-down \
       nuitka-clean-app nuitka-clean-dashboard nuitka-clean-scheduler \
       nuitka-clean-ingestion-worker nuitka-clean-playwright nuitka-clean-libreoffice nuitka-clean-runner \
       nuitka-local-install nuitka-local-version \
       nuitka-local-app nuitka-local-dashboard nuitka-local-scheduler \
       nuitka-local-ingestion-worker nuitka-local-playwright nuitka-local-libreoffice nuitka-local-runner \
       nuitka-local-all nuitka-local-clean \
       nuitka-local-run-app nuitka-local-run-dashboard nuitka-local-run-scheduler \
       nuitka-local-run-ingestion-worker nuitka-local-run-playwright nuitka-local-run-libreoffice nuitka-local-run-runner

help:
	@echo "Makefile commands:"
	@echo "  make install     - Install dependencies using uv"
	@echo "  make run         - Run FastAPI app with Uvicorn"
	@echo "  make lint        - Run Ruff linter with auto-fix"
	@echo "  make format      - Format code using Ruff and isort"
	@echo "  make test        - Run tests using pytest"
	@echo "  make build       - Lint + Test"
	@echo "  make clean       - Remove cache files"
	@echo "  make dev         - Run Uvicorn with auto-reload"
	@echo "  make precommit   - Run all pre-commit hooks"
	@echo "  make run-sch        - Run scheduler app with Uvicorn"
	@echo "  make dev-sch        - Run scheduler app with Uvicorn + reload"
	@echo "  make run-runner     - Run Temporal runner"
	@echo "  make dev-runner     - Run Temporal runner (poll loop, no reload needed)"
	@echo "  make nuitka-build              - Build Nuitka standalone Docker image (BuildKit)"
	@echo "  make nuitka-run                - Run the Nuitka image on port 8000"
	@echo "  make nuitka-clean-all          - Remove all Nuitka images and dangling build cache"
	@echo "  make nuitka-clean-app          - Remove Nuitka app image"
	@echo "  make nuitka-clean-dashboard    - Remove Nuitka dashboard image"
	@echo "  make nuitka-clean-scheduler    - Remove Nuitka scheduler image"
	@echo "  make nuitka-clean-ingestion-worker - Remove Nuitka ingestion worker image"
	@echo "  make nuitka-clean-playwright   - Remove Nuitka playwright image"
	@echo "  make nuitka-clean-libreoffice  - Remove Nuitka libreoffice image"
	@echo "  make nuitka-build-app          - Build Nuitka image for main app"
	@echo "  make nuitka-build-dashboard    - Build Nuitka image for dashboard"
	@echo "  make nuitka-build-scheduler    - Build Nuitka image for scheduler"
	@echo "  make nuitka-build-ingestion-worker - Build Nuitka image for ingestion worker"
	@echo "  make nuitka-build-playwright   - Build Nuitka image for playwright"
	@echo "  make nuitka-build-libreoffice  - Build Nuitka image for libreoffice"
	@echo "  make nuitka-build-all          - Build all Nuitka images sequentially"
	@echo "  make nuitka-up                 - Start all Nuitka services with Redis"
	@echo "  make nuitka-down               - Stop all Nuitka services"
	@echo ""
	@echo "Local Nuitka binary builds (requires Nuitka-commercial as sibling dir):"
	@echo "  make nuitka-local-install       - Install Nuitka-commercial into uv venv"
	@echo "  make nuitka-local-version       - Print installed Nuitka version"
	@echo "  make nuitka-local-app           - Compile main app binary"
	@echo "  make nuitka-local-dashboard     - Compile dashboard binary"
	@echo "  make nuitka-local-scheduler     - Compile scheduler binary"
	@echo "  make nuitka-local-ingestion-worker - Compile ingestion worker binary"
	@echo "  make nuitka-local-playwright    - Compile playwright binary"
	@echo "  make nuitka-local-libreoffice   - Compile libreoffice binary"
	@echo "  make nuitka-local-all           - Compile all binaries sequentially"
	@echo "  make nuitka-local-run-app       - Run compiled main app binary"
	@echo "  make nuitka-local-run-dashboard - Run compiled dashboard binary"
	@echo "  make nuitka-local-run-scheduler - Run compiled scheduler binary"
	@echo "  make nuitka-local-run-ingestion-worker - Run compiled ingestion worker binary"
	@echo "  make nuitka-local-run-playwright - Run compiled playwright binary"
	@echo "  make nuitka-local-run-libreoffice - Run compiled libreoffice binary"
	@echo "  make nuitka-local-clean         - Remove local Nuitka build artifacts"

lint:
	uv run ruff check --fix .

dry-lint:
	uv run ruff check .

format:
	uv run isort .
	uv run ruff format .

test:
	uv run pytest tests -n 15 --timeout=300 --durations=25
	#uv run pytest -vv -n 5 --timeout=180

# Real Mongo and real provider APIs. Slow, and costs money.
test-integration:
	uv run pytest integration_tests -n 15 --timeout=300 --durations=25

test-all:
	uv run pytest tests integration_tests -n 15 --timeout=300 --durations=25

# Every test's duration, slowest first - use this to decide what belongs in
# integration_tests. `--durations=0` reports all of them, not just the top N.
test-timing:
	uv run pytest tests -n 15 --timeout=300 --durations=0

build: lint test

openrouter-models:
	uv run python -m src.autobots.conn.openrouter.generate_openrouter_models

clean:
	find . -type d -name "__pycache__" -exec rm -r {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov dist

precommit:
	uv run pre-commit run --all-files

run:
	uv run uvicorn --loop asyncio src.autobots.main:app --host 0.0.0.0 --port 80 --workers 1 --lifespan on

dev:
	uv run uvicorn --loop asyncio src.autobots.main:app --host 0.0.0.0 --port 8000 --workers 3 --lifespan on --reload

run-sch:
	uv run uvicorn --loop asyncio src.autobots.scheduler:app --host 0.0.0.0 --port 8001 --workers 1 --lifespan on

dev-sch:
	uv run uvicorn --loop asyncio src.autobots.scheduler:app --host 0.0.0.0 --port 8001 --workers 1 --lifespan on --reload

run-runner:
	uv run uvicorn --loop asyncio src.autobots.runner:app --host 0.0.0.0 --port 8005 --workers 1 --lifespan on

dev-runner:
	uv run uvicorn --loop asyncio src.autobots.runner:app --host 0.0.0.0 --port 8005 --workers 1 --lifespan on --reload

NUITKA_IMAGE ?= autobots-nuitka
NUITKA_DOCKERFILE ?= nuitka.dockerfile

nuitka-build:
	DOCKER_BUILDKIT=1 docker build -f $(NUITKA_DOCKERFILE) -t $(NUITKA_IMAGE) ..

nuitka-run:
	docker run --rm -p 8000:8000 $(NUITKA_IMAGE)

nuitka-clean-all:
	-docker rmi $(NUITKA_IMAGE) autobots-nuitka-app autobots-nuitka-dashboard autobots-nuitka-scheduler autobots-nuitka-ingestion-worker autobots-nuitka-playwright autobots-nuitka-libreoffice autobots-nuitka-runner
	docker builder prune -f

nuitka-clean-app:
	-docker rmi autobots-nuitka-app

nuitka-clean-dashboard:
	-docker rmi autobots-nuitka-dashboard

nuitka-clean-scheduler:
	-docker rmi autobots-nuitka-scheduler

nuitka-clean-ingestion-worker:
	-docker rmi autobots-nuitka-ingestion-worker

nuitka-clean-playwright:
	-docker rmi autobots-nuitka-playwright

nuitka-clean-libreoffice:
	-docker rmi autobots-nuitka-libreoffice

nuitka-clean-runner:
	-docker rmi autobots-nuitka-runner

# --- Per-service Nuitka builds ---
# Build context is parent dir (..) so both autobots/ and Nuitka-commercial/ are accessible

nuitka-build-app:
	DOCKER_BUILDKIT=1 docker build --build-arg NUITKA_MODE=$(NUITKA_MODE) -f nuitka.dockerfile -t autobots-nuitka-app ..

nuitka-build-dashboard:
	DOCKER_BUILDKIT=1 docker build --build-arg NUITKA_MODE=$(NUITKA_MODE) -f nuitka.dashboard.dockerfile -t autobots-nuitka-dashboard ..

nuitka-build-scheduler:
	DOCKER_BUILDKIT=1 docker build --build-arg NUITKA_MODE=$(NUITKA_MODE) -f nuitka.scheduler.dockerfile -t autobots-nuitka-scheduler ..

nuitka-build-ingestion-worker:
	DOCKER_BUILDKIT=1 docker build --build-arg NUITKA_MODE=$(NUITKA_MODE) -f nuitka.ingestion-worker.dockerfile -t autobots-nuitka-ingestion-worker ..

nuitka-build-playwright:
	DOCKER_BUILDKIT=1 docker build --build-arg NUITKA_MODE=$(NUITKA_MODE) -f nuitka.autobots_playwright.dockerfile -t autobots-nuitka-playwright ..

nuitka-build-libreoffice:
	DOCKER_BUILDKIT=1 docker build --build-arg NUITKA_MODE=$(NUITKA_MODE) -f nuitka.libreoffice.dockerfile -t autobots-nuitka-libreoffice ..

nuitka-build-runner:
	DOCKER_BUILDKIT=1 docker build --build-arg NUITKA_MODE=$(NUITKA_MODE) -f nuitka.autobots_runner.dockerfile -t autobots-nuitka-runner ..

# Build all nuitka images sequentially (Nuitka compilation is CPU-heavy, parallel would thrash)
nuitka-build-all: nuitka-build-app nuitka-build-dashboard nuitka-build-scheduler nuitka-build-ingestion-worker nuitka-build-playwright nuitka-build-libreoffice nuitka-build-runner

# Start/stop all Nuitka services with Redis
nuitka-up:
	docker compose -f docker-compose.nuitka.yml up -d

nuitka-down:
	docker compose -f docker-compose.nuitka.yml down

# --- Local Nuitka binary builds ---
# Builds standalone binaries on the local machine using Nuitka-commercial.
# Assumes Nuitka-commercial is a sibling directory (../Nuitka-commercial).

NUITKA_COMMERCIAL_DIR ?= ../Nuitka-commercial
NUITKA_MODE ?= standalone
NUITKA_FLAGS = --mode=$(NUITKA_MODE) --remove-output \
    --include-package=unstructured_client \
    --include-package=deepgram \
    --include-package=weaviate \
    --include-distribution-metadata=protobuf \
    --include-distribution-metadata=grpcio \
    --include-distribution-metadata=anthropic

nuitka-local-install:
	uv run pip install $(NUITKA_COMMERCIAL_DIR)
	uv run python -m nuitka --version

nuitka-local-version:
	uv run python -m nuitka --version

nuitka-local-app:
	uv run python -m nuitka $(NUITKA_FLAGS) --output-filename=autobots_app src/autobots/main.py

nuitka-local-dashboard:
	uv run python -m nuitka $(NUITKA_FLAGS) --output-filename=autobots_dashboard src/autobots/dashboard_app.py

nuitka-local-scheduler:
	uv run python -m nuitka $(NUITKA_FLAGS) --output-filename=autobots_scheduler src/autobots/scheduler.py

nuitka-local-ingestion-worker:
	uv run python -m nuitka $(NUITKA_FLAGS) --output-filename=autobots_ingestion_worker src/autobots/ingestion_worker.py

nuitka-local-playwright:
	uv run python -m nuitka $(NUITKA_FLAGS) --output-filename=autobots_playwright src/autobots/autobots_playwright.py

nuitka-local-libreoffice:
	uv run python -m nuitka $(NUITKA_FLAGS) --output-filename=autobots_libreoffice src/autobots/libreoffice.py

nuitka-local-runner:
	uv run python -m nuitka $(NUITKA_FLAGS) --output-filename=autobots_runner src/autobots/runner.py

nuitka-local-all: nuitka-local-app nuitka-local-dashboard nuitka-local-scheduler nuitka-local-ingestion-worker nuitka-local-playwright nuitka-local-libreoffice nuitka-local-runner

# Run compiled binaries locally
nuitka-local-run-app:
	./main.dist/autobots_app

nuitka-local-run-dashboard:
	./dashboard_app.dist/autobots_dashboard

nuitka-local-run-scheduler:
	./scheduler.dist/autobots_scheduler

nuitka-local-run-ingestion-worker:
	./ingestion_worker.dist/autobots_ingestion_worker

nuitka-local-run-playwright:
	./autobots_playwright.dist/autobots_playwright

nuitka-local-run-libreoffice:
	./libreoffice.dist/autobots_libreoffice

nuitka-local-run-runner:
	./runner.dist/autobots_runner

nuitka-local-clean:
	rm -rf main.dist main.build
	rm -rf dashboard_app.dist dashboard_app.build
	rm -rf scheduler.dist scheduler.build
	rm -rf ingestion_worker.dist ingestion_worker.build
	rm -rf autobots_playwright.dist autobots_playwright.build
	rm -rf libreoffice.dist libreoffice.build
	rm -rf runner.dist runner.build
