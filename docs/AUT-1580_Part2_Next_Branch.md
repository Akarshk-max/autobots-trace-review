# AUT-1580 · Next branch — Part 2: persistence rework

> ⚠️ **Historical.** This spec's `K == 1` byte-identity promise was deliberately given up in Part
> 4.5 — a DAG now keeps a one-slot context vector so both shapes run one code path. See
> `AUT-1580_Part4.5_Work_Summary.md` §3. Turn numbering is run-wide now too, so anything here about
> turns counting per node from 0 no longer holds.



> Prerequisite: `AUT-1580-cyclic-workflow-v1` (Part 1) is merged. See
> `AUT-1580_Part1_Work_Summary.md` for what landed, and
> `Cyclic_Workflow_Implementation_Plan.md` for the 8-part plan.
>
> **This branch changes no behaviour.** Same runs, same result documents, far fewer round trips. It
> lands before the cyclic scheduler because cycles multiply node executions and the context vector
> multiplies document size — both of which make the current write path worse in proportion.

---

## 1. The problem, measured

Every call to `UserActionGraphResult.update_action_graph_result`
(`user_action_graph_result.py:435-505`) does **all** of this:

| # | Step | Cost |
|---|---|---|
| 1 | `get_action_graph_result(...)` | **S3 GET of the whole run document** |
| 2 | `model_copy(update=...)` | full model copy |
| 3 | `model_dump_json()` | serialise the whole document |
| 4 | `fs_crud.upload_binary(...)` | **S3 PUT of the whole document** |
| 5 | `event_result_crud.update_one(...)` | Mongo write (lite copy) |
| 6 | `_bust_search_cache_for_user(...)` | Redis generation bump |

It is called **10 times** from the engine:

| Site | Frequency |
|---|---|
| `run_action_graph_v2:336` | once, at start |
| `run_action_graph_v2:540` | **once per node** |
| `run_action_graph_v2:566` / `:577` | once, final status |
| `run_node:651` / `:670` / `:685` | 3× per single-node rerun |
| `run_action_with_excel_loop:1233` | **once per row** |
| `run_action_with_loop_concurrently:1656` | **once per iteration**, under the lock |
| `run_action_with_loop_concurrently:1705` | once, final collation |

### It is quadratic, not linear

The two per-iteration sites write the *entire* run document each time, and that document **grows**
with each iteration because `collate_action_results` appends. Iteration *i* writes a document
containing *i* results, so total bytes ≈ **n²/2**.

A 200-row excel loop is therefore ~200 S3 GETs, ~200 S3 PUTs, 200 Mongo writes, 200 Redis bumps, and
roughly **20,000 result-objects' worth of JSON serialised** to persist 200. Under `asyncio.gather`
with an 11-slot semaphore, those writes also serialise behind `lock`, so the persistence cost is on
the critical path rather than beside it.

Cycles make each of those numbers larger by the lap count, and the context vector multiplies
document size by `K`.

---

## 2. Scope of this branch

### 2.1 Drop the read-before-write

Step 1 exists so the update can merge `exclude_unset` fields onto the stored document. But **the
engine already holds that document in memory** — it passes `result=action_graph_result_doc.result`
straight back in. The S3 GET re-fetches what the caller just had.

Add a path that takes the caller's document as authoritative and skips the fetch. Keep the fetching
path for external callers (the `PUT /action_graphs_results/` endpoint, which genuinely has only a
partial patch).

**Halves the S3 traffic on its own.**

### 2.2 Coalesce the per-iteration writes

`run_action_with_excel_loop:1233` and `run_action_with_loop_concurrently:1656` write after every
iteration purely so the UI can show partial progress. That is a *progress* requirement, not a
*durability* one — and there is already a separate progress channel (`Notify` → Redis pub/sub → SSE).

Options, in preference order:

1. **Time-throttled flush** — at most one write every N seconds, plus an unconditional final write.
   Keeps partial results durable, bounds the cost, no API change.
2. **Every Nth iteration** — simpler, but N is wrong at both ends of the size range.
3. **Progress via SSE only, persist once at the end** — cheapest, but loses partial results if the
   worker dies mid-loop. Only acceptable if the run is retried from scratch anyway.

Recommend (1). Whatever is chosen, the **final** write must be unconditional — the current code
already has a final collation write at `:1705`, so the invariant is available.

### 2.3 One cache bump per run, not per node

`_bust_search_cache_for_user` bumps a Redis generation counter and invalidates the in-memory search
cache on **every** write. Per node and per loop iteration, that is pure waste — the run's searchable
fields (`name`, `status`) change at most a handful of times. Bump on status transitions and at run
end.

### 2.4 Clean up the `action_response` aliasing

Flagged during Part 1 and deliberately deferred. `action_response` is bound once to `result.output`
(`action_graph.py:349`, `:621`), but `action_graph_result_doc` is **rebound by every
`update_action_graph_result`** — which re-reads from S3 and only re-attaches the caller's object when
the update carries `result=`. The status-only update at `:566` does not, so `action_response` can end
up pointing at a detached dict.

