# AUT-1580 · Branch summary — cyclic workflows, Part 1

> ⚠️ **Historical.** This spec's `K == 1` byte-identity promise was deliberately given up in Part
> 4.5 — a DAG now keeps a one-slot context vector so both shapes run one code path. See
> `AUT-1580_Part4.5_Work_Summary.md` §3. Turn numbering is run-wide now too, so anything here about
> turns counting per node from 0 no longer holds.



> Branch `AUT-1580-cyclic-workflow-v1`. Scope: **Part 1 of the cyclic-workflow plan**, plus the
> single-write-point work that had to land before the scheduler.
>
> **Nothing in this branch executes a cycle.** A graph with no redirect edges has `K == 1` and
> behaves exactly as it did before — that property is the point of the whole part, and it is
> asserted rather than assumed.
>
> Companion docs: `Action_Graph_Architecture.md` (how the engine works today),
> `Cyclic_Workflow_Implementation_Plan.md` (the 8-part plan, decisions, risks, open questions).

---

## 1. What shipped

Six commits, ~5,300 lines added / 220 removed across 20 files. Suite: **575 passed, 13 skipped**
(153 of them in `tests/graphs/`, up from 23 when the branch started).

| Commit | Contents |
|---|---|
| `1342b214e` | Architecture + implementation-plan docs |
| `333142393` | Edge grammar models; memoised the four unmemoised traversals |
| `6ac43f791` | Dashboard-field initialisation fix (RISK-8) |
| `a1e2ec932` | `GraphTopology` |
| `f59cc17d0` | Graph validation, warn-only |
| `7c6e9b607` | Context vectors + the single node-result write point |

### New modules

| File | Purpose |
|---|---|
| `action_graph/graph_topology.py` | Every structural fact, resolved once: R.N. set, `K`, context-index map, ancestors/descendants, precomputed `R.S.(i,j)`, start-node resolution, validation, and the two edge predicates |
| `action_graph/context_vector.py` | `ContextEntry` / `ContextSlot` / `NodeContextVector`, the `f_comb` merge, and the case-(i) seed |
| `action_graph/node_result_writer.py` | The one place a node result is written, plus the drift guards |
| `action_graph/graph_validation.py` | Validation on the write path, warn-only behind a setting |

---

## 2. The five parts of the work

### 2.1 Edge grammar (additive, no migration)

`graph` keeps its exact current meaning — the forward/data adjacency, which must stay **acyclic**.
Cycles are expressed only in new optional fields:

```python
redirect_edges:    List[EdgeSpec]    # dashed - back edge j ⇢ i
conditional_edges: List[EdgeSpec]    # dotted - a data edge the caller may decline to fire
```

`EdgeSpec` carries **no `type` field**: the list an edge lives in is what types it, so there is no
second source of truth to drift. Absent/empty ⇒ `K == 1` ⇒ old behaviour, byte for byte.

On `ActionGraphUpdate` both default to **`None`, never `[]`** — `CRUDBase.update_one` builds its
`$set` from `model_dump()` skipping only `None`, so a `[]` default would be written on *every* save
and silently wipe a graph's edges on an unrelated rename.

### 2.2 `GraphTopology`

Built once per graph; the engine never rediscovers structure ad hoc.

```
R.S.(i, j) = ( subgraph(i) ∩ ancestors(j) ) ∪ descendants(j)
```

"Related to *j*" means **comparable to *j*** — ancestor or descendant. Both populations matter:
ancestors of *j* re-run because *i* re-runs and feeds down the loop; descendants re-run because *j*
recomputes and feeds them. Incomparable branches take no part. `descendants(j)` needs no clipping,
since *j* ∈ subgraph(*i*) implies subgraph(*j*) ⊆ subgraph(*i*).

**`build()` never raises.** Validation ships warn-only, so a graph that fails it must still run —
every traversal is cycle-safe and `start_node_id` degrades to `None` rather than throwing.

Also exposes two authoring predicates, `check_redirect_edge` / `check_conditional_edge`, returning a
truthy `EdgeCheck` with a reason. The validators **call these**, so the answer the UI gets before
drawing an edge and the answer validation gives afterwards cannot disagree.

### 2.3 Validation, warn-only

