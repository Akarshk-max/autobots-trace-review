# AUT-1580 · Branch summary — cyclic workflows, Part 2: persistence rework

> ⚠️ **Historical.** This spec's `K == 1` byte-identity promise was deliberately given up in Part
> 4.5 — a DAG now keeps a one-slot context vector so both shapes run one code path. See
> `AUT-1580_Part4.5_Work_Summary.md` §3. Turn numbering is run-wide now too, so anything here about
> turns counting per node from 0 no longer holds.



> Continues on `AUT-cyclic-workflow-v2`, on top of Part 1. Scope: the persistence rework specified
> in `AUT-1580_Part2_Next_Branch.md`, plus pre-existing bug #6.
>
> **No behaviour changes.** Same runs, same result documents, far fewer round trips — with one
> documented exception to "same documents", in §5.
>
> Suite: **601 passed, 13 skipped** (was 575/13). `tests/graphs/` is now 179 tests, up from 153.

---

## 1. What it cost before, measured

The benchmark (`tests/graphs/test_persistence_write_amplification.py`) drives the real engine
through the real `UserActionGraphResult`, with counting doubles standing in for S3, Mongo and
Redis. Numbers from `main`'s write path:

| Shape | S3 GETs | S3 PUTs | Bytes written | Final document | Amplification |
|---|---|---|---|---|---|
| DAG chain, 6 nodes | 8 | 8 | 48.6 KB | 7.3 KB | 6.7× |
| Loop, 10 iterations | 15 | 15 | 61 KB | 3.6 KB | 17× |
| Loop, 20 iterations | 25 | 25 | 145 KB | 4.4 KB | 33× |
| Loop, 40 iterations | 45 | 45 | 423 KB | 5.9 KB | 71× |
| Loop, 100 iterations | 105 | 105 | **2.12 MB** | 10.5 KB | **201×** |

Doubling 20 → 40 iterations cost 2.9× the bytes; 40 → 100 cost 5.0×. That is the n²/2 the plan
predicted, now measured rather than argued.

## 2. What it costs now

| Shape | S3 GETs | S3 PUTs | Bytes written | Cache bumps |
|---|---|---|---|---|
| DAG chain, 6 nodes | 8 → **0** | 8 → 8 | 48.6 → 48.6 KB | 8 → **2** |
| Loop, 10 iterations | 15 → **0** | 15 → **5** | 61 → **15.6 KB** | 15 → **2** |
| Loop, 40 iterations | 45 → **0** | 45 → **5** | 423 → **23.3 KB** | 45 → **2** |
| Loop, 100 iterations | 105 → **0** | 105 → **5** | 2,123 → **38.8 KB** | 105 → **2** |

A 100-iteration loop went from 201× amplification to 3.7×, and growth is now linear in the
iteration count rather than quadratic. The DAG case keeps its per-node PUT on purpose — that is
the durability guarantee that lets a failed run resume — and simply stops paying for a GET
alongside it.

### The cyclic shape (exit criterion 2)

Benchmarked separately, as the plan required, because only this shape carries the vector. Part 3
builds the scheduler that produces these; the persistence path does not care how the vector got
into the document, only how large the document is, so `a_cyclic_run_doc` constructs the shape
directly — which is what lets this criterion be met before Part 3 exists.

| Shape | Document | vs DAG | 6 saves | Amplification | S3 GETs |
|---|---|---|---|---|---|
| DAG (`K == 1`) | 7,261 B | 1.0× | — | — | 0 |
| cyclic `K=2` | 9,207 B | 1.3× | 55.2 KB | 6.0× | 0 |
| cyclic `K=3` | 10,367 B | 1.4× | 62.2 KB | 6.0× | 0 |
| cyclic `K=4` | 11,527 B | 1.6× | 69.2 KB | 6.0× | 0 |
| cyclic `K=8` | 16,167 B | 2.2× | 97.0 KB | 6.0× | 0 |