Harmless today only because it happens after the node loop. Once nodes re-enter, it is not.

Drop the `action_response` parameter from the seven helper signatures in favour of
`node_result_writer.node_results(result_doc)`, which reads through the live document. The writer
already writes through `result_doc.result`, so the write side is done.

**Note:** §2.1 makes this partly self-solving — if the update stops re-fetching, it stops rebinding.
Do both; the parameter removal is what prevents it recurring.

### 2.5 Keep intact

- S3-primary / Mongo-lite split, and the S3-then-Mongo read fallback.
- `ActionGraphLiteDoc` as the Mongo shape (no `output`, no `node_vectors`).
- Final documents **byte-identical** to what the current code produces.

---

## 3. Exit criteria

1. A benchmark graph shows a large reduction in S3 round trips per run, with **byte-identical**
   final documents. Compare `main` vs branch on the same input.
2. Benchmark **both** shapes separately, since only one carries the extra payload:
   - a DAG (`node_vectors is None`),
   - a cyclic graph (`K > 1`, K slots per node).
3. A loop node of ~200 iterations shows sub-quadratic total bytes written.
4. `tests/graphs/` (148 tests) stays green, unchanged.
5. Partial results still visible mid-run — whatever throttle is chosen must not make a long loop look
   frozen.

`tests/load_testing/load_test_endpoints.py` exists as a starting point;
`load_testing_action_graph_runs.py` is referenced in the architecture doc.

---

## 4. Decide first: do the pre-existing bugs ride along?

Six were found during Part 1 testing. None originate in that branch, and none are fixed. They are
**not** persistence work, so the honest options are a sibling branch or a deliberate inclusion — but
they should not be silently folded in.

Ranked by severity:

| # | Bug | Why it matters | Size |
|---|---|---|---|
| 1 | **`batch_id` never persisted on the Temporal path.** The Temporal branch's `init()` omits it; `grep -c batch_id activity.py` → 0. A `BatchDoc` is created with `total=N` and orphaned. | Temporal **is** production. Batch progress cannot be reported at all. | ~2 lines, but needs a call on which end owns it |
| 2 | **`run_group_chat` 500s for the standard graph shape.** `get_director_node` picks the root, which by convention is the mock Start node with `actionId: ""`; a bare `assert` at `action_group.py:269` blows up. Leaves the run **stuck in `processing` forever**. | Endpoint is unusable on every graph following the Start-node convention. | small — skip the start node when choosing a director; also mark the run failed |
| 3 | **`DELETE` on a run does not delete it.** Only the Mongo row goes; `get_action_graph_result` reads S3 first, so `GET /{id}` still returns the run in full. | User-deleted prompts and outputs stay retrievable by id indefinitely. | small; needs a decision on existing orphaned S3 objects |
| 4 | **`get_temporal_client()` has no error handling**, so the in-process fallback that `run_in_background`'s `if temporal_client:` anticipates is unreachable from HTTP. | With Temporal down, runs 500 with a raw transport error instead of falling back. | small |
| 5 | **`realtime_indexer.py:45` projects `result.output` from Mongo**, which stores `ActionGraphLiteDoc` — no such field. | Per-node chunking for graph runs is a silent no-op. | investigate first; may be dead code |
| 6 | **`empty_downstream_nodes`' broad `except Exception`** turns real errors into "emptied nothing". | A rerun then runs against stale results, silently. | narrow the handler |

**Suggestion:** #1 and #2 are production-visible and small — worth their own short branch *before*
Part 2, since neither touches the persistence path and both are currently breaking real usage.

---

## 5. After this branch

**Part 3 — cyclic scheduler.** The first part where a cycle actually executes:

- frontier/work-queue scheduler that can re-enter nodes, replacing the layer walk;
- redirect stack, push-on-redirect / pop-on-loop-closure with the same-context condition, supporting
  multiple coexisting stacks;
- case (i) / case (ii) read-write asymmetry;
- `f_comb` within a slot;
- per-`(i,j)` budgets with the pause-and-ask exhaustion path (**D5**), with counters stored
  **durably on the result doc** — three retry layers already stack, so an in-memory counter would
  reset on retry (RISK-4).

Part 3 supplies a real `NodeRunContext` (tag, turn_no, target_slots) per execution instead of
`NodeRunContext.for_dag(node)`, and **does not need to touch a single write site** — Part 1 routed
them all through `record_node_result`.

### Still outstanding, unrelated to Part 2

- **Frontend handover** (Part 1 DoD item 5): the two new edge fields; the rule that a conditional
  edge must be written into **both** `graph` and `conditional_edges`; `GET /{id}/validation`; and
  whether to expose `check_redirect_edge` / `check_conditional_edge` over HTTP.
- **RISK-1**: validation is warn-only. Collect real failure rates from the `validation_codes` log
  field before flipping `ACTION_GRAPH_VALIDATION_ENFORCED`. Early signal is good — all stored
  production graphs tested validate clean.
- **Open questions**: OQ-3 (C.R.N. liveness), OQ-4 (revocation), OQ-8 (stale per-edge queue at fire
  time — a Part 3 decision).
