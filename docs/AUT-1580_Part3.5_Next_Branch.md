# AUT-1580 · Next branch — Part 3.5: edge identity and lap numbering

> ⚠️ **Historical.** This spec's `K == 1` byte-identity promise was deliberately given up in Part
> 4.5 — a DAG now keeps a one-slot context vector so both shapes run one code path. See
> `AUT-1580_Part4.5_Work_Summary.md` §3. Turn numbering is run-wide now too, so anything here about
> turns counting per node from 0 no longer holds.



> Prerequisite: Part 3 has landed. See `AUT-1580_Part3_Work_Summary.md` for what shipped and
> `Cyclic_Workflow_Implementation_Plan.md` §Part 3.5 for where this sits in the sequence.
>
> **This branch changes no behaviour for any graph that exists today.** Every graph in production
> is acyclic (`K == 1`), and every cyclic graph currently has at most one redirect edge per node.
> This part makes a *second* edge from the same node correct — which Part 5 turns into the normal
> shape.
>
> It lands before Part 4 because Part 4 needs the redirect target on the frame, and before Part 5
> because Part 5 makes multi-edge routing routine.

---

## 1. The problem, measured

Part 3 assumed a redirecting node has **one** outgoing redirect edge. Give it two and laps
overwrite each other.

```
graph:    s → a → b → z          redirect edges:  z ⇢ a  and  z ⇢ b
result:   budgets a|z fired 3, b|z fired 3   = 6 laps opened
          z slot 1: [(s,0), (z,0), (z,1), (z,2), (z,3)]   = 4 laps recorded
```

**Six laps ran. Four survived.**

### Why

A lap's turn number comes from `ActionGraph._laps_opened`, which counts entries **already written**
to the document:

```python
return len(vector.slot(context_index).tagged(node))
```

A lap that has been *opened and queued* has written nothing yet, so the count cannot see it. With
one edge that is harmless — open a lap, run it, it writes, the next read sees the new count. With
two edges several laps are in flight at once, and the count only knows about the ones that
finished.

Traced, with the partial mitigation already in place:

```
z runs (lap 0, turn 0) | _laps_opened saw 0 | fired 2   → hands out turns 0, 1
z runs (lap 1, turn 0) | _laps_opened saw 1 | fired 2   → hands out turns 1, 2
z runs (lap 1, turn 1) | _laps_opened saw 2 | fired 2   → hands out turns 2, 3
                                                                    ↑ turn 2 given out twice
```

Two laps then carry `(turn_no=2, tag=z)`. `upsert_entry` treats a matching `(turn_no, node_id)` as
the same cell and replaces, so the later lap erases the earlier one.

> ### `upsert_entry` is **not** the bug. Do not change it.
>
> It replaces on a duplicate identity for a deliberate reason, recorded in its own docstring: a
> loop node reports *progressively*, calling `record_node_result` after every iteration with the
> collated result so far. `append_entry` there would turn one execution into N entries; `merge_slots`
> would keep the first partial collation and discard the complete one.
>
> The fault is that two distinct laps are **handed the same number**. Fixing it in `upsert` would
> break progressive loop reporting to paper over a numbering mistake made in `fire_redirects`.

### The mitigation already in the tree

`fire_redirects` carries an `opened_here` counter that offsets the turn per edge **within one
call**. That removes the same-call collision. It does nothing across calls, which is where the
remaining four-of-six loss comes from. Keep it or replace it; it is subsumed by item 1 below.

---

## 2. Scope

### 2.1 Lap numbers become scheduler state

Stop deriving the turn from stored entries. Keep a monotonic counter per `(node, context_index)`
that increments when a lap is **opened**, not when one is recorded.

The analogy that makes the requirement obvious: a ticket machine hands out 1, 2, 3 and remembers
what it gave. Counting how many people have been *served* gives the same number twice whenever two
are being served at once.

Constraints:

- **It must survive a retry.** Three retry layers stack (Temporal ×3 → tenacity ×3 → attachment ×3).
  An in-memory counter resets under each, so this belongs on the run document beside
  `redirect_budgets` — the same reasoning as RISK-4.