Gated by `ACTION_GRAPH_VALIDATION_ENFORCED` (default `False`), so flipping to reject is config, not
code. Wired into `UserActionGraphs.create` / `.update` and `SpaceActionGraphs.update_action_graph`.

- **Update validates the merged *result*, not the patch** — a patch can look harmless and only be
  invalid once combined with the stored graph. `touches_topology()` gates it so a rename skips the
  extra read entirely. **Note:** the label-sync
  (`sync_and_save_node_details_label_with_action_name`) does not benefit from that gate — it calls
  `action_graph_crud.update_one` directly, bypassing `UserActionGraphs.update` and therefore
  validation altogether. A read path that silently writes without validating; worth closing.
- `merge_topology` uses field values, not `model_dump()` — dumping turns `redirect_edges` into dicts
  and `model_copy` does not re-validate.
- New `GET /action_graphs/{id}/validation` → `{is_valid, is_cyclic, issues[]}`, so the editor can
  surface warnings during the warn-only phase. A separate endpoint on purpose: `model_dump()` feeds
  several persistence paths, and a computed field would get written to Mongo and S3.

### 2.4 Context vectors

```
V_n = [ V_n0, V_n1, ... ]    V_ni = the context n should have when the redirect into it is due to i
```

Slot `0` is the baseline. Entries are `(input, output, node_id, turn_no)` where `node_id` is a
**provenance tag** — the start node on the forward pass, the redirecting node on a re-entry — and
`turn_no` counts **per (slot, tag)**, independently and sparsely.

`f_comb` (`merge_slots`) is pure addition: no overwrite, no duplicates, identity `(turn_no, node_id)`,
existing entry wins. Sort key is `(turn_no, start_rank, node_id)`.

Three properties worth keeping in mind:

- **The start-first rank is mandatory, not cosmetic.** Node ids are digit strings and the start node
  id is a letter, so plain lexicographic ordering sorts the start node **last** (`'0'`=0x30 < `'a'`=0x61).
- **`start ∉ R.N.`, provably.** A redirect source must be a descendant of its target; the start node
  is the data-DAG root, so a descendant of nothing. Hence `(start, 1)` is unreachable, and the tag
  namespace `{start} ∪ R.N.` is disjoint — a bare tag says unambiguously which kind of entry it is.
- **One node execution = one entry**, not one per `ActionResult`. A loop node fans out into N runs
  collated into a single ActionDoc; that is still one entry *into* the node.

### 2.5 The single write point

`result.output[node]` and `node_vectors[node]` must agree, but **readers only ever touch the first** —
so a missed vector write is invisible until the scheduler reads a slot and gets the wrong context.

All ~7 direct assignments now go through `record_node_result` / `forget_node` / `forget_all`.
`NodeRunContext.for_dag(node)` writes no vector at all, so *"a DAG keeps `node_vectors` None"* holds
structurally rather than by convention.

`upsert_entry` was added for this: a loop node reports *progressively*, so `append_entry` would make
one execution into N entries and `merge_slots` would keep the first partial collation.

Guards:

1. **A source-level test** — the only one currently wired, and the only one that *prevents* drift
   rather than detecting it. Fails on any write to `.result.output` / `.result.node_vectors` (both
   subscripted and whole-dict) across `action_graph/` **and** `conn/temporal/action_graph/`,
   resolved from the repo root rather than CWD.
2. `assert_baseline_mirror` and `assert_vector_covers_output` exist but have **no call site yet** —
   they compare a vector that nothing writes until Part 3, so wiring them now would wire no-ops.
   Part 3 calls them once it has a `start_node_id` to compare against.

---

## 3. Ownership split (decided, enforced)

| | Holds | Notes |
|---|---|---|
| `result.output[node]` | the **executable** (`id`, `name`, `type`, `config`) **+** the baseline record | Kept permanently. `run_node_action_with_existing_doc` re-executes from the *stored* doc, and `action_factory` hard-requires `type`/`config` |
| `node_vectors[node]` | the context, tagged by provenance and indexed by redirector | Populated **only when `K > 1`** |

The vector does **not** subsume the ActionDoc. For loop nodes the ActionDoc carries *more*:
`ContextEntry` is one execution, `ActionDoc.results` is one entry per iteration, and
`dashboard_service.py:1096` counts `len(output[node].results)` as files processed. So the correct
framing is *"the ActionDoc is the finer-grained record, the vector is the coarser context-indexed
one"* — **not** "the vector will eventually replace it".

