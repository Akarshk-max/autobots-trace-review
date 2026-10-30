# Autobots + Temporal — Execution Architecture

> Knowledge base for how **Action**, **Action Graph** and **Action Team** runs flow from the
> API container (`autobots`) through Temporal into the worker container (`autobots-runner`).
>
> Written against branch `AUT-1578`. Line references are approximate anchors, not contracts.

---

## 1. Service topology

There is **one codebase, many entrypoints**. Every container ships the same `src/autobots`
tree and differs only in which ASGI app / process it boots.

| Container | Entrypoint | Port | Role |
|---|---|---|---|
| `autobots` | `src/autobots/main.py` | 8000 (80 in prod) | Public REST API. Auth, CRUD, permissions, result reads, SSE. **Dispatches runs, does not execute them.** |
| `autobots-runner` | `src/autobots/runner.py` | 8005 | Temporal worker. Polls the task queue and *actually executes* actions, graphs and teams. |
| `autobots-scheduler` | `src/autobots/scheduler.py` | 8001 | APScheduler. **Still executes in-process — bypasses Temporal (see §8).** |
| `autobots-ingestion-worker` | `src/autobots/ingestion_worker.py` | — | Redis-queue ingestion jobs. Unrelated to Temporal. |
| `autobots-dashboard` | `src/autobots/dashboard_app.py` | — | Dashboard API. |
| `autobots-playwright` / `libreoffice` | dedicated apps | — | Sidecar capability services called by action types. |

Shared infrastructure: **MongoDB** (all state), **Redis** (SSE pub/sub, caches, heartbeat topic),
**Supabase** (JWT auth), **Temporal** (durable execution).

### The runner is a FastAPI app that exists mostly to hold a worker

`src/autobots/runner.py` is deliberately thin: `api_hello` plus a `/health` endpoint. The real
work happens in the lifespan:

```
src/autobots/core/lifespan_runner.py:50
    worker = await create_worker()
    worker_task = asyncio.create_task(worker.run())
    app.state.worker_task = worker_task
```

`/health` returns **503 when the poll loop has died** (`runner.py:26-37`) — otherwise the process
would keep serving 200s while consuming nothing from the task queue. The Dockerfile pins
`--workers 1` and comments that it is *required, not tuning*: each uvicorn worker runs the
lifespan, so N workers means N pollers on the same queue.

---

## 2. The three runnables

### 2.1 Action — the unit of work

An **Action** is a typed, configured invocation of one capability (an LLM call, a scrape, a
transcription, an API call…). There are ~130 action types in `ActionType`
(`src/autobots/action/action_type/action_types.py`).

Every action type implements `ActionABC` (`action/action_type/abc/ActionABC.py`), a generic over
five Pydantic types:

```
ActionABC[ConfigCreate, ConfigUpdate, Config, Input, Output]
```

with the contract:

- `get_config_create_type() / get_config_update_type() / get_config_type()`
- `get_input_type() / get_output_type()` — these two drive JSON-schema exposure to the UI and to LLM tool-calling
- `create_config / update_config / delete_config` — lifecycle hooks (default: pass-through)
- `update_config_with_prev_results(ctx, app_state, curr_config, prev_results)` — lets an action fold its own history into config (this is how chat actions accumulate messages)
- `async run_action(ctx, app_state, action_input, notify) -> Output | Exception`
- `async run_tool(...)` — **signature is load-bearing**; it is reflected on to generate LLM function definitions

`ACTION_MAP` (`action_type/action_map.py`) maps `ActionType → class`. `ActionFactory` is the
only thing that touches `ACTION_MAP`.

**Two persisted shapes:**

- `ActionDoc` (collection `Actions`) — the template: `type`, `config`, `results[]`, `input`, `output`, sharing flags.
- `ActionResultDoc` (an `EventResult`) — one *run*: `status`, `error_message`, `result` (a full embedded snapshot of the `ActionDoc`), `queued_inputs`, `batch_id`.

The embedded snapshot matters: **a run carries its own copy of the action**, so editing the
template mid-flight cannot corrupt an in-progress run, and reruns replay exactly what ran.

`ActionFactory.run_action` (`action_factory.py:164`) is the pure execution core:

```
config   = action.get_config_type().model_validate(action_doc.config)
config   = await action.update_config_with_prev_results(ctx, app_state, config, action_doc.results)
input    = action.get_input_type().model_validate(action_input_dict)
output   = await action(config, user, db).run_action(ctx, app_state, input, notify)
→ RunActionObj(config_dict, input_dict, output_dict, status)
```

Note it reads config back off `action_to_run.action_config` (not the local variable) because some
actions mutate their config during the run, and it excludes `files` from the persisted config to
avoid writing raw multipart bytes into Mongo.

### 2.2 Action Graph — a DAG of nodes over actions

`ActionGraphDoc` (collection `ActionGraphs`) holds:

- `nodes: Dict[node_id, action_id]`
- `node_details: Dict[node_id, Node]` — UI position plus the *behavioral* `NodeData`
- `graph: Dict[node_id, List[node_id]]` — forward adjacency
- `output: Dict[node_id, ActionDoc]` — per-node result snapshot, accumulated during the run

`NodeData` (`action_graph_doc_model.py:28`) is where most graph semantics live:

