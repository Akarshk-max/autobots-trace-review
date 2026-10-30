# AUT-1580 · Branch summary — Part 4.5: the user operations become first-class

> Branch `AUT-1580-cyclic-workflow-v5`, on top of Parts 1–4. Commits `6cc766715`, `88981eea2`,
> `cb59bc9b3`, `4f4a4fa81`, `9b4347671`.
>
> **This part was not planned.** It began as the first hour of Part 5 and became its own piece of
> work, because tracing the Part 5 spec turned up a chain of defects in what Parts 1–4 had left
> behind — every one of them in the paths a *user* drives rather than the paths the scheduler
> drives: continue-run, rerun, and chat-with-a-node.
>
> **The headline is a reversal.** `K == 1` is no longer byte-identical. Parts 1 through 4 each held
> that property and measured its cost; this part spends it deliberately, so that a DAG and a cyclic
> graph run the same code instead of two engines side by side. §3 has the reasoning and the number.
>
> Terminology unchanged from Part 3.5: **turn** is one trip round one loop (`turn_counters`, on the
> document); **lap** is one wave of the work queue (`frontier.ScheduledNode.lap`, in memory only).
>
> Suite: `tests/graphs/` is **420 passed, 0 xfailed**, up from 391 at the branch point.

---

## 1. The defect this part exists for, and how it was found

Part 4's summary says a re-entered node can now read its own transcript. It can. But
`assert_baseline_mirror` — the guard that pins `output[node]` to slot 0 — was **reporting nine
broken nodes on a healthy three-turn run**, and had been since Part 4 landed.

Nobody saw it for two compounding reasons:

- the guard is a **canary**: it logs and continues, raising only under `strict`;
- **every cyclic fake in `tests/graphs/` returns the same output on every call**, so
  `baseline_matches_action_doc` compared a value to itself and passed however wrong the write path
  was.

Give the fakes a counter — which is all a real action is, from the mirror's point of view — and it
is immediate:

```
a: slot0=('s',0,'a exec #2')   output[a]='a exec #11'   <-- DRIFT
b: slot0=('s',0,'b exec #3')   output[b]='b exec #12'   <-- DRIFT
z: slot0=('s',0,'z exec #4')   output[z]='z exec #13'   <-- DRIFT
s: slot0=('s',0,'s exec #1')   output[s]='s exec #1'     (never re-entered)
```

`record_node_result` wrote `graph.output[node] = stored` **unconditionally**, before it even looked
at `target_slots`. A re-entry is case (ii) and targets slot 1 alone, so `output` moved on while slot
0 stayed at the forward pass.

This is the "many actions are deterministic" trap the Part 5 spec warns about, arriving from a
direction the spec did not anticipate — not in a re-run check, but in the invariant guard itself.

`tests/graphs/test_baseline_mirror_under_a_cycle.py` is the file that does not let it happen again,
and its own necessity is mutation-tested: with a constant-output fake, the drift test passes **with
the bug present**.

---

## 2. `output[node]` is the baseline. Everything else follows from that

One rule, and most of this part is its consequences.

> `result.output[node]` holds the node's **baseline** execution. A re-entry writes the dimensions
> its turn disturbs and leaves `output` alone. Per-turn values are read from `node_vectors`.

```python
if not run_ctx.target_slots or BASELINE_SLOT in run_ctx.target_slots:
    graph.output[run_ctx.node] = stored
```

`propagation_slots` can never contain slot 0 — context indices start at 1 and the start node is
never an R.N. — so a redirect turn structurally cannot reach that branch. The writers are a DAG (no
slots at all) and case (i), which seeds every slot including 0.

### It fixed a second bug nobody was looking for

`contextual_input` falls through to `responses[parent]` — i.e. `output[parent]` — when a parent
outside this turn's `R.S.` contributes the value it already has (case (ii)). That used to be a value
from *whichever dimension ran last*. It is now the parent's baseline, which is what case (ii)
actually specifies.

### And it forced two reads to change with it

- **`baseline_matches_action_doc` no longer filters to `tagged(start_node_id)`.** Slot 0 is the
  baseline **column**, not the start node's private lane: a continue-run tags its entries with the
  node it was started from, so the highest-turn entry is no longer start-tagged. Under the old
  filter the check compared the forward pass's output to a freshly re-run `output[n]` and reported
  drift on every node of every continue-run.