---

## 4. Bugs fixed on the way

| Bug | Impact |
|---|---|
| **`ActionGraphUpdate` dashboard fields defaulted to `0.1`/`50`, not `None`** | Live in `main`. Every partial save reset a user's configured values — including the label-sync that fires on every `get_owned_or_shared`, so merely *opening* a workflow could reset them. `ActionUpdate` already had this right |
| **Four traversals had no `visited` set** | Exponential re-visits on a plain DAG (2^k on a k-diamond ladder). One duplicated prompt lines; one issued a **DB call per path** |
| **`build_node_group_profile_prompt` aliased `result.graph`** | `to_visit_nodes = child_nodes` then `+=` mutated the graph in place |
| **`coffman_graham` silently dropped cycle nodes** | Those nodes never executed and the run still reported `success`. Now raises `GraphCycleError` (400) naming them — and the call was moved *inside* `run_action_graph_v2`'s `try`, or the run would sit in `processing` until the 720-minute stuck monitor |
| **Three dead functions** | `run_action_with_text_split_and_loop`, `run_action_with_file_split_and_loop`, `run_action_with_loop_synchronously` — deleted |

---

## 4b. Review findings addressed

An independent review raised ten points; nine held up under verification.

| Fixed | |
|---|---|
| `conn/temporal/action_graph/activity.py:93` was an **unguarded 8th write site on the production path** — the Temporal twin of the engine's `forget_all`. The guard missed it twice: it scanned only `action_graph/`, and its regex matched subscripted writes but not `.output = {}`. Now routed through `forget_all`; the guard scans both trees, matches both shapes, and resolves from the repo root instead of CWD |
| The `coffman_graham` raise **contradicted the warn-only posture** — the one change flipping a currently-succeeding run to `error`, while `_validate_structure` reported the same condition as a mere issue. Now gated by `raise_on_cycle`, which the engine passes from `ACTION_GRAPH_VALIDATION_ENFORCED`, so both flip together. Validation always passes `True` |
| **Validation fired on ordinary authoring** — a fresh graph reports `no_start_node`, an unwired node adds `isolated_node`, and `create`/`update` run on every new workflow and autosave. That buried the RISK-1 signal. `ValidationIssue` now carries `severity`; only the **error** class is enforced or logged as a failure |
| `record_node_result` / `forget_node` / `forget_all` now **assert the full-doc precondition**. `result` is `ActionGraphDoc \| ActionGraphLiteDoc`, and a lite doc would raise an `AttributeError` that `empty_downstream_nodes`' broad `except` swallows into "emptied nothing" |
| **`EdgeType` removed** — dead, and precisely the second source of truth `EdgeSpec` is designed to refuse |

| Recorded, deliberately not changed | |
|---|---|
| `node_details` in `TOPOLOGY_FIELDS` means a node *drag* costs an extra read plus an `O(V·(V+E))` build. Kept for correctness — `node_details` keys define the node set, so excluding it would miss real topology changes. Revisit with Part 2 |
| `node_vectors` is writable over the public create/update body — consistent with `output`, which already is. Worth restricting, but as a deliberate change to both |
| The Mongo copy drops `node_vectors` at `user_action_graph_result.py:481` via `ActionGraphLiteDoc(**...)`. Intended — Mongo holds the metadata projection, S3 the full document — but previously unwritten down, and Part 2 reasons about exactly this split |

The one imprecision in the review: "turns a currently-succeeding run into an error" is true, but
those runs succeeded *while silently skipping every node inside the cycle*. Not a working run
breaking — a broken run becoming visible. It does not change the recommendation.

## 5. Verification

Every load-bearing rule was checked against its *wrong* version, not just asserted:

| Rule | Broken deliberately → |
|---|---|
| Traversal memoisation | 5 failures |
| `Optional[...] = None` update contract | 2 failures |
| Dashboard-field defaults | 1 failure |
| `R.S.` = ancestors-only | 4 failures |
| `R.S.` = whole subgraph | 2 failures |
| Start-first sort rank | 6 failures |
| `f_comb` overwriting instead of adding | 1 failure |
| A new direct `output[...] =` write | source-level test names the file and line |