| Field | Meaning |
|---|---|
| `data_flow_type` | `only_output` / `both_input_output` / `as_is_last_output` — what upstream data reaches this node |
| `is_loop`, `is_split`, `seperator` | fan the upstream text out on a separator and run the node once per piece |
| `file_split`, `excel_loop`, `api_loop`, `batches` | other fan-out modes (per file, per spreadsheet row, per API page) |
| `header`, `file_column_index`, `instruction_column_index` | spreadsheet-loop column wiring |
| `user_review_required` / `user_review_done` | pause the graph after this node until a human approves |
| `user_input_required` / `user_input_done` | pause the graph *before* this node until a human supplies input |
| `sleep_time` | seconds to sleep before executing (rate-limit escape hatch) |
| `process_url_in_text`, `process_attachment_url`, `attachment_requested` | input-preprocessing toggles |

**Scheduling.** `run_action_graph_v2` (`action_graph.py:308`) inverts the adjacency map and runs
**Coffman–Graham layering** (`coffman_graham.py`) over the *inverted* graph to get
`List[List[node_id]]` — a list of layers. It then walks layers in order and nodes within a layer.

Two guards decide whether a node executes (`action_graph.py:~372`):

```
if is_work_done([node]) or not is_work_done(upstream_nodes): continue
```

i.e. **skip if already computed, skip if any parent is missing from `output`**. This is what makes
rerun/continue cheap: emptying a node's downstream entries from `output` is sufficient to schedule
exactly that subtree for recomputation (`empty_downstream_nodes`, `action_graph.py:1013`).

Dispatch per node:

| Situation | Handler |
|---|---|
| no upstream, plain | `run_action_simple` |
| no upstream + `is_loop` | `run_action_with_text_split_and_loop_concurrently` |
| no upstream + `excel_loop` | `run_action_with_excel_loop` |
| no upstream + `is_loop` + `file_split` | `run_action_with_file_split_and_loop_concurrently` |
| has upstream + `is_loop` | `run_action_with_loop` → `run_action_with_loop_concurrently` |
| has upstream, plain | `run_action_with_merge` |

Input construction for a node with parents goes through `ActionGraphInputCreator.to_input`, which
takes the full `NodeData` flag set. There is also an **LLM fallback**, `ActionGraph.gen_input`
(`action_graph.py:946`): given the upstream output JSON schemas and the current node's input
schema, it asks `gpt-4.1-mini` (Azure) to synthesize a conforming input object. Multiple parent
outputs are merged by `collate_action_results` (`action_graph.py:906`), which concatenates
`output["blocks"]` and side-loads extra inputs as `text_0`, `text_1`, ….

`to_wait` is the pause flag: if any node needs user input, or any parent needs unfinished review,
the graph finishes the pass and lands in status **`waiting`** rather than `success`.

Three entry modes exist, distinguished by the workflow payload:

- **fresh / resume** — `run_action_graph_v2` with no node id
- **rerun** — `is_rerun_graph=True`; downstream outputs are emptied first, then a full pass
- **continue from node** — `is_continue_run_graph=True` + node id; `get_subgraph_map` BFS's the subtree, layers only that, and skips the root itself
- **single node** — node id with neither rerun nor continue → `run_node` (`action_graph.py:602`)

### 2.3 Action Team — a leader/member delegation loop

`ActionTeamDoc` (collection `ActionTeams`):

- `members: List[TeamMember]` — each with `member_id`, `action_id`, `role` (exactly **one** `leader`, validated in `ActionTeamCreate`), `name`, `description`, `action_run_ids[]`
- `max_iterations: int = 50`
- `thread: List[ThreadEntry]` — the conversation transcript (`user` / `leader` / `member` turns, each with input+output)
- `current_active_members: List[Dict[member_id, action_run_id]]` — live "who is working right now"

`ActionTeam._execute_team_loop` (`action_team.py:252`) is a plain `for iteration in range(max_iterations + 1)` loop:

1. `_build_leader_input` — original input + rendered thread + member roster + iteration counter
2. run the **leader action** via `ActionGraph.run_action`
3. append the leader turn to `thread`, persist
4. `_parse_leader_response` → `LeaderDecision(is_complete, summary, delegations[])`, parsed out of JSON blocks in the leader's text
5. if `is_complete` → append a final-summary turn and **break**
6. otherwise for each `Delegation(member_id, member_action_id, member_action_run_id, task)`:
   - build member input from the delegation task + thread
   - resolve the member's `ActionDoc` (reusing `member_action_run_id` if the leader is continuing an existing member run)
   - execute via `ActionFactory.run_action_in_background(..., background_tasks=None)` — **which awaits inline**, so members run sequentially inside the team activity
   - append the member turn, record `action_run_ids`, clear `current_active_members`, persist
7. on the second-to-last iteration, inject a synthetic user turn telling the leader to wrap up

Terminal status: `success` only if the leader declared completion; otherwise **`error`** with
`"Max iterations reached without leader completion"`.

Note the nesting: a team member is a full Action run with its own `ActionResultDoc`, linked back
up through `AppState.ancestors`.

---

## 3. The Temporal wrapper

Everything Temporal-specific lives under `src/autobots/conn/temporal/`, mirroring the
service-folder convention:

```
conn/temporal/
  client.py            # lazy singleton Client
  worker.py            # registers the 3 workflows + 3 activities
  converter.py         # Pydantic <-> Payload
  ctx_utils.py         # rebuild Context inside workflow/activity
  workflow_retry.py    # custom retry-only-on-heartbeat-timeout helper
  action/       {shared.py, workflow.py, activity.py}
  action_graph/ {shared.py, workflow.py, activity.py}
  action_team/  {shared.py, workflow.py, activity.py}
  schedule/     # empty placeholder
```