Exactly 1,160 bytes per extra slot — **linear in K, not quadratic** — and amplification stays at
one document per save regardless of K, which is the property Part 3's scheduler leans on. A K=8
document is 2.2× a DAG one, so the vector is a constant-factor cost on a path that no longer
re-reads.

---

## 3. The four changes

### 3.1 Drop the read-before-write (§2.1)

`update_action_graph_result` fetched the stored document so it could merge `exclude_unset` fields
onto it. The engine has nothing to merge: it mutates one document in memory and passed the same
object straight back in, so the fetch re-read what the caller was already holding.

Split in two, both ending in one `_write_run_doc` so they cannot produce different bytes:

| | Fetches first | Used by |
|---|---|---|
| `update_action_graph_result` | yes | `PUT /action_graphs_results/`, the Temporal error path — callers holding only a patch |
| `save_run_doc` | no | all 11 engine sites |

**`save_run_doc` returns `None` on purpose.** That is what fixes §2.4 structurally rather than by
convention — see 3.4.

Also dropped a `model_copy(deep=True)` on the Mongo path. It existed so `result` could be swapped
for the lite shape without touching the caller's document, and cloned the entire `output` map on
every write only to discard it. A shallow `model_copy(update={"result": ...})` is equivalent —
nothing downstream mutates the copy — and the dump it feeds is the small lite one either way.

### 3.2 Coalesce the per-iteration writes (§2.2)

Option 1 from the plan: time-throttled, via
`ACTION_GRAPH_RESULT_FLUSH_INTERVAL_SECONDS` (default 3.0s; 0 restores a write per iteration).
Applied to exactly the two per-iteration sites — `run_action_with_excel_loop` and
`run_action_with_loop_concurrently`.

The plan asked for an unconditional final write to backstop the throttle. **There already is one,
and the excel loop needed no new code**: the per-node write in `run_action_graph_v2` runs after
every node returns, loop nodes included. So the invariant is "a throttled write can be skipped
because a node-level write always follows", which holds for both loops without touching either.
`test_a_throttled_loop_still_persists_every_result` runs a 40-iteration loop with *every*
intermediate write suppressed and asserts all 40 results are in the stored document.

### 3.3 One cache bump per run, not per node (§2.3)

Search covers `name`, `status` and `result.name` (`user_action_graph_result.py:376`). A per-node
write changes none of them. `bump_search_cache` now defaults to `False` and is passed only on
status transitions and at run end — hence 2 bumps per run instead of one per node and per
iteration.

### 3.4 Clean up the `action_response` aliasing (§2.4)

`action_response` was bound once to `result.output`, while `action_graph_result_doc` was rebound by
every `update_action_graph_result` — so a status-only update could leave `action_response` pointing
at a detached dict.

Both halves done, as the plan asked:

- `save_run_doc` returns nothing, so there is no return value to rebind. The document the engine
  passes in is the document that gets written, and `test_the_engine_never_rebinds_the_document_it_was_given`
  asserts `final is run_doc`.
- The parameter is gone from **eight** helpers — the plan said seven; `run_action_simple` is the
  eighth. Each derives the map through `node_results(action_graph_result_doc)` instead, and all
  eight already received the document, so nothing new had to be threaded. Five of the eight turned
  out never to use it at all once `record_node_result` was doing the writing.

`forget_all` now empties the maps **in place** instead of rebinding them to fresh `{}`. That was
the remaining half of the same hazard: `node_results` hands out the live dict and the engine binds
it once per function, so a rebind would leave that binding on a detached map and `is_work_done`
would read results that had been cleared. Today every `forget_all` runs before the engine takes its
binding — but only by ordering, and Part 3 re-enters nodes mid-run. Clearing in place holds
regardless of order. (`forget_node` already popped in place.)

Two related simplifications fell out:

- `action_response[node] = record_node_result(...)` was a second write of the same value — the
  writer already writes through `result_doc.result.output`, which is the same dict object. Five
  such assignments dropped.
- `ActionGraph.gen_input` and `collate_action_results` keep their `action_response` parameter.
  Both are readers that are *handed* the map rather than holding a binding across writes, which
  was never the failure mode. (`gen_input` also has no call site — noted, not touched.)

---

## 4. Bug #6, fixed on the way

`empty_downstream_nodes` wrapped its whole per-node body in `except Exception`, **with the
`to_visit` extension inside the same `try`**. So one bad node did two things: swallowed the error,
and pruned its entire subtree from the walk. Replayed against a graph where one node is missing its
`node_details` entry:

```
OLD -> emptied: ['a']            | leftover stale: ['b', 'c', 'd']
NEW -> emptied: ['a', 'c', 'd']  | leftover stale: ['b']
```

A rerun then ran against those stale results and still reported `success`. **A half-emptied graph is
worse than an unemptied one** — it looks like it worked.

Now: errors propagate, the one genuinely expected gap (an edge naming a node with no `node_details`
entry) is handled explicitly and keeps the walk going, and the function returns the set it emptied.

**This required a second fix to be an improvement at all.** `_run_as_background_task` logged
escaping exceptions and returned without touching the run's status, so anything raised outside
`run_action_graph_v2`'s own `try` left the run in `processing` until the 720-minute stuck monitor —
the exact trap Part 1 hit with `coffman_graham`. Narrowing the handler without this would have
traded a silent-stale-results bug for a stuck-run bug. It now marks the run `error` with the
message, and fires the webhook.

Also fixed in passing: `node is not action_graph_node_id` compared strings by **identity**. It
worked only because the seed is literally the same object on the first pop. Now compared by value,
with a test that passes an equal-but-distinct string.

Bugs #1–#5 from the Part 1 list are untouched and still open, as agreed.

---

## 5. The one way the documents are *not* byte-identical

Exit criterion 1 asked for byte-identical final documents. They are, **except for timestamp
representation**, and the reason is worth reading because it is a pre-existing bug this branch
merely stops masking.

`Clock.datetime_now()` returns `datetime.now()` — **naive local time** — into fields annotated as
tz-aware `DateTime`:

```python
@staticmethod
def datetime_now() -> datetime:
    return datetime.now()          # core/clock/clock.py:24 - no tzinfo
```

Serialised fresh, that is `2026-08-27T13:25:36.702043`. Parsed back, pydantic reads the naive value
**as UTC** and re-serialises it as `...702043Z` — the same wall-clock reading, now labelled UTC, and
so wrong by the deployment's offset.

An update carrying `result=` overwrote the fetched copy with the caller's, so those writes never
relabelled anything. But the old engine's **final** write set `status` alone — no `result` — so it
persisted the copy it had just fetched and parsed. That is why stored documents currently carry
`Z`-suffixed timestamps, and why they no longer do.

`test_timestamps_are_no_longer_relabelled_as_utc_by_the_round_trip` pins this precisely: it asserts
the `Z` difference *and* that nothing else differs. It deliberately does not assert which form is
correct — the naive value is what `Clock` actually produced.

**The difference is confined to the stored bytes.** Both forms parse back to the identical value,
because the field is annotated as a tz-aware `DateTime` and pydantic reads a naive string as UTC:

```
"2026-08-27T13:25:36.702043"   -> DateTime(2026, 8, 27, 13, 25, 36, 702043, tzinfo=UTC)
"2026-08-27T13:25:36.702043Z"  -> DateTime(2026, 8, 27, 13, 25, 36, 702043, tzinfo=UTC)   # equal
```

So every consumer that reads a run through `ActionGraphResultDoc` sees no change whatsoever. Only
something reading the raw S3 JSON without the model — an analytics job, a byte-level diff — could
tell the two apart. `TIMEZONE` is already `"UTC"` (`settings.py:513`), though note that
`datetime_now()` does not consult it: it takes the host clock, so the two forms coincide exactly
when the container runs UTC.