- **`is_node_done` reads the baseline by ordering, not identity**: `max(turn_no) >= asked`. The
  baseline column carries mixed tags — `s` for the forward pass, the operated node for a user
  operation — so matching on `(turn, tag)` would report every node as never having run this turn.
  Slots 1..K-1 keep exact identity; their turns count per tag and interleave, and ordering there
  would let one redirector's turn satisfy another's.
- **`is_node_done` no longer opens with `if node not in output: return False`.** That line was
  doubling as "has this node ever run", which only held while every execution wrote `output`.
  Presence in `output` is now the `K == 1` answer alone.

---

## 3. `K == 1` is no longer byte-identical — deliberately

**The property Parts 1–4 all held, and this part gives up.** Recorded plainly because a future
reader will find the old promise in four documents and needs to know it was spent rather than
forgotten.

Before: a DAG kept `node_vectors = None` and took a separate route — no context, no turn, `for_dag`
everywhere. That is two behaviours to reason about, and it left a DAG's continue-run and chat with
no record beyond `ActionDoc.results`.

Now:

```python
writes_vectors = bool(topology.start_node_id)
```

**A start node, not a redirect edge.** Every entry carries a provenance tag and the start node tags
the baseline. A graph with no single root — one isolated node, or a pure data cycle — has nothing to
tag with, so it keeps no vector and behaves exactly as before. Both shapes are legal while
validation is warn-only (RISK-1). Gating on `True` instead raises a **500** on
`test_the_same_graph_still_runs_as_a_dag`; the condition is load-bearing, not defensive.

`cyclic` still gates the redirect machinery — budgets, `fire_redirects`,
`resume_outstanding_turns`, `close_open_turns` — none of which a DAG needs.

### The cost, measured

6-node DAG, ~2 KB payloads:

| | |
|---|---|
| document without vector | 32,729 B |
| document with vector | **46,664 B — 1.43×, +13,935 B** |
| S3 puts | unchanged, one per node save |
| execution order | unchanged |

At `K == 1` there is one slot, so the vector is slot 0 mirroring `results`. **It buys uniformity of
code path, not new information.** That is the trade, stated so nobody has to re-derive it.

### Tests re-baselined, and two that are reversals

Eight moved from `node_vectors is None` to "one slot, and it mirrors `results`". Two say so in
place:

- `test_a_graph_with_no_redirect_edges_is_untouched` now asserts no *redirect* machinery — no
  budgets, no turn counters, one dimension — which is what it was really for.
- `test_the_run_document_holds_every_node_and_no_context_vector` gives up the byte-identity pin and
  asserts the vector covers exactly the nodes `output` does.

---

## 4. The three user operations, and what each does now

This is the reference table. Everything in §5 and §6 is a correction to one row of it.

| operation | writes which slots | tag | turn from | touches `output[node]` | `results` |
|---|---|---|---|---|---|
| **forward pass** — case (i) | **all** | start node | 0 | ✅ | ✅ |
| **redirect turn** — case (ii) | `propagation_slots`, never slot 0 | the redirector | `turn_counters` | ❌ | ❌ |
| **chat / single-node rerun** | **all** | the node itself | `next_user_turn` | ✅ | ✅ |
| **continue-run**, participants | **all** | the continue-run root | `next_user_turn` | ✅ | ✅ |
| **continue-run**, root + outside parents | **none** | — | — | ❌ | ❌ |
| node outside `R.S.` on a turn | **none** | — | — | ❌ | ❌ |
| **any operation, no start node** | no vector exists | — | — | ✅ | ✅ |

Two consequences worth stating outright:

- **`results` and slot 0 count the same events.** Both grow only on baseline-column operations. A
  redirect turn is invisible to `results` because it targets slot 1 — measured: a node that executed
  four times, once forward and three times round the loop, holds **one** result. That fact is why
  the seed in §6 writes every recorded turn into every slot.
- **`output[node]` moves for the forward pass, chat and continue-run, and not for a redirect turn.**
  That is what keeps `output` and slot 0 mirrored.

---

## 5. Continue-run: three defects, in the order they were found