`temporalio==1.31.0` (`pyproject.toml:99`).

### 3.1 Client

`get_temporal_client()` (`client.py`) is a double-checked-lock async singleton:

```python
Client.connect(
    settings.TEMPORAL_ADDRESS,      # default localhost:7233
    namespace=settings.TEMPORAL_NAMESPACE,   # default "default"
    api_key=settings.TEMPORAL_API_KEY,
    tls=settings.ENV != "local",
    data_converter=get_temporal_data_converter(),
)
```

Task queue: `settings.TEMPORAL_TASK_QUEUE`, default `"autobots-tasks"` (`core/settings.py:91-94`).
One queue for all three runnable types.

### 3.2 Data converter

`PydanticPayloadConverter` (`converter.py`) wraps the default converter:

- **out**: any `BaseModel` → `model_dump()` → default JSON encoding
- **in**: if the type hint is a `BaseModel` subclass → decode to `dict` → `model_validate`

This is what lets `start_workflow(Workflow.run, SomePydanticModel(...))` and the typed
`async def run(self, payload: ActionWorkflowInput)` line up on both sides.

### 3.3 Worker

```python
Worker(
    client,
    task_queue=settings.TEMPORAL_TASK_QUEUE,
    workflows=[ActionWorkflow, ActionGraphWorkflow, ActionTeamWorkflow],
    activities=[execute_action_activity, execute_action_graph_activity, execute_action_team_activity],
    workflow_runner=UnsandboxedWorkflowRunner(),
)
```

`UnsandboxedWorkflowRunner` disables Temporal's determinism sandbox. It is necessary here because
the workflow modules transitively import the whole Autobots dependency tree — but it also means
**nothing stops non-deterministic code from running in workflow context** (see §9).

### 3.4 Workflow / activity split

The split is uniform across all three and deliberately minimal:

**Workflow** (durable, replayed): build `Context` + `AppState`, register in the heartbeat
registry, invoke exactly one activity, deregister in `finally`. No fan-out, no child workflows,
no signals, no queries.

**Activity** (the real work): open Mongo, load the user + result doc, run the engine wrapped in a
heartbeat pump, persist, fire webhooks.

```python
async def _run_with_heartbeat(coro):        # identical in all 3 activities
    task = asyncio.ensure_future(coro)
    try:
        while not task.done():
            activity.heartbeat("executing")
            await asyncio.wait({task}, timeout=15)   # HEARTBEAT_INTERVAL_SECONDS
        return task.result()
    except (CancelledError, asyncio.CancelledError):
        task.cancel(); ...; raise CancelledError("Activity cancelled during execution")
```

### 3.5 Retry policy — heartbeat-timeout only

`workflow_retry.py` implements a deliberately narrow policy:

```python
await workflow.execute_activity(
    activity_fn, payload,
    start_to_close_timeout=...,
    heartbeat_timeout=...,
    retry_policy=RetryPolicy(maximum_attempts=1),   # Temporal retries DISABLED
)
```

…and then retries **in the workflow** only when the failure is
`ActivityError → TimeoutError(type=HEARTBEAT)`, up to `max_attempts=3`, with deterministic
exponential backoff (`2s → 4s`, capped at 60s) via `workflow.sleep`.

The rationale in the docstring: Temporal's built-in retry fires on many failure classes, but
Autobots only wants to retry the *"worker died / went unresponsive"* class — and since every long
activity heartbeats, that class reliably surfaces as a heartbeat timeout. A business failure (bad
LLM response, 4xx from a provider) must **not** be replayed. Cancellation is never retried.

**Timeouts in effect:**

| Runnable | start_to_close | heartbeat_timeout | max attempts |
|---|---|---|---|
| Action | 10 hours (hardcoded, `action/workflow.py:40`) | 60s | 3 |
| Action Graph | `ACTION_GRAPH_START_TO_CLOSE_TIMEOUT_MINUTES` = **1200 min / 20 h** | 60s | 3 |
| Action Team | same setting, 1200 min | 60s | 3 |

Heartbeats are emitted every 15s against a 60s timeout — 4× margin.

### 3.6 Workflow inputs

| Model | Fields |
|---|---|
| `ActionWorkflowInput` | `action_id`, `action_run_id`, `user_id`, `trace_id`, `consumer_id`, `access_token`, `input`, `webhook` |
| `ActionGraphWorkflowInput` | `action_graph_id`, `action_graph_result_id`, `user_id`, `trace_id`, `access_token`, `input`, `webhook`, `action_graph_node_id`, `is_rerun_graph`, `is_continue_run_graph`, `is_empty_downstream_only`, `batch_id`, `start_to_close_timeout_minutes` |
| `ActionTeamWorkflowInput` | `action_team_id`, `action_team_result_id`, `user_id`, `trace_id`, `access_token`, `input`, `webhook`, `batch_id`, `start_to_close_timeout_minutes` |

**Workflow IDs are derived from the result-doc id**, which makes them stable and idempotent:

```
action-{action_result_id}
action-graph-{action_graph_result_id}
action-team-{action_team_result_id}
```

Two dispatches for the same result doc collide on workflow id rather than double-running.

---

## 4. End-to-end: Action run

