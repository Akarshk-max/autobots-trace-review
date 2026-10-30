# Action Graph — Architecture & Execution Model (0 → 1)

> Status: as-implemented on branch `AUT-1580-cyclic-workflow-v1` (identical to `main` at time of writing).
> Scope: everything under `src/autobots/action_graph/`, plus the API, Temporal, scheduler, sharing,
> and persistence layers it touches.

---

## 1. What an Action Graph is

An **Action Graph** is a user-authored **workflow template**: a set of **nodes**, each node
wrapping exactly one **Action** (an LLM call, an image generation, a browser automation, an API
call — any of the 60+ `ActionType`s), plus **edges** describing which node feeds which.

Two levels of abstraction:

| Concept | Meaning | Persisted as |
|---|---|---|
| **Action** | One unit of work (one LLM/tool/API call), with its own config | `Actions` collection (`ActionDoc`) |
| **Action Graph** | A DAG of nodes; each node points at an Action by id | `ActionGraphs` collection (`ActionGraphDoc`) |
| **Action Graph Result** | One *execution* of a graph, holding per-node outputs | `EventResults` collection + a JSON blob on S3 (`ActionGraphResultDoc`) |

The graph itself is a **template**: it holds no run state. All run state lives on the result doc.
Running the same graph 50 times produces 50 result docs, each carrying a full deep copy of the
graph definition frozen at run time (`ActionGraphResultDoc.result` is an entire `ActionGraphDoc`).

**It is a DAG today.** There is no cycle detection, no cycle support, and no loop-back edges —
see §12 for exactly what breaks if a cycle is introduced.

---

## 2. File map

```
src/autobots/action_graph/
├── action_graph/
│   ├── action_graph_doc_model.py     # Pydantic/Mongo models: Node, NodeData, ActionGraphDoc, ...
│   ├── action_graph_crud.py          # Mongo CRUD (user-scoped + admin-scoped)
│   ├── action_graph.py               # ★ THE ENGINE (1853 lines) — all execution logic
│   ├── action_graph_input_creator.py # ★ Upstream outputs → next node's input (fan-in / fan-out)
│   ├── coffman_graham.py             # Topological layering (execution order)
│   ├── user_action_graph.py          # Multi-tenant service layer (CRUD + run entry points)
│   ├── space_action_graph.py         # Space-admin-scoped access (no user_id filter)
│   ├── admin_action_graphs.py        # Unrestricted by-id lookup (used by share/clone)
│   ├── action_group.py               # "Group chat" / org-hierarchy delegation mode
│   ├── sample.json                   # Example graph payload
│   └── action_graph_golds/
│       └── action_graph_20240330.py  # DEAD historical snapshot of the engine, unreferenced
└── action_graph_result/
    ├── action_graph_result_model_doc.py  # Result models (subclass EventResult*)
    ├── user_action_graph_result.py       # ★ Result persistence (Mongo + S3), search, cache
    └── space_action_graph_result.py      # Space-admin-scoped result access
```

Touch points outside the folder:

```
src/autobots/api/v1/api_action_graph/api_action_graphs.py          # REST: CRUD + run
src/autobots/api/v1/api_action_graph_result/api_action_graph_results.py  # REST: runs
src/autobots/conn/temporal/action_graph/{workflow,activity,shared}.py    # Durable execution
src/autobots/schedule/user_schedule.py                             # Cron/interval scheduling
src/autobots/llm/tools/action_graph_tools.py                       # LLM function-calling CRUD/run
src/autobots/llm/tools/action_graph_run_tools.py                   # LLM tools over run results
src/autobots/event_result/event_monitor/event_monitor.py           # Stuck-run recovery
src/autobots/share_data/user_share_data.py                         # Clone graph + its actions
src/autobots/webhook/user_webhook.py, api_webhooks/api_webhook_slack.py  # Webhook triggers
src/autobots/dashboard/dashboard_service.py                        # ROI metrics
src/autobots/conn/excel/read_and_split.py                          # Spreadsheet fan-out helper
```

---

## 3. The data model

### 3.1 Graph shape — three parallel maps

`ActionGraphDoc` stores the topology in **three separate maps**, all keyed by `node_id`:

```python
nodes:        Dict[str, str]          # node_id -> action_id      ("who runs")
node_details: Dict[str, Node]         # node_id -> Node           ("how it runs" + UI position)
graph:        Dict[str, List[str]]    # node_id -> [child ids]    ("what runs next")
```

Example (`sample.json`, trimmed):

```json
{
  "nodes":  { "n1": "653083...", "n2": "657eb5...", "n3": "653083..." },
  "graph":  { "n3": ["n1", "n2"], "n2": ["n1"] },
  "node_details": {
    "n1": { "id": "n1", "position": {"x": 288, "y": 203}, "type": "default",
            "data": { "label": "japanese house", "actionId": "653083..." } }
  }
}
```

Notes / gotchas:

- `graph` is the **forward** (downstream) adjacency list. It is **sparse**: leaf nodes usually have
  no key at all. `ActionGraph.get_nodes()` therefore unions keys *and* values to get the node set.
- `nodes` and `node_details[*].data.actionId` are **redundant**. The engine reads the action id from
  `nodes` (`node_action_map.get(node)`); `node_details[*].data.actionId` is what the frontend and the
  clone flow read. They can drift.
- The map key (`node_id`) is a frontend-generated id (React Flow), typically a millisecond timestamp
  string like `"1702836730903"`.