- **It must survive a resume.** A resumed run must continue the numbering, not restart it. This is
  the property `_laps_opened` was reaching for and the reason it read the document at all; keep the
  property, change the source.
- **`None` for a DAG**, never `{}`. A `{}` default is written on every partial save and would make
  a DAG's document differ from a pre-cyclic one. Same discipline as `node_vectors` and
  `redirect_budgets`.

### 2.2 `RedirectFrame` carries the redirect target

Today a frame holds `(node, context_index, turn_no)` — who fired, which slot, which lap. Not
**which edge**.

Without the target there is no way to look up `R.S.(i, j)` at the node. Part 4 needs that, and so
would a precise upstream check.

Note what does **not** change: two edges leaving one node still share that node's column, because
the context vector is indexed by the redirecting node (§1.3). Only the laps inside that column need
distinguishing.

### 2.3 Per-`(edge, lap)` decision state

Storage only. Nothing reads it in this branch — Part 4 does, and Part 5 writes it.

**Resolve OQ-9 before building this.** The shape is undecided, and the requirement is narrower than
it first appears:

> It is **not** a readiness question. A parent that declines an edge still ran and still produced
> output, so `is_work_done` passes and nothing blocks. This engine cannot deadlock on a declined
> edge — §1.11's framing assumes a node waits for a message, and this one checks whether output
> exists.
>
> The requirement is **input composition**: `to_input` must leave a declined parent out of the
> input it would otherwise contribute to. That is the whole job.

### 2.4 A cyclic graph with no single start node dies confusingly

Found in review. `start_node_id = topology.start_node_id or ""`, and a graph with two roots has
none — so case (i) builds a `NodeRunContext` whose provenance tag is `""`.

Part 1's guard catches it, which is the good news: it **refuses to write a mistagged entry** rather
than silently corrupting the transcript. But the run then dies on an internal invariant message
with nothing executed:

```
topology.start_node_id = None   K=2
status = error
error  = "Node 'r1' writes context slots but has no provenance tag. Pass start_node_id for a
          forward pass, or tag=<redirecting node> for a re-entry."
nodes with output: []
```

That message is written for whoever is building a `NodeRunContext`, not for someone who drew a
second root in the editor. Validation already detects the condition (`D7`, single-root rule) but is
warn-only, so the run proceeds to the crash.

**Refuse it where the reason is still explainable** — a cyclic graph needs a start node to tag its
baseline, so this is a precondition of `K > 1`, not a general validation question. A DAG is
unaffected: it writes no vector and needs no tag.

### 2.5 The precise upstream check, as a canary

With 2.2 in place this becomes expressible:

> every parent in `R.S.(i, j)` holds an entry `(tag=j, turn=t)` before the child runs at lap `t`

Ship it as a **canary**, not a gate — log and continue, raise under `strict`, exactly like
`agree()` and the mirror guards. Two reasons:

- It is guaranteed today by the ordering. `rank` is topological and execution inside a lap is
  sequential, so it can never fire. As a gate it would be dead code.
- It becomes load-bearing the moment anything inside a lap runs concurrently, and a canary that has
  been quietly correct for months is worth more than one written under pressure later.

### 2.6 Keep intact

- One slot per **R.N.**, not per edge.
- `upsert_entry`'s replace-on-identity behaviour.
- Everything Part 3's exit criteria pinned — particularly that `K == 1` stays byte-identical.

---

## 3. Where the code is

| File | What changes |
|---|---|
| `action_graph/redirect_stack.py` | `RedirectFrame` gains `target` |
| `action_graph/action_graph.py` | `fire_redirects` takes the turn from the counter, not `_laps_opened`; `_laps_opened` is deleted or demoted to a resume seed |
| `action_graph/action_graph_doc_model.py` | the lap counter field, `Optional[...] = None` |
| `action_graph/frontier.py` | likely home for the counter, beside the queue it belongs to |
| new, or `redirect_budget.py` | the per-`(edge, lap)` decision store, once OQ-9 is settled |

---

## 4. Exit criteria