```
POST /v1/actions/{id}/async_run          (api_action.py:487)
 └─ autobots container
    ├─ auth (Supabase JWT) + require_permission(Agents, Execute)
    ├─ resolve ActionDoc: space-admin lookup → owned/shared/system lookup
    ├─ if an existing result is already `processing` → append input to queued_inputs, return early
    ├─ UserActionResult.init(...)  → ActionResultDoc(status=processing), LLM-generated title
    ├─ bind ctx.consumer_id / app_state.action_run_id
    └─ temporal_client.start_workflow(
           ActionWorkflow.run, ActionWorkflowInput(...),
           id=f"action-{result.id}", task_queue=TEMPORAL_TASK_QUEUE)
    → returns ActionResultDoc(status=processing) immediately   [HTTP 200]

 ↓ Temporal task queue "autobots-tasks"

 autobots-runner container
 ├─ ActionWorkflow.run
 │   ├─ build_ctx(user_id, trace_id, consumer_id or action_run_id)
 │   ├─ HeartBeatRunRegistry.register(...)
 │   ├─ execute_activity_retry_on_heartbeat_timeout(execute_action_activity, 10h, 60s, 3)
 │   └─ finally: HeartBeatRunRegistry.deregister(...)
 └─ execute_action_activity
     ├─ db = next(get_mongo_db())
     ├─ load UserORM, re-attach payload.access_token   ← downstream actions call back into the API as the user
     ├─ load ActionResultDoc; fall back to fetching the ActionDoc if the snapshot is missing
     ├─ activity.is_cancelled() guard
     ├─ notify = Notify(topic=action_run_id)           ← Redis pub/sub → SSE
     ├─ _run_single_action(...)
     │    └─ _run_with_heartbeat(ActionFactory.run_action(...))
     │        ├─ success → status/config/input/output written onto the doc; results[-1] updated
     │        ├─ error   → status=error, error_message=str(e), re-raise
     │        └─ finally → update_action_result(exclude={'queued_inputs'}) + webhook.send()
     └─ _process_queued_inputs(...)  → drains queued_inputs sequentially, same path per input
```

**The `queued_inputs` mechanism** is how a "chat with a running agent" works: while a run is
`processing`, further inputs are appended to `ActionResultDoc.queued_inputs` by the API
(`api_action.py:~540`) instead of starting a second workflow. The activity drains that queue in a
loop after the primary input completes. Every persist during a run **excludes `queued_inputs`**
(`ActionFactory._EXCLUDED_FIELDS_FROM_UPDATE_DURING_RUN`) precisely so a concurrent enqueue from
the API is not clobbered by the runner's write.

**Other action entrypoints, all identical in shape:**

- `POST /{id}/async_run_loop` — a list of inputs → one result doc + one workflow **per input**
- `POST /async_run` — run an inline `ActionCreate` (unsaved action)
- `POST /{id}/batch_run` — `batch_run_action_in_background` splits `input.urls` into batches, creates a `Batch` doc and one result doc per batch (it **only creates docs** despite the name), then the endpoint starts one workflow per result doc

---

## 5. End-to-end: Action Graph run

```
POST /v1/action_graphs/{id}/async_run       (api_action_graphs.py:253)
 └─ UserActionGraphs.run_in_background(..., temporal_client=<client>)   [user_action_graph.py:654]
    ├─ if temporal_client:                     ← the branch that makes this Temporal-backed
    │    ├─ user_action_graph_result.init(...) → ActionGraphResultDoc
    │    ├─ ActionGraphWorkflowInput(..., start_to_close_timeout_minutes=settings.…)
    │    └─ start_workflow(id=f"action-graph-{result.id}")
    └─ else: ActionGraph.run_in_background(...)   ← legacy in-process path, still reachable

 ↓

 execute_action_graph_activity
 ├─ load user (+access_token), UserActions, UserActionGraphResult
 ├─ load ActionGraphResultDoc (scrub_ip=False)
 ├─ if node_id and is_rerun   → ActionGraph.empty_downstream_nodes(...)
 ├─ if no node_id and is_rerun→ result.output = {}
 ├─ node_id and not rerun and not continue → _run_with_heartbeat(ActionGraph.run_node(...))
 ├─ otherwise                              → _run_with_heartbeat(ActionGraph.run_action_graph_v2(...))
 ├─ on exception: mark result error + webhook, re-raise
 └─ if result.status == error: raise HTTPException(500)   ← makes the activity (and workflow) fail
```

`POST /{id}/batch_run` wraps this: `batch_run_in_background` splits `input.urls` by
`RUNNABLE_BATCH_LIMIT_FOR_URLS` (default 100), creates a `Batch` doc for tracking, and starts
**one workflow per batch**, each titled `"{n}/{total} {title}"`.

**Architecturally important:** the *entire graph* — every layer, every node, every loop fan-out —
runs inside **one Temporal activity on one runner pod**. Node concurrency is `asyncio.gather`
inside that activity, not Temporal-level fan-out. Temporal provides durability and worker-loss
recovery at graph granularity, not node granularity. A retry re-runs the activity from the top
(cheaply, because completed nodes are already in `result.output` and get skipped by `is_work_done`).

---

## 6. End-to-end: Action Team run

```
POST /v1/action_teams/{id}/async_run        (api_action_teams.py:~239)
 └─ UserActionTeams.run_in_background(..., temporal_client=<client>)   [user_action_team.py:236]
    ├─ user_action_team_result.init(...) → ActionTeamResultDoc
    └─ start_workflow(ActionTeamWorkflow.run, id=f"action-team-{result.id}")

 ↓

 execute_action_team_activity
 └─ _run_with_heartbeat(ActionTeam._execute_team_loop(...))
     └─ per iteration: leader action → parse → member actions (inline await) → persist
```