### 5.1 It overwrote turn 0 instead of appending

A continue-run re-executed its subtree under `(start, turn 0)` — the identity the forward pass
already holds — so `upsert_entry` replaced that cell and the earlier output was gone.

```
before:  z slot 0: [('s', 0, 'out action-z 0')]
after:   z slot 0: [('s', 0, 're-run')]          ← same cell, replaced
```

Nothing complained: `output[node]` was rewritten in the same pass, so the mirror guard still passed.
Meanwhile `ActionDoc.results` grew. The two structures disagreed about how many times the node ran.

It now takes a **fresh run-wide turn**, tagged with the node it was started from.

### 5.2 Readiness had to be scoped, and the first fix was wrong

Making readiness a turn comparison meant a child asks *"is my parent at turn N?"* Two parents can
never answer yes: the **root**, deliberately skipped, and a parent **outside the subtree** —
`upstream_nodes` comes from the full inverted map, not the subgraph. Measured on a join shape, a
strict check against both left `c` never ready, nothing executed, and the run reported **success**.

The first fix had those parents write a **carry-forward entry** so the check would pass. It shipped
and was then removed, and the removal is the more instructive half:

- **the node never ran**, so the entry was a fiction;
- it copied `output[node]`, the **baseline** record — true of slot 0 and false of every other
  dimension. Filing a node's forward-pass output as its work for a later turn reads as a regression
  to old work;
- and it was **write-only**. `contextual_input` returns by identity at the baseline slot, so nothing
  ever read the entry's content — only its turn number. A full input+output payload per feeder, per
  continue-run, to convey one integer.

So the question is scoped rather than the answer faked:

- a parent **inside** the subtree is asked about this turn;
- the root and any parent **outside** it are asked whether they hold anything at all — §1.4's case
  (ii), contributing the value they already have.

`results` and slot 0 now agree for every node, because nothing writes an entry for a node that did
not execute.

### 5.3 A human gate cancelled the operation instead of pausing it

The one that matters most in practice.

A continue-run's identity lived in three local variables — root, turn, participants. A gate that
stopped it mid-operation ended it: the locals died with the function, nothing on the document said
an operation was half done, and the next run asked every node about turn 0, found them all done, and
reported **`success` having executed nothing**. The work being propagated never arrived and nothing
said so.

Same problem RISK-12 solved for redirect turns, and the same four parts:

| redirect turn | continue-run |
|---|---|
| `open_turns` | **`open_continue_run`** — `<root>\|<turn_no>` |
| `outstanding_turns()` | `outstanding_continue_run()` |
| `resume_outstanding_turns()` | the resume-or-open block, beside it |
| `close_open_turns()` | pruned when every participant holds the turn |

**Read on every run, not only when a continue-run is requested** — as `resume_outstanding_turns` is,
and here it must be: the API refuses a continue-run on a `waiting` run (405), so the run that
resumes one is an **ordinary run**, issued after the human approved whatever stopped it.

Two things the plan for it did not anticipate, both found by checking invariants rather than the
happy path:

- **The continue-run context applies to participants only.** On a resume the frontier holds the
  whole graph, not the subtree, so a node outside the operation would be asked "are you done at
  `(root, N)`?", answer no, and re-run for nothing.
- **The run paths chose their execution route from the request.** `run_action_with_merge` picks
  `run_action` (fetches the action fresh, **replaces** `results`) or
  `run_node_action_with_existing_doc` (**appends**) from `action_graph_node_id and
  is_continue_run_graph` — both false on a resume. The resumed participants went down the replacing
  path and their `results` stopped matching their slot: `b: slot0=2 results=1`. It now asks whether
  *this node* is a participant.

Walked end to end on a 7-node DAG with gates on two nodes:

| step | request | ran | status | `open_continue_run` |
|---|---|---|---|---|
| 5 | continue-run from `a` | nothing | waiting | **`'a\|3'`** |
| 6 | approve `a` → **plain run** | `f* b* c* d*` | waiting | `'a\|3'` |
| 7 | approve `d` → **plain run** | `e*` | success | **`None`** |

One operation, three runs, two approvals, one turn number.

---

## 6. Old runs: the seed, and why it is lazy

