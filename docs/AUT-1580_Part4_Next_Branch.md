# AUT-1580 · Next branch — Part 4: Relevant-Subgraph slot propagation, and making a slot readable

> ⚠️ **Historical.** This spec's `K == 1` byte-identity promise was deliberately given up in Part
> 4.5 — a DAG now keeps a one-slot context vector so both shapes run one code path. See
> `AUT-1580_Part4.5_Work_Summary.md` §3. Turn numbering is run-wide now too, so anything here about
> turns counting per node from 0 no longer holds.



> ### ⚠️ Corrected 2026-09-02 — §2.1 and §2.2 of this spec were wrong
>
> This brief told the implementer to push one node's result into **other nodes'** cells, and to
> build every node's prompt from its data parents. Both are wrong, both were built before it was
> caught, and the corrected rule now lives in `Cyclic_Workflow_Implementation_Plan.md` §Part 4,
> which is the authority. The banners in §2.1 and §2.2 below say what changed. Kept as written
> otherwise, as the historical brief.

> Prerequisite: Part 3.5 has landed. See `AUT-1580_Part3.5_Work_Summary.md` for what shipped and
> `Cyclic_Workflow_Implementation_Plan.md` §Part 4 for where this sits in the sequence.
>
> **This is the part where a cycle stops being write-only.** Parts 3 and 3.5 made a cycle execute
> and record correctly. Nothing reads what it records: `to_input` still builds every prompt from
> `output[parent]`, the flat baseline mirror. A re-entered node therefore has a perfect transcript
> of its own history and cannot see a word of it.
>
> The safety property is unchanged and still the thing to protect: **a graph with no redirect edges
> has `K == 1` and behaves exactly as it did before.** Parts 1, 2, 3 and 3.5 all held it. There are
> tests pinning it; keep them green.

---

## 1. The honest state of things

Part 3's summary says it plainly, and it is still true:

> **Part 3 makes a cycle execute and record. Part 4 makes it read.**

Concretely, today:

```python
# action_graph.py:396 — the whole graph run
action_response: Dict[str, ActionDoc] = node_results(action_graph_result_doc)
#                                       ^ result.output — one ActionDoc per node, no turn, no slot
```

`action_response` is handed to `ActionGraphInputCreator.to_input`, which builds prompt text out of
each parent's single stored output. There is no way for it to reach `node_vectors[parent].slot(k)`,
so the whole context vector is, from the model's point of view, invisible.

Two consequences worth stating separately, because they are fixed by different halves of this part:

1. **A node cannot read its own history.** Lap 3 of a reviewer node sees exactly what lap 0 saw.
2. **A redirect cannot carry a message.** Part 5's per-target text ("go back and fix the tone") is
   not any parent's output, so there is nowhere for it to live and nowhere to read it from. That is
   why Part 5 comes after this and not before.

---

## 2. Scope

### 2.1 Slot propagation across `R.S.(i,j)`

> **⚠️ WRONG AS ORIGINALLY WRITTEN.** This section said the update "walks **down a column**
> (context index *j*), not across a row", meaning `(V_ij)_new` is pushed into `V_kj` for every
> `k ∈ R.S.(i,j)`. That copies one node's output into other nodes' cells, so a node ends up holding
> its **grandparent's** output — which it should never see; it should only know what its own parent
> handed it.
>
> **The rule is:** when *j* ⇢ *i* opens turn *t*, the affected dimensions are the context indices
> of the **R.N.s inside `R.S.(i,j)`**, and **every participating node writes its own result into all
> of them**. A **row** update — one node, several of its own dimensions. No node ever writes into
> another node's slot. Full statement in `Cyclic_Workflow_Implementation_Plan.md` §Part 4.
>
> The original "updating a row would collapse the dimensions" warning is only true of writing the
> same value into *every* slot, which is case (i) and is meant to collapse them.

`R.S.` is already precomputed on `GraphTopology` (Part 1) and already has one consumer — the
scheduler's re-run set at `action_graph.py:960`. Part 4 adds the second, and the two are easy to
conflate, so Part 3's summary tabulated them:

| | Uses `R.S.(i,j)` for | Part |
|---|---|---|
| Scheduling | which **nodes** re-run this turn | 3 |
| Propagation | which **slots of each participant** get written | **4** |

### 2.2 `to_input` reads the correct slot

It must read the slot for the current context rather than always the baseline, and re-linearise a
tagged transcript into prompt text.