Nesting, in one picture:

```
ActionTeamWorkflow                      (Temporal)
└── execute_action_team_activity        (Temporal)
    └── _execute_team_loop              (plain Python loop)
        ├── leader ActionDoc run        (ActionGraph.run_action → ActionFactory)
        └── member ActionDoc runs       (ActionFactory.run_action_in_background, awaited inline)
            └── each produces its own ActionResultDoc, linked via AppState.ancestors
```

Member runs do **not** get their own workflows. Everything is one activity.

---

## 7. Cross-cutting mechanics

### 7.1 Context and AppState

Two orthogonal carriers, both rebuilt inside the runner from the workflow payload:

- **`Context`** (`data_model/context.py`) — *who and which trace*: `trace_id`, `user_id`, `consumer_id`, `action_run_name`. Rebuilt by `build_ctx()` (`conn/temporal/ctx_utils.py`), which normalizes empty strings to `None` and lets `trace_id` default-generate if absent.
- **`AppState`** (`data_model/app_state.py`) — *what is running*: action / graph / team ids and run ids, node id, team member id, plus `parent_*` fields and an `ancestors: list[dict]` chain. `app_type` is a computed field deriving `AppCode` from whichever ids are set. `child_context(**overrides)` pushes the current ids onto `ancestors` and returns a fresh child — this is how a team-member action knows it belongs to a team run, and how a graph node knows its graph.

In the API container both are injected by `middleware_add_context.add_context_dispatch` onto
`request.state`. In the runner they are reconstructed from the payload — **the middleware does not
run there**, which is why `access_token` has to be an explicit payload field.

### 7.2 Result documents and status

All three result docs are `EventResult` variants sharing `EventResultStatus`:

`success | processing | error | waiting | stuck`

`waiting` is graph-specific — the human-in-the-loop pause. `stuck` is a monitoring state, not set
by the execution path.

The contract the API relies on: **the result doc is created with `processing` before the workflow
starts**, so the caller gets a pollable/subscribable id synchronously. Everything after that is
the runner mutating that doc.

### 7.3 Live updates — Redis → SSE

`Notify(topic=<result_id>)` (`server_sent_events/server_sent_events.py`) is passed down into
action execution. Calling it fires `asyncio.create_task(redis.publish(topic, message))`,
truncating messages to the last 5000 chars. The SSE/WebSocket endpoint in the **API** container
(`api/v1/api_server_sent_events/`) subscribes to the same Redis topic.

So the streaming path crosses containers purely through Redis: runner publishes, API relays.
Both containers must point at the same Redis (`REDIS_HOST=redis` in `docker-compose.yml`).

### 7.4 Webhooks

`Webhook` is passed through the workflow payload as a plain dict and re-validated in the activity
(`Webhook.model_validate(payload.webhook)`). It is fired on every meaningful persist — after each
graph node, after each team turn, and at terminal state.

### 7.5 Batches

`BatchService.create_batch_for_run` records the fan-out width *before* dispatch, and its id
becomes the `batch_id` carried by every run in the batch, so N result docs are queryable as one
logical job. Batch sizes: `RUNNABLE_BATCH_LIMIT_FOR_URLS` = 100 (graphs/teams),
`RUNNABLE_BATCH_LIMIT_FOR_URLS_ACTIONS` = 25 (actions).

### 7.6 Heartbeat registry

`HeartBeatRunRegistry` (`core/heartbeat/running_executions.py`) is a process-local, thread-locked
dict of `runnable_id → {runnable_type, started_at}`. The workflows register/deregister around the
activity call. `HeartbeatPublisher` polls the registry every `HEARTBEAT_INTERVAL_SEC` (10s) and
publishes the active set to the Redis topic `autobots:heartbeat`.

See §9 for why this currently does not work end-to-end.

---

## 8. What does *not* go through Temporal

Worth knowing precisely, because the "everything runs in the runner" mental model has holes:

1. **Scheduled runs.** `UserActions.run_scheduled_action` (`user_actions.py:841`) calls
   `ActionFactory.run_action_in_background` directly. `UserActionGraphs.run_scheduled_action_graph`
   (`user_action_graph.py:~713`) calls `run_in_background` **without** `temporal_client`. Both
   therefore execute **inside the scheduler container**, in-process. `conn/temporal/schedule/` is
   an empty placeholder — the intent is clearly there, unimplemented.

2. **The legacy in-process path is still live.** Every `run_in_background` keeps an
   `if temporal_client: … else: <run locally>` branch. Any caller that omits the client executes
   in whatever container it is in. This is what tests use, and what the scheduler hits.

3. **Streaming endpoints.** `ActionFactory.run_action_stream` and the SSE/`StreamingResponse`
   routes are inherently request-scoped and run in the API container.

4. **Sync `/run` endpoints** in `api_action.py` are commented out (`:376`, `:413`) — async-only by
   design now.

5. **Terminal / PTY, ingestion, dashboard, playwright, libreoffice** — separate concerns, never
   Temporal-mediated.

---

## 9. Observations and sharp edges

Ordered roughly by how much they'd bite. Items marked **[FIXED]** were resolved by the
cancellation work described in §11.

**a. The heartbeat registry is published from the wrong container. [FIXED]**
`lifespan.py:31` starts `HeartbeatPublisher` in the **main API app**. `lifespan_runner.py` does
**not**. The workflows that call `HeartBeatRunRegistry.register()` run in the **runner**. So the
runner accumulates registry entries nobody publishes, and the API publishes a registry that is
now almost always empty (only the legacy in-process paths still populate it). The
`autobots:heartbeat` topic is effectively dead for Temporal-backed runs.