`is_node_done` answers "in `output`, but no vector" with **not done** — at `K > 1` that combination
is drift, a write that used `for_dag` where it should have carried slots. On a document written
before vectors existed it means the opposite: **the node ran, before the deploy.**

Measured on a run that parked partway and was resumed twice:

```
without the seed:  resume#1 ran [b, z, z, z, z]          ← 'a' never re-enters; the cycle is wrong
                   resume#2 ran [s, a, a, a, a, b, b, b] ← s and a re-executed and re-billed
with the seed:     resume#1 ran [b, z, a, b, z, a, b, z, a, b, z]
                   resume#2 ran []
```

Two distinct failures, not one. **It takes a *partially* complete run to reproduce**: on a fully
complete one no node runs, so no vector is ever materialised and `is_node_done` stays on the safe
`node in output` path. A first attempt at this measurement used a complete run and was confounded.

`seed_baseline_from_output` reconstructs from `ActionDoc.results`, entry *i* → turn *i*. **Lazy
rather than a migration**: it fires when a run is next touched, so historical documents nobody
reopens cost nothing, there is no S3 sweep to schedule and no race with a run in flight.
`node_vectors is not None` is the idempotence signal. Precedent: Part 3.5 kept `_turns_recorded` for
exactly this shape.

Two details:

- **Every recorded turn goes into every slot**, because every execution `results` records did — see
  §4. A first version seeded turn 0 broadly and later turns narrowly, borrowing the redirect-turn
  rule for entries no redirect turn produced.
- **The final turn is built from the `ActionDoc`, not `results[-1]`.**
  `baseline_matches_action_doc` compares the highest-turn entry against the document, so sourcing it
  from the document makes the mirror hold by construction rather than by assuming those two agree.

---

## 7. Turn numbers are now one run-wide sequence

`next_user_turn` = highest turn recorded **anywhere in the run**, plus one. Computed **once per
operation** and held for every node it touches.

Two rules fell out, both learned the hard way:

- **Per node is wrong.** Each node owns its vector, so "highest turn in the baseline slot" is a
  per-node number, and per-node numbers drift apart the moment one node's history differs from
  another's. "Turn N of this operation" then stops being comparable across nodes, which the
  readiness check depends on.
- **Every slot, not just the baseline.** These operations write into every dimension, and a number
  drawn from the baseline alone lands mid-transcript in the others: on a graph whose slot 1 is
  already at turn 2, a chat numbered from the baseline's max of 0 takes turn 1 and sorts *between*
  two earlier redirect turns, reading as though the user had spoken in the middle of the loop.

```
before:  slot 1: [(s,0), (z,0), (a,1,'CHAT'), (z,1), (z,2)]   ← mid-loop
after:   slot 1: [(s,0), (z,0), (z,1), (z,2), (a,3,'CHAT')]    ← sorts last
```

### `next_turn`'s seed became a floor

```python
turn_no = max(updated.get(key, seed), seed)
```

A user operation writes into every slot and can leave an entry numbered above what a redirector's
column has reached. As a *default* the seed was only consulted when the column had no counter, so
the next redirect reused that number and the two entries sorted as equal.

**This reverses a rule Part 3.5 wrote deliberately** —
`test_a_stored_counter_beats_the_seed_even_when_it_is_lower` asserted the stored counter wins
unconditionally. That held while `seed` was a per-column *recorded count*, which can only lag. The
half of Part 3.5's concern that still stands is now its own test,
`test_a_turn_in_flight_never_pulls_numbering_back`: a turn already handed out has written nothing,
so a transcript-derived floor is *lower* than the counter and `max` keeps it. The floor only raises.

### What this does not fix, and why that is fine

Two redirecting nodes firing in the same **lap** still take the same number — both read a transcript
that has not been written yet. Measured: `b` and `c` both take turns 1, 2, 3.

That is **correct**, not a defect: they fire in the same wave, so their turns genuinely are the same
round. Nothing is lost, since identity is `(turn, tag)` and the tags differ. Making it otherwise
would need a single durable counter, not a scan — the same reasoning that made `turn_counters` a
counter rather than a count.

### D3 is now wrong as written