One test (`get_subgraph_map` visit count) passed *both* ways on first writing and was rewritten with
a counting adjacency — it had been asserting on an idempotent result rather than on the behaviour.

### Live API testing

Full local stack (Temporal + worker + API) against real graphs. **13/14 run modes passed**:
fresh `async_run`, resume into an existing run id, single-node rerun, rerun whole graph, rerun from
node, rerun downstream-only, continue-run, `batch_run` at default and `batch_size=1`, the validation
endpoint, and three guard paths.

Backward compatibility confirmed on stored production graphs: all read back with `redirect_edges: []`,
`conditional_edges: []`, `node_vectors: null`, and all validate clean — so **D7's single-root rule
rejects none of them**, an early positive signal for RISK-1.

---

## 6. Pre-existing bugs found, not fixed

Ordered by severity. None originate in this branch.

1. **`batch_id` is never persisted on the Temporal path.** The Temporal branch's `init()` call omits
   it, and `grep -c batch_id activity.py` → **0**. A `BatchDoc` is created with `total=N` and left
   orphaned, so batch progress cannot be reported. Temporal is the production path.
2. **`run_group_chat` 500s for the standard graph shape.** `get_director_node` picks the graph root,
   which by convention is the mock Start node with `actionId: ""`; the lookup returns `None` and a
   bare `assert` blows up (`action_group.py:269` — there is a `# TODO: remove start node` right
   above it). It also leaves the run **stuck in `processing` forever**, because the result doc is
   created before the call and never marked failed.
3. **`DELETE` on a run does not delete it.** Only the Mongo row is removed; `get_action_graph_result`
   reads **S3 first**, so `GET /{id}` still returns the run in full — prompts and outputs included.
4. **`get_temporal_client()` has no error handling**, so the in-process fallback that
   `run_in_background`'s `if temporal_client:` clearly anticipates is unreachable from HTTP. With
   Temporal down the endpoint returns a raw tonic transport error.
5. **`realtime_indexer.py:45` projects `result.output` from Mongo**, which stores `ActionGraphLiteDoc`
   — no such field. For graph runs its per-node chunking is a silent no-op.
6. **`empty_downstream_nodes`' broad `except Exception`** converted a real `AttributeError` into
   "emptied nothing" during testing. Any future error there fails silently and a rerun then runs
   against stale results.

---

## 7. Running it locally

Three processes. Without the worker, `async_run` returns `processing` forever and nothing executes —
indistinguishable from a hung run.

```bash
brew install temporal                                   # one-off; ships a dev server

temporal server start-dev --port 7233 --ui-port 8233 \
  --db-filename /tmp/temporal.db                        # 1. Temporal (UI :8233)
make dev-runner                                         # 2. worker (runner:app, :8005)
make dev                                                # 3. API (:8000)
```

`ENV=local` already disables client TLS. `--db-filename` matters: the dev server is in-memory by
default, so restarts lose in-flight workflows.

---

## 8. What's next

**Part 2 — persistence rework.** `update_action_graph_result` does a full S3 read + full-document
write + Mongo update + Redis bump **once per node and once per loop iteration**. Cycles multiply
executions and the vector multiplies document size, so this lands before the scheduler. Benchmark the
DAG case (no vectors) and the cyclic case (K slots) separately.

Then Part 3 (scheduler: redirect stack, case (i)/(ii), per-pair budgets) — which supplies a real
`NodeRunContext` per execution and **does not need to touch a write site**.

### Handover to the frontend — outstanding

- Two new optional array fields: `redirect_edges`, `conditional_edges`, each `[{source, target, id?}]`.
- **A conditional edge must be written in *both* places** — into `graph` like any other edge, *and*
  as a pair in `conditional_edges`. Writing only the latter fails validation.
- `GET /action_graphs/{id}/validation` for editor warnings.
- `check_redirect_edge` / `check_conditional_edge` exist for pre-flight "can I draw this?" answers
  but are **not exposed over HTTP yet** — worth deciding whether the FE wants an endpoint.

### Open questions (later parts)

**OQ-3** C.R.N. liveness · **OQ-4** revocation · **OQ-8** whether a stale per-edge queue is read at
fire time. **RISK-1**: validation is warn-only — collect real failure rates from the
`validation_codes` log field before flipping `ACTION_GRAPH_VALIDATION_ENFORCED`.