**b. Registry mutation happens inside workflow code.**
`register()` / `deregister()` are called in `@workflow.run`, i.e. in replayed, supposed-to-be-
deterministic context — they touch a module-global dict, take a threading lock, and log. The
`UnsandboxedWorkflowRunner` is the only reason this doesn't blow up. On replay after a worker
restart these re-execute. The natural home for them is the activity, not the workflow.

**c. `return` inside `finally` in `run_action_graph_v2`. [FIXED]**
`action_graph.py:~578` ends with `finally: return action_graph_result_doc`. A `return` in a
`finally` block **swallows any in-flight exception**, including `CancelledError`. The activity
compensates by inspecting `result.status == error` and raising an `HTTPException(500)` — but
cancellation propagation through this function is not reliable.

**d. There is no cancel path. [FIXED]**
Nothing in the codebase calls `client.get_workflow_handle(...).cancel()` or `.terminate()`. The
activities check `activity.is_cancelled()` and `_run_with_heartbeat` handles `CancelledError`
correctly — the plumbing on the worker side exists, but no API endpoint ever triggers it. A user
cannot stop a 20-hour graph run.

**e. `access_token` is persisted in Temporal history.**
The user's JWT rides in the workflow input, which Temporal stores durably and renders in its UI.
It is needed (the runner has no middleware to derive it), but it means token lifetime is now
coupled to workflow-history retention. Worth considering a short-lived exchange token or a
Temporal codec/encryption converter.

**f. Retry semantics differ between the two execution paths.**
The legacy path wraps execution in tenacity `@retry(stop_after_attempt(3), wait_fixed(1))`
(`ActionFactory._run_retry_action_as_background_task`). The Temporal path calls
`ActionFactory.run_action` directly through `_run_single_action` — **no tenacity**, and Temporal
retries are disabled except for heartbeat timeouts. So the same failing action retries 3× on the
scheduler and 0× on the runner.

**g. `_run_with_heartbeat`, `_process_queued_inputs` / `_continue_run` are duplicated. [FIXED]**
`_run_with_heartbeat` is copy-pasted identically into all three activity modules.
`_process_queued_inputs` (activity) and `ActionFactory._continue_run` are the same algorithm in
two places, already drifting. Both are candidates for `conn/temporal/heartbeat.py` and a single
shared queue-drain helper.

**h. The converter uses `model_dump()`, not `model_dump(mode="json")`.**
`converter.py:14` passes Python-native objects (datetimes, enums, `Decimal`) to the default JSON
payload converter. It works for the current payload shapes, but `input: Dict[str, Any]` comes from
`DataBlock.model_dump()` and is not schema-constrained — an exotic value type would fail at
dispatch time, in the API request, not at execution time.

**i. Graph-level durability granularity.**
One activity per graph means a worker loss mid-graph replays the whole activity. Node results
already in `output` are skipped, so the cost is bounded — but any node that was *in flight* is
fully re-executed, including its external side effects (an LLM call, an outbound API POST). There
is no node-level idempotency key.

**j. `batch_run_action_in_background` no longer runs anything.**
Despite the name (and the still-accepted `background_tasks` parameter, now unused), it only
creates result docs; execution is the caller's job via `start_workflow`. Renaming it would prevent
a future double-run bug.

**k. One task queue, no priority separation.**
Actions, graphs and teams share `autobots-tasks`. A burst of 20-hour graph activities can starve
short action runs, since activity slots are per-worker and undifferentiated.

---

## 10. Quick reference

**Run it locally**

```bash
make dev          # API on :8000, hot reload
make dev-runner   # Temporal worker on :8005, hot reload
make dev-sch      # Scheduler on :8001
docker compose up # api + runner + ingestion-worker + redis + datadog
```

**Settings that matter** (`src/autobots/core/settings.py`)

```
TEMPORAL_ADDRESS    = "localhost:7233"
TEMPORAL_NAMESPACE  = "default"
TEMPORAL_TASK_QUEUE = "autobots-tasks"
TEMPORAL_API_KEY    = ""
ACTION_GRAPH_START_TO_CLOSE_TIMEOUT_MINUTES = 1200
HEARTBEAT_INTERVAL_SEC = 10
REDIS_HEARTBEAT_TOPIC  = "autobots:heartbeat"
RUNNABLE_BATCH_LIMIT_FOR_URLS         = 100
RUNNABLE_BATCH_LIMIT_FOR_URLS_ACTIONS = 25
```

**Where to look**

| I want to… | File |
|---|---|
| add a workflow/activity pair | `conn/temporal/worker.py` + a new `conn/temporal/<x>/` |
| change retry/timeout behavior | `conn/temporal/workflow_retry.py`, the three `workflow.py` |
| change what crosses the wire | `conn/temporal/<x>/shared.py` |
| change how a graph schedules nodes | `action_graph/action_graph/action_graph.py:308` |
| change node fan-out semantics | `action_graph/action_graph/action_graph_doc_model.py` (`NodeData`) + `action_graph_input_creator.py` |
| change team delegation | `action_team/action_team/action_team.py:252` |
| add an action type | `action/action_type/<family>/` + `action_types.py` + `action_map.py` |
| change dispatch (API side) | `api/v1/api_action/`, `api_action_graph/`, `api_action_team/` |


