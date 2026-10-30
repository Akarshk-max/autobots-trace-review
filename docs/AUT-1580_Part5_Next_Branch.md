# AUT-1580 · Next branch — Part 5: runtime routing, and an edge that can say no

> Prerequisite: Part 4 has landed (PR #239, merged into this branch's base). See
> `AUT-1580_Part4_Work_Summary.md` for what shipped and `Cyclic_Workflow_Implementation_Plan.md`
> §Part 5 for the full design. **§Part 5 · 5.0 is the part written specifically for you** — read it
> before anything else in that document.
>
> **This is the part where an edge fires because the work says so, not because it exists.** Today
> both edge kinds fire unconditionally: a redirect is bounded only by its budget, and a conditional
> edge is inert structure the engine treats as a plain data edge. So a cycle always burns its full
> budget and never converges early, and a `conditional_edges` entry does nothing at all.
>
> Every read Part 4 built is already wired and waiting for a writer. `edge_decisions` is durable and
> empty; `EdgeDecision.message` exists and nothing sets it; `contributing_upstream` subtracts a set
> that is always empty. **Part 5 is mostly a writer for stores that already exist.**
>
> ---
>
> ## ⚠️ READ THIS FIRST — Part 4.5 landed after this spec was written
>
> This document was written against `7a8fa9f80`. **Five commits have landed since**
> (`6cc766715`, `88981eea2`, `cb59bc9b3`, `4f4a4fa81`, `9b4347671`), and they change things this
> spec asserts. `AUT-1580_Part4.5_Work_Summary.md` is the full account; what changes *your* job is
> inlined below and marked **[4.5]**.
>
> **[4.5] The `K == 1` byte-identity property is gone, on purpose.** A DAG now keeps a context
> vector too - one slot, mirroring `ActionDoc.results` - so the acyclic and cyclic paths are one
> code path rather than two engines. Measured: document and S3 volume **1.43x**; execution order and
> write counts unchanged. **Do not try to restore it, and do not use it as a review criterion.** The
> gate is `writes_vectors = bool(topology.start_node_id)`; a graph with no single root keeps no
> vector, which is what stops a one-node graph or a pure data cycle from crashing.
>
> The property that **replaces** it, and which you must keep: *a graph with no redirect edges and no
> conditional edges opens no turn, charges no budget, and keeps exactly one dimension.* Same intent -
> Part 5 must not disturb a plain DAG - stated in terms that are still true.
>
> **[4.5] Turn numbers are run-wide, so D3 is wrong as written.** They no longer "count per
> `node_id` from 0". A redirect's first turn is **1**, because the forward pass owns 0. Columns are
> sparse, and numbers can repeat across a run's life, because they are derived from what is
> *recorded* and a rerun erases records. Six tests that asserted `turns == list(range(n))` now assert
> uniqueness and ordering. **Do not build on density, and do not assume turn 0.**
>
> **[4.5] There is a fifth run-state field, `open_continue_run`, and a fourth kind of write.**
> SS2.8's account of what writes which slots is superseded by SS4 of the Part 4.5 summary. Read that
> table before touching `record_node_result` or `resolve_run_context`.
>
> **[4.5] `output[node]` is the baseline, not the latest.** A re-entry writes its own dimensions and
> leaves `output` alone. If you need a turn's value, read its slot.
>
> Baseline: `tests/graphs/` is **422 passed, 0 xfailed** at `a41314b3b`. **Every line number below
> was checked against `7a8fa9f80` and most have moved** - `action_graph.py` alone grew by ~700
> lines. Re-grep rather than trusting any `file.py:NNN`.
>
> ---

---

## 1. The honest state of things

Part 4's summary says it plainly, and it is still true:

> Nothing writes a verdict until Part 5, so every graph running today has `edge_decisions is None`,
> gets its parent list back *by identity*, and produces a byte-identical prompt.

Concretely, today:

```
$ grep -rn 'record_decision(' src/ | grep -v 'def record_decision'
  (nothing)
```

There is **no writer anywhere in `src/`**. The consequence is that three finished mechanisms are
sitting inert:

| Built in | Mechanism | Waiting for |
|---|---|---|
| Part 3.5 | `edge_decisions` store, keyed `<target>\|<source>\|<context_index>\|<turn_no>` | a verdict |
| Part 4 | `contributing_upstream` — drops declined parents before composing input | a verdict |
| Part 4 | `EdgeDecision.message` + `message_for` — the redirect handback the target reads | a message |

That last one is worth dwelling on. Part 4 built the target's read *and* the channel the message
arrives through, and left the channel empty on purpose. A redirect target today composes its prompt
from its own transcript and nothing else. **The moment you write a message, it appears in the next
prompt with no further wiring.**

---

## 2. Scope

### 2.1 The decider (D9, D10)

A node with an outgoing conditional or redirect edge is asked, at call time, which of its targets
should fire. Via a **system prompt applied at call time only** — never written into the stored
`ActionDoc`, never present in the recorded `input` (**D10**). The transcript stays the user's
content, and re-running a stored action is unaffected.

Prompt shape and the JSON output contract are fully specified in §Part 5 · 5.1 and 5.2. Two
constraints there are load-bearing and easy to "simplify" away:

- **No `redirection: true/false` field.** The graph already knows which list each target's edge
  lives in. A boolean the model also supplies is a second source of truth that can disagree with
  the first — the same reason `EdgeSpec` carries no `type` (D2).
- **`targets` is a map, not two parallel arrays.** Parallel `redirect_to` / `redirection_text`
  lists go out of sync the first time a model emits three of one and two of the other.

### 2.2 The parse ladder, and the give-up branch that differs by edge kind

§Part 5 · 5.3. Reuse `ActionTeam._extract_json_blocks`
(`src/autobots/action_team/action_team/action_team.py:777`) rather than writing a third copy — lift
it to shared code. Note the deliberate divergence from `_parse_leader_response` (`:741`), which
fails **open** and loops again:

| Edge | Give up means |
|---|---|
| Redirect | **do not loop.** Fail-closed: the run ends rather than spinning. |
| Conditional | **fire it.** A parent whose verdict is unknown is treated as having sent its output — which is exactly today's behaviour, so an unparseable decision degrades to the current engine rather than to something new. |

### 2.3 Writing the verdicts, and the message

`record_decision(...)` already takes `fired`, `reason` **and** `message`
(`edge_decision.py:101`). One call per candidate edge, per turn.

**`reason` and `message` are different things and must not be conflated.** `reason` is the decider
accounting for its own choice (RISK-9) and is never shown to anyone; `message` is the instruction
the target reads. There is already a test pinning that a target never sees `reason` —
`test_context_input.py::test_the_message_is_not_the_reason`.

### 2.4 OQ-7 + OQ-10 — the readiness gate. **Do this in the same change as the decider**

Read §Part 5 · 5.0 in full; it was written for this. The short version:

> A node whose **every** feeding edge declined this turn **does not run** — no execution, no
> `ContextEntry`, no slot propagation.

The moment 2.1 makes an edge capable of declining, a node can be re-armed for a turn nothing fed
it. Until then the case cannot arise; from 2.1 onward it arises immediately. Doing this "after"
means shipping a window where the engine pays for a model call to recompute the same output from
the same inputs.

**The gate cannot live in `R.S.`** `R.S.(i, j)` is handed to `frontier.rearm()` at the instant the
redirect *fires*, and edges decline *later*. The information does not exist when `R.S.` is used, so
narrowing it is not merely wrong — it is not possible. It goes at **execution** time in
`run_action_graph_v2`, beside the existing `node_already_done` (`action_graph.py:525`) and
`upstream_ready` (`:528`) checks, where the parents have already run and their verdicts are on the
document.

### 2.5 What each kind does to the queue

§Part 5 · 5.4. One line there is the whole trap:

> **Redirect, on fire:** re-arm `R.S.(target, node)` — **not** just the target, or the target's
> descendants never re-run and its new output goes nowhere.

`fire_redirects` (`action_graph.py:938`) already does this correctly. Part 5 gates *whether* it
fires, and must not change *what* it arms.

A conditional edge queues nothing — every node is already in the frontier from lap 0. It changes
**readiness**, not membership.

### 2.6 Loop nodes: collate, then decide once (D11)

§Part 5 · 5.5. The iterations get the **normal** prompt; one decider call follows collation.
"Inject into the last iteration" is not available — iterations run under `asyncio.gather` with a
semaphore, so there is no defined last one.

### 2.7 C.R.N. groups (§1.10) and validation (§5.7)

Built on the same negative signal, which is what makes them deadlock-free without timeout
machinery. **OQ-3 still needs a rule** (a member never *reached* at all in a round). §5.7 adds one
cheap validation: a node with an outgoing conditional or redirect edge whose action cannot produce
text can never route, and the run will silently never loop — catch it while someone is still
drawing the graph.

### 2.8 Keep intact

- **[4.5 — REPLACED] A DAG opens no turn, charges no budget, and keeps one dimension.** The old
  wording here was "`K == 1` byte-identical", and that property was deliberately given up in Part
  4.5: a DAG now keeps a one-slot vector so both shapes run one code path. What must still hold is
  that Part 5 does not disturb a plain DAG — `redirect_budgets`, `turn_counters`, `open_turns` and
  `open_continue_run` all absent, exactly one slot per node.
  `test_a_graph_with_no_redirect_edges_is_untouched` is that assertion now.
  If Part 5 adds a document field, still measure its cost and say so — and still consider whether it
  can live on an existing record instead, the way `message` went onto `EdgeDecision`. Count the
  run-state fields on `ActionGraphDoc` before you add one - the list has grown past
  `open_continue_run` and keeps growing from `main`.
- **`upsert_entry`'s replace-on-identity behaviour.** Not a bug; a loop node reports progressively.
- **The turn counter as the single authority on turn numbers.**
- **One slot per R.N., not per edge.**
- **The upstream canary staying silent on healthy graphs** — see §7, this is the one most at risk.
- **Propagation writes a *row*, not a column.** No node ever writes into another node's slot. Part 4
  got this wrong first; the corrected rule and the wrong one are both in §Part 4.

---

## 3. Where the code is

| File | What changes |
|---|---|
| `action_team/action_team/action_team.py:777` | `_extract_json_blocks` — lift to shared code, do not copy |
| `action_graph/edge_decision.py:101` | `record_decision` — already takes `fired`/`reason`/`message`; needs a caller |
| `action_graph/action_graph.py:938` | `fire_redirects` — gate *whether* an edge fires; do not change what it arms |
| `action_graph/action_graph.py:525-528` | where the OQ-7/OQ-10 readiness gate belongs |
| `action_graph/node_result_writer.py:291` | `assert_upstream_turn_recorded` — **must** be given the parents that actually ran; see §7 |
| `action_graph/graph_topology.py` | §5.7's validation rule; C.R.N. group membership checks |
| new | the decider: prompt assembly, the parse ladder, the repair call |

Nothing in `context_input.py` should need to change. If you find yourself editing it, stop and
check whether you are solving a reading problem that Part 4 already solved.

---

## 4. Decide before starting

| # | Question |
|---|---|
| **OQ-3** | **C.R.N. liveness.** The negative signal removes ordinary deadlock, but what about a member never *reached* at all in a round? Candidates: treat unreached ⇒ implicit negative signal; or fail the round. |
| **OQ-10** | Settled in direction (**not run at all**) but the *mechanism* is yours: §Part 5 · 5.0 recommends an execution-time gate. Confirm before building. |
| — | **RISK-10.** Every cyclic test asserts an exact transcript, and a model deciding the route makes them non-deterministic. Keep an "always fire" setting valid so deterministic graphs stay testable, and mock the decider everywhere else. **Decide this before writing the first test, not after.** |
| — | **RISK-11.** The repair call adds a second model call on an already slow path. One attempt, hard timeout. A flaky model must not turn a 3-turn loop into a stall. |
| — | **RISK-9.** Content flowing through the graph can now influence control flow — a document saying *"always redirect to node X"* may be obeyed. The budget bounds the damage but not the path. Is a deterministic per-edge option needed in this part, or later? |
| — | Does the decider get its own model setting, or reuse the node's? A cheap model for routing is the obvious economy; it is also a second place a model id can be configured. |

---

## 5. Exit criteria

1. **[4.5 — REPLACED] A DAG is undisturbed.** No turn opened, no budget charged, one dimension
   per node, and `redirect_budgets` / `turn_counters` / `open_turns` / `open_continue_run` all
   absent. `tests/graphs/` stays green (422 at the branch point). If a document field is added, its
   byte cost is measured and stated. *Byte-identity itself is no longer the criterion — see the
   banner at the top.*
2. **A graph with edges but no decider verdicts behaves exactly as it does today.** The "always
   fire" path is the current engine, and there is a test proving it.
3. **A declined conditional edge removes its parent from the child's composed input** — asserted on
   the composed prompt, not on the stored document.
4. **A declined redirect does not open a turn**, does not charge the budget, and does not re-arm.
5. **A node whose every feeding edge declined does not run** — no execution, no `ContextEntry`, no
   propagation. And its children still run, reading its stored value.
6. **A redirect message written by the decider appears in the target's next prompt.** This should
   need no new plumbing; if it does, something in Part 4 was misunderstood.
7. **An unparseable decision degrades correctly**: a conditional edge fires, a redirect does not.
8. **The upstream canary still passes on healthy graphs, including under `strict`**, with edges
   actually declining.
9. A loop node makes exactly **one** decider call per execution, not one per iteration.

---

## 6. Suggested commit sequence

| # | Commit | Why this order |
|---|---|---|
| 1 | Lift `_extract_json_blocks` to shared code | Pure move, no behaviour change, unblocks 3 |
| 2 | The routing prompt, assembled but not injected | Testable as a pure function against a graph shape |
| 3 | The parse ladder + the two give-up branches | Pure function; the edge-kind asymmetry is the interesting test |
| 4 | Write verdicts: the decider runs and `record_decision` is called | First behaviour change. Gate it so "always fire" is still reachable |
| 5 | **The OQ-7/OQ-10 readiness gate, with the canary's parent set fixed** | Must land with 4 or immediately after — see §2.4 |
| 6 | The redirect message: `record_decision(..., message=...)` | Should be small. Part 4 built the reader |
| 7 | C.R.N. groups (§1.10), OQ-3's rule | Independent of 1–6; can be its own PR |
| 8 | §5.7 validation | Cheap, independent |
| 9 | Work summary doc | Same shape as Parts 1–4 |

---

## 7. Notes for whoever picks this up

### Terminology — read this first

**"turn" and "lap" are different things, and the plan doc uses "lap" for both.**

| | | |
|---|---|---|
| **turn** | one trip round one loop, and the number identifying it | `turn_counters`, saved on the document |
| **lap** | one wave of the work queue | `frontier.ScheduledNode.lap`, in memory, never saved |

One lap can carry several turns. Where the plan says "six laps fired", read "six turns". There is a
glossary at the top of `turn_counter.py`.

### The canary will cry wolf, and the tempting fix is the wrong one

**This is the single most likely way to lose something valuable in this part.**

`assert_upstream_turn_recorded` (`node_result_writer.py:291`) has been quietly correct since Part
3.5. It cannot fire today, because work inside a turn is sequential. The first time an edge
declines, it will start reporting **healthy** graphs as broken ordering invariants.

**The obvious guess at the cause is wrong.** A parent that *declined an outgoing edge* still ran and
still recorded — declining is a fact about the edge, not the node (Part 3.5 §4, and the whole reason
OQ-9 is an input-composition question). The parent holding no entry is one **skipped by §2.4's
rule**, because every edge feeding *it* declined.

So the fix is the **parent set** — pass the parents that actually ran this turn, not everyone in
`R.S.` Do not loosen the check. It exists to catch a real concurrency bug the day anything inside a
turn runs in parallel, and a canary that has been quietly correct for months is worth more than one
written under pressure later.

### Things that will cost you time if nobody tells you

- **Nothing writes a verdict today.** If a test of yours passes without you having written one,
  check whether it is testing anything at all.
- **[4.5] A deterministic fake hides a broken write path, and every fake in `tests/graphs/` is
  deterministic.** `baseline_matches_action_doc` compares a value to itself and passes however wrong
  the write path is. This hid nine mirror-drift reports on a healthy three-turn run for the whole of
  Part 4. If you touch anything that writes a result, test it with actions whose output *changes per
  call* — `test_baseline_mirror_under_a_cycle.py` is the pattern, and its own necessity is
  mutation-tested by making the fake constant and watching it pass with the bug in.
- **[4.5] `ActionDoc.results` and slot 0 count the same events, and a redirect turn is not one of
  them.** A redirect turn targets `propagation_slots`, which never contains slot 0, so
  `record_node_result` leaves `output[node]` alone and the turn never reaches `results`. Measured: a
  node that executed four times — once forward, three times round the loop — holds **one** result.
  If you are counting executions, count slot entries.
- **[4.5] A continue-run is measured by `run_action_doc`, not `run_action`.** It goes through
  `run_node_action_with_existing_doc`. Patching only `ActionGraph.run_action` makes a working
  continue-run look dead — that mistake cost an hour and produced a confident wrong conclusion.
- **[4.5] `to_wait` is sticky, and a gate never fires for a node that is already done.** One gated
  node stops the whole remaining pass including unrelated branches; and the done-check `continue`s
  before the gate checks, so a run can report `success` with an unapproved gate outstanding. Both
  pre-existing, both surprising the first time.
- **[4.5] An interrupted mutation sweep can leave the source mutated.** Write mutants to a copy, or
  verify the restore before trusting the next run.
- **Turn numbers are not dense**, a node does not take part in every turn, and its first turn is not
  necessarily 0. `test_multi_edge_turns.py` asserts `turns == list(range(n))` — that is asserting
  "no crash happened", not an invariant. Do not build on density.
- **Never verify a re-run by comparing `updated_at` or output text.** Those timestamps are the
  action *definition's* and never change per execution; many actions are deterministic. This has
  now fooled three separate attempts.
- **The two re-execution signals are OPPOSITE.** A **rerun** empties the node first, so
  `len(results)` goes 1 → gone → 1 — delta zero or negative; the only reliable signal is polling the
  run document and watching the node leave and re-enter `output`. A **continue-run** appends, so
  `len(results)` grows. Verified live in Part 4 against a real graph.
- **A polling harness must be seeded with a pre-request baseline.** Starting from an empty dict
  makes every pre-existing node look like it just appeared, which reads as a re-execution that never
  happened. Part 4 lost time to exactly this.
- **One run-document GET costs ~2.3s** (it reads the whole thing from S3), so a serial poll loop
  samples every ~2.7s however small the sleep. A node with no action re-executes well inside that
  window. Use staggered concurrent pollers if you need sub-second resolution.
- **`upsert_entry` is not a bug and must not be changed.** It replaces on a duplicate
  `(turn_no, node_id)` because a loop node reports progressively; `append_entry` there would turn
  one execution into N entries.
- **The tests in `tests/graphs/` are written to fail against the wrong version.** Run them before
  assuming a change is right — and mutation-test your own new ones the same way. Part 4 ran
  fourteen deliberate mutations and every one was caught; two real defects were found that way.
- **The transcript being correct and the prompt being correct are different claims.** Part 4 shipped
  a defect where one node's output was attributed to another in the composed prompt, and every test
  passed — they asserted history *appeared*, not that it was attributed correctly. **Read an actual
  composed prompt.**

### Live testing

**Run `/test-graph-run` first** (`.claude/skills/test-graph-run/SKILL.md`). It carries the working
polling harness, the exact query params for all six run modes, what to assert for each, the cyclic
checks, and the traps below — written after Part 4's live testing so you do not rediscover them.
`/dev-async` brings the stack up.

Three processes — Temporal, the runner worker, the API. Recipe in the `dev-async` skill
(`.claude/skills/dev-async/SKILL.md`) and `AUT-1580_Part1_Work_Summary.md` §7. Facts that cost time
in Parts 3.5 and 4, all still true:

- `POST /{id}/async_run` has **two body params** (`input` and `webhook`), so FastAPI embeds them.
  The body must be `{"input": {...}}`. A bare `DataBlock` gets a 422.
- The results prefix is **`/v1/action_graphs_results`**, not `/action_graph_results`.
- A **user-review gate blocks the reviewed node's *children*, not the node.** `user_input_required`
  blocks the node itself. Both park the run at `waiting`.
- **Approving a review on the graph does not reach an existing run** — the run carries its own
  `result.node_details` snapshot. Write it to the run via `PUT /v1/action_graphs_results/?id=<run_id>`.
- **Continue-run on a `waiting` run is refused with 405.** That guard exists because such a run has
  nodes that never produced output. Keep it.
- **The rerun/empty logic is duplicated in the Temporal activity**
  (`conn/temporal/action_graph/activity.py:73-81`) *without* the logging that
  `_run_as_background_task` has. Do not conclude from a missing "Empty all Nodes" log line that
  `forget_all` did not run — Part 4 did, and was wrong.
- `make dev` runs uvicorn with `--workers 3 --reload`, so killing the port holder is not enough to
  stop it; kill the tree.

### Out of scope — pre-existing, already logged, do not fold them in

- `run_action_with_excel_loop` records nothing when every row fails, unlike the concurrent loop
  which writes a synthetic `"Loop Failed"` document.
- The bare `output[node]` subscript at `action_graph.py:2218` on this branch (it was `:1987` on
  the Part 4 branch — it moves, so re-grep rather than trusting the number), which raises a bare
  `KeyError` for a node with no stored result.
- **`PUT /v1/action_graphs/{id}` returns 500 for any id that does not exist**, where `GET` and
  `DELETE` both correctly return 404. Confirmed still present in Part 4's live API testing.
- `batch_id` / `run_group_chat` / DELETE items from the Part 1 summary §6.
- **[4.5] Continue-run from a leaf node returns 500.** `get_subgraph_map` only adds a node as a key
  when it *has* children, so a childless root yields `{}` and `coffman_graham` gets `[[], [], …]`.
  Confirmed identical at `bf379f22e`, so it pre-dates this branch. Reproduced in Part 4.5 §10.
- **[4.5] Chatting a node that has never run raises `AttributeError`** — `run_node` does
  `action_response.get(node).id` with no None check. Same family as the bare `output[node]`
  subscript.
- **[4.5] Nothing tracks staleness.** Chat with a node and everything below it is silently derived
  from an output that no longer exists, while the run reports `success`.

### What Parts 3.5 and 4 settled, so you do not reopen it

- **OQ-1 — static `R.S.`, closed**, confirmed with the author: the §6/§7 diagrams are wrong, the
  rule is right. The reason is **fresh input**, not an empty cell — an earlier draft argued the
  latter and it is wrong, and is kept marked in §8 precisely because it is what a future narrowing
  proposal would be checked against.
- **OQ-9 — per-`(edge, turn)` verdicts** live in a flat `edge_decisions` map on the run document.
  `to_input` does not read it; the run paths subtract declined parents. **Absence means the parent
  contributes.**
- **A declined edge is an input-composition problem, not a readiness one** — for the *parent*. The
  one readiness question in the system is §2.4's, and it is about a node that received nothing at
  all, which is a different thing.
- **Propagation is a row, not a column.** The write set is the context indices of the R.N.s inside
  `R.S.(i,j)`; every participant writes its **own** result into all of them; no node ever writes
  into another node's slot.
- **Reading is two rules, by role.** The redirect **target** reads its own slot plus the handback;
  every other participant reads its data parents' slots. A target with no data parents (the graph
  root, sent work back to it) gets the same context through the graph input via
  `contextual_input_dict`.
- **The redirect message lives on `EdgeDecision.message`**, not in the slot and not on `reason`.
- **RISK-12 is fixed.** `open_turns` records `turn → the edge that opened it`; a retry completes an
  outstanding turn rather than opening a new one, and does not re-charge the budget.
