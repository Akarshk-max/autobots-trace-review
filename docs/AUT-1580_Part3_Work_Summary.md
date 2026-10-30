# AUT-1580 · Branch summary — cyclic workflows, Part 3: the cyclic scheduler

> ⚠️ **Historical.** This spec's `K == 1` byte-identity promise was deliberately given up in Part
> 4.5 — a DAG now keeps a one-slot context vector so both shapes run one code path. See
> `AUT-1580_Part4.5_Work_Summary.md` §3. Turn numbering is run-wide now too, so anything here about
> turns counting per node from 0 no longer holds.



> Branch `AUT-1580-cyclic-workflow-v3`, on top of Parts 1 and 2 (PRs #187, #188).
> Plan: `AUT-1580_Part3_Next_Branch.md`. Algorithm: `Cyclic_Workflow_Implementation_Plan.md`
> §1.4–§1.9.
>
> **A cycle executes.** Parts 1 and 2 were deliberately inert — Part 1 built the vocabulary, Part 2
> made the write path affordable. Neither changed what runs. This one does.
>
> The safety property is unchanged and still the thing to protect: **a graph with no redirect edges
> has `K == 1` and behaves exactly as it did before.**
>
> Suite: **683 passed, 13 skipped** (was 614 at the branch point). `tests/graphs/` is 261 tests,
> up from 192.

---

## 1. What shipped

Seven commits, each separately green.

| Commit | Contents |
|---|---|
| `e48187e5d` | `is_work_done` → a re-armable per-context predicate |
| `e039a3617` | Durable per-`(i,j)` redirect budgets |
| `098f423fd` | The redirect stack, standalone and immutable |
| `75ab8f464` | Frontier scheduler replacing the layer walk |
| `c2a735d42` | **A cycle executes** |
| `322ac7d26` | Budget exhaustion parks the run and asks (D5) |
| `31322fa03` | The Part 1 mirror guards get call sites |

### New modules

| File | Purpose |
|---|---|
| `action_graph/redirect_stack.py` | Which redirections are open; the two-part pop condition; C1/C3 at a fan-in |
| `action_graph/frontier.py` | The re-armable work queue, seeded in Coffman-Graham order |
| `action_graph/redirect_budget.py` | Per-pair counters, durable on the document |

---

## 2. The three reasons the engine could not cycle, and what each became

| # | Before | After |
|---|---|---|
| 1 | The layer list was computed once and iterated, so a node's position was fixed before the run started | A frontier queue ordered `(lap, rank, sequence)`. `rank` is the node's Coffman-Graham position, so lap 0 drains in exactly the old sequence |
| 2 | `is_work_done` meant "key present in `output`", making *done* a permanent property of a node | A per-context predicate whose identity is `(turn_no, tag)` — a fresh lap is a fresh turn, so a node done for the last lap is not done for this one |
| 3 | `coffman_graham` raises on a cycle | Untouched. Cycles live only in `redirect_edges`; the data adjacency stays acyclic, which is what Part 1's edge grammar bought |

---

## 3. Four places the implementation departed from the plan

Each was a better answer to the same requirement, not a shortcut around it.

### 3.1 Immutability instead of deep copy

§1.8.1 requires a queue be handed to children **by deep copy, never by reference**, because under
sharing one branch's push becomes visible to every sibling and a later mutation can retroactively
change a queue a node already received.

`RedirectStack` is a frozen dataclass over a tuple. Every operation returns a new stack and never
mutates the receiver, so handing one object to N children is safe *by construction* — there is no
mutation for a sibling to observe. Same invariant, **zero copying**.

That also disposes of **R3-3** (the `O(fan-out × depth)` copy cost) and makes the copy-on-write
optimisation the plan deferred unnecessary rather than pending.

### 3.2 `turn_no` lives on the frame, not on the execution

The obvious implementation derives a node's turn when it runs, as "the next turn for this tag".
That is fresh by construction, so **a resumed run would redo work it had already recorded** instead
of recognising it — and the done-predicate would be vacuous.

Instead `RedirectFrame` carries `turn_no`, fixed when the lap opens: *j*'s m-th redirect opens turn
m for tag *j*, whoever asks and however often. The number is read off the document
(`_laps_opened`), so a retry resumes the numbering rather than overwriting the transcript.

### 3.3 Redirects fire before the save, not after

Found in review. The original order was execute → save → fire, which left a window: a crash between
the save and the next node's save kept the work but lost the budget increment, so a retry would
re-fire a redirect that had already fired — the exact failure RISK-4's durable counters exist to
prevent.

The justification for firing after the save was simply wrong: `_laps_opened` reads the **in-memory**
document, which `record_node_result` has already updated, so nothing in `fire_redirects` needs the
save to have happened. Firing first makes the increment land in the same write as the result that
triggered it — closing the window at **no extra I/O**, rather than by adding a second save.

The skip path had a wider version of the same gap and no save at all after it, which matters most
on a resumed run where *every* node is skipped and no later save would ever flush the increment.
It now saves when a redirect actually fired.

The test for this had to be rewritten once: comparing final memory against final storage passes
under **both** orderings, because the run's closing status save flushes the increment anyway — a
node too late. It now samples the instant before each node executes, and fails three times under
the old ordering.

### 3.4 Already-recorded work still advances the wave

Found by the resume test, and the one fix whose shape the plan did not imply.

A resumed run finds its forward pass complete, so every node is skipped as already-done — including
the node that fires the redirect. A human raising the budget would therefore buy **nothing**: the
run would come straight back as `waiting` having done no work, and D5's resume path would be
nominal rather than real.

The skip path now evaluates redirects for a node it skipped. Whether a redirect fires is a question
about **the edge's budget**, not about whether that particular result had to be recomputed.

---

## 4. Case (i) / case (ii), and where the budget bound landed

`resolve_run_context` implements §1.4's asymmetry:

| | Condition | Writes | Tag |
|---|---|---|---|
| **(i)** | every slot still empty | **all** slots | the start node, turn 0 |
| **(ii)** | slots populated | **only** the responsible R.N.'s slot | the redirecting node, the frame's turn |

Case (i) writes broadly because there is nothing to disambiguate; case (ii) writes narrowly because
there is. A DAG takes neither — `for_dag` writes no vector at all.

**The budget bound moved from commit 6 into commit 5.** The plan put enforcement in 6, but commit 5
without it leaves a cycle that runs and never stops, which is not a green tree. Commit 5 stops the
loop; commit 6 makes stopping *visible* — status `waiting`, counters readable, resume by result id.

`exhausted_pairs_hit` is deliberately **not** the sticky `to_wait` flag. That flag is right for a
node that cannot run — once set, every later node is skipped — but wrong here: a redirect that
cannot fire does not stop unrelated work from finishing. The run drains, then reports `waiting`.

---

## 5. The honest limitation

**Part 3 makes a cycle execute and record. Part 4 makes it read.**

`ActionGraphInputCreator.to_input` still builds a prompt from **baseline outputs**, not from the
slot transcript. A re-entered node therefore records its lap correctly but does not yet reason over
its own history.

This is Part 4's scope, stated in the plan, and the tests are written so it cannot silently flatter
the result: `tests/graphs/test_cyclic_execution.py` asserts on the **recorded transcript** — slots,
tags, turn numbers, re-run sets — and never on whether a prompt looked right.

`relevant_subgraph` is used by both parts for different things, which is easy to conflate:

| | Uses `R.S.(i,j)` for | Part |
|---|---|---|
| Scheduling | which nodes re-run this lap | **3** |
| Propagation | which *slots of each participant* get written | **4** |

> **Corrected 2026-09-02.** This row previously read "`(V_ij)_new` into `V_kj` for all
> `k ∈ R.S.`", i.e. one node's result copied into other nodes' cells. That is wrong — no node
> writes into another node's slot. Propagation decides which of a **participant's own** slots
> are written: the dimensions of the R.N.s inside `R.S.(i,j)`. See
> `Cyclic_Workflow_Implementation_Plan.md` §Part 4.

---

## 6. Exit criteria

| # | Criterion | Evidence |
|---|---|---|
| 1 | `K == 1` byte-identical | Order parity asserted against `coffman_graham` on three graph shapes and end to end; benchmarks unchanged at 0 reads / 8 writes |
| 2 | Single-node cycle executes, terminates, tags correctly | `test_a_single_node_cycle_re_enters_and_terminates`, `test_turns_are_numbered_per_tag_and_do_not_collide` |
| 3 | Correct re-run set | `R.S.(0,5)`: node 1 is a sibling of 5, incomparable, and must not re-run. The degenerate `R.S.(0,2)` is kept beside it because on its own it passes under a wrong rule |
| 4 | Pop condition tested against its wrong version | Reaching *j* under a different context must not pop |
| 5 | Sibling isolation | Immutability, asserted on both push and pop |
| 6 | C3 exercised both ways | Agreeing queues return one; disagreeing ones log, and raise under `strict` |
| 7 | Exhaustion → `waiting` + resume | Raising the limit continues the cycle; the counter advances and the transcript grows rather than being overwritten |
| 8 | Counters survive a retry | A repeat run does not re-fire an exhausted redirect |
| 9 | Guards have live call sites | Asserted called, with the cycling node and a real start node id |
| 10 | Part 2's benchmarks hold for `K > 1` | Below |

### `K > 1` persistence, re-measured now that something writes the slots

| Shape | Document | vs DAG | 6 saves | Amplification | S3 GETs |
|---|---|---|---|---|---|
| DAG (`K == 1`) | 7,285 B | 1.0× | — | — | 0 |
| cyclic `K=2` | 9,231 B | 1.3× | 55.4 KB | 6.0× | 0 |
| cyclic `K=4` | 11,551 B | 1.6× | 69.3 KB | 6.0× | 0 |
| cyclic `K=8` | 16,191 B | 2.2× | 97.1 KB | 6.0× | 0 |

Still linear in K (1,160 bytes per slot), still one document per save, still zero reads.

---

## 7. Open questions closed here

| # | Closed as |
|---|---|
| **OQ-2** | Case (i) fills every slot, as written. §1.12's rerun rule (`V_nk := V_n0`) is only coherent if the baseline is uniform |
| **OQ-8** | Reading **(A)** — the queue belongs to the wave, not the edge. A node uses the stack the scheduler handed it for this lap; there are no retained per-edge queues to go stale |

---

## 8. Carried forward

- **Part 4** — `to_input` reads the correct slot and re-linearises a tagged transcript; column
  propagation across `R.S.`. This is what turns §5's limitation into a working feature.
- **Bugs #1–#5** from Part 1 §6 — still open. #1 (`batch_id` on the Temporal path) and #2
  (`run_group_chat` 500s and strands the run) remain the production-visible pair.
- **The `Clock.datetime_now()` decision** from Part 2 §5 — naive local time into a tz-aware field.
- **RISK-1** — validation is still warn-only, and Part 3 gives it new weight: `coffman_graham`'s
  cycle raise is gated on the same setting.
- **RISK-3** — an exhausted run parks at `waiting` with nobody notified until Part 8's inbox. It
  logs loudly and fires the webhook.
- **Concurrency within a wave** — deliberately not attempted. Today's loop is sequential within a
  layer and stayed that way; adding re-entrancy and concurrency in one branch makes any bug twice
  as hard to attribute.
- **The single-node rerun path** (`run_node`) passes `for_dag`, so it writes the baseline only.
  Giving a rerun its own context is Part 6.
### Found after this summary was first written

Three things surfaced in review conversation, all now reflected in
`Cyclic_Workflow_Implementation_Plan.md`:

- **The human gates are scoped to the baseline context.** They read `node_details`, which has no
  per-lap dimension, so a re-entry could only ever see the baseline's verdict — and the sticky
  `to_wait` would otherwise kill lap work already queued. Fixed here; a DAG is unaffected.
- **Multi-edge lap numbering is broken.** A node with two outgoing redirect edges hands the same
  turn number to two laps, and `upsert_entry` — correctly — treats them as one cell, so the second
  overwrites the first. Measured: six laps fired, four recorded. Partially reduced here; the real
  fix is **Part 3.5**, which moves lap numbers into scheduler state instead of counting written
  entries. `upsert` is not at fault and must not be changed.
- **`run_action_with_excel_loop` records nothing when every row fails**, unlike
  `run_action_with_loop_concurrently`, which writes a synthetic `"Loop Failed"` document. That
  asymmetry is the one case where `upstream_ready` does real work, and it has no test.

- **OQ-1** (static vs runtime `R.S.`) — Part 4. **OQ-3**, **OQ-7** — Part 5. **OQ-4** — Parts 7–8.
  **OQ-6** — Part 6.