**Not fixed here.** Fixing `Clock.datetime_now()` changes stored timestamps across the whole
product, well outside a persistence branch. It needs its own decision:

1. Make `Clock.datetime_now()` tz-aware (`pendulum.now("UTC")`) — fixes the root cause, changes
   recorded timestamps everywhere, and would need a view on already-stored data.
2. Accept the naive form and drop the pretence that these fields are tz-aware.
3. Leave it; the two forms differ by the deployment offset, which is zero if the deployment runs UTC.

---

## 6. Verification

- **597 passed, 13 skipped**, up from 575/13; 22 new tests, no failures, no test deleted.
- The benchmark is the regression fence, not documentation: it asserts 0 GETs, 2 cache bumps, and
  **sub-quadratic** loop growth (the same test previously asserted growth was *super*-linear, and
  the assertion was flipped rather than deleted, so the property is still under test in both
  directions).
- All three shapes benchmarked separately: DAG (`node_vectors is None`), the loop case, and the
  cyclic `K > 1` case at K = 2, 3, 4 and 8 — counts, document growth, and both write paths agreeing
  on a document that carries a vector.
- Two test doubles had to change, so exit criterion 4's "unchanged" could not hold literally:
  `FakeResults` in `test_graph_traversal_memoisation.py` gained `save_run_doc`, and
  `test_baseline_mirror.py` dropped an `action_response=` kwarg. Both are signature-driven; no
  assertion was weakened.
- The Part 1 source-level guard still passes — no new direct `.result.output` write.

### Deliberately verified, not assumed

| Claim | How |
|---|---|
| The throttle cannot lose results | 40-iteration loop with every intermediate write suppressed; all 40 present |
| The engine never rebinds its document | `assert final is run_doc` through a whole run |
| The two write paths agree | Same run written both ways, compared with timestamps blanked |
| The `Z` difference is *only* timestamps | Asserted alongside a full comparison of everything else |
| Bug #6's old behaviour was wrong | Old loop body replayed against the fixture; emptied `['a']`, left `b,c,d` stale |
| `save_run_doc` reaches no wrapper lacking it | Every engine caller constructs a real `UserActionGraphResult`; `SpaceActionGraphResults` only serves the API and delegates |
| The `Z` difference cannot reach a consumer | Both forms parse to the same `DateTime`; asserted directly |
| `node_vectors` survives S3 but not Mongo | K=3 document round-tripped through both write paths; slots `{0,1,2}` intact on every node |

---

## 7. What's next

**Part 3 — cyclic scheduler**, unchanged from `AUT-1580_Part2_Next_Branch.md` §5: frontier
scheduler, redirect stack, case (i)/(ii) asymmetry, `f_comb` within a slot, per-`(i,j)` budgets with
counters stored durably on the result doc.

Part 3 still does not need to touch a write site. Part 1 routed them all through
`record_node_result`; Part 2 routed the *saves* through `save_run_doc`, which takes the document
rather than a patch — so a re-entering node saves the same way a forward-pass node does.

### Carried forward

- **Bugs #1–#5** from Part 1 §6, still open. #1 (`batch_id` never persisted on the Temporal path)
  and #2 (`run_group_chat` 500s and strands the run) remain the two that are production-visible.
- **The `Clock.datetime_now()` decision** in §5.
- **`node_details` in `TOPOLOGY_FIELDS`** — Part 1 flagged this as "revisit with Part 2". Still
  correct as it stands: `node_details` keys define the node set, so excluding it would miss real
  topology changes. Not revisited further; it is a validation-read cost, not a run-write cost, and
  so untouched by this branch.
- **Frontend handover**, **RISK-1**, **OQ-3/4/8** — all unchanged from Part 1.
