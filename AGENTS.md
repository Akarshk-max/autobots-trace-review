# Repository Guidelines

## Project Structure & Module Organization
- Core backend code lives in `src/autobots/` (API, actions, auth, integrations, core services, middleware, ingestion, terminal, UI).
- API entrypoints are under `src/autobots/main.py`, `scheduler.py`, `dashboard_app.py`, `autobots_playwright.py`, and `libreoffice.py`.
- Frontend assets/templates are in `src/autobots/ui/static/` and `src/autobots/ui/templates/`.
- Tests mirror runtime modules under `tests/` (for example `tests/action/`, `tests/api/v1/`, `tests/ingestion/`, `tests/llm/tools/`).
- Infra and CI live in `.github/workflows/`; local build helpers are in `Makefile` and Dockerfiles at repo root.

## Build, Test, and Development Commands
- Install deps: `uv sync`
- Run API in dev mode: `make dev` (uvicorn on `:8000`, reload enabled)
- Run scheduler in dev mode: `make dev-sch` (uvicorn on `:8001`)
- Lint + autofix: `make lint`
- Format: `make format` (`isort` + `ruff format`)
- Run tests: `make test` (pytest with xdist + timeout)
- Full local gate: `make build` (lint + test)
- Nuitka container build/run: `make nuitka-build` then `make nuitka-run`

## Coding Style & Naming Conventions
- Python 3.12, 4-space indentation, max line length 100 (`ruff` config).
- Use `snake_case` for modules/functions/variables, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants/env names.
- Keep modules focused by domain (`conn/`, `api/`, `core/`, `ingestion/`).
- Run `make format` and `make lint` before opening a PR.

## Testing Guidelines
- Framework: `pytest` with `pytest-xdist`, `pytest-timeout`, and async support.
- Prefer test files named `test_*.py`; colocate by domain (e.g., `tests/ingestion/unit/test_*.py`).
- Add regression tests for bug fixes and API behavior changes.
- Quick run example: `uv run pytest tests/api/v1/test_api_hello.py -vv`.

## Commit & Pull Request Guidelines
- Follow existing history style: ticket-prefixed, imperative subjects such as `AUT-1230: Make update non-destructive`.
- Keep commits scoped to one change area; avoid mixing refactor + feature + infra in one commit.
- PRs should include: purpose, changed modules, test evidence (commands/output), and deployment/runtime notes if config or entrypoints changed.

## Security & Configuration Tips
- Copy `.env.sample` to `.env.local`; never commit secrets.
- Many integrations are env-driven; missing variables can start the app with degraded functionality.
