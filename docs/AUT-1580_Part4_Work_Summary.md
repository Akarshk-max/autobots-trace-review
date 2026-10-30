# AUT-1580 · Branch summary — cyclic workflows, Part 4: the slot becomes readable

> Branch `AUT-1580-cyclic-workflow-v4`, on top of Parts 1–3.5 (PRs #187, #188, #192).
> Plan: `AUT-1580_Part4_Next_Branch.md`. Algorithm: `Cyclic_Workflow_Implementation_Plan.md`
> §1.3–§1.9, §Part 4.
>
> **A cycle stops being write-only.** Parts 3 and 3.5 made a cycle execute and record correctly.
> Nothing read what it recorded: `to_input` built every prompt from `output[parent]`, the flat
> baseline mirror, so a re-entered node had a perfect transcript of its own history and could not
> see a word of it.
>
> The safety property is unchanged and still the thing to protect: **a graph with no redirect edges
> has `K == 1` and behaves exactly as it did before.**
>
> Suite: **1060 passed, 13 skipped, 0 failed** across `tests/`. `tests/graphs/` is 391 tests, up
> from 332 at the branch point, and **0 xfailed**, down from 2 — RISK-12's two are now real passes.
>
> **This branch was rewritten once.** Two of its rules were wrong, caught in review, and the
> history was rebuilt rather than patched — see §5. `part4-wrong-turn` tags the original ten
> commits.
>
> Terminology unchanged from Part 3.5: **turn** is one trip round one loop (`turn_counters`, on the
> document); **lap** is one wave of the work queue (`frontier.ScheduledNode.lap`, in memory only).
> One lap can carry several turns.


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

## 1. What shipped

| Commit | Contents |
|---|---|
| 1 | Slot linearisation — a tagged transcript becomes prompt text |
| 2 | `to_input` reads the slot, **by role** |
| 3 | Slot propagation — a turn writes every dimension it disturbs |
| 4 | `contributing_parents` wired into the run paths |
| 5 | RISK-12 — a retry completes the outstanding turn |
| 6 | This document, and the doc corrections §5 describes |

### New modules

| File | Purpose |
|---|---|
| `action_graph/context_input.py` | The read path: what a node sees when it runs, for the context it runs under |

`context_vector.py` gains `linearise_slot` and `entry_label`; `graph_topology.py` gains
`propagation_slots`; `edge_decision.py` gains `EdgeDecision.message` and `message_for`;
`turn_counter.py` gains the outstanding-turn record.

### New document field

| Field | Key | Meaning |
|---|---|---|
| `open_turns` | `<node>\|<context_index>\|<turn_no>` | the redirect **target** that opened that turn |

`Optional[...] = None`, never `{}`, and back to `None` when the last one closes — the same
discipline `node_vectors`, `redirect_budgets` and `turn_counters` follow.

---

## 2. The two rules this part is actually about

### Propagation — which slots a turn writes

When redirect *j* ⇢ *i* opens turn *t*, the affected dimensions are the context indices of the
**R.N.s that lie inside `R.S.(i,j)`**, and **every node executing in that turn writes its own
result into all of them**. A **row** update — one node across several of its own dimensions.

```
slots(i, j) = { context_index(r) : r ∈ R.N. ∩ R.S.(i, j) }
```

A slot index *is* a redirecting node. If R.N. *r* lies inside the region this redirect disturbs
then what happens in the turn is part of what *r*'s dimension should see, because *r* is standing
in it. An R.N. on an incomparable branch is genuinely untouched and its slot stays put.

**No node ever writes into another node's slot.** A node's row holds its own work; it learns what
its parent produced by reading that parent's slot, one hop.

`GraphTopology.propagation_slots` is precomputed beside `R.S.` at build time, which is what §13
asks for. `j` is a descendant of itself so its own slot is always in the set, which is never empty.

**Test it on §1.6's worked graph.** Both its redirects disturb a region containing both R.N.s, so a
turn of either writes both dimensions. A graph where each redirect's `R.S.` holds only its own R.N.
collapses the rule to "write the current slot" and passes under the wrong version too — there is a
test on that shape, kept to pin the narrow case, with a comment saying not to test the rule with it.

### Reading — two rules, by role

| | reads |
|---|---|
| the redirect **target** | its **own** slot, plus the input the redirector handed back |
| every **other** participant | its **data parents'** slots, one hop |

The target does *not* re-read its data parents: they sit upstream of the loop and did not take part
in the turn. On `s → a → b → z` with `z ⇢ a`, reading `s` leaves the node being *told to try again*
looking at a stale value and none of the feedback. §1.4's case (ii), literally:
`{ V_ik } + k_input → i`.

A single rule for both is the mistake; it is what shipped first, and §5 has the cost.

#### And a target need not have parents at all

The **root** of a graph can be a redirect target — *"go back to the beginning and try again"* is the
most ordinary loop anyone draws, and §1.7.1 confirms `j ⇢ root` is structurally valid. Such a node
has an empty parent list, so it **never reaches `to_input`**: `run_action_simple`, the two
split-loop paths and `run_action_with_excel_loop` take the graph input straight to the action.

Measured before the fix, on the shape `test_crash_mid_turn.py` already uses:

```
node a executed 3 times (root AND redirect target)
  run 0: {'text': 'ORIGINAL GRAPH INPUT'}
  run 1: {'text': 'ORIGINAL GRAPH INPUT'}
  run 2: {'text': 'ORIGINAL GRAPH INPUT'}
a's slot 1 meanwhile: [('a',0), ('d',0), ('d',1)]
```

Byte-identical input three times while the slot faithfully records each turn — it records its
history and never reads it, which is *the* limitation this part exists to remove, and worse than an
ordinary node's version because the loop still burns its whole budget producing the same answer.

`contextual_input_dict` sends the same context in through the graph input instead — standing
instruction first, then what it has produced, then what it is being asked to change — composed once
at the dispatch so all four zero-upstream paths get it without knowing about contexts. Identity for
every other node.

**This was missed on the first pass** because the check was "does this path call `to_input`?" —
right about parents, wrong about the target, which does not need parents. It needs itself.

---

## 3. The other decisions §4 of the plan asked for

### OQ-1 — static `R.S.`, and the diagram is the thing that is wrong

**Closed static, confirmed with the author.** D1's rule includes node 5 in `R.S.(0,2)`; the §6/§7
diagrams dim it out. The rule wins.

The reason is **fresh input**: node 5 sits directly downstream of the redirect target 0 on an
ordinary, always-fires data edge, so when 0 re-runs node 5 has new input waiting. Excluding it means
5 ignores that input and node 2 — which joins on **both** 1 and 5 — combines turn *t* from one
parent with turn *t−1* from the other. That is §1.5's own justification for the ancestor half.

> **A wrong argument was made for this first, and is recorded rather than deleted.** An earlier
> draft argued that narrowing leaves the excluded cell **empty**. It does not: case (i) seeds every
> slot, so the cell holds the start-tagged baseline and the node reads a **stale** value — the
> documented, intended case-(ii) behaviour. The measurement behind the claim filtered entries by
> the *redirector's* tag and so hid the baseline entry. The conclusion stands on fresh input.

Also worth knowing: `relevant_subgraph` already had **two** live consumers before Part 4 — the
scheduler's re-run set and Part 3.5's canary — so narrowing was never the isolated one-function
change §8 assumed.

### OQ-7 — *not* settled here; it belongs to Part 5, and the answer is runtime

An earlier draft closed OQ-7 "static, same as OQ-1". **Withdrawn.** The two are opposites on what
matters:

| | fresh input waiting? | so it should |
|---|---|---|
| **OQ-1**'s node 5 | **yes** — the redirect target re-ran and feeds it | re-run |
| **OQ-7**'s node | **no** — its only feeding edge declined | not run |

Re-running a node that received nothing recomputes the same output from the same inputs: a billed
model call, and a nondeterministic answer change for no reason.

**The gate cannot live in `R.S.`** — that is handed to `frontier.rearm()` when the redirect *fires*,
and edges decline *during* the turn, so there is nothing to narrow by at the point `R.S.` is used.
It goes at **execution** time. That makes OQ-7 a consequence of **OQ-10**, and both are Part 5's.
The note is repeated at every point a Part 5 implementer will be standing — §Part 5 · 5.0, §8's two
rows, §1.11, `edge_decision.py`, and the three functions that must change or pointedly not change.

### The redirect message lives on `EdgeDecision`

**Changed from an earlier decision in this branch,** which said "in the slot, not on
`EdgeDecision`". That was made under the wrong read model, where the message was to be read *from*
the slot. Under the corrected model the target reads `k_input`, which is per-`(edge, turn)` — and
that is exactly `EdgeDecision`'s key, which is what §2.4 asks for. A separate store would add a
second null key to every DAG document and a second thing that could disagree about which turn it
describes.

It is emphatically **not** `reason`, which is the decider accounting for its own choice (RISK-9)
and is never shown to anyone. `reason` explains the routing; `message` instructs the target. There
is a test pinning that the target never sees `reason`.

### RISK-12 — persist `turn → edge`, reconstruct the frame

Not a serialised frame. Everything a frame holds is either in the key or recorded beside it; the
**target** is the only piece that was missing, and it is the one that matters, because `R.S.(i, j)`
is keyed by the pair and without it nothing can name the nodes that owed the turn.

---

## 4. The `edge_decisions` subtraction, wired and inert

Part 3.5 built the store and left it unread. The run paths now drop the parents that declined to
feed a node on this turn, before composing.

**No behaviour change today, by construction.** Nothing writes a verdict until Part 5, so every
graph currently running has `edge_decisions is None` and gets its parent list back *by identity*.
There is a test pinning exactly that, which is also the proof that landing it now was safe.

A verdict is scoped to its own turn **and** its own column — `turn_no` alone is ambiguous, since two
columns both hold a turn 0. The end-to-end test had to separate the declined turn from the later
ones: a filter that lumped them together sees the parent return on turn 1 and misreads that as the
subtraction failing.

---

## 5. The wrong turn, and what it cost

Recorded because the mistake is more instructive than the fix, and because a wrong rule is what a
future proposal gets checked against.

**The plan doc said propagation "walks down a column, not across a row"** — push `(V_ij)_new` into
`V_kj` for every `k ∈ R.S.(i,j)`. That was implemented, tested, benchmarked and committed. It is
wrong twice over:

- **Wrong axis.** It copies one node's output into *other nodes'* cells, so a node ends up holding
  its **grandparent's** output. It has no business seeing that; it should know exactly what its own
  parent handed it.
- **Wrong warning.** "Updating a row would collapse the dimensions" is only true of writing into
  *every* slot — which is case (i), and is meant to collapse them. Writing the subset `R.S.` picks
  out collapses nothing.

**The tell was there and was mistaken for a rendering bug.** A composed prompt read
`## Upstream: action-p` over node `m`'s own output and node `z`'s. That was the design saying it was
wrong; it was "fixed" by filtering those entries out at render time — muting the alarm rather than
answering it.

**Reading was also one rule where it needed two**, which left the redirect target — the single most
important node in the loop — still reading a stale value from above the loop.

**The correction deletes machinery.** Propagation turned out not to be a separate pass at all: it is
the right `target_slots`, and `record_node_result` already writes a set of them. So
`propagate_down_column` does not exist, nor `ContextEntry.contributed_by` (the discriminator the
column version needed so a copied entry would not make a node read as *done* for a turn it never
ran), nor the render-time filter. Three pieces of complexity that existed only to prop up the wrong
shape.

**Why the history was rebuilt rather than patched.** Nothing was pushed, so it cost nobody anything;
the record lives in the documents, where someone will actually read it, rather than in commit
messages nobody greps; and a fix-on-top would have been four commits of mostly reverting, with the
wrong rule still sitting in the middle of the sequence for anyone reading it in order.

The corrected rules are in `Cyclic_Workflow_Implementation_Plan.md` §1.4, §1.5 and §Part 4, with the
wrong text preserved in banners there and in the Part 3 / Part 4 hand-off docs.

---

## 6. Exit criteria

| # | Criterion | Evidence |
|---|---|---|
| 1 | `K == 1` byte-identical | Measured against the branch point on three DAG shapes: identical execution order, identical S3 gets/puts and Mongo writes, identical key set, no cyclic key present. The document is **18 bytes larger** — exactly `,"open_turns":null`, one null key and no content, the same trade-off Part 3.5 recorded for `edge_decisions` |
| 2 | A re-entered node reads its own transcript | Asserted on the **composed input**, never on the stored document — `test_context_input.py`. Including the role split (the target's re-entries read `["a"]`, its forward pass reads `["s"]`) and the parentless target, whose input must stop being identical every turn |
| 3 | A turn writes the right slots, and no others | `test_slot_propagation.py`, on §1.6's worked graph. The negative is on a shape whose `R.S.` excludes an R.N. Plus a test that every entry in a node's slot is that node's own output — the defect the first implementation had |
| 4 | A declined parent is left out; one with no verdict contributes | §4 above |
| 5 | RISK-12 fixed | The two `xfail(strict=True)` tests XPASSed — failing the build, as designed — markers removed, characterisation test deleted. The abandoned turn no longer charges the budget |
| 6 | The upstream canary still passes on healthy graphs, including under `strict` | `test_upstream_canary.py` green |

### Everything mutation-tested

Every new test file was checked against deliberately wrong implementations. Caught, among others:
renumbering turns 1..N, not sorting, letting a malformed cell raise, folding urls into the text,
**one read-rule for everyone**, a target that keeps its data parents alongside itself, leaking
`reason` to the target in place of `message`, **skipping the compose for zero-upstream nodes**,
**writing only the current slot**, **writing every
slot regardless of `R.S.`**, using R.N. ancestors instead of R.N.-inside-`R.S.`, treating absence of
a verdict as a decline, ignoring the turn in a verdict lookup, disabling the RISK-12 resume, and
re-charging the budget on resume.

### Persistence, on §1.6's worked graph

| Shape | Document | Entries | S3 GETs | S3 PUTs |
|---|---|---|---|---|
| DAG (`K == 1`) | 8,810 B | — | 0 | 9 |
| cyclic, 1 redirect (`K=2`) | 13,395 B | 26 | 0 | 21 |
| cyclic, 2 redirects (`K=3`) | 19,807 B | 65 | 0 | 31 |

Still **zero reads**, still one document per save, so Part 2's write-amplification properties hold.
Growth is in `K` and in the number of dimensions a turn disturbs — which is bounded by `|R.N.|`,
not by `|R.S.|`, so it is far cheaper than the column version it replaced. Logged against RISK-2.

---

## 7. Carried forward

- **Part 5** — writes the verdicts `edge_decisions` stores and the messages `EdgeDecision.message`
  now has room for. **OQ-7 and OQ-10 are settled together there** (§3). OQ-3 still needs a rule.
- **Concurrency within a wave** — still not attempted; the canary is what will notice first.
- **RISK-1** — validation is still warn-only; unchanged here.
- Unchanged from Parts 3 and 3.5 and out of scope here: `run_action_with_excel_loop` recording
  nothing when every row fails, the bare `output[node]` subscript at `action_graph.py:1987`,
  `PUT /v1/action_graphs/{id}` returning 500 for a missing id, and the `batch_id` /
  `run_group_chat` / DELETE items in the Part 1 summary §6.

### Doc corrections made here

- **§Part 4's propagation rule** and **§2.1 of the Part 4 spec** — the column rule, corrected in
  place with the wrong text preserved in a banner. Same for the Part 3 hand-off docs, which carried
  it in their tables.
- **§1.4** — `k_input` is the redirector's handback, not the data parents' output.
- **§1.5** — "propagate" does not mean copying between nodes.
- **§8's OQ-1 row** — the "empty cell" argument replaced by the fresh-input one.
- The Part 4 spec undercounted `R.S.`'s existing consumers (two, not one). Every line number it
  cites (`396`, `960`, `1803`, `1880`, `1987`) is accurate; the Part 3.5 summary's `:1898` is not.