- `position` / `type: "default"` are pure React Flow canvas metadata — never read by the engine.
- **`start_node_id` exists on the model but is never used by the engine.** Execution roots are
  derived structurally (nodes with no upstream). It is only mentioned in LLM tool docstrings.

### 3.2 The "start node" convention

A node whose `action_id` is the **empty string** `""` is a passthrough/start node.
`UserActions.run_action_v1` special-cases it:

```python
if action_id == "":
    # returns a synthetic ActionDoc of type mock_action whose output == the input
    # { "blocks": [ <the input DataBlock> ] }
```

So a start node echoes the graph's run input into the graph, giving downstream nodes a uniform
upstream to read from. `ActionGraphInputCreator` labels such an upstream `"Start"` (because its
`name` is empty).

### 3.3 `NodeData` — the complete node settings surface

`node_details[node_id].data` (`src/autobots/action_graph/action_graph/action_graph_doc_model.py:28`).
This is the **entire per-node configuration vocabulary** of the engine:

| Field | Type / default | What it does |
|---|---|---|
| `label` | `str` | Display name. Auto-synced to the Action's name on every graph read (§6.4). |
| `actionId` | `str` | Duplicate of `nodes[node_id]`; used by frontend + clone. |
| `type` | `ActionType \| None` | Action type hint for the UI. Not read by the engine. |
| `updated_at` | `datetime \| None` | UI bookkeeping. |
| `characterId` | `str \| None` | Persona/character link. Not read by the engine. |
| **`data_flow_type`** | `DataFlowType = only_output` | How upstream results are rendered into this node's prompt. See §5.1. |
| **`user_review_required`** | `bool = False` | Gate: children of this node will not run until reviewed. |
| **`user_review_done`** | `bool = False` | Clears the gate. |
| **`user_input_required`** | `bool = False` | Gate: this node will not run until input supplied. |
| **`user_input_done`** | `bool = False` | Clears the gate. |
| **`is_loop`** | `bool = False` | Master switch: this node fans out into N parallel action runs. |
| `seperator` | `str = "\|"` | Delimiter for text splitting (note the misspelling — it is the real field name). |
| **`is_split`** | `bool = False` | With `is_loop`: split upstream **text** by `seperator`, one run per segment. |
| **`file_split`** | `bool = False` | With `is_loop`: one run per **URL** in the upstream output. |
| **`api_loop`** | `bool = False` | Fan out into `batches` runs, each told to fetch `batch_id=<i>` via `api_call`. |
| **`excel_loop`** | `bool = False` | Fan out **one run per spreadsheet row**. |
| `header` | `bool = False` | Excel: treat row 0 as a header. |
| `data_processing` | `bool = False` | Excel mode A: inject the whole row as JSON into the prompt. |
| `file_processing` | `bool = False` | Excel mode B: read an instruction column + a file-URL column. |
| `file_column_index` | `int = 0` | Excel: which column holds the file URL. |
| `instruction_column_index` | `int = 0` | Excel: which column holds the instruction. |
| `batches` | `int = 0` | `api_loop`: how many batches to fan out into. |
| `process_url_in_text` | `bool = True` | Metadata passed to the action: extract & attach URLs found in text. |
| `process_attachment_url` | `bool = True` | Metadata: process attachment URLs. |
| `attachment_requested` | `int = 0` | Metadata: how many attachments the action is expected to emit. |
| **`sleep_time`** | `float = 0, ge=0` | Seconds to sleep before running; in concurrent loops it becomes a **stagger** (§5.4). |

`DataFlowType` (3 values):

```python
only_output        # default: render "## Upstream: <name>" + "### Run n" + "**Output:**" blocks
both_input_output  # same, but also render "**Input:**" for each upstream run
as_is_last_output  # no markdown scaffolding at all — raw upstream text passed through verbatim
```

### 3.4 Graph-level fields

`ActionGraphDoc` (`= ActionGraphDocCreate + id`), collection `ActionGraphs`:

```
id, user_id, space_id?                     # identity + multi-tenancy
name, version (float), description, user_manual
nodes, node_details, graph, start_node_id  # topology (§3.1)
input:  Dict[str, Any] | None              # last/template run input (a DataBlock dump)
output: Dict[str, ActionDoc] | None        # node_id -> the ActionDoc that ran, with its results
is_saved, is_published, shared_with: List[SpaceId|str]
time_saved_per_file: float = 0.1           # dashboard ROI maths
dollar_cost_per_hour: float = 50           # dashboard ROI maths
created_at, updated_at
```

`ActionGraphLiteDoc` is the same minus `nodes`/`node_details`/`graph`/`input`/`output` — used for
list/search responses and for the Mongo copy of a result (§4.4).

### 3.5 Result model

```python
ActionGraphResultDoc(EventResultDoc):
    id, user_id, space_id?
    type   = EventType.action_graph_run
    status: EventResultStatus            # processing | success | error | waiting | stuck
    name                                 # LLM-generated run title
    error_message: TextObj | None
    file_url                             # S3 URL of the full JSON
    batch_id: str | None                 # links sibling runs of one batch_run
    is_saved, is_published, shared_with
    result: ActionGraphDoc | ActionGraphLiteDoc   # ← the frozen graph + live outputs
```