> **⚠️ INCOMPLETE AS WRITTEN.** "Read the slot" is not one rule — it is two, by role. The
> **redirect target** reads *its own* slot plus the input the redirector handed back, and does
> **not** re-read its data parents (they are upstream of the loop). Every **other** participating
> node reads its data parents' slots. Building the target's prompt from its data parents leaves the
> one node being told to try again reading a stale value and none of the feedback. See
> `Cyclic_Workflow_Implementation_Plan.md` §1.4 and §Part 4.

**Note what does not change.** `to_input` takes `upstream_nodes: List[str]` and
`action_response: Dict[str, ActionDoc]`. Part 3.5 settled (OQ-9) that the *caller* filters the
parent list and `to_input` keeps its signature. Whether the same shape works for slot reading is
this part's call — but changing `to_input`'s signature reaches `run_action_with_loop` (`:1803`),
`run_action_with_merge` (`:1880`) and every loop variant, so it is worth deciding deliberately
rather than drifting into.

### 2.3 The `edge_decisions` subtraction gets wired

Part 3.5 built the store and left it unread, by design. Part 4 is where it is consumed:

```python
contributing_parents(decisions, node, upstream_nodes, context_index=..., turn_no=...)
```

The run paths subtract the declined parents before composing input. **Absence means the parent
contributes** — nothing writes a verdict until Part 5, so wiring this changes no behaviour today,
which is exactly what makes it safe to land now rather than under pressure later.

### 2.4 The redirect message lands here

Part 5's per-target text is not a parent's output. Its home is the context slot, and this is the
part that makes the slot readable. Decide the shape now even though Part 5 writes it: a message is
attached to a `(edge, turn)` and read by the target node when it runs that turn.

Note `EdgeDecision` deliberately carries only `fired` and `reason` today. `reason` is the decider's
account of itself (RISK-9), **not** the message. If the message belongs on the same record, adding
a field is trivial; if it belongs in the slot, say so and put it there. Do not quietly overload
`reason`.

### 2.5 RISK-12 — resume must complete an outstanding turn

**New, found during Part 3.5 review, and this is the part where it stops being harmless.**

A crash mid-turn abandons the turn. The turn counter is durable; the *open frames* are not, so
nothing records that a turn was handed out and never completed. On the retry the frontier is
rebuilt empty, every node reads as done for the forward pass, and the redirecting node fires again
taking the **next** number.

Measured on `a → b → c → d` with `d ⇢ a`, crashing during turn 0:

```
attempt 1 (crash)   a:[0]      b:[0]      c:[]     d:[]
attempt 2 (retry)   a:[0,1,2]  b:[0,1,2]  c:[1,2]  d:[1,2]
                                          ↑ c and d never receive turn 0
```

Two costs. The transcript holds a turn only half the re-run set took part in — **harmless only
while nothing reads a slot, which is precisely what this part changes.** And the abandoned turn
still charged the budget, so a crash permanently costs the user one of their turns and parks the
run at `waiting` sooner than it should.

**Complete the turn; do not roll it back.** The node really executed and really cost money, and
`merge_slots` is explicitly "pure addition — nothing is ever overwritten and nothing is dropped".
Rollback would also need to know which nodes owed the turn, which needs the edge — the thing that
was not saved. Completing it needs one new piece of durable state: **turn → the edge that opened
it**, or equivalently a persisted open-frame list. Everything else exists already.

`tests/graphs/test_crash_mid_turn.py` reproduces it. Two of its tests are `xfail(strict=True)`, so
**they fail the build the moment this is fixed** — remove the markers then, and delete the
characterisation test beside them.

### 2.6 Keep intact

- `K == 1` byte-identical. Same execution order, same documents, same S3 counts.
- `upsert_entry`'s replace-on-identity behaviour. It is not a bug; a loop node reports
  progressively and this is what keeps one execution as one entry.
- The turn counter as the single authority on turn numbers.
- One slot per **R.N.**, not per edge.
- The upstream canary staying silent on healthy graphs.

---

## 3. Where the code is

| File | What changes |
|---|---|
| `action_graph/action_graph_input_creator.py` | `to_input` reads a slot and re-linearises a tagged transcript |
| `action_graph/action_graph.py` | `:396` `action_response` is no longer the whole story; `:1803` / `:1880` the two run paths; propagation after `record_node_result` |
| `action_graph/node_result_writer.py` | where a write currently lands in `run_ctx.target_slots` only — propagation across `R.S.` belongs near here |
| `action_graph/context_vector.py` | re-linearisation of a slot into prompt text, if it lives with the model rather than the creator |
| `action_graph/edge_decision.py` | already built; `contributing_parents` needs a call site |
| `action_graph/turn_counter.py` + `redirect_stack.py` | RISK-12: persisting open frames |