1. **`K == 1` is byte-identical.** Same execution order, same documents, same S3 counts as Part 3's
   benchmarks. `tests/graphs/` stays green.
2. **A single redirect edge behaves exactly as it does today** — dense turns from 0, same transcript.
3. **Two redirect edges from one node record every lap.** The reproduction in §1 must go from
   4-of-6 to 6-of-6, asserted on the stored document.
4. **Turn numbers are unique per `(slot, tag)`** across any number of edges and laps — property-style,
   not one hand-picked graph.
5. **A retry does not restart the numbering.** Same shape as Part 3's
   `test_the_counter_resumes_rather_than_restarting`.
6. **A resume continues the numbering**, and the transcript grows rather than being overwritten.
7. The frame's `target` is used by at least one real call site, so it is not dead weight.
8. The upstream canary is exercised both ways — a healthy graph passes, a deliberately broken one
   raises under `strict`.

---

## 5. Suggested commit sequence

| # | Commit | Why this order |
|---|---|---|
| 1 | `RedirectFrame` gains `target`, threaded through `fire_redirects` | Pure data, no behaviour; unblocks the rest |
| 2 | Lap counter on the document, `None` for a DAG | Pure data |
| 3 | `fire_redirects` reads the counter; `_laps_opened` retired | The behaviour change, alone and reviewable |
| 4 | Per-`(edge, lap)` decision store | Only after OQ-9 is answered |
| 5 | The upstream canary | Needs 1 to be able to name the R.S. |
| 6 | Work summary doc | Same shape as Parts 1–3 |

---

## 6. Decide before starting

| # | Question |
|---|---|
| **OQ-9** | Where does the per-`(edge, lap)` verdict live, and how does `to_input` read it? Blocks 2.3. |
| — | Does the lap counter live on the frontier, on the document, or both — one authoritative, one durable? It must satisfy both the retry and resume constraints in 2.1. |
| — | Is `_laps_opened` deleted, or kept as the seed a resumed run starts the counter from? |

---

## 7. Notes for whoever picks this up

- **The tests in `tests/graphs/` are written to fail against the wrong version.** When you change
  numbering, run them before assuming your change is right — `test_turns_are_numbered_per_tag_and_do_not_collide`
  caught an off-by-one in the mitigation within seconds.
- **`tests/graphs/persistence_probe.py`** gives you a real `UserActionGraphResult` wired to counting
  fakes. Use it rather than mocking the writer; it is what proves the S3 behaviour did not regress.
- **Do not verify a re-run by comparing `updated_at` or output text.** Both will fool you. The
  `created_at` / `updated_at` on a stored ActionDoc are the **action definition's** timestamps and
  never change per execution, and many actions are deterministic, so identical input gives
  identical output. A first attempt at verifying the rerun modes reported four false failures for
  exactly this reason. Two signals that do work:
  - **`len(doc.results)`** — each execution appends one entry. Continue-run took a node from 1 to 2.
  - **Poll the run document every ~0.4s during a re-run** and watch nodes disappear from `output`
    and come back. That is how "emptied and refilled" was actually confirmed:
    ```
    t= 2.3s  [A, B, Start]     before
    t= 6.9s  [Start]           emptied
    t=11.4s  [A, Start]        A refilled
    t=15.8s  [A, B, Start]     B refilled
    ```
    A mock start node re-runs in milliseconds, so the fully-empty window can be shorter than one
    poll — absence of evidence there is not evidence of absence.
- A continue-run is now refused on a run whose status is `waiting` (`user_action_graph.py`), because
  such a run has nodes that never produced output and continue-run re-executes from stored results.
  If 3.5 changes that area, keep the guard.
- Part 3's live end-to-end setup is three processes — Temporal, the runner worker, the API. The
  recipe is in `AUT-1580_Part1_Work_Summary.md` §7.
- Two unrelated things found during Part 3 review and deliberately not fixed, in case they surface:
  `run_action_with_excel_loop` records nothing when every row fails (unlike the concurrent loop,
  which writes a synthetic `"Loop Failed"` document), and that asymmetry is the only case where
  `upstream_ready` does real work. It has no test.