**`result.output[node_id]` is the run state.** It maps node → the `ActionDoc` that executed there,
including its `results: List[ActionResult]` (each `{input, output}`) and its final `output`
(a `DataBlocks` dump). Presence of a key in `result.output` is the engine's definition of
"this node is done" (§5.2).

---

## 4. Persistence & storage layout

### 4.1 Graph storage
Plain MongoDB via motor: collection `ActionGraphs`, `ActionGraphCRUD` (user-scoped, extends the
generic `CRUDBase`), `ActionGraphCRUDForAdmin` (same collection, find-by-id without `user_id`,
used only by the share/clone flow).

### 4.2 Result storage — dual write (S3 + Mongo)
This is the single most important persistence detail. `UserActionGraphResult` writes **every**
result update to **two** places:

1. **S3** — `{result_id}.json`, the **full fidelity** doc (`result` is a complete `ActionGraphDoc`
   with all node outputs). Written under `AWS_S3_RUNS_FOLDER` via `UserFiles.upload_binary`.
2. **MongoDB** (`EventResults` collection) — the **lite** copy: `result` is downgraded to
   `ActionGraphLiteDoc`, i.e. topology and outputs are stripped. Mongo holds only metadata for
   listing, searching and filtering.

Read path (`get_action_graph_result`): **S3 first**, fall back to Mongo if the object is missing.
`get_action_graph_result_mongo` is the metadata-only variant.

Consequence: the graph engine reloads/rewrites the entire result JSON to S3 **after every node**
(and, inside loop nodes, after **every loop iteration**) — see §5.5 on write amplification.

### 4.3 Search & caching
`UserActionGraphResult.search` uses a two-tier cache:
- `InMemoryTTLCache` (60 s TTL, 5000 items) keyed by `(user_id, generation, query, limit, offset)`.
- A Redis "generation" counter per `(user_id, event_type)`; `create`/`update` bump it, which
  invalidates every cached page for that user (`_bust_search_cache_for_user`).

### 4.4 IP scrubbing
`scrub_ip()` — when a result is read by someone who is not `result.user_id` (a shared run), every
`result.output[node].config` is blanked, so the borrower cannot see the prompts/credentials inside
the actions. Applied on read, not on write.

---

## 5. The execution engine (`action_graph.py`)

### 5.1 Entry points and the call chain

```
POST /action_graphs/{id}/async_run
POST /action_graphs/{id}/batch_run
        │
        ▼
UserActionGraphs.run_in_background(...)          # user_action_graph.py
        │  ├─ resolves the graph (owned | shared | space-admin)
        │  ├─ if temporal_client: init result doc → start Temporal workflow → RETURN
        │  └─ else ▼
ActionGraph.run_in_background(...)               # action_graph.py:51
        │  ├─ UserActionGraphResult.init(...)     # create OR resume the result doc
        │  ├─ HeartBeatRunRegistry.register(...)
        │  └─ dispatch on one of three modes:
        │        background_tasks → FastAPI BackgroundTasks.add_task
        │        fire_and_forget  → asyncio.create_task
        │        (neither)        → await inline
        ▼
ActionGraph._run_as_background_task(...)         # indirection: BackgroundTasks + @retry don't mix
        ▼
ActionGraph._run_retry_as_background_task(...)   # @retry(stop_after_attempt(3), wait_fixed(1))
        │  ├─ optional: empty_downstream_nodes(...)          # rerun semantics
        │  ├─ single-node rerun?  → ActionGraph.run_node(...)
        │  └─ otherwise           → ActionGraph.run_action_graph_v2(...)   ← ★ the main loop
        └─ if final status == error: raise HTTPException(500) → triggers the retry
```

Four run-shaping booleans, all threaded from the REST layer:

| Flag | Combined with | Effect |
|---|---|---|
| — | — | Fresh run of the whole graph. |
| `is_rerun_graph=True`, no node id | | Wipe `result.output` entirely, re-run everything. |
| `is_rerun_graph=True` + `action_graph_node_id` | `is_empty_downstream_only=False` | Clear that node **and** all its descendants, then re-run the graph. |
| `is_rerun_graph=True` + node id | `is_empty_downstream_only=True` | Clear only the **descendants** (keep the node's own result), re-run. |
| `action_graph_node_id` only | | **Single-node rerun** — `run_node()`, no graph traversal. |
| `is_continue_run_graph=True` + node id | | Take the **subgraph** rooted at that node (BFS over `graph`) and re-run just it; requires an existing `action_graph_result_id`. |

### 5.2 `run_action_graph_v2` — the main loop

```python
graph_map    = result.graph                       # node -> children
inverted_map = invert_map(graph_map)              # node -> parents          (always FULL graph)
if action_graph_node_id and is_continue_run_graph:
    graph_map            = get_subgraph_map(node_id, graph_map)   # BFS descendants
    inverted_map_subgraph = invert_map(graph_map)

total_nodes = get_nodes(graph_map)
nodes_order = coffman_graham(inverted_map_subgraph or inverted_map, len(total_nodes))
```

`nodes_order` is a **list of layers** (`List[List[str]]`), layer *i* = all nodes at topological
depth *i+1*. Then:

```python
for layer in nodes_order:
    for node in layer:
        upstream_nodes = inverted_map.get(node)     # NOTE: full map, even in subgraph mode
        ... gates ...
        ... dispatch ...
        result.output[node] = action_response[node]
        result_doc = await update_action_graph_result(...)   # S3 + Mongo write
        if webhook: await webhook.send(...)
```

**Both loops are sequential `await`s.** Coffman–Graham gives *ordering*, not parallelism —
nodes in the same layer run one after another. The only concurrency in the engine is *inside* a
single loop-node's fan-out (§5.4).

**The skip gate** (per node, before anything else):

```python
if not (node_id and is_continue_run_graph) and (
        is_work_done([node], action_response)          # already has an output → skip
        or not is_work_done(upstream_nodes, action_response)):   # parent missing → skip
    continue
elif (node_id and is_continue_run_graph) and node == node_id:
    continue        # in continue mode, the subtree ROOT itself is not re-run
```

`is_work_done(values, action_response)` is simply "every id is a key in `result.output`".
Note there is **no re-check**: a node skipped because a parent was missing is never revisited in
this run.

**The two human-in-the-loop gates:**

```python
# (a) this node needs user input
if node_details[node].data.user_input_required and not user_input_done:
    to_wait = True; continue

# (b) any parent needs user review
for up in upstream_nodes:
    if node_details[up].data.user_review_required and not user_review_done:
        to_wait = True; break
if to_wait: continue
```

`to_wait` is a **function-scoped latch that is never reset**. Once any node trips it, every
subsequent node in the traversal is skipped — including nodes on unrelated branches. At the end:

```python
status = EventResultStatus.success if not to_wait else EventResultStatus.waiting
```

So a graph that hits a review gate ends `waiting`; the UI flips `user_review_done`/`user_input_done`
on the graph (via `PUT /action_graphs/{id}` → `node_details`) and re-runs with the same
`action_graph_result_id`, at which point the already-done nodes are skipped by `is_work_done`.

**Error handling:** the whole `for` is wrapped in `try/except/finally`. Any escaping exception sets
status `error` + `error_message`, and — because `_run_retry_as_background_task` re-raises on
`status == error` — the **entire graph run is retried up to 3 times**. Completed nodes survive the
retry (they're in `result.output` and get skipped), so the retry effectively resumes.

Note the `finally: return action_graph_result_doc` — a `return` inside `finally` swallows any
in-flight exception. Intentional here (status is already recorded), but it means the function
never propagates.

### 5.3 Node dispatch table

```
upstream_nodes == 0  (root / start nodes — input comes from the GRAPH INPUT dict)
├── is_loop and not file_split and not excel_loop → run_action_with_text_split_and_loop_concurrently
│       split input["text"] by `seperator` → one DataBlock per segment
├── excel_loop                                    → run_action_with_excel_loop        (SEQUENTIAL)
│       pick_spreadsheet_url(input.urls, input.text) → split into 1-row batches
│       per row, one of three shapes: data_processing | file_processing | default
├── is_loop and file_split                        → run_action_with_file_split_and_loop_concurrently
│       one DataBlock per URL in input["urls"]  (raises 404 if no urls)
└── else                                          → run_action_simple
        sleep_for_node(); run the action once with the raw graph input dict

upstream_nodes >= 1  (input is SYNTHESISED from upstream outputs)
├── is_loop → run_action_with_loop
│       ActionGraphInputCreator.to_input(...) → List[DataBlock]
│       → run_action_with_loop_concurrently
└── else   → run_action_with_merge
        sleep_for_node()
        ActionGraphInputCreator.to_input(...) → a single DataBlock
        run once (or run_node_action_with_existing_doc in continue mode)
```

### 5.4 `run_action_with_loop_concurrently` — the fan-out primitive

```python
semaphore = get_semaphore()      # process-wide, lru_cached asyncio.Semaphore
                                 # size = settings.ACTION_GRAPH_LOOPS_CONCURRENCY_LIMIT (default 11)
lock = asyncio.Lock()
inter_action_results = [Exception("Empty")] * len(action_inputs)

async def process_action_input(idx, action_input, notify):
    if sleep_time > 0:                       # STAGGER, not a plain sleep
        delay = min(idx * sleep_time, 30.0) + random.uniform(0, min(sleep_time, 1.0))
        await asyncio.sleep(delay)
    async with semaphore:
        result = await ActionGraph.run_action(...)          # or run_node_action_with_existing_doc
        inter_action_results[idx] = result
        async with lock:
            seed = first ActionDoc in inter_action_results
            collated = collate_action_results([seed] + [others])
            action_response[node] = collated
            await update_action_graph_result(...)           # ← S3+Mongo write PER ITERATION

await asyncio.gather(*[process_action_input(i, ai) for i, ai in enumerate(action_inputs)])
# final collation; if EVERY iteration failed, synthesise a mock_action ActionDoc named "Loop Failed"
```

Key properties:
- The semaphore is **global to the process**, `lru_cache`d — all concurrent loop nodes across all
  graph runs on that worker share the same 11 slots.
- `sleep_time` becomes a **ramp**: item *i* waits `min(i·sleep_time, 30s)` plus up to 1 s jitter.
  Designed to avoid bursting a rate-limited downstream API.
- Results are collated **incrementally** under a lock so the UI streams partial progress.
- Individual iteration failures are tolerated; the node only "fails" if *all* iterations fail, and
  even then it produces a `mock_action` doc rather than raising.

### 5.5 `collate_action_results` — N runs → 1 `ActionDoc`

Takes `List[ActionDoc | Exception | None]`, deep-copies element 0 as the seed, then for each
subsequent element:

```python
results.input[f"text_{i}"] = other.input["text"]      # inputs flattened into text_0, text_1, ...
results.output["blocks"]  += other.output["blocks"]   # DataBlocks concatenated
results.results           += other.results            # run history concatenated
        # …unless in continue-run mode, where a single fresh ActionResult is appended instead
Exception / None → a DataBlock carrying the error string / "No Response"
```

So a fan-out node's downstream consumers see **one** `ActionDoc` whose `output.blocks` is the
concatenation of every iteration's blocks. That's what makes `is_split` on the *next* node able to
re-fan-out over those blocks.

### 5.6 Write amplification (worth knowing)

Every `update_action_graph_result` does: S3 read (`get_action_graph_result`) → mutate → S3 write of
the whole JSON → Mongo update of the lite copy → Redis cache-generation bump. This happens
**once per node** and **once per loop iteration**. A 10-node graph with a 200-row Excel loop
performs on the order of 200+ full-document S3 round trips.

### 5.7 Ordering: `coffman_graham.py`

```python
def coffman_graham(graph, width):   # `graph` here is the INVERTED map: node -> parents
    in_degree[X] = number of children of X
    queue        = nodes with no children (sinks)
    ordered_nodes = Kahn's algorithm over the inverted edges  → sinks first, roots last
    node_labels[n] = 1 for roots, else max(label of parents) + 1     # = topological depth
    layers[label-1].append(n)  (with an overflow bump if a layer reaches `width`)
```

Called as `coffman_graham(inverted_map, len(total_nodes))`. Because `width` equals the total node
count, the "layer is full, push to the next layer" branch **can never fire** — the classic
Coffman–Graham width constraint is effectively disabled. It degenerates into "group nodes by
topological depth".

`invert_graph()` at the bottom of that file, and the `__main__` demo block, are unused by the engine
(`ActionGraph.invert_map` is the async version actually used).

### 5.8 `run_action` — resolving and executing one action

`ActionGraph.run_action(ctx, app_state, user_actions, action_id, input_dict, notify)` tries three
resolutions in order, and each has its own execution path:

1. **Owned action** (`user_actions.get_action`) → `run_action_v1` (persists action state).
   Also the `action_id == ""` start-node case.
2. **Shared action** (`get_shared_action`) → `run_action_doc` (does **not** persist action state).
3. **System action** (`get_system_action`) → `run_action_doc`.
4. Otherwise → returns (does not raise) `HTTPException(404, "Action not found")`.

Wrapped in a **3-attempt retry loop specific to attachment failures**: if the action raises
`HTTPException` whose detail says `"Attachments not matched"`, it sleeps 1 s and retries — this
handles LLMs that "forget" to emit the requested file. After 3 attempts it returns a human-readable
`HTTPException(500, ...)` including the last model output.

Note the return convention: **exceptions are returned, not raised.** Callers `match` on
`ActionDoc()` vs `Exception()`.

### 5.9 Single-node rerun (`run_node`)

Used when `action_graph_node_id` is given without `is_rerun_graph`/`is_continue_run_graph`.
It does **not** recompute input from upstream — it reuses the existing `result.output[node]`
`ActionDoc`, replaces its `input` with the supplied `action_graph_input_dict`, clears its `output`,
and re-executes it via `run_node_action_with_existing_doc` → `UserActions.run_action_doc`.
Honours `sleep_time`. Sets the whole run's status to `processing` → `success`/`error`.

---

## 6. `ActionGraphInputCreator` — how data flows along an edge

`to_input(...)` is the fan-in/fan-out translator: **upstream `ActionDoc`s → the next node's
`DataBlock` (or `List[DataBlock]`)**. It has four mutually exclusive branches, checked in order:

### 6.1 `api_loop`
For each upstream block, emit `batches` DataBlocks, each containing the upstream text plus
`"Get Google Sheets data for batch_id=<i> using api_call."`. (A fairly specific, hard-coded
instruction — it presumes the node's action has an `api_call` tool.)

### 6.2 `excel_loop`
For each upstream block: `pick_spreadsheet_url(block.urls, text=block.text)` →
`split_excel_into_batches(url, 1, header=header)` → one DataBlock **per row**, in one of three
shapes:

| Mode | DataBlock contents |
|---|---|
| `data_processing` | upstream text + the entire row serialised as JSON |
| `file_processing` | instruction from `instruction_column_index`, `urls=[row[file_column_index]]`, + upstream text |
| default | upstream text + `urls=[row[file_column_index]]` |

Under `as_is_last_output` all the markdown scaffolding (`## Upstream:`, `### Row n/N`, `**Base
Output:**`) is dropped and only the raw payload survives.

### 6.3 `is_loop` (the general fan-out)
Three sub-modes:
- `is_split and not file_split` — split each upstream block's **text** on `seperator`, one DataBlock
  per non-empty segment.
- `file_split` — one DataBlock per **URL** on the upstream block (text is repeated on each).
- neither — one DataBlock per upstream block, then **zip across upstreams by index**: for `i` in
  `range(min(len per upstream))`, concatenate the *i*-th block of every upstream into one DataBlock
  (headed `## Upstream: <name>` unless `as_is_last_output`), unioning their URLs. Note this
  **truncates to the shortest upstream**.

### 6.4 default (merge / fan-in)
Builds one markdown document from **all** upstream nodes' **full run history**:

```markdown
## Upstream: Persona Writer

    ### Run 1
    **Input:**            ← only when data_flow_type == both_input_output
        <input text>
    **Output:**
    - Block 1:
        <block text>
    - Block 2:
        ...

## Upstream: Product Researcher
    ### Run 1
    ...
```

with `urls` accumulated across everything. Under `as_is_last_output`, none of the headings are
emitted — only the raw block texts, concatenated.

Indentation deepens by one level (`4 spaces`) **per upstream node** (`tabs += 1` per upstream, plus
one more when not `as_is_last_output`) — an artifact, but it is the current behaviour.

### 6.5 Large-input offload — `_maybe_convert_to_file`
Applied to **every** DataBlock produced by `to_input`. If `len(text) > ACTION_GRAPH_MAX_INPUT_CHARS`
(default **75 000**):

1. deep-copy the block, run `process_url_in_text` if enabled,
2. upload the **original** text to S3 as `graph_input_<uuid>.txt` via `UserFiles.upload_binary`,
3. replace `text` with `"Input text was too long (N characters) and has been converted to a file.
   Please read and process the content from the attached file URL."`,
4. append the S3 URL to `urls`, and set `metadata.process_url_in_text = False`.

If the upload fails, it falls back to **truncating at 60 000 chars** with a
`...[TRUNCATED DUE TO UPLOAD FAILURE]...` marker.

---

## 7. Service layer — `UserActionGraphs`

Multi-tenant wrapper. Every method scopes by `user_id` and, when the caller is a space member, by
`space_id` (raising 400 if a space member has no `space_id`).

| Method | Notes |
|---|---|
| `create` | Injects `user_id` + `space_id`. |
| `list_owned_or_shared` | Owned OR `shared_with` containing the caller's `SpaceId`. `offset` is a **page index**, not a row offset. |
| `get_owned` / `get_owned_or_shared` | The latter ORs on `shared_with == user.email` and `shared_with == SpaceId`. Also runs the label sync below. |
| `get_owned_or_shared_by_name` | Name lookup with version selection. |
| `search` | Mongo text search over `name`, `description`. |
| `update` | If `shared_with` changed, **cascades the share to every action in the graph** (`sync_action_for_shared_with`) — computing added/removed grantees and updating each `ActionDoc.shared_with`. |
| `sync_and_save_node_details_label_with_action_name` | On every `get_owned_or_shared`, rewrites `node_details[*].data.label` to the Action's current `name` and persists if anything changed. Silent self-healing write on a read path. |
| `delete` | `delete_many` on the scoped find. |
| `run_in_background` | Resolve graph → either start a Temporal workflow or call `ActionGraph.run_in_background`. |
| `batch_run_in_background` | §8. |
| `run_scheduled_action_graph` (static) | Entry point for APScheduler: rebuilds `UserORM` + space membership from ids, then calls `run_in_background`. |

`SpaceActionGraphs` mirrors the read/update/delete/search surface but queries by `space_id` only
(no `user_id`), and its constructor raises 403 unless `space_member.role in ADMIN_ROLES`.
`AdminActionGraphs.get_action_graph` is an unrestricted by-id fetch used by the clone flow.

---

## 8. Batch runs

`POST /action_graphs/{id}/batch_run` with a `DataBlock` carrying many `urls`:

1. Generate a run title once via `LlmFunc.gen_title` (fallback: `"<name> - <timestamp>"`).
2. Chunk `input.urls` into batches of `batch_size` (query param, `1..100`; anything out of range
   falls back to `RUNNABLE_BATCH_LIMIT_FOR_URLS`, default 100).
3. `BatchService.create_batch_for_run(...)` records one `BatchDoc` with `total = len(batches)`;
   its id becomes the `batch_id` stamped on every child run.
4. For each batch, call `run_in_background` with `title_override = "<i>/<n> <title>"`.
   **Batches are launched sequentially in a `for` loop** (each `run_in_background` may itself be
   async/background, but the fan-out is not `gather`ed).
5. Returns `List[ActionGraphResultDoc]`.

Sibling runs are later correlated by `batch_id` (see `tests/graphs/test_graph_team_batch_id.py`).

---

## 9. Durable execution via Temporal

When `get_temporal_client()` returns a client, `run_in_background` takes the Temporal path instead
of in-process background tasks:

```
UserActionGraphs.run_in_background
  ├─ UserActionGraphResult.init(...)                    # create the result doc synchronously
  ├─ build ActionGraphWorkflowInput { graph_id, result_id, user_id, trace_id, access_token,
  │                                   input, webhook, node_id, is_rerun/continue/empty_downstream,
  │                                   batch_id, start_to_close_timeout_minutes }
  └─ temporal_client.start_workflow(ActionGraphWorkflow.run,
                                    id=f"action-graph-{result_id}",
                                    task_queue=TEMPORAL_TASK_QUEUE)
                                    → returns the result doc immediately (status: processing)
```

`ActionGraphWorkflow.run` registers the run in `HeartBeatRunRegistry`, then executes a single
activity:

```python
workflow.execute_activity(
    execute_action_graph_activity, payload,
    start_to_close_timeout = timedelta(minutes=ACTION_GRAPH_START_TO_CLOSE_TIMEOUT_MINUTES),  # 600 default
    heartbeat_timeout      = timedelta(seconds=60),
    retry_policy           = RetryPolicy(initial_interval=1s, maximum_attempts=3),
)
```

`execute_action_graph_activity` rebuilds `db`/`ctx`/`UserORM` (with `access_token`) from the payload,
loads the result doc from S3, applies the same `empty_downstream_nodes` pre-step, then calls
`ActionGraph.run_node` or `ActionGraph.run_action_graph_v2` wrapped in `_run_with_heartbeat` — a
helper that pings `activity.heartbeat("executing")` every 15 s and cancels the inner task on
Temporal cancellation. On failure it writes status `error` + fires the webhook, then re-raises so
Temporal retries.

**So there are three retry layers stacked**: Temporal (3 activity attempts) → tenacity
`@retry(3)` inside `_run_retry_as_background_task` (only on the non-Temporal path) → the
attachment retry (3) inside `run_action`.

---

## 10. Other run triggers

| Trigger | Path |
|---|---|
| **REST** | `POST /{id}/async_run`, `POST /{id}/batch_run`, `POST /{id}/run_group_chat`. Guarded by `require_permission(ProductResource.Workflows, ResourceOperation.Execute)`. |
| **Schedule** | `UserSchedule` → APScheduler job → `UserActionGraphs.run_scheduled_action_graph` (cron or interval). |
| **Webhook / Slack** | `user_webhook.py`, `api_webhook_slack.py` → `run_in_background`. |
| **LLM tool call** | `llm/tools/action_graph_tools.py` exposes create/get/find/update/delete/run as function-calling tools; `action_graph_run_tools.py` exposes the run results. |
| **Stuck-run recovery** | `EventMonitor.run_stuck_action_graphs` — finds runs stuck longer than `STUCK_ACTION_GRAPH_THRESHOLD_MINUTES` (720) and re-runs up to `STUCK_ACTION_GRAPH_GRAPHS_TO_RUN_LIMIT` (2) of them per pass, reusing the same `action_graph_result_id`. |

**Progress reporting** is out-of-band: `Notify(topic=result_id)` publishes short strings to Redis
pub/sub (consumed by the SSE endpoint), and an optional `Webhook` receives a full
`result_doc.model_dump()` POST after every node.

---

## 11. `ActionGroup` — the "group chat" alternate mode

`POST /{id}/run_group_chat` does **not** use the DAG engine. It reinterprets the same graph as an
**org chart**:

1. For each node, build a prompt describing (a) the org hierarchy below it (`"You manage Agent X"` /
   `"Agent Y manage Z"`) and (b) profiles (id/name/description/user_manual) of every descendant.
2. Find the **director node** = the node with no *parents* in `invert_map(graph)`… note it iterates
   the inverted map and returns the first node with `len(children_ids) == 0`, i.e. a graph root.
3. Ensure the director's action config has the `"delegate"` tool (`add_delegate_tool_to_director_node`
   mutates and persists the `ActionDoc`).
4. Append the org prompt to the user input and run **only** the director action; the LLM then
   delegates to descendants at runtime via the `delegate` tool.
5. Store the director's `ActionDoc` at `result.output[director_node_id]` and mark the run success.

So: DAG mode = the engine walks the graph; group-chat mode = the graph is a *prompt* and the LLM
walks it.

---

## 12. It is a DAG — exactly where that assumption is baked in

There is **no cycle detection anywhere** (`grep -i "acyclic\|cycle"` over `src/` returns nothing for
the graph module). If a cycle is authored, here is what happens, in order:

1. **`coffman_graham` silently drops the cycle.** It is Kahn's algorithm: nodes inside a cycle never
   reach in-degree 0, so they never enter `ordered_nodes` and therefore never land in any layer.
   They are simply **never executed** — no error, no warning. The run reports `success`.
2. **`node_labels` would KeyError-guard around it** — `[node_labels[dep] for dep in graph[node] if
   dep in node_labels]` already filters missing deps, so no exception surfaces.
3. **`is_work_done(upstream_nodes, ...)` can never be satisfied** for a node in a cycle: its parent
   is its own descendant, so the "parents complete" precondition is unsatisfiable under the
   single-pass model.
4. **`empty_downstream_nodes` would loop forever** — it is an unbounded BFS (`to_visit` list, no
   `visited` set) over `result.graph`; a cycle makes it spin indefinitely.
5. **`get_subgraph_map` (continue-run) would also loop forever** — same shape, `deque` BFS with no
   visited set.
6. **`ActionGroup.build_node_group_hierarchy_prompt` / `build_node_group_profile_prompt`** likewise
   BFS with `visited_nodes` recorded but **never consulted** — infinite loop on a cycle.
7. **`result.output` is a `Dict[node_id, ActionDoc]`** — one slot per node. There is no notion of
   "iteration *k* of node N", so a node that runs twice would overwrite its own history (only the
   `results` list inside the `ActionDoc` accumulates, and `collate_action_results` is the only thing
   that appends there).
8. **`is_work_done` = "key present in output"** is the sole completion predicate. It is monotonic and
   irreversible within a run — a node can never be marked "not done again", which is precisely what
   a loop-back edge requires.
9. **No iteration counter, no loop-condition evaluation, no max-iterations guard, no convergence
   check** exists anywhere in the model or the engine.

The only "looping" the system has today is **data-parallel fan-out inside a single node**
(`is_loop`/`excel_loop`/`api_loop`/`file_split`/`is_split`) — N independent runs of the *same*
action over N inputs, collated back into one `ActionDoc`. That is a `map`, not a cycle.

---

## 13. Known quirks / sharp edges

- **`to_wait` is a global latch.** One node awaiting review halts every remaining node in the run,
  including independent branches.
- **A skipped node is never revisited within a run** — `is_work_done` on parents is checked once.
- **Layers are ordering only.** No intra-layer parallelism; a wide graph runs strictly serially.
- **Coffman–Graham's width bound is inert** (`width = len(total_nodes)`).
- **`inverted_map` stays full in continue-run mode** while `graph_map` is the subgraph — upstream
  lookups therefore reference nodes outside the subgraph.
- **Two sources of truth for the action id** (`nodes[n]` vs `node_details[n].data.actionId`) that can
  drift; the engine reads the former, clone/UI the latter.
- **`start_node_id` is stored but never executed against.**
- **Read paths write.** `get_owned_or_shared` persists label re-syncs.
- **`finally: return` in `run_action_graph_v2` and `run_node`** swallows in-flight exceptions.
- **`run_action` returns exceptions instead of raising**, so callers must `match` rather than
  `try/except`.
- **Write amplification**: a full S3 read + write of the whole result JSON per node *and* per loop
  iteration.
- **Global loop semaphore** (`lru_cache`d, 11 slots) shared by every concurrent graph run in the
  process.
- **Non-splitting multi-upstream loop zip truncates to the shortest upstream.**
- **`seperator`** is the actual (misspelled) field name — renaming it is a breaking schema change.
- **Dead code** in the module: `ActionGraph.gen_input` (LLM-schema-driven input synthesis),
  `run_action_with_text_split_and_loop`, `run_action_with_file_split_and_loop`,
  `run_action_with_loop_synchronously`, `coffman_graham.invert_graph`, and the whole
  `action_graph_golds/action_graph_20240330.py` snapshot — none are referenced.

---

## 14. Relevant settings

| Setting | Default | Effect |
|---|---|---|
| `ACTION_GRAPH_LOOPS_CONCURRENCY_LIMIT` | `11` | Process-wide semaphore for loop-node fan-out. |
| `ACTION_GRAPH_MAX_INPUT_CHARS` | `75000` | Threshold above which node input is offloaded to an S3 file. |
| `ACTION_GRAPH_START_TO_CLOSE_TIMEOUT_MINUTES` | `600` | Temporal activity timeout. |
| `RUNNABLE_BATCH_LIMIT_FOR_URLS` | `100` | Default/cap for `batch_run` batch size. |
| `STUCK_ACTION_GRAPH_THRESHOLD_MINUTES` | `720` | When a `processing` run is considered stuck. |
| `STUCK_ACTION_GRAPH_GRAPHS_TO_RUN_LIMIT` | `2` | How many stuck runs the monitor restarts per pass. |
| `TEMPORAL_TASK_QUEUE` | `autobots-tasks` | Queue the graph workflow is dispatched to. |
| `AWS_S3_RUNS_FOLDER` | — | S3 prefix for result JSON blobs. |
| `API_ACTION_GRAPHS` / `API_ACTION_GRAPHS_RESULTS` | `/action_graphs` / `/action_graphs_results` | Route prefixes. |

---

## 15. End-to-end trace (concrete)

Graph: `n1 (Start, actionId="") → n2, n3 → n4`, where `n2` has `is_loop + is_split`,
`seperator="|"`, and `n4` has `user_review_required` on its parent `n2`.

```
POST /action_graphs/G/async_run   body: {"text": "Nike Diwali campaign", "urls": []}
 → UserActionGraphs.run_in_background
 → get_owned_or_shared(G)                     # + label sync
 → (Temporal available) init result doc R (status=processing, name=LLM-generated title)
 → start workflow "action-graph-R"
 ← 200 {id: R, status: processing}

[worker] execute_action_graph_activity
 → load R from S3
 → run_action_graph_v2
     inverted_map = {n1:[], n2:[n1], n3:[n1], n4:[n2,n3]}
     layers       = [[n1], [n2, n3], [n4]]

  layer [n1]:
     upstream=[] ; not loop → run_action_simple
     action_id "" → mock_action echoing the input → output.blocks = [ the input DataBlock ]
     result.output["n1"] = ActionDoc(mock)      → S3+Mongo write, webhook

  layer [n2, n3]:                                # SEQUENTIAL
   n2: upstream=[n1] done ; is_loop
       to_input(is_loop, is_split) → split n1's text on "|" → [DataBlock, DataBlock, ...]
       run_action_with_loop_concurrently: gather over semaphore(11), staggered by sleep_time
       collate → one ActionDoc whose output.blocks is every iteration's blocks
       result.output["n2"] = collated           → S3+Mongo write per iteration + final
   n3: upstream=[n1] done ; not loop → run_action_with_merge
       to_input(default) → "## Upstream: Start / ### Run 1 / **Output:** - Block 1: ..."
       (if >75k chars → uploaded to S3, text replaced with a pointer)
       result.output["n3"] = ActionDoc          → S3+Mongo write

  layer [n4]:
     upstream=[n2,n3] both done, BUT n2.user_review_required and not user_review_done
       → to_wait = True ; continue

  end: to_wait → status = waiting               → S3+Mongo write, webhook
```

The user reviews `n2` in the UI → `PUT /action_graphs/G` sets
`node_details.n2.data.user_review_done = true` → `POST /action_graphs/G/async_run?
action_graph_result_id=R` → n1/n2/n3 are skipped by `is_work_done`, n4 runs, status → `success`.