---

## 11. Cancellation

Cancellation is cooperative, and deliberately so: Temporal delivers a cancel by calling
`asyncio.Task.cancel()` **exactly once** (`worker/_activity.py:712-731`). If any code on the path
absorbs that one `CancelledError`, nothing ever cancels the run again. The design therefore leans
on a sticky token rather than on the exception alone.

### 11.1 The token

`src/autobots/core/cancellation/run_cancellation.py`

```python
class RunCancelledError(asyncio.CancelledError): ...

class RunCancellation:
    is_cancelled            # bool, backed by threading.Event
    cancel(reason)          # idempotent; fires registered driver cancels
    raise_if_cancelled()    # cooperative checkpoint
    register_driver_cancel(fn) -> remover
```

Two choices carry the design:

- **`RunCancelledError` subclasses `asyncio.CancelledError`**, so it is a `BaseException`. None of
  the several hundred `except Exception` blocks in this codebase can swallow it, the Temporal SDK
  still records a *cancelled* (not failed) completion, and the OpenAI retry predicates reject it.
  No new handling was needed at any intermediate layer.
- **`threading.Event`, not `asyncio.Event`**, because `is_cancelled` is polled from
  `asyncio.to_thread` worker threads and driver cancels fire cross-thread.

### 11.2 How the token reaches the work

The token rides on `AppState` as a `PrivateAttr` — not serialized, not logged, propagated by
`child_context()`. `AppState` already threads into every LLM connector and `ToolFactory.run_tool`,
so no signature churn was required except in the data plane.

`conn/temporal/heartbeat.py` bridges Temporal to the token, and is now the single shared
implementation for all three activities:

```python
while not task.done():
    activity.heartbeat("executing")
    if cancellation is not None and activity.is_cancelled():
        cancellation.cancel("Activity cancelled")
    await asyncio.wait({task}, timeout=heartbeat_interval_seconds)
```

Polling `activity.is_cancelled()` — which stays set once delivered — is what makes this robust: a
swallowed `CancelledError` downstream cannot un-cancel the run.

### 11.3 Latency

Cancellation rides back on the heartbeat RPC, and two throttles stack. Both are now tunable:

| Setting | Value | Effect |
|---|---|---|
| `TEMPORAL_HEARTBEAT_INTERVAL_SECONDS` | 1 | how often we call `activity.heartbeat()` |
| `TEMPORAL_HEARTBEAT_THROTTLE_SECONDS` | 1 | `Worker(max_heartbeat_throttle_interval=...)` |

`heartbeat_timeout` stays at 60s. That separation is load-bearing: the retry policy fires
*exclusively* on heartbeat timeout, so shrinking it would turn every GC pause into a spurious
"worker lost" replay of a 20-hour run. Before this change the effective throttle was
`heartbeat_timeout * 0.8` = **48 seconds**.

### 11.4 Checkpoints

- `ToolFactory.run_tool` and the three provider-specific tool factories, before the tool runs.
- The top of every provider agentic loop: claude, openrouter (sync + stream), openai chat
  (sync + stream), openai responses v1/v2 (sync + stream), gemini (agentic loop and tool loop),
  deepseek.
- The data plane shard loop and its thread-side chunk loop.

### 11.5 Blocking DB work

`asyncio.to_thread` threads survive cancellation, so the SQL runner registers a driver cancel that
aborts the query server-side:

| Dialect | Mechanism |
|---|---|
| postgres (psycopg2) | `connection.cancel()` |
| sqlite | `connection.interrupt()` |
| mysql (pymysql) | `KILL QUERY <thread_id>` over a second connection |
| postgres (pg8000, Cloud SQL) | none available — no cancel on the driver |
| bigquery, snowflake | not wired; both need the live cursor, which SQLAlchemy does not expose |

### 11.6 The API

`POST /v1/actions/{action_result_id}/cancel` — ownership-checked, no-op on an already terminal
run. It writes `cancelled` and clears `queued_inputs` **before** requesting Temporal cancellation,
so the UI flips immediately while teardown proceeds asynchronously.

That ordering creates a race with the runner finishing concurrently, closed by an opt-in
`skip_if_cancelled` on `update_action_result`: the guard is a `status != "cancelled"` clause in the
`find_one_and_update` filter, so check and write are one atomic operation. It is opt-in because the
cancel endpoint itself must be able to write `cancelled` over `processing`.

Workflow IDs come from `conn/temporal/workflow_ids.py` rather than inline f-strings, so the
endpoint and the dispatcher cannot drift.

### 11.7 Preserving partial work

A cancelled 30-minute agentic turn used to lose everything. `ActionFactory.run_action` builds its
config snapshot only *after* the action returns, from `action_to_run.action_config` — a local
variable. Cancellation is a `BaseException`, so `except Exception: return e` never caught it, the
frame unwound, and every accumulated message went with it. The turn's `result.config` stayed at the
*previous* turn's state.

Snapshots fix this by capturing at the agentic loop boundary, in
`src/autobots/llm/chat_snapshot/`:

```
chat_snapshot.py        ChatSnapshot, store(), apply_to_result()
provider_snapshots.py   one capture function per provider
```

**Why the loop top.** At the top of iteration N, iteration N-1 has appended both its assistant
message *and* the matching tool results. The state is well-formed by construction, so a cancel at
loop 13 keeps loops 1-12 whole and discards loop 13's assistant message and tool results together.
No trimming, no provider-specific repair of orphaned `tool_calls`, no risk of a malformed history
being rejected on the next turn.