**`turn_no` no longer "counts per `node_id` from 0".** It is run-wide and monotonic; a column skips
whatever other columns and user operations consumed, and a redirect's first turn is 1 because the
forward pass owns 0. Six tests asserted `turns == list(range(n))`; the Part 5 spec already called
that "asserting no crash happened, not an invariant", and they now assert uniqueness and ordering
instead.

Turn numbers can also **repeat across a run's life**, because the number is derived from what is
*recorded* and a rerun erases records. Safe — each node's slot is still internally unique — but
"turn N" does not name a moment across the graph.

---

## 8. Approvals belong to the output they were given for

The last defect, and the one with the widest blast radius.

Withdrawal of `user_input_done` / `user_review_done` was folded into `empty_downstream_nodes` and
reached **only** by a continue-run. A rerun, which also replaces a node's output, left stale
approvals standing. Measured: rerun `d`, and `e` consumed the brand-new `d#23` under an approval
granted for the deleted `d#21` — the gate quietly not working, rather than the gate being satisfied.

Where an approval is withdrawn is a question about the *operation*, not about whether output
changed. An approval follows the node whose stored result was explicitly emptied; the operations
that regenerate output without emptying it leave the human's verdicts alone.

It is now `withdraw_approvals`, called by each operation with **its own** set, because the sets
genuinely differ:

| operation | withdraws | root included? |
|---|---|---|
| rerun from a node | exactly what `empty_downstream_nodes` emptied | **yes** on a full rerun, **no** on downstream-only |
| continue-run (new) | subtree **minus the root** | **no** — the root is skipped, so its output does not change and its approval still describes what its children read |
| continue-run (resume) | nothing | withdrawn once, when the operation opened |
| rerun with no node id | nothing | n/a |
| chat | nothing | n/a |

The last two withdraw nothing on purpose, and the reason is the same in both cases: they are how a
human keeps working on a run, so taking their verdicts away turns the operation against itself.
A rerun with no node id is what drives a graph that could not finish; clearing there means approve
the gate, re-run, the approval is gone, park at the same gate. A chat is iteration on a node the
human is already holding open; clearing there re-blocks that node's children on every message.

Both leave the same residue — the node produces new output while an older approval still stands
over it. That is the staleness gap in §10, and these two widen it knowingly rather than accept an
operation that cannot get past its own gates.

The root rule is worth dwelling on. The original had the exclusion on `user_input_done` and not on
`user_review_done` — exactly inverted, because the two gates govern different nodes:

- `user_input_required` is read on **the node itself**, and the continue-run root is skipped before
  that line, so its flag is never consulted;
- `user_review_required` is read on **the node's parents**, and the root *is* the parent of the
  subtree — so clearing it blocked the very subtree the operation exists to re-run.

`empty_downstream_nodes` is back to one job — dropping results — and its `is_continue_run_graph`
parameter is gone. `conn/temporal/action_graph/activity.py` mirrors the dispatch, since that is the
same decision for the async path.

---

## 9. Things that will cost the next person time

Everything here was paid for once already.

- **A deterministic fake hides a broken write path.** `baseline_matches_action_doc` compares a value
  to itself. Any test of the mirror needs actions whose output changes per call — and the test that
  proves it is mutation-tested by making the fake constant and watching it pass with the bug in.
- **`results` and slot 0 count baseline-column events only.** A redirect turn appears in neither.
  Four executions, one result.
- **A continue-run is measured by what `run_action_doc` did, not `run_action`.** Patching only
  `ActionGraph.run_action` makes a working continue-run look dead — the continue-run path goes
  through `run_node_action_with_existing_doc`.
- **The two re-execution signals are still opposite** (Part 5 spec §7): a rerun empties first, a
  continue-run appends.
- **`to_wait` is sticky.** One gated node stops the whole remaining pass, including unrelated
  branches — measured: `c` blocked with no gate of its own, because `b` tripped first. Approving
  gates can take several rounds.
- **A gate never fires for a node that is already done.** The done-check `continue`s before the gate
  checks, so setting a gate on a finished graph does nothing, and a run can report `success` with an
  unapproved gate outstanding.
- **A chat bypasses the gates entirely.** `run_node` never enters the scheduling loop, so a node
  with `user_input_required` and no approval can be chatted with freely. Pre-existing; more visible
  now that a chat is a recorded event.
