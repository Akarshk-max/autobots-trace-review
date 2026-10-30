# AUT-1580 · Branch summary — cyclic workflows, Part 3.5: edge identity and turn numbering

> Branch `AUT-1580-cyclic-workflow-v3.5`, on top of Parts 1–3 (PRs #187, #188, #192).
> Plan: `AUT-1580_Part3.5_Next_Branch.md`. Algorithm: `Cyclic_Workflow_Implementation_Plan.md`
> §1.3–§1.9, §Part 3.5.
>
> **An edge becomes a thing the scheduler can name.** Part 3 assumed a redirecting node had one
> outgoing redirect edge. Part 5 makes a router with several targets the normal shape, so that
> assumption had to go before either could be built on.
>
> The safety property is unchanged and still the thing to protect: **a graph with no redirect edges
> has `K == 1` and behaves exactly as it did before.**
>
> **A note on wording.** The plan says "lap" for both *one trip round a loop* and *one wave of the
> work queue*. They are different, and one wave can carry several trips, so this document and the
> code say **turn** for the trip and keep **lap** for the queue wave only. Where the plan says
> "six laps fired", read "six turns".
>
> Suite: **959 passed, 13 skipped, 0 failed** across `tests/`. `tests/graphs/` is 331 tests, up
> from 272 at the branch point, and every commit is green on its own (272 / 283 / 299 / 315 / 327 /
> 331).
>
> A full `make test` also reports 137 failures, every one of them under `integration_tests/`: they
> call real external services - OpenAI, Azure, Daytona, SQL, PTY - that this environment has no
> credentials for, and none of them import `action_graph`. Four were re-run on the Part 3 tip and
> on this branch and fail identically on both, so "pre-existing" is measured rather than assumed.


> ---
>
> ## ⚠️ Superseded in places by Part 4.5
>
> `AUT-1580_Part4.5_Work_Summary.md` landed after this document and changed three of its claims.
> The rest of this file stands; these three do not:
>
> - **"a graph with no redirect edges has `K == 1` and behaves exactly as it did before"** — a DAG
>   now keeps a one-slot context vector, so the two shapes run one code path. Byte-identity was
>   given up deliberately, at a measured 1.43x document cost. What still holds: a DAG opens no turn,
>   charges no budget, and keeps exactly one dimension.
> - **`turn_no` counting per `node_id` from 0** — numbering is run-wide and monotonic now. A
>   redirect's first turn is 1, columns are sparse, and numbers can repeat across a run's life.
> - **`output[node]` holding whatever ran most recently** — it holds the **baseline**. A re-entry
>   writes its own dimensions and leaves `output` alone.
>
> ---
---

## 1. The measurement this branch exists for

`s → a → b → z`, redirect edges `z ⇢ a` and `z ⇢ b`, run before any change:

```
executed: [s, a, b, z, a, b, b, z, z, a, a, b, b, z, z]
fired:    {a|z: 3, b|z: 3}                            = 6 turns opened
z slot 1: [(s,0), (z,0), (z,1), (z,2), (z,3)]         = 4 turns recorded
```

Six turns ran, four survived. After:

```
executed: [s, a, b, z, a, b, b, z, z, a, a, b, b, b, b, z, z, z, z]
fired:    {a|z: 3, b|z: 3}                            = 6 turns opened
z slot 1: [(s,0), (z,0), (z,1), (z,2), (z,3), (z,4), (z,5)]   = 6 turns recorded
```

### The mechanism, and the one it is not

A turn number came from `_laps_opened`, which counted entries **already written**:

```python
return len(vector.slot(context_index).tagged(node))
```

A turn that has been opened and queued has written nothing, so the count cannot see it. With one
edge that is harmless. With two, several turns are in flight at once and the count knows only about
the ones that finished — so two turns take the same `(turn_no, tag)`.

`upsert_entry` then treats them as one cell and replaces. **That is not the bug**, and it was not
changed: it replaces on a duplicate identity because a loop node reports *progressively*, calling
`record_node_result` after every iteration with the collation so far. `append_entry` there would
turn one execution into N entries; `merge_slots` would keep the first partial collation and discard
the complete one. The fault was that two distinct turns were handed the same number.

**A second symptom, found while reproducing the first.** `is_work_done` identifies an execution by
that same `(turn_no, tag)` pair, so a turn handed a number already in the transcript reads as
*already done* and is skipped rather than run. The collision cost the work as well as the record —
which is why `z` executed 5 times for 6 turns before, and 7 times after. The plan predicted the lost
entries; the lost executions were only visible by replaying it.

---

## 2. What shipped

| Commit | Contents |
|---|---|
| 1 | `RedirectFrame` carries the redirect target |
| 2 | Turn counters on the run document |
| 3 | **Turn numbers come from the counter, not the transcript** |
| 4 | Per-`(edge, turn)` decision state (OQ-9) |
| 5 | The precise upstream check, as a canary |
| 6 | A cyclic graph with no single start node is refused where the reason is explainable |
| 7 | This document |

`tests/graphs/test_crash_mid_turn.py` is added alongside, and documents a defect rather than a
feature — see §7.

### New modules

| File | Purpose |
|---|---|
| `action_graph/turn_counter.py` | The ticket machine: turns handed out per `(node, context_index)` |
| `action_graph/edge_decision.py` | Per-`(edge, turn)` verdicts, and the one question they answer |

### New document fields

Both `Optional[...] = None`, never `{}` — a `{}` default is written on every partial save and would
make a DAG's document differ from one produced before cyclic support existed. Same discipline as
`node_vectors` and `redirect_budgets`.

| Field | Key | Meaning |
|---|---|---|
| `turn_counters` | `<node>\|<context_index>` | turns handed out for that column |
| `edge_decisions` | `<target>\|<source>\|<context_index>\|<turn_no>` | what that edge answered on that turn |

---

## 3. The three decisions §6 of the plan asked for

### OQ-9 — where the per-`(edge, turn)` verdict lives, and how `to_input` reads it

**A flat map on the run document, and `to_input` does not read it.**

The store answers exactly one question — `declined_parents(decisions, node, upstream_nodes,
context_index, turn_no)` — and the run paths subtract that set from `upstream_nodes` before
composing input. `to_input` keeps its signature and its ignorance of turns.

Three things settle the shape:

- **Keyed by edge**, because one node runs once but can answer differently for each edge leaving it
  (D9). A verdict cannot live on the node's execution.
- **And by turn**, because two edges leaving one node share that node's column (§1.3) — the slot
  does not distinguish them either. `(context_index, turn_no)` is the turn: `turn_no` alone is
  ambiguous across columns, and the tag is recoverable from the slot, so those two are sufficient
  and minimal.
- **Not on `ContextEntry`**, which is the tempting home since the entry *is* the turn's record.
  `upsert_entry` replaces the whole cell on every write and a loop node writes once per iteration,
  so a verdict stored there is erased by the node's own second iteration. There is a test that
  demonstrates this rather than asserting it in prose.

**Absence means the parent contributes.** Nothing writes a verdict until Part 5, so every graph
running today has `edge_decisions is None`, gets an unfiltered parent list, and produces a
byte-identical prompt. Absence also covers a parent outside `R.S.` that did not re-run this turn: it
is not declining, it is contributing the value it already has (case (ii)).

The parent and child of an edge always run in the **same** turn — `rearm` hands one stack to every
node in `R.S.` and the frame fixes the turn — so the child looks a verdict up with its own run
context and nothing is threaded from the parent's execution.

Storage only. Part 4 wires the subtraction; Part 5 writes the verdicts.

### Where the turn counter lives

**On the document, and nowhere else.** The frontier gets no copy. An in-memory counter dies under
each of the three stacked retry layers (RISK-4), and a second copy beside a durable one is two
sources of truth that can disagree. It costs no extra I/O: `fire_redirects` already mutates
`redirect_budgets` in memory and the existing save flushes both in one write.

Deliberately **not derived from `redirect_budgets`**, though the arithmetic would work — the turn
is the sum of `fired` over the edges leaving the node. The budget is a guardrail a human edits to
resume an exhausted run (D5), and §1.12 empties a vector's counters without touching budgets
(OQ-6). Turn numbering must not move when either happens.

### Whether `_laps_opened` is deleted

**Demoted, to `_turns_recorded`.** It is the seed a column with no counter starts from — which is
exactly a run begun under Part 3: transcript, no `turn_counters`. Its recorded entries are then the
only record of how many turns it opened, so they seed the machine rather than restarting it at 0 and
overwriting what it already wrote. Once the key exists the stored value wins, **even when it is
lower**: it is the one that also knows about turns still in flight.

---

## 4. §1.11's deadlock framing does not apply, and the requirement is narrower

Worth recording, because reading OQ-9 as a readiness question is the trap and the plan says so
twice without saying why in one place.

A parent that declines an edge **still ran and still produced output**, so `is_work_done` passes
and nothing blocks. This engine cannot deadlock on a declined edge: §1.11's framing assumes a node
waits for a *message*, and this one checks whether *output exists*. The requirement is **input
composition** — leave the declined parent out of the input it would otherwise contribute to. That
is the whole job, and it is why the store needs no readiness half.

---

## 5. The canary, and why it is not a gate

With the target on the frame, §2.5's check becomes expressible:

> every parent in `R.S.(i, j)` holds an entry `(tag=j, turn=t)` before the child runs at turn `t`

It ships as a canary — logs and continues, raises under `strict` — exactly like `agree()` and the
two mirror guards. Two reasons, both from the plan and both still true:

- **It cannot fire today.** `rank` is topological and execution inside a turn is sequential, so a
  parent has always run by the time its child does. As a gate it would be dead code that could only
  ever be wrong.
- **It becomes load-bearing the moment anything inside a turn runs concurrently**, and a check that
  has been quietly correct for months is worth more than one written under pressure later.

A canary that has never been shown to sing is indistinguishable from a broken one, so it is
exercised both ways: a healthy cyclic run is asserted to call it with a non-empty parent set and
never trip it (including under `strict`), and a run whose parents are robbed of their turn entry the
instant before the check is asserted to trip it.

The `R.S.` intersection is what makes it *precise* rather than merely present. On the `R.S.(0,5)`
shape — `1` and `5` both feed `2`, `5` redirects into `0` — node `1` is incomparable to `5`, takes
no part in the turn, and holds nothing for it. A check over all parents rather than over `R.S.`
would fire on that healthy graph every turn; there is a test that would catch that regression.

---

## 6. A cyclic graph with no single start node

Found in review. `start_node_id = topology.start_node_id or ""`, and a graph with two roots has
none — so case (i) built a `NodeRunContext` whose provenance tag was `""`. Part 1's guard caught it,
which is the good news: it refuses to write a mistagged entry rather than silently corrupting the
transcript. But the run then died on an internal invariant message with nothing executed, and that
message is written for whoever is building a `NodeRunContext`, not for someone who drew a second
root in the editor.

It is refused now where the reason is still explainable, naming the roots. This is a **precondition
of `K > 1`**, not a general validation rule: a cyclic graph needs a start node to tag its baseline,
and a DAG writes no vector, needs no tag, and is unaffected — a second root has always been legal
for it and stays legal (RISK-1). `GraphTopology` now carries `roots` rather than recomputing them
in `validate()`, so the two answers cannot drift.

---

## 7. Found while reviewing this branch: a crash mid-turn abandons the turn (RISK-12)

Not fixed here, and not in scope — recorded because it is the natural next thing and Part 4 is
where it starts to bite.

The turn counter is durable. The **open frames are not**: nothing records that a turn was handed
out and never completed. On a retry the frontier is rebuilt empty, every node reads as done for the
forward pass, and the redirecting node fires again taking the *next* number.

Measured on `a → b → c → d` with `d ⇢ a`, crashing during turn 0:

```
attempt 1 (crash)   a:[0]      b:[0]      c:[]     d:[]      turn 0 half done
attempt 2 (retry)   a:[0,1,2]  b:[0,1,2]  c:[1,2]  d:[1,2]
                                          ↑ c and d never receive turn 0
```

Two costs. The transcript holds a turn only half the re-run set took part in — harmless while
nothing reads a slot, which is exactly what Part 4 changes. And the abandoned turn **still charged
the budget**, so a crash permanently costs the user one of their turns and parks the run at
`waiting` sooner than it should.

**The fix is to complete the turn, not to roll it back.** `a` really executed and really cost
money, and `merge_slots` is explicitly "pure addition — nothing is ever overwritten and nothing is
dropped". Rollback would also need to know which nodes owed the turn, which needs the edge — the
thing that was not saved. Completing it needs one new piece of durable state (turn → the edge that
opened it); everything else already exists, since `R.S.(i,j)` is precomputed and `is_node_done`
answers "did this node record this turn".

**Turn numbers do not need to be dense.** Identity is exact-match `(turn_no, tag)` and ordering
only needs them to sort, so an abandoned turn leaving a hole is harmless.
`test_multi_edge_turns.py` asserts `turns == list(range(n))` — that is asserting "no crash
happened", not an invariant, and should not be treated as one.

`tests/graphs/test_crash_mid_turn.py` reproduces it: two `xfail(strict=True)` tests asserting the
behaviour we want, so they fail the build the moment it is fixed, plus one characterisation test
recording what happens today.

---

## 8. Exit criteria

| # | Criterion | Evidence |
|---|---|---|
| 1 | `K == 1` byte-identical | `tests/graphs/` green at every commit; `test_a_graph_with_no_redirect_edges_is_untouched` pins `node_vectors`, `redirect_budgets` and `turn_counters` all `None`; the DAG document gains one `null` key and no content; Part 2's benchmarks unchanged |
| 2 | A single redirect edge behaves as before | `test_every_lap_opened_is_a_lap_recorded[one edge]` passes against both versions — the case that hid the bug |
| 3 | Two edges record every turn | 4-of-6 → 6-of-6, asserted on the stored document, plus the execution count 5 → 7 |
| 4 | Turns unique per `(slot, tag)` | A sweep over five shapes: one edge, two, three, two redirectors with two edges each, and a redirect into the start node |
| 5 | A retry does not restart the numbering | `test_a_retry_does_not_restart_the_numbering` |
| 6 | A resume continues it, transcript grows | `test_a_resume_continues_the_numbering_and_grows_the_transcript`, plus the Part 3-document case |
| 7 | `target` has a real call site | The canary's `R.S.(frame.target, frame.node)` lookup, in the main loop |
| 8 | The canary exercised both ways | §5 above |

---

## 9. Carried forward

- **Part 4** — `to_input` reads the correct slot and re-linearises a tagged transcript; column
  propagation across `R.S.`; and the `declined_parents` subtraction this branch made expressible.
- **Part 5** — writes the verdicts `edge_decisions` stores, and makes multi-target routing routine.
  OQ-10 (what happens when *every* incoming conditional edge declines) still needs a rule; this
  branch's default — absence contributes — deliberately does not answer it.
- **RISK-12** — a crash mid-turn abandons the turn (§7 above). Logged against Part 4, reproduced in
  `tests/graphs/test_crash_mid_turn.py`.
- **Concurrency within a wave** — still not attempted, and the canary is the thing that will notice
  first when it is.
- Unchanged from Part 3 and out of scope here: `run_action_with_excel_loop` recording nothing when
  every row fails, the bare `output[node]` subscript at `action_graph.py:1898`, and the
  `batch_id` / `run_group_chat` / DELETE items in the Part 1 summary §6.
- **RISK-1** — validation is still warn-only. §6 above adds one narrow enforcement inside `K > 1`
  only; it does not change the warn-only posture for anything else.