---

## 4. Decide before starting

| # | Question |
|---|---|
| **OQ-1** | D1's static `R.S.` includes node 5 in `R.S.(0,2)`, which the §6/§7 diagrams dim out. Ship static (precomputable) or narrow it at runtime? It is a one-function change to `GraphTopology.relevant_subgraph`, and **Part 4 is the part that first makes the difference visible**, because it decides which cells get written. |
| **OQ-7** | If a conditional edge declines, does the static `R.S.` still include nodes reachable only through it? Current stance: yes — conditionality affects control flow, not context resolution. Confirm before propagation depends on it. |
| — | Does `to_input` take the slot, or does the caller linearise and pass text? Signature change reaches every run path. |
| — | Where does the redirect message live — on `EdgeDecision`, or in the slot? §2.4. |
| — | RISK-12: persist open frames, or persist `turn → edge` and reconstruct? |

---

## 5. Exit criteria

1. **`K == 1` is byte-identical.** Same execution order, same documents, same S3 counts as Part 3's
   benchmarks. `tests/graphs/` stays green.
2. **A re-entered node reads its own transcript.** Asserted on the composed input, not on the
   stored document — this is the one part where asserting on the transcript would flatter the
   result, because Part 3 already made the transcript correct.
3. **A turn writes the right slots** *(restated — see the §2.1 banner)*: every participating node
   writes its **own** result into the slots of the R.N.s inside `R.S.(i,j)`, and into no others.
   Test the negative on a graph whose `R.S.` excludes an R.N., since one where each redirect's
   `R.S.` contains only its own R.N. cannot tell this rule from "write the current slot".
4. **A declined parent is left out of the input** it would otherwise contribute to, and a parent
   with no verdict still contributes.
5. **RISK-12 is fixed**: the two `xfail(strict=True)` tests in `test_crash_mid_turn.py` pass with
   their markers removed, and the abandoned turn no longer charges the budget.
6. The upstream canary still passes on healthy graphs, including under `strict`.

---

## 6. Suggested commit sequence

| # | Commit | Why this order |
|---|---|---|
| 1 | Slot linearisation — a tagged transcript becomes prompt text | Pure function, testable alone |
| 2 | `to_input` reads the slot for the current context | The behaviour change, alone and reviewable |
| 3 | Slot propagation across `R.S.` | Needs 2 to be observable |
| 4 | Wire `contributing_parents` into the run paths | No behaviour change today; safe on its own |
| 5 | RISK-12 — persist open frames, complete outstanding turns | Independent of 1–4; can be done first if preferred |
| 6 | Work summary doc | Same shape as Parts 1–3.5 |

---

## 7. Notes for whoever picks this up

### Terminology — read this first

**"turn" and "lap" are different things, and the plan doc uses "lap" for both.**

| | | |
|---|---|---|
| **turn** | one trip round one loop, and the number identifying it | `turn_counters`, saved on the document |
| **lap** | one wave of the work queue | `frontier.ScheduledNode.lap`, in memory, never saved |

One lap can carry several turns. Where the plan says "six laps fired", read "six turns". The code
was renamed in Part 3.5 (`turn_counter.py`, `next_turn`, `turns_taken`, `_turns_recorded`,
`assert_upstream_turn_recorded`) and `frontier.py` keeps "lap" for the wave. There is a glossary at
the top of `turn_counter.py`.

### Things that will cost you time if nobody tells you

- **Turn numbers are not dense.** Identity is exact-match `(turn_no, tag)` and ordering only needs
  them to sort. `test_multi_edge_turns.py` asserts `turns == list(range(n))` — that is really
  asserting "no crash happened", not an invariant. Do not build on density.
- **A node does not take part in every turn**, and its first turn is not necessarily 0. A node only
  runs the turns whose `R.S.` contains it, so its recorded turns have gaps (`[0, 2, 4]`), and
  which edge is declared first decides who gets turn 0.
- **Turns for one node run in ascending order** — verified across four shapes. That is a
  *consequence* of the scheduler being sequential, not a rule anything enforces. It breaks the day
  work inside a lap runs in parallel, which is what the upstream canary watches for.
