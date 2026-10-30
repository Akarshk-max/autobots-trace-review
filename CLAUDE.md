# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Autobots

AI orchestration SaaS platform that automates goal completion through user-defined action sequences. Uses LLMs (OpenAI, Claude, Gemini) for dynamic text generation, decision-making, and multi-cloud routing across 60+ action types (text, image, audio, video, browser automation).

## Common Commands

```bash
make dev           # Run with hot-reload on port 8000
make run           # Production mode on port 80 (5 workers)
make test          # pytest -n 15 --timeout=300
make lint          # ruff check --fix
make format        # isort + ruff format
make build         # lint + test
make clean         # Remove __pycache__, .pytest_cache, etc.
make dev-sch       # Scheduler service on port 8001 (hot-reload)
```

Run a single test file:
```bash
uv run pytest tests/path/to/test_file.py -vv --timeout=300
```

## Architecture

### Entry Points

- `src/autobots/main.py` - FastAPI app (main API service)
- `src/autobots/scheduler.py` - APScheduler service
- `src/autobots/dashboard_app.py` - Dashboard service
- `src/autobots/ingestion_worker.py` - Background ingestion worker
- `src/autobots/autobots_playwright.py` - Browser automation service
- `src/autobots/libreoffice.py` - Document processing service

### Service Folder Pattern

Each feature folder in `src/autobots/` follows this convention:

```
x/
  x_doc_model.py    # MongoDB + Pydantic models
  x_crud.py         # DB operations
  x.py              # Core business logic
  user_x.py         # Multi-tenant wrapper (service layer)
  x_service.py      # Public contract for other modules
```

Cross-module imports should only use `x_service.py` from other folders.

### Key Modules

- **`core/`** - Settings (Pydantic BaseSettings), lifespan events, database connections (MongoDB via motor, Cloud SQL), Redis, logging (loguru)
- **`api/v1/`** - REST endpoints organized by domain (actions, action_graphs, LLM, files, datastores, schedules, webhooks, teams)
- **`action/`** - Action system: types, execution, results. `action_type/` has 18+ implementations with abstract base in `abc/`
- **`action_graph/`** - DAG-based workflow engine with loop support, conditional execution, configurable concurrency
- **`conn/`** - 37+ external service connectors (AWS, Google, Slack, OpenAI, Claude, Pinecone, Chroma, Neo4j, Selenium, etc.)
- **`llm/tools/`** - Tool definitions for LLM function calling (terminal, file reading, PTY sessions)
- **`terminal/`** - WebSocket-based remote terminal with PTY session management
- **`middleware/`** - Request context injection, usage tracking, exception handling, blacklist checking

### Data Layer

- **MongoDB** (via motor async driver) - Primary data store for user data, actions, graphs
- **Supabase** - Authentication (JWT)
- **Redis** - Session cache, cross-worker sync
- **PostgreSQL** (Cloud SQL) - Relational data
- **Vector DBs** - Pinecone, Chroma, Weaviate for semantic search/RAG

### Multi-Tenancy

User context propagated via `Context` object. `user_*` wrapper classes scope all operations by user_id.

## Configuration

- Environment via `.env.local` (see `.env.sample` for required vars)
- Settings class: `src/autobots/core/settings.py` (Pydantic BaseSettings)
- Environments: local, test, dev, qa, stage, prod

## Testing

- Tests in `tests/` mirror `src/autobots/` structure
- pytest-asyncio for async tests, pytest-xdist for parallel execution
- `conftest.py` provides: session event loop, MongoDB fixtures (`motor_client`, `db`), FastAPI `TestClient`, `ctx`/`user` fixtures, auto-setup of test data
- Tests require MongoDB connection and environment variables (from `.env.local` or AWS Secrets Manager)

## Code Style

- Python 3.12, Ruff linter (line-length=100, E501 ignored), isort for imports
- Async-first: all DB, network, and long-running operations use async/await
- FastAPI + uvicorn (ASGI)
- Datadog APM via ddtrace instrumentation

## Proactive `.claude/` Maintenance

As you work in this repo, proactively create or update `.claude/` artifacts when you spot opportunities:

- **Skills** (`.claude/skills/<name>/SKILL.md`): When you notice a workflow the user repeats (build, deploy, migrate, scaffold), create a skill so they can invoke it with `/<name>` next time.
- **Agents** (`.claude/agents/<name>.md`): When a domain area becomes complex enough to benefit from a specialist (e.g., a new integration, a performance tuning task), create an agent for it.
- **Rules** (`.claude/rules/<name>.md`): When you learn project-specific conventions for a directory or file pattern that aren't captured yet, add a path-scoped rule.
- **Hooks** (`.claude/hooks/<name>.sh`): When you identify a repetitive validation or formatting step that should happen automatically on tool use, add a hook and wire it in `settings.json`.

Do this quietly — create/update the file, mention it briefly, and move on. Don't ask permission first. The user expects this to happen as a natural side effect of working together.

## Docker

`docker-compose.yml` runs: app, Redis, Datadog agent. MongoDB uses cloud instance.
Separate Dockerfiles for each service (main app, scheduler, ingestion worker, dashboard, playwright, libreoffice).