**Only the message list is captured, and it is merged into the persisted config rather than
replacing it.** The connector loops operate on a provider *request* object, not the action config —
Claude rebuilds its request with `generated_files` and `tool_results` emptied and `tools` swapped
for expanded runtime schemas; OpenRouter's is a `model_dump` superclass projection. Persisting
either wholesale would silently wipe action-level fields the request never carried, and the next
run of that action would load the corrupted config. Capturing just the messages also keeps the
per-iteration cost proportional to the conversation rather than to the whole config.

Providers differ in two places, which is the whole reason the logic is per-provider:

| Provider | Message field | Cancellation notice |
|---|---|---|
| openrouter | `messages` (reasoning fields stripped) | `{"role": "assistant", "content": ...}` |
| openai chat, deepseek | `messages` | `{"role": "assistant", "content": ...}` |
| claude | `messages` | `{"role": "assistant", "content": [{"type": "text", ...}]}` |
| gemini | `messages` | `{"role": "model", "parts": [{"text": ...}]}` |
| openai responses v1/v2 | `input` | `{"role": "assistant", "content": "...", "type": "message"}` |

The notice must be a **legal input message for its provider**, because the next turn re-validates
the whole persisted config at `action_factory.run_action`. An illegal notice cancels cleanly and
then kills every subsequent turn. Responses is the strict case: `EasyInputMessageParam.content`
takes a string or *input*-type blocks only, and `ResponseOutputMessageParam` additionally requires
`id`/`status`/`type` — so an `output_text` block with neither matches no union branch.
`tests/core/test_chat_snapshot.py` re-validates every provider's notice against its real config
type for exactly this reason.

The snapshot rides on the `RunCancellation` token, the only object threaded from inside a connector
back up to `ActionFactory`. Each capture binds its own notice-appender, so the cancel branch
finalizes without knowing which provider ran.

On cancel, `apply_to_result` writes the finalized config, sets `result.output`, and fills
`results[-1].output` so the turn no longer renders as an unanswered prompt. Both the Temporal
activity and the legacy in-process path (team members, scheduler) call it.

The output notice is written **unconditionally**, snapshot or not. Most of the ~130 action types
have no agentic loop and so never snapshot; without this a cancelled image or video generation would
land with `error_message: "Cancelled"` and `output: {}`, which reads as "produced nothing" rather
than "stopped". The return value still reports whether a snapshot was restored.

Logging is `debug` per capture; the SSE `notify` fires once, on apply — the connectors already emit
a line per iteration, so notifying per snapshot would double the stream for no benefit.

### 11.8 Resource reclamation

Cancellation has to reclaim what a run holds, and asyncio cancellation alone does not:

| Resource | How it is reclaimed |
|---|---|
| In-flight SQL query | driver cancel (psycopg2 `.cancel()`, sqlite `.interrupt()`, pymysql `KILL QUERY`) |
| Remaining statements in a batch | cooperative `is_cancelled` check before each statement, both the parallel and sequential paths |
| SQLAlchemy engine | `engine.dispose()` in a `finally` |
| Data-plane shard | chunk-loop check; the shard reports `ok: False` so resume retries it rather than skipping |
| Data-plane job document | `except asyncio.CancelledError` finalizes as `cancelled` instead of leaving it `running` |
| Daytona sandbox | poll loop wrapped so a cancel still deletes session and sandbox |
| Temp files | unlinked in `finally` |
| Heartbeat registry entry | deregistered in `finally` |

Driver cancels run on a short-lived daemon thread, never inline. `RunCancellation.cancel()` is
invoked from the worker's event loop, and the MySQL cancel opens a second connection to issue
`KILL QUERY` — running that inline would block the loop and starve every other activity's heartbeat
on the same worker, triggering spurious worker-lost retries of unrelated runs.

The post-cancel unwind is bounded by `TEMPORAL_CANCEL_CLEANUP_TIMEOUT_SECONDS` (30s). Without it a
hung `finally` would hold the activity open until `start_to_close_timeout` — ten hours.

### 11.9 Known gaps

- **`action_sql_runner.py` blocks the event loop directly** instead of using `asyncio.to_thread`.
  Since `RunCancellation.cancel()` is invoked from the event loop, no cancel can be delivered while
  it runs. Needs moving onto a thread before a driver cancel would mean anything.
- **bigquery and snowflake** have no driver cancel.
- **Graph and team runs** get the token and cooperative checkpoints, but have no cancel endpoint of
  their own yet — only actions do.
- **`action_graph_golds/action_graph_20240330.py`** still has two `return`-in-`finally` sites. It is
  a frozen artifact and was left alone.
- **`ChatSnapshot.output_text` is always empty.** The parameter is plumbed through every capture
  function, but no connector passes its accumulated assistant text yet, so a cancelled run's
  `result.output` carries only the notice. The full transcript is still in `result.config`, and
  `finalized()` appends the notice after any partial text, so populating it later needs no other
  change.
- **One snapshot slot per token, and the token is shared with children.** It is cleared at the start
  of every action run, so a cancel before the first loop iteration can no longer restore the
  previous run's messages. Genuinely concurrent loops under one token (a node fanned out by
  `run_action_with_loop_concurrently`) still last-writer-win; they share a single result doc, so the
  effect is an imprecise transcript rather than cross-document corruption.
- **Gemini's inner tool loop** has a cancellation checkpoint but no snapshot — snapshots are taken
  at turn boundaries only.