- **Never verify a re-run by comparing `updated_at` or output text.** Those timestamps are the
  action *definition's* and never change per execution; many actions are deterministic. A previous
  attempt reported four false failures this way.
- **The two re-execution signals are different, and using the wrong one will fool you:**
  - **rerun** *empties* the node first, so `len(results)` goes 1 → gone → 1. **Delta zero or
    negative.** The only reliable signal is polling the run document every ~0.4s and watching the
    node leave and re-enter `output`.
  - **continue-run** appends, so `len(results)` grows. Delta positive.

  Measured on a real graph: rerun took node B from 2 results to 1 (replaced); continue-run took it
  from 1 to 2 (appended).
- **`tests/graphs/persistence_probe.py`** gives you a real `UserActionGraphResult` wired to
  counting fakes. Use it rather than mocking the writer; it is what proves the S3 behaviour did not
  regress.
- **The tests in `tests/graphs/` are written to fail against the wrong version.** Run them before
  assuming a change is right.

### Live testing

Three processes — Temporal, the runner worker, the API. Recipe in `AUT-1580_Part1_Work_Summary.md`
§7 and in the `dev-async` skill (`.claude/skills/dev-async/SKILL.md`). Most of Part 4 is verifiable
in-process with the probe harness; reach for the live stack only for end-to-end confirmation.

Facts learned running it in Part 3.5, all of which cost time to discover:

- `POST /{id}/async_run` has **two body params** (`input` and `webhook`), so FastAPI embeds them.
  The body must be `{"input": {...}}`. A bare `DataBlock` gets a 422.
- The results prefix is **`/v1/action_graphs_results`**, not `/action_graph_results`. The wrong one
  404s, and a polling harness that treats an unreadable document as empty will read that as "the
  run has no nodes".
- **A user-review gate blocks the reviewed node's *children*, not the node.** The reviewed node
  runs; its children are held and the run parks at `waiting`.
- **Approving a review on the graph does not reach an existing run.** The run carries its own
  `result.node_details` snapshot and a rerun deliberately skips fetching the graph, so the run
  stays `waiting` forever. The approval must be written to the run via
  `PUT /v1/action_graphs_results/?id=<run_id>`. Whether that is intended or a gap depends on what
  the frontend does — worth confirming before anything is changed.
- **Continue-run on a `waiting` run is refused with 405** and an explanation. That guard exists
  because such a run has nodes that never produced output and continue-run re-executes from stored
  results. If Part 4 touches that area, keep it.

### Out of scope — pre-existing, already logged, do not fold them in

- `run_action_with_excel_loop` records nothing when every row fails, unlike the concurrent loop
  which writes a synthetic `"Loop Failed"` document. That asymmetry is the only case where
  `upstream_ready` does real work, and it has no test.
- The bare `output[node]` subscript at `action_graph.py:1987` - `copy.deepcopy(...result.output[node])`, which raises a bare `KeyError` for a node with no stored result.
- `batch_id` / `run_group_chat` / DELETE items from the Part 1 summary §6.
- **`PUT /v1/action_graphs/{id}` returns 500 for any id that does not exist**, where `GET` and
  `DELETE` both correctly return 404. Found during Part 3.5 API testing; the endpoint returns
  whatever `.update()` gives it with no `None` check, so response validation fails and a Pydantic
  error leaks the internal `InvalidId`. Three-line fix, matching what delete already does —
  unrelated to AUT-1580.

### What Part 3.5 settled, so you do not reopen it

- **OQ-9** — per-`(edge, turn)` verdicts live in a flat `edge_decisions` map on the run document,
  keyed `<target>|<source>|<context_index>|<turn_no>`. `to_input` does not read it; the run paths
  subtract `declined_parents` from `upstream_nodes`. Absence means the parent contributes. Not on
  `ContextEntry` — `upsert_entry` replaces the cell on every progressive loop write and would erase
  it.
- **It is not a readiness question.** A parent that declines still ran and still produced output, so
  `is_work_done` passes and nothing blocks. This engine cannot deadlock on a declined edge; §1.11's
  framing assumes a node waits for a message and this one checks whether output exists. It is an
  **input-composition** problem.
- **Turn numbers are scheduler state**, on the document, keyed per `(node, context_index)`.
  `_turns_recorded` survives only as the seed for a run begun before the field existed.
- **`RedirectFrame` carries `target`**, so `R.S.(i, j)` is finally addressable at the frame. That is
  what makes §2.1 expressible at all.
