# AUT-1580 · Next branch — Part 3: the cyclic scheduler

> ⚠️ **Historical.** This spec's `K == 1` byte-identity promise was deliberately given up in Part
> 4.5 — a DAG now keeps a one-slot context vector so both shapes run one code path. See
> `AUT-1580_Part4.5_Work_Summary.md` §3. Turn numbering is run-wide now too, so anything here about
> turns counting per node from 0 no longer holds.



> Branch `AUT-1580-cyclic-workflow-v3`. Prerequisite: Parts 1 and 2 are merged (PRs #187, #188).
> See `AUT-1580_Part1_Work_Summary.md`, `AUT-1580_Part2_Work_Summary.md`, and
> `Cyclic_Workflow_Implementation_Plan.md` §1.4–§1.9 for the algorithm this implements.
>
> **This is the first branch where a cycle actually executes.** Parts 1 and 2 were deliberately
> inert: Part 1 built the vocabulary (edge grammar, topology, context vectors) and Part 2 made the
> write path affordable. Neither changed what runs. This one does.
>
> The safety property carries over unchanged and is still the thing to protect: **a graph with no
> redirect edges has `K == 1` and must behave exactly as it does today.**

---

## 1. Why the engine cannot cycle today

`run_action_graph_v2` schedules with a **layer walk**:

```python
nodes_order: List[List[str]] = coffman_graham(inverted_map, len(total_nodes))
for nodes in nodes_order:          # layer
    for node in nodes:             # node within layer
        if await ActionGraph.is_work_done([node], action_response): continue
        ...
```

Three properties of that loop make cycles impossible, and each has to go:

| # | Property | Why it blocks cycles |
|---|---|---|
| 1 | **The layer list is computed once, up front.** | A node's position is fixed before the run starts. Nothing can put a node back in the queue. |
| 2 | **`is_work_done` means "key present in `output`".** | Once a node has run, it is permanently done. Re-entry is indistinguishable from "already finished". |
| 3 | **`coffman_graham` raises on a cycle.** | Part 1 made it raise deliberately (gated on `ACTION_GRAPH_VALIDATION_ENFORCED`) rather than silently drop the nodes. It operates on the data adjacency, which stays acyclic — so this is only a problem if redirect edges ever leak into `graph`. They must not. |

Everything else Part 3 needs already exists. `GraphTopology` hands over `K`, `context_index`,
`relevant_subgraph`, `redirecting_nodes` and `start_node_id`, all precomputed.
`NodeRunContext(tag, turn_no, target_slots)` is already the writer's input — Part 1 built it for
exactly this moment and every write site already routes through it. Part 2's `save_run_doc` takes a
document rather than a patch, so a re-entering node saves the same way a forward-pass node does.

**No write site needs to change in Part 3.** That was the point of the two preceding branches.

---

## 2. Scope

### 2.1 Frontier scheduler

Replace the layer walk with a work queue that can re-arm nodes. Data edges still give the forward
order; redirect edges put nodes back on the queue.

**The ordering decision — make it before writing code.** A frontier scheduler and a Coffman-Graham
layering will not, in general, visit a DAG's nodes in the same order. Same results, different order,
different logs. Three options:

| Option | Trade |
|---|---|
| **(a) Two schedulers**: keep the layer walk for `K == 1`, frontier only for `K > 1`. | Byte-identical DAG behaviour by construction. Cost: two code paths, and the cyclic one gets far less production exercise. |
| **(b) One frontier scheduler, seeded in Coffman-Graham order.** The ready-set is drained in layer order, so a DAG executes in exactly today's sequence. | One code path, identical DAG order. Needs the priority to be a stable, precomputed rank. **Recommended.** |
| **(c) One frontier scheduler, arbitrary ready order.** | Simplest. Changes DAG execution order — which is observable in logs, in `Notify` messages, and in any node whose action has side effects. Rejected. |

**Recommend (b).** It keeps the "K == 1 ⇒ nothing changed" guarantee that Parts 1 and 2 both held,
without maintaining two schedulers. The layer index becomes a sort key, not a control structure.

### 2.2 The redirect stack (§1.8)

Push on redirect, pop only on genuine loop closure:

```python
on redirect(j -> i):    Q.push(j)
p = Q.top()
if p == current_node and context == context_of(p):    Q.pop()
```

Three things here are easy to get wrong and each needs its own test:

- **The pop condition is two-part.** Reaching *j* is not enough — it must be reached *in the same
  redirected context the stack top defines*. The same node under a different context is a different
  event and must not pop.
- **Propagation is by deep copy, never by reference** (§1.8.1). `dispatch(child, queue=deepcopy(q))`.
  Under shared references, a push on one branch is visible on every sibling holding the same object,
  and a later sibling mutation can retroactively change a queue a node already received. The
  copy-on-write optimisation is explicitly deferred until the eager version is correct and measured.
- **Multiple stacks coexist**, one per redirect chain. There is no single global traversal state.

**Fan-in takes any one incoming queue (C1) and asserts the others are equal (C3).** The assert is a
canary, not a merge point. A mismatch means Lemma 1 or Lemma 2 has been violated — raise it loudly,
never reconcile. This is the one place where "be permissive" is the wrong instinct.

### 2.3 Case (i) / case (ii) asymmetry (§1.4)

| Case | Condition | Behaviour |
|---|---|---|
| **(i)** | every slot still `None` | run normally, write the same result into **every** slot — establishes the baseline |
| **(ii)** | slots populated | read **only** `V_nk` where *k* is the R.N. responsible |

`context_vector.seed_all_slots` already implements case (i)'s broad write. Case (ii)'s narrow read
is where the interim limitation in §3 bites.

### 2.4 `f_comb` within a slot (D3)

Already built and tested in Part 1 — `merge_slots`, `upsert_entry`, per-`(slot, tag)` turn counters,
start-first ordering. Part 3 supplies real `tag` and `turn_no` values instead of
`NodeRunContext.for_dag(node)`'s defaults. No new merge logic.

### 2.5 `is_work_done` generalised

From "key present in `output`" to a per-slot, re-armable predicate. This is the single most
load-bearing change in the branch, because it is what stops a re-entered node being skipped as
"already finished" — and it is on the path of *every* run, DAG included. For `K == 1` it must reduce
to exactly today's key-presence check.

### 2.6 Per-`(i,j)` budgets, durable (§1.9, D5, RISK-4)

One counter per redirect edge, `|R.E.|` rows — not global (trips on wide graphs with many healthy
cycles) and not per-node (conflates distinct loops sharing a node).

**Counters must be stored on the result document, not in memory.** Three retry layers already stack
— Temporal ×3, tenacity ×3, attachment ×3 — so an in-memory counter resets on retry and the budget
silently stops bounding anything. This needs a new field on the run's graph doc, and it rides
Part 2's `save_run_doc` like everything else.

On exhaustion: **pause and ask** — status `waiting`, exhausted pair recorded on the run, resume via
the existing same-`result_id` path. This reuses the user-review gate that already exists (`to_wait`).
Until Part 8's inbox exists, also log loudly and fire the webhook (RISK-3).

### 2.7 Wire Part 1's dormant guards

`assert_baseline_mirror` and `assert_vector_covers_output` have existed since Part 1 with **no call
site**, because they compare a vector nothing wrote. Part 1 recorded this as a Part 3 task, and a
recent review flagged it again. Part 3 is when they stop being no-ops:

- `assert_vector_covers_output` — once at run completion. O(nodes), no payload comparison.
- `assert_baseline_mirror` — once per node after its final write, never per loop iteration; the deep
  comparison is the expensive part and only the last write matters.

Both log by default and raise only under `strict`. Drift is a data-quality bug, not a reason to fail
a run the user has already paid for.

---

## 3. Explicitly not in Part 3

**Writes land in slot *k* only. Propagation across the turn's full slot set is Part 4.**

> **Corrected 2026-09-02.** "Cross-node" is wrong: propagation never writes into another
> node's slot. Part 4 widens which of a participant's **own** slots are written, to the
> dimensions of the R.N.s inside `R.S.(i,j)`. See `Cyclic_Workflow_Implementation_Plan.md`
> §Part 4.

The distinction is subtle enough to state twice, because `relevant_subgraph` is used by both parts
for *different purposes*:

| | Uses `R.S.(i,j)` for | Part |
|---|---|---|
| **Scheduling** — which nodes re-run this lap | deciding the re-run set; an incomparable sibling is not in `R.S.` and does not re-run, which is what makes §1.8.2's theorem hold | **Part 3** |
| **Propagation** — which *slots of each participant* get written | see the correction above; Part 4| **Part 4** |

**The honest limitation:** `ActionGraphInputCreator.to_input` still reads the baseline ActionDocs,
not the slot transcript. So Part 3 makes a cycle **execute and record** correctly; Part 4 makes it
**read** correctly. A re-entered node's prompt is still built from baseline outputs until then.

That is coherent for the simplest cycles — a single node re-entered, where the slot and the baseline
carry the same thing — and it keeps this branch reviewable. It must not be over-claimed: **"a cycle
runs" is not yet "a cycle reasons over its own transcript."** Any demo or test written for Part 3
should be one where that distinction does not silently flatter the result.

Also out of scope: C.R.N. groups and conditional-edge firing (Part 5), user operations as vector
edits (Part 6), and the copy-on-write queue optimisation (§1.8.1, deferred by the plan itself).

---

## 4. Decide first

| # | Question | Recommendation |
|---|---|---|
| 1 | Scheduler shape — §2.1 (a) / (b) / (c) | **(b)**, one frontier scheduler seeded in Coffman-Graham order |
| 2 | **OQ-8** — is a stale per-edge queue consulted at fire time? | **Reading (A)**: the queue belongs to the *wave*, not the edge. No extra re-execution, matches "top = nearest enclosing redirection", keeps the C3 assert meaningful rather than routinely false. The plan already carries (A) as the working assumption; Part 3 is where it becomes code. |
| 3 | **OQ-2** — why fill every slot in case (i)? | Implement as written. §1.12's rerun rule (`V_nk := V_n0`) is only coherent if the baseline is uniform. |
| 4 | Where do budget counters live on the doc? | New optional field on the graph doc, `None` for a DAG — same discipline as `node_vectors`, so a DAG run stays byte-identical. Must default to `None`, never `{}` (Part 1's `ActionGraphUpdate` lesson: a `{}` default gets written on every save). |
| 5 | Does the frontier scheduler run nodes concurrently within a wave? | **No, not in Part 3.** Today's loop is sequential within a layer. Adding concurrency and re-entrancy in one branch makes any bug twice as hard to attribute. Revisit after the semantics are proven. |

---

## 5. Risks

| # | Risk | Mitigation |
|---|---|---|
| **R3-1** | `is_work_done` is on the path of every run, DAG included. A regression here breaks all workflows, not just cyclic ones. | It must reduce provably to key-presence when `K == 1`, with a test that asserts exactly that. Land it as its own commit, ahead of the scheduler. |
| **R3-2** | A cycle that never converges burns budget ×3 retry layers before anyone notices. | RISK-4's durable counters, plus a test that a retry *resumes* the count rather than restarting it. |
| **R3-3** | Deep-copying the queue per dispatch is `O(fan-out × depth)`. | Measure it with Part 2's probe harness before reaching for copy-on-write. The plan explicitly says correctness first. |
| **R3-4** | The C3 equality assert fires in production on a graph shape we did not anticipate. | Log-and-continue with the offending queues recorded, behind the same `strict` discipline as the mirror guards. A canary that takes down runs is a worse canary. |
| **R3-5** | Over-claiming Part 3. A cycle that executes but reads baseline context can look correct on a shallow demo. | §3's limitation stated in the DoD, and at least one test that would *fail* if slot reads were silently substituted by baseline reads. |

---

## 6. Exit criteria

1. **`K == 1` is byte-identical.** Same execution order, same documents, same counts as Part 2's
   benchmarks. `tests/graphs/` stays green.
2. A single-node cycle executes, terminates, and records tagged entries with correct `turn_no`
   sequencing per `(slot, tag)`.
3. A multi-node cycle executes the correct re-run set — the `R.S.(0,5)` shape from §1.5, where the
   incomparable sibling (node 1) must **not** re-run. `R.S.(0,2)` alone is degenerate and would pass
   under a wrong rule; both must be tested, as Part 1 did for the topology.
4. The pop condition is tested against its wrong version: reaching *j* under a *different* context
   must not pop.
5. Queue propagation is by deep copy — a test that mutates a sibling's queue and asserts the other
   branch is unaffected.
6. The C3 equality assert is exercised by a graph that satisfies it, and by a deliberately broken
   one that does not.
7. Budget exhaustion parks the run at `waiting` with the pair recorded, and a resume against the
   same `result_id` continues rather than restarting.
8. Budget counters survive a simulated retry.
9. `assert_vector_covers_output` and `assert_baseline_mirror` have live call sites and are shown to
   fire on injected drift.
10. Part 2's write-amplification benchmarks still hold for `K > 1` — the cyclic shape was measured at
    K = 2/4/8 in Part 2 and must not regress now that something actually writes those slots.

---

## 7. Suggested commit sequence

Each step should be separately reviewable and green.

| # | Commit | Why this order |
|---|---|---|
| 1 | Generalise `is_work_done` to a per-slot re-armable predicate, reducing to key-presence at `K == 1` | Highest-blast-radius change, landed alone where a regression is unmissable |
| 2 | Budget model: durable counters on the doc, `None` for a DAG | Pure data, no behaviour; unblocks the scheduler's guardrail |
| 3 | The redirect stack as a standalone, unit-testable structure (push/pop/deep-copy/equality assert) | Testable without the engine; the trickiest logic in isolation |
| 4 | Frontier scheduler replacing the layer walk, seeded in Coffman-Graham order, still `K == 1` only | Prove "nothing changed" before anything cycles |
| 5 | Wire the stack in: re-entry, case (i)/(ii), real `NodeRunContext` | The first commit where a cycle runs |
| 6 | Budget exhaustion → `waiting` + resume path | The guardrail, once there is something to guard |
| 7 | Wire the Part 1 mirror guards | They can only be meaningful once vectors are actually written |
| 8 | Part 3 work summary doc | Same shape as Parts 1 and 2 |

---

## 8. Carried forward, unchanged

- **Bugs #1–#5** from Part 1 §6 — still open. #1 (`batch_id` never persisted on the Temporal path)
  and #2 (`run_group_chat` 500s and strands the run) remain the production-visible pair.
- **The `Clock.datetime_now()` decision** from Part 2 §5 — naive local time into a tz-aware field.
- **RISK-1** — validation is still warn-only. Collect real failure rates from the `validation_codes`
  log field before flipping `ACTION_GRAPH_VALIDATION_ENFORCED`. Note that Part 3 gives this new
  weight: `coffman_graham`'s cycle raise is gated on the same setting.
- **Frontend handover** from Part 1 — the two edge fields, the both-places rule for conditional
  edges, `GET /{id}/validation`, and whether to expose the edge-check predicates over HTTP.
- **OQ-1** (static vs runtime `R.S.`) — Part 4. **OQ-3** (C.R.N. liveness), **OQ-7** — Part 5.
  **OQ-4** (revocation) — Parts 7–8. **OQ-6** (turn counters on rerun) — Part 6.