- **Nothing checks that output existed when an approval was granted.** `user_review_done` can be
  set on a node that has never run and is honoured when it does — so a whole graph's gates can be
  cleared up front and the run walks straight through. Measured live. The review gate orders the
  work; it does not evidence that anyone read anything.
- **An interrupted mutation sweep can leave source mutated.** Write mutants to a copy, or verify the
  restore.

---

## 10. Found and not fixed

Reproduced, out of scope, logged here so they are not rediscovered.

- **Continue-run from a leaf node returns 500.** `get_subgraph_map` only adds a node as a key when
  it *has* children, so a childless root yields `{}`, `total_nodes` is empty, and
  `coffman_graham` gets `[[], [], …]` → `list index out of range`. Confirmed identical at
  `bf379f22e`, so it pre-dates this branch. Both DAG and cyclic.
- **Chatting a node that has never run raises `AttributeError`.** `run_node` does
  `action_response.get(node).id` with no None check. Same family as the bare `output[node]`
  subscript in `run_node_action_with_existing_doc`.
- **Nothing tracks staleness.** `is_node_done` asks "have you recorded this turn", never "is what
  you recorded still derived from your parents' current values". Chat with a node and everything
  below it is silently stale while the run reports `success`.
- Unchanged from Parts 3–4: `run_action_with_excel_loop` recording nothing when every row fails,
  the bare `output[node]` subscript, `PUT /v1/action_graphs/{id}` returning 500 for a missing id,
  and the `batch_id` / `run_group_chat` / DELETE items in the Part 1 summary §6.

---

## 11. Test coverage — including where it is missing

**`tests/graphs/`: 420 passed, 0 xfailed.**

New files:

| file | covers |
|---|---|
| `test_baseline_mirror_under_a_cycle.py` | the mirror under non-deterministic actions; chat writes a turn and reaches every slot; the `K == 1` case |
| `test_continue_run_turns.py` | the fresh turn, the shared turn, the root gaining no entry, the join that used to deadlock, the no-start-node fallback |
| `test_seed_baseline_from_output.py` | the seed's guards, its shape, and the partial pre-vector resume |

Deleted: `test_continue_run_user_gates.py`, written against a helper that no longer exists.

### Everything else was mutation-tested

Among the mutations caught: writing `output` on every execution, seeding turn 0 broadly and later
turns narrowly, sourcing the seed's last entry from `results[-1]`, the engine never calling the
seed, strict-for-all-parents, reusing turn 0, reverting the floor, floor-overrides-unconditionally,
cyclic-only vectors, vectors without a start node, chat writing the baseline only, and a chat
numbered from the baseline's max.

Two mutations were **not** caught, and both are honest limits rather than gaps to fill:

- **loose-for-all-parents** on the continue-run readiness check. Within a lap the frontier drains
  parents before children, so the strict half is only observable under concurrency — the same reason
  `assert_upstream_turn_recorded` is a canary rather than a gate.
- **firing the gate-clearing on a plain run with `action_graph_node_id=None`**, where the walk is a
  no-op. The realistic mis-scoping — dropping `is_continue_run_graph` — is caught, by a test added
  specifically because mutation testing found the hole.

### A 242-scenario invariant sweep

Every operation at every node, gates at every node, gate × operation, two gates at once, chat before
any run, and all 24 permutations of a 4-step sequence — on both a DAG and a cyclic graph with nested
redirects. After **every** step: the vector covers `output`, `output[node]` equals the latest slot-0
entry, `len(slot 0) == len(results)`, and no duplicate `(turn, tag)` in any slot.

**0 invariant failures.** The only exceptions were the two pre-existing crashes in §10.

---

## 12. Carried forward

- **Part 5 has not started.** `grep -rn 'record_decision(' src/` still returns nothing.
  `AUT-1580_Part5_Next_Branch.md` has been updated for what this part changed.
- **Two decisions from the Part 5 spec's §4** were made and recorded there but not built: the
  per-edge `routing` field (RISK-10 and RISK-9 together) and OQ-3's "unreached ⇒ negative, decided
  at lap drain".
- **Concurrency within a wave** — still not attempted; the canary is what will notice first.
- **RISK-1** — validation still warn-only. §3's start-node condition is a runtime fallback, not
  enforcement.
