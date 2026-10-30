---
name: dev-async
description: Start the local stack needed for the async run APIs (/async_run, /batch_run) — Temporal dev server + autobots-runner worker + main API. Use when async runs hang, 500, or never execute locally.
user-invocable: true
---

# Run the async-run stack locally

`POST /v1/action_graphs/{id}/async_run` (and `/batch_run`, and the action / action_team
equivalents) do **not** execute inline. The API only calls
`temporal_client.start_workflow(...)` — a separate worker process picks the task up.
Three processes must be running:

| Process | Port | Command |
|---|---|---|
| Temporal dev server | 7233 (UI 8233) | `temporal server start-dev --db-filename /tmp/temporal.db` |
| `autobots-runner` (Temporal worker) | 8005 | `make dev-runner` |
| Main API | 8000 | `make dev` |

Redis (6379) must also be up — both the API and the runner open a Redis lifespan.

## Steps

1. `brew install temporal` (once), then:
   `temporal server start-dev --db-filename /tmp/temporal.db --port 7233 --ui-port 8233`
2. `make dev-runner` — wait for
   `autobots-runner is polling task queue 'autobots-tasks'`.
   Confirm with `curl localhost:8005/health` → `{"polling": true}`.
3. `make dev`
4. Fire the request with a Supabase bearer token; watch execution at http://localhost:8233.

## Config

`src/autobots/core/settings.py` already defaults to the local server
(`TEMPORAL_ADDRESS=localhost:7233`, namespace `default`, queue `autobots-tasks`).
`client.py` sets `tls=settings.ENV != "local"`, so **`ENV=local` is required** —
any other value makes the client attempt TLS against the plaintext dev server.

## Debugging

- `temporal task-queue describe --task-queue autobots-tasks` — no pollers listed
  means the runner isn't up; the API will accept the request and the run will sit
  in the backlog forever.
- `temporal workflow show --workflow-id action-graph-<result_id>` — workflow ids
  are `action-graph-{action_graph_result_id}`, `action-{...}`, `action-team-{...}`.
- Activity failures surface in the runner log, not the API log.
