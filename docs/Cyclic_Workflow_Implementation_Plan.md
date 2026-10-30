# Cyclic & Conditional Workflows — Understanding and Implementation Plan (0 → 1)

> Source design: `cyclic-workflow-algorithm-2.html` (transcribed handwritten notes, §1–§19).
> Current engine: see `docs/Action_Graph_Architecture.md` for the as-implemented DAG engine.
> Branch: `AUT-1580-cyclic-workflow-v1`. **Part 1 is what we build in this branch.**

---

## 0. How to read this document

The design has two halves and they are largely independent:

- **Track A — Architecture** (§1–§13 of the design): make the engine survive back edges. This is a
  change to the *execution model*.
- **Track B — Multiplayer** (§14–§18): admins, stakeholders, scoped rights, inboxes, run sharing.
  This is a change to the *authorization model*.

This document is in four movements: what the design says (§1–2), what the code does today and where
the gaps are (§3), the decisions we have locked (§4), and the phased build plan (§5–§7), with Part 1
specified to the file-and-function level.

---

## 1. Understanding — Track A, the algorithm

### 1.1 The one observation everything follows from

In a DAG each node fires exactly once, so "the context of node *n*" is a single well-defined value:
the accumulated history of its ancestors. Add a back edge and *n* can be entered many times, from
structurally different places. Context stops being a function of *n* alone — it becomes a function of
*n* **and whoever sent me here this time**.

```
DAG                              CYCLIC
a → b → c → d                    a → b → c → d
                                       ↑       ┊
                                       └───────┘   (d redirects into b)

context(b) = one value           context(b) = one value PER SENDER
V(b) → a single result object    V(b) → a vector, one slot per sender
```

Single context dimension → multiple context dimensions. That is the whole design.

### 1.2 Three kinds of neighbour

From node *n*'s point of view:

| Role | Meaning |
|---|---|
| **Upstream nodes (U.N.)** | Normal parents. Feed *n* on the forward pass. |
| **Downstream nodes (D.N.)** | Normal children. *n* feeds them. |
| **Redirecting nodes (R.N.)** | A **subset of the downstream nodes** that also point back into *n*. These are what make *n* run again. |

`R.N. ⊆ D.N.` is a hard invariant — a redirect edge *j* ⇢ *i* is only meaningful when *j* is already
a descendant of *i*. We validate it (§7.3).

### 1.3 The context vector

Every node carries a vector of result objects, one slot per possible redirecting node:

```
Vₙ = [ Vₙ₀, Vₙ₁, Vₙ₂, … ]
```

> **Definition.** `V_ni` is the context node *n* should have — and will have — when the redirection
> into it is due to node *i*.

The index is **not** a timestep and **not** a visit counter. It is the **identity of the redirecting
node**. Two entries into *n* caused by the same R.N. see the same context; entries caused by
different R.N.s do not.

Slot `0` is the **baseline** — the pre-redirection dimension. It is also the only dimension the user
ever talks to (§1.9).

### 1.4 The two cases

Initialise every slot of every node to `None`. Then, each time a node receives input:

**Case (i) — every slot is still `None`.** No redirection has happened yet, so there is nothing to
disambiguate. Run the node normally and write the same result object into **every** index.

```
R.O. = { (U.N.)input , node_output }
V_ni = R.O.    ∀ i
```

This is not waste — it establishes a **baseline**. Whoever redirects here later, the pre-cycle
history is identical for all of them, so every dimension should start from the same place.
(The notes leave this as an open "Why?"; this is the reading we implement, and §1.9 confirms it —
"reset to the start node" is only coherent if the baseline is uniform.)

**Case (ii) — the slots are populated.** Somebody has already redirected. Let *k* be the R.N.
responsible for this entry. Read **only** `V_nk`; the other dimensions are irrelevant here.

```
{ V_nk } + k_input → n → n_output
```

The asymmetry is the point: **case (i) writes broadly because there is no ambiguity; case (ii) reads
narrowly because there is.**

> **`k_input` is the redirector's handback, not the data parents' output — and this applies to the
> redirect *target*.** When *k* sends work back to *n*, what *n* reads is its own slot `V_nk` plus
> what *k* handed it. *n* does **not** re-read its data parents: they sit upstream of the loop and
> have nothing new to say. Every *other* node in the turn is ordinary and reads its data parents'
> slots as usual. One rule per role. `k_input` is also where Part 5's redirect message
> ("go back and fix the tone") lives — see §Part 4.

### 1.5 Which slots get updated — the Relevant Subgraph

After a node runs, the result must propagate. The notes reject "only V_n0", "everything", "all
unrelated nodes", "only downstream of the target", "only upstream of the target" — and land on:

> **Read "propagate" carefully.** `R.S.` answers **which nodes take part in the turn**. It does
> *not* mean one node's output is copied into other nodes' cells — a node's row only ever holds its
> own work. What `R.S.` additionally decides is **which of a participant's own slots** get written:
> the dimensions of the R.N.s lying inside `R.S.(i,j)`. The full rule, and the wrong version that
> was implemented first, are in §Part 4.

> **Definition.** For a redirect *j* → *i*, a node *k* belongs to the Relevant Subgraph iff *k* is
> related to *j* **and** *k* ∈ subgraph(*i*).

"Related to *j*" is the design's own admitted loose end. **Decision D1** (§4) pins it to the static
reading: **related to *j* means comparable to *j* in the reachability order** — an ancestor of *j*
or a descendant of *j*. What is genuinely *un*related is what is **incomparable**: a branch off *i*
that neither feeds *j* nor is fed by *j*.

```
R.S.(i, j) = ( subgraph(i) ∩ ancestors(j) )  ∪  descendants(j)
```

all sets inclusive of their endpoints.

**Both populations are load-bearing:**

| Population | Why it is affected by the redirect |
|---|---|
| **ancestors of *j*** (clipped to subgraph(*i*)) | They re-run because *i* re-runs and feeds down the loop toward *j*. |
| **descendants of *j*** | They re-run because *j* recomputes and pushes new output down its own data edges. |

Note the asymmetry in the formula: because *j* ∈ subgraph(*i*), **all** of `descendants(j)` is
already inside `subgraph(i)` by transitivity, so no clipping is needed on that side. Only the
ancestor side needs the intersection, because ancestors of *j* can run *above* *i* (node −1 in the
worked graph).

This is fully **precomputable at graph-build time**, which is exactly what §13 asks for
("precompute the ancestral R.N.s / descendant R.N.s").

On the worked graph (§1.6):

| | | |
|---|---|---|
| `R.S.(0,2)` | `({0,1,5,2,3,4} ∩ {-1,0,1,5,2}) ∪ {2,3,4}` | `= {0,1,5,2,3,4}` — all of subgraph(0) |
| `R.S.(0,5)` | `({0,1,5,2,3,4} ∩ {-1,0,5}) ∪ {5,2,3,4}` | `= {0,5,2,3,4}` — **node 1 excluded** |

> **The 2 ⇢ 0 example is degenerate.** Every node under 0 happens to be comparable to 2, so `R.S.`
> collapses to `subgraph(0)` there and tells you nothing about whether the rule is narrow enough.
> It is **5 ⇢ 0 that shows the rule biting**: node 1 is a sibling of 5 (both feed 2), incomparable
> to it, and correctly drops out. Any test that only exercises `R.S.(0,2)` would pass under the
> wrong definition — so §6.6 tests both.

> **Documented divergence.** The §6/§7 diagrams dim node 5 out of `R.S.(0,2)`. No purely static rule
> reproduces that (5 *is* a structural ancestor of 2 via 5→2), so the picture implies a runtime,
> trace-based notion of "related". We are deliberately taking the static rule for its
> precomputability. If runtime behaviour later shows the extra column write is wrong, narrowing
> `R.S.` is a **localised change to one function** (`GraphTopology.relevant_subgraph`) and touches
> nothing else. This is recorded as **OQ-1** in §8.

### 1.6 The worked graph

```
 -1 ──▶ 0 ──▶ 1 ──▶ 2 ──▶ 3
        │            │ ╰──▶ 4
        └──▶ 5 ──────┘
        ▲            ┊
        ├┈┈┈┈┈┈┈┈┈┈┈┈┘   redirect 2 ⇢ 0
        └┈┈┈┈┈┈ 5         redirect 5 ⇢ 0

data edges:     -1→0, 0→1, 0→5, 1→2, 5→2, 2→3, 2→4
redirect edges: 2⇢0, 5⇢0
R.N. = {2, 5}   →   K = |R.N.| + 1 = 3
context_index = { baseline: 0, "2": 1, "5": 2 }
```

Node 0 is re-entered by **two different** redirectors — precisely the case a single context field
cannot represent.

### 1.7 The result object and f_comb — **fully specified**

A slot does not hold one value; it holds an **ordered, tagged transcript**, because a merged object
is "no longer one node's turn but an interleaved transcript of several".

**Entry shape** (`ContextEntry`):

```python
{
    "input":    Dict[str, Any],   # the input DataBlock dump for this contribution
    "output":   Dict[str, Any],   # the output DataBlocks dump
    "node_id":  str,              # PROVENANCE TAG (see below)
    "turn_no":  int,              # counter per (slot, node_id) — independent per tag, sparse
}
```

**Provenance tag (`node_id`).**
- On the **forward pass** — the node receiving from its upstream nodes — the tag is the
  **start node's id**.
- On **each redirection** — the tag is the **corresponding redirecting node's id**.

**Turn number.** A counter keyed by **(slot, provenance tag)**. The first entry carrying a given
`node_id` in a given slot has `turn_no = 0`; each subsequent one is `prev_turn_no + 1` **for that tag
alone**.

> **Invariant — turn counters are independent and sparse.** Each tag advances on its own. There is
> **no** global lap number, **no** cross-tag synchronisation, and **no** requirement that one tag
> reach turn *N* before another may. A transcript may legitimately hold `("2", 0)`, `("2", 1)`,
> `("2", 2)` and only `(start, 0)` — the start tag simply never reaches turn 1. This holds for
> **every pass ≥ 2**, not just the second.
>
> **The start tag is stronger than sparse — it is pinned.** `(start, 1)` cannot exist under *any*
> topology. See the theorem in §1.7.1: the start node can never be a redirecting node, so the only
> event that ever emits a start-tagged entry is the single initial forward pass. **Every** transcript
> therefore holds exactly one start-tagged entry, at turn 0, and every other entry it will ever hold
> is R.N.-tagged.
>
> Do **not** confuse this with "the root never re-runs" — the root *can* be re-entered, by a redirect
> `j ⇢ root`. That entry is tagged `j`. Re-entry of the root and a turn-1 start tag are unrelated
> events, and only the first is possible.
>
> **Implementation trap:** do not derive `turn_no` from a global "which lap are we on" counter, and
> do not pad a transcript so every tag has an entry at every turn. The sort key is a total order over
> *whatever entries exist*; missing combinations are simply absent, and the ordering stays
> well-defined without them.
>
> Keep the two axes distinct: **case (i) writes broadly across the context-index axis** (the same
> R.O. into every slot); **turn numbers advance along the transcript axis inside one slot**. They are
> unrelated.

**Merge policy (`f_comb`).** Pure addition:
- **no overwriting** — straight assignment `(V_ik)_new ≡ V_ij` is explicitly marked *contradicting*
  in the notes: a redirect does not erase what the node already knew along its forward path;
- **no duplicates** — identity is the pair `(turn_no, node_id)`, which is unique by construction
  since `turn_no` counts per `node_id`;
- **right addition only** — the incoming transcript is folded in, never substituted.

**Ordering.** Node ids in this system are digit strings; the start node's id is a letter. Plain
lexicographic ordering would therefore sort the start node **last** (`'0'` = 0x30 < `'a'` = 0x61),
which is backwards. The sort key carries an explicit start-first rank:

```python
sort_key = (turn_no, 0 if node_id == start_node_id else 1, node_id)
```

Chronological by turn; **within each turn the start-node conversation always leads**, then the
redirecting nodes. At turn 0 this also makes the start node globally first.

The typical transcript — note the start tag stops at turn 0, because −1 is never re-entered:

```
turn 0 │ s   start-node forward pass      ← the ONLY start-tagged entry
turn 0 │ 2   redirect from 2, lap 1
turn 0 │ 5   redirect from 5, lap 1
turn 1 │ 2   redirect from 2, lap 2       ← no (turn 1, s) above it; none is needed
turn 2 │ 2   redirect from 2, lap 3
```

A `(turn 1, s)` row is **impossible** — see §1.7.1. Its absence must never block, gate, or renumber
the `("2", 1)` and `("2", 2)` rows.

#### 1.7.1 Theorem — the start node is never a redirecting node

> **Claim.** `start ∉ R.N.`, for every valid graph.
>
> **Proof.** `R.N. ⊆ D.N.` (§1.2): a redirect edge *j* ⇢ *i* requires *j* ∈ descendants(*i*), i.e.
> a redirect source must be a **descendant** of its target. The start node is the unique root of the
> data DAG (D7), hence an ancestor of every node and a descendant of none. So no redirect edge can
> have the start node as its source. ∎

Three consequences, all load-bearing:

1. **There is no such event as "a redirection due to the start node."** The provenance tag is always
   the *redirecting* node, never the target — so even a perfectly legal redirect `j ⇢ root` (which
   *is* structurally valid, since `descendants(root)` is every node) emits an entry tagged **`j`**.
   The root being re-entered and the start tag advancing are **unrelated events, and only the first
   can happen.**

2. **The start tag is pinned to turn 0.** The only event that emits a start-tagged entry is the
   single initial forward pass, so every transcript holds exactly one, at turn 0. In the sort key,
   `start_rank` therefore only ever discriminates *within turn 0* — which is precisely where it is
   needed, to put `(s,0)` ahead of `("2",0)` and `("5",0)`.

3. **The tag namespace is provably disjoint.** Tags are drawn from `{start_id} ∪ R.N. ids`, and the
   theorem makes those two sets disjoint. A tag therefore says unambiguously *"forward pass"* or
   *"which redirect"*, with **no collision possible** — no sentinel or discriminator field is needed
   on `ContextEntry`. Likewise `context_index` never contains the start node, so the baseline slot
   `0` and the R.N. slots `1..K-1` can never alias.

**Start node identity** (**Decision D7**): the start node is the **unique root of the data DAG** —
the node with no incoming data edges. If `start_node_id` is set on the doc it must agree with it.
Zero or 2+ roots is a validation error. This needs no backfill and no frontend change, and matches
§4 where node −1 is the only root.

### 1.8 Scheduling — the redirect stack

The stack does **not** track traversal. It tracks which **redirections are currently open**.

| Step | Rule |
|---|---|
| **Push** | Every time a redirection *j* → *i* happens, push *j*. |
| **Hold** | Execution proceeds through the relevant subgraph in *j*'s context; the entry stays. |
| **Pop** | Pop **only** when — *while still inside the same redirected context the current stack top defines* — execution reaches *j* again. One full lap. |
| **Repeat** | The next redirect pushes again at the new depth. |

```python
# push
on redirect(j → i):
    q.push(j)

# pop — only when the loop actually closes
p = q.top()
if p == current_node and context == context_of(p):
    q.pop()
```

**The condition that matters:** reaching *j* is not sufficient. It must be reached in the **same
redirected context** as the current stack top. The same node arrived at under a different context is
a different event and must not pop the entry.

Invariants the stack gives us:

```
Q = [ q₍ₙ₋₁₎ , … , q₁ , q₀ ]        i > j  ⟺  qᵢ is an ancestor of qⱼ
```

- The **top is always the nearest enclosing redirection** — which is why `Q.top()` is the correct
  context index to read.
- Unrelated nodes never enter the stack; only R.N.s are ever pushed.
- **Multiple stacks can coexist.** Each redirect chain carries its own; there is no single global
  traversal state.

#### 1.8.1 Propagation is by deep copy, never by reference

When a node hands its queue to its children, it hands each child a **deep copy**. No child shares
structure with its parent or with a sibling.

```python
for child in children(n):
    dispatch(child, queue=deepcopy(q))    # never `queue=q`
```

This is not defensive habit — it is what makes §1.8.2 an invariant instead of a coincidence. Under
shared references a `push` performed on one branch would be visible on every other branch that holds
the same object, and a sibling mutating later could **retroactively change a queue a node has
already received**. Deep copy makes the queue effectively immutable along a wave, so "what I was
handed" cannot change under me.

> *Optional optimisation, behaviour-preserving:* since the only mutation points are redirecting
> nodes, copy-on-write is observationally identical to eager deep copy and avoids `O(fan-out × depth)`
> copying. Take it only once the eager version is correct and measured.

#### 1.8.2 Theorem — all queues arriving at a node are identical

> **Claim.** If a node *n*, during **one** execution, receives queues `Q₁ … Q_m` from parents
> `p₁ … p_m`, then `Q₁ = Q₂ = … = Q_m`.

**The premise that does the work — join semantics.** *A node executes only once it has received from
**all** of its upstream nodes.* It does not fire on first arrival. Everything below follows from
this plus the pop rule.

**Lemma 1 — the stack mutates only at redirecting nodes.** `push` happens when a node fires a
redirect; `pop` when a node closes its own loop. Every other node forwards what it received,
unchanged in value (§1.8.1). So the queue can only differ between two nodes if a redirecting node
lies between them. ∎

**Lemma 2 — a nested redirect always closes before the join it is nested inside completes.**

> Let *a* be awaiting its join, and let *b* fire a redirect while *a* is still waiting. For *b*'s
> redirect to be able to reach *a* at all, *b* must be an ancestor of *a*. The queue is now `[…, a, b]`
> for the nodes inside *b*'s lap, and still `[…, a]` for those outside it — so the queue **is**
> momentarily non-uniform across the graph.
>
> But *b* ∈ anc(*a*) means there is a path *b* →\* *a*, so **at least one parent of *a* lies in
> desc(*b*)**. That parent cannot deliver until the wave has passed through *b*. And "the wave passes
> through *b*" is *precisely the pop condition for b*.
>
> Therefore: **`a`'s join cannot complete until `b` has been popped.** The non-uniformity is real but
> strictly transient — it exists only during an interval in which *a* provably cannot fire. ∎

**Proof of the theorem.** Consider the moment *a* fires. By Lemma 2, every redirect opened after *a*
began waiting has already been popped — none can still be on any parent's queue. By Lemma 1, no
other mutation is possible. So every parent's queue is the queue that was current when *a*'s join
began, and they are all equal. ∎

*Base case:* on the initial forward pass every stack is empty, so the claim holds trivially.

**The shape of the argument is worth internalising:** the queue does **not** stay uniform at all
times — it demonstrably does not, mid-lap. What holds is that **it is uniform at every instant a node
is eligible to fire**, because the very event that would resolve the non-uniformity (passing through
the redirecting node) is the same event that unblocks the join. *No matter which redirecting paths
an ancestor chooses in the middle, by the time all upstream have delivered, the queue is back to
what it was.*

**Why the incomparable-sibling case cannot break it either.** A parent incomparable to *j* is **not
in `R.S.(i,j)`** (D1), so it does not re-run this lap and sends **no queue** — *a* reads its stored
value instead (§1.4 case (ii)). A parent that *is* in `R.S.` participated in the same wave. There is
no third possibility.

#### 1.8.3 The one detail Lemma 2 does not settle by itself — **OQ-8**

Lemma 2 proves *b* is popped before *a* fires. It does **not**, on its own, say what happens to a
queue a parent delivered **earlier in the same pending join**, while *b* was still open. A minimal
diamond spanning *b* makes the gap concrete:

```
data edges:  i → p₂ ,  p₂ → a ,  p₂ → b ,  b → p₁ ,  p₁ → a
redirect:    b ⇢ i                       (b ∈ anc(a) via b → p₁ → a)

push b  → queue [b]
  i  runs [b]
  p₂ runs [b] → delivers to a with [b]     ← a still waiting on p₁
  p₂ → b, wave reaches b → POP → queue []
  b  runs, delivers to p₁ with []
  p₁ runs []  → delivers to a with []
a's join completes: it holds [b] from p₂ and [] from p₁.
```

`p₂` is an **ancestor of b** and a parent of `a`; `p₁` is a **descendant of b** and also a parent of
`a`. Lemma 2 held — `b` popped before `a` fired — yet the two *retained* per-edge queues differ.

The theorem is recovered under either of these readings; they differ in implementation, not in
outcome:

| Reading | Consequence |
|---|---|
| **(A) The queue belongs to the wave, not the edge.** Per-edge queues are transport only; at fire time *a* uses the queue current for the wave that unblocked it. | `p₂`'s stale `[b]` is simply not consulted. C1 becomes "use the wave's current queue", and the equality assert applies to *live* deliveries only. |
| **(B) The queue belongs to the edge, and a re-run refreshes it.** `p₂`'s delivery is superseded when the wave that completes the join passes it, so only post-pop values survive. | Requires that `p₂` be re-entered after the pop — i.e. that the lap does not terminate at `b`. Needs the scheduler to guarantee it. |

**Working assumption: (A).** It needs no extra re-execution, matches "the top is the nearest
enclosing redirection" (a property of *now*, not of when a message was sent), and makes the assert in
C3 meaningful rather than routinely false. Recorded as **OQ-8**; it is a Part 3 decision, so Part 1
is unaffected either way.

**Corollaries — these are the reasons the theorem is worth having:**

| # | Consequence |
|---|---|
| **C1** | *n* may take **any one** incoming queue and discard the rest. There is **no merge, no reconciliation, no tie-break** for queues — unlike `f_comb`, which merges *transcripts*. Do not confuse the two. |
| **C2** | `top(Q)` — the context index for case (ii) — is **unambiguous**, whichever parent you ask. Without this theorem a fan-in node would have no well-defined context to read. |
| **C3** | Equality should be **asserted in code as a canary**. A mismatch does not mean "merge them"; it means Lemma 1 or Lemma 2 has been violated (a push leaked across a wave boundary, or a stale queue was retained) — a bug to raise loudly, not paper over. |
| **C4** | Storage is `O(1)` queues per node execution, not `O(in-degree)`. A wide fan-in costs no extra stacks. |

### 1.9 Guardrail — per-pair budgets

Cap the number of times a redirection *j* → *i* may fire, **per (i, j) pair** — one counter per
redirecting edge, so `|R.E.|` rows, not N².

The granularity is deliberate: a global counter would trip on a wide graph with many independent,
healthy cycles; a per-node counter would conflate distinct loops that happen to share a node.
Per-pair matches how the context vector is already indexed.

```
pair (0,2)   edge 2⇢0   fired 3 / limit 3    ← exhausted
pair (0,5)   edge 5⇢0   fired 1 / limit 3    ← unaffected
```

**Decision D5** — on exhaustion, **pause and ask a human**: the run parks at status `waiting`,
exactly like the existing user-review gate, with the exhausted pair recorded on the result. A user
raises the limit (or stops the run) and re-runs against the same `action_graph_result_id`; completed
nodes are skipped by the existing `is_work_done` check, so it resumes cleanly.

> This reuses machinery that already exists (`EventResultStatus.waiting`, the resume-by-result-id
> path). It does mean a run can stall with nobody notified until the §17 inbox (Part 8) lands —
> logged as **RISK-3** in §9.

### 1.10 Cumulative Redirecting Nodes (C.R.N.)

By default a node fires the moment **any single** R.N. redirects into it. A user may instead group
R.N.s — *which need not be related to each other* — into a **C.R.N. group declared on the target
node**. The target then waits for **all** members before proceeding.

- Members that redirect send **information**.
- Members that are **not** redirecting send an explicit **"nothing from me" signal**.

**Why the negative signal is load-bearing:** a barrier that only accepts data deadlocks the first
time a member has nothing to send. Making "I am not redirecting" an explicit message means the group
always completes — every member says something every round, and the target can tell *still waiting*
apart from *nobody else is coming*.

This turns a **race** into a **rendezvous**: without a group the target runs once per redirect,
merging one contribution each time; with a group it runs once per **round**, and `f_comb` folds in
every contribution that round produced.

Groups are **per-target-node**: the same node *b* may belong to one group for *a*, a different group
for *c*, and none elsewhere.

### 1.11 Conditional edges

A dotted edge from a node to an unrelated node. It behaves as a **normal data edge in every
respect** — the only difference is with the caller, which decides **at runtime** whether to fire it.

Conditionality affects **control flow, not context resolution**: the Relevant Subgraph rule, `f_comb`
and the stack invariant are all untouched by it. Node settings stay the same.

Critically, it uses **the same signalling mechanism as C.R.N.**: every node with an outgoing
conditional edge signals the counterpart whether it is transferring a message or not. The engine-wide
rule becomes:

> **An edge always says something, even when what it says is nothing.**

A downstream node never blocks on an edge that quietly declined to fire — which is what keeps both
conditional edges and cumulative groups deadlock-free **without any timeout machinery**. This shared
mechanism is why conditional edges and C.R.N. groups are built together (Part 5).

> **"Conditionality affects control flow, not context resolution" is about `R.S.`, not about
> whether a node runs.** The sentence is easy to over-read. It means a declined edge does not
> reshape the Relevant Subgraph — that stays static (D1, OQ-1). It does **not** mean a node must
> execute on a turn that fed it nothing: under **OQ-7 / OQ-10** a node whose every feeding edge
> declined is *skipped*, which is not the same as *blocked* and so does not touch the
> deadlock-freedom argument above. See §Part 5 · 5.0.

### 1.12 User operations, as vector manipulation

All three are defined as **vector edits, not graph re-traversals** — that is what makes them cheap.

**Rerun (subgraph S).**
1. Empty the vectors for all nodes in *S*.
2. For every node *n* outside *S*, reset `V_nk` to the case-(i) baseline **for every `k` in the
   contaminated index set `K(S)`** defined below.

**The index set is not "every k ∈ S".** Two corrections to the literal reading of §11:

```
J_S  = R.N. ∩ S                                     # the redirecting nodes inside S
K(S) = { k ∈ R.N. :  k ∈ J_S   ∨   ∃ j ∈ J_S . comparable(k, j) }
```

where `comparable(k, j) ⟺ k ∈ ancestors(j) ∪ descendants(j)` — the **same relation as D1**.

| Correction | Why |
|---|---|
| **`k` must be a redirecting node.** | Only R.N.s index a slot at all (§1.13: `K = \|R.N.\| + 1`). A non-redirecting node in *S* indexes nothing, so "every `k ∈ S`" would range over columns that do not exist. |
| **Also every R.N. *related to* an R.N. in *S*, even if it lies outside *S*.** | Wiping *S* invalidates more than *S*'s own columns. |

**Why relatedness is the right closure — and why it stops there.** By the stack-nesting invariant
(§1.8: entries are ordered by ancestry), **two redirects can only be open simultaneously if their
sources are comparable.** So a column `k` can contain state produced during a lap that also involved
`j ∈ J_S` **exactly when `k` and `j` are comparable**. That makes relatedness both:

- **necessary** — an R.N. `k` incomparable to every `j ∈ J_S` can never have been nested with one, so
  its column is independent of *S*'s redirect activity and must be left alone (wiping it would
  destroy valid, unrelated history); and
- **sufficient** — no further transitive closure is needed, because contamination reaches a column
  only through co-occurrence on a stack, and co-occurrence requires comparability.

Note `k ∈ J_S` is formally subsumed by the second clause (`k` is comparable to itself), but both are
stated because they are separate cases to a reader.

"Reset to the baseline" means "pretend no redirection ever happened into this region" — coherent
precisely because case (i) made the baseline uniform across all slots (§1.4). Cost is
O(affected slots) — **no node is re-executed to restore a consistent state.**

**The reset target is `n`'s own baseline (D8).**

```python
V_nk := V_n0        for every n ∉ S, for every k ∈ K(S)
```

§11's phrase "set their `V_nk` equal to `V_s` where *s* is the start node" means **the start-tagged
dimension**, not the start node's own vector — for node *n* that dimension is `V_n0` (§1.7.1). Read
literally it would write one node's result into every other node's slot; the justification given
("`V_s` is exactly the uniform case-(i) baseline") is what settles it, since case (i) is precisely
what makes every slot of *n* equal.

> **The source of the reset is provably never a target of it.** `K(S)` ranges only over redirecting
> nodes, and `start ∉ R.N.` (§1.7.1), so **index 0 can never be in `K(S)`**. `V_n0` is therefore
> untouched by any rerun — which makes the operation **well-defined** (the value it reads always
> exists and is never mid-overwrite) and **idempotent** (running Rerun(S) twice yields the same
> state). Neither property would hold if the baseline could itself be reset.

**Rerun downstream.** Identical, with *S* replaced by *S* \ {root}: keep the root's own result,
discard everything it fed. `K(S)` is recomputed from the reduced *S* — dropping the root may drop an
R.N. from `J_S` and therefore shrink the reset set.

> **Part 1 already provides everything this needs.** `ancestors`, `descendants` and
> `redirecting_nodes` are precomputed on `GraphTopology` (§6.2) for D1's `R.S.`; `K(S)` is a pure
> set expression over them. No new traversal, and no per-rerun graph walk.

**Continue run.** The user may chat only with `V_i0` of any node *i*. **No state change, no vector
change.** Index 0 is the baseline dimension, so the user always talks to the un-redirected view.
User interaction reads one dimension; execution writes the others — **they can never race for the
same cell.**

### 1.13 Optimisations

**Collapse the width.** A slot indexed by node *i* is only ever read when *i* redirects. Nodes that
never redirect never index anything, so the real width is:

```
(R.O.)^N  →  (R.O.)^K        K = |R.N.| + 1
```

The `+1` is the baseline, stored separately. On the worked graph N = 6 collapses to K = 3. Storage
stops scaling with graph size and starts scaling with **how cyclic the graph actually is** — and an
acyclic graph falls back to **K = 1, which is exactly the old DAG behaviour**. That property is the
backbone of our backward-compatibility strategy.

**Precompute the redirector sets.** Ancestral R.N.s and descendant R.N.s are static properties of the
graph; resolve once at build time rather than rediscovering on every redirect. With Decision D1, the
Relevant Subgraph is static too and joins this precompute.

---

## 2. Understanding — Track B, multiplayer

### 2.1 The distinction everything turns on

| | |
|---|---|
| **WORKFLOW** | The architecture of the graph — nodes, edges, node settings. The blueprint. |
| **WORKFLOW RUN** | The runtime of a workflow. One execution. |

A workflow has many runs; a run belongs to exactly one workflow because it inherits that exact
architecture. **A run is not a copy you can edit — it is an instance of an architecture.** Change the
workflow and future runs differ; past runs do not.

Sharing the blueprint and sharing an execution are therefore **two different acts with two different
permission models**.

> This distinction already exists in the code: `ActionGraphDoc` (blueprint) vs `ActionGraphResultDoc`
> (run), where the run carries a **frozen deep copy** of the graph in `result`. Past runs genuinely do
> not change when the workflow does. The foundation is sound.

### 2.2 Sharing a workflow — two modes

**Deep copy.** Produces a completely new workflow. Everyone shared with becomes an **independent
admin of their own copy**. Nobody can see anyone else's runs unless they share them via the workflow
run mechanism. There is **no reconciliation path back**.

**Reference.** One workflow, one shared surface. Everyone with admin rights may change anything;
admins choose the access rights for **stakeholders**.

> Deep copy trades collaboration for autonomy; reference trades autonomy for collaboration.

### 2.3 Stakeholder rights

A stakeholder is **not a diminished admin** — they are **scoped to a region of the graph**, and the
scope is what an admin grants.

| Scope | May touch |
|---|---|
| **Only read** | Look, not touch. |
| **Read / modify a node** | A node or group of nodes, including its **outward edges** and **node settings**. |
| **Read / modify a subgraph** | Node settings and the edges **inside** the subgraph. |

Scope grows from a point, to a region, to a branch — but **never** crosses into "change the
architecture anywhere", which stays admin-only.

#### 2.3.1 Run visibility — asymmetric, and the asymmetry is the point

> **Admins may view *anybody's* runs** — any admin's, any stakeholder's. They opt in by adding that
> person to the relevant list; the direction is **admin-as-viewer**, watching someone else's runs.
> It is not the admin granting viewing rights to a third party.
>
> **Stakeholders have no such power.** A stakeholder sees a run **only** by one of two routes:
>
> 1. it was **shared** with them — automatically (workflow configured to share with members) or
>    explicitly (the launcher shared it); or
> 2. they are **involved in the user-action part** — listed as a permitted responder on a node the
>    run reaches (§2.4).

**Stated as a negative, because it is the easy bug:** being a stakeholder does **not**, by itself,
confer visibility into runs of the workflow. Neither does holding read/modify scope over a node —
scope governs the **blueprint**, not the **runs**. A stakeholder with modify rights over half the
graph still sees zero runs until one is shared with them or they are named on a responder list.

This falls straight out of §2.1: the workflow and its runs are different objects with different
permission models. Rights over the architecture do not propagate to executions.

```python
def can_view_run(u, run, w) -> bool:
    if run.launched_by == u:                      return True   # your own run
    if effective_role(u, w) is ADMIN:                           # §2.3.1 — admins may watch anyone
        return u in watchers_of(w) or is_owner(u, w)
    if effective_role(u, w) is STAKEHOLDER:
        return (shared_with(run, u)                # route 1 — automatic or explicit share
                or u in responders_on_any_node_reached_by(run))   # route 2 — user-action involvement
    return shared_with(run, u) and VIEW_ONLY       # outsider — §2.5
```

### 2.4 Running a shared workflow

Nodes that ask a human something — user review, user input, user action — carry a **list of users who
may answer**. Two conditions govern list membership:

1. The listed user ∈ (admins + stakeholders). If not already, an admin can add them.
2. That user must hold **read/modify access for that particular node**.

Run rights go to **both** — stakeholders and admins alike may launch the workflow.

The **inbox** is pinged by **list membership, not by who launched the run**. So a stakeholder listed
on any node will see **all runs of the shared workflow that reach that node**.

> **The consequence worth naming:** being on a user list is not just a permission, it is a
> **subscription**. Adding someone to a node's user-actions list silently grants them visibility into
> every future run touching that node. That should be visible in the UI at the moment an admin makes
> the change.

**Back requests.** Scope is negotiable in one direction: a stakeholder may ask an admin for more —
more nodes, a bigger subgraph — and the admin decides.

### 2.5 Sharing a workflow run

| Mode | Behaviour |
|---|---|
| **Automatic** | When the workflow is configured to share with admins/stakeholders, its runs are shared automatically. |
| **Explicit** | Anyone may share a run **they launched** with anyone else. |

For the explicit case, what the recipient can do depends on one test:

- recipient **∉** (admins + stakeholders) → **view only**, that one run, no interaction in any format;
- recipient **∈** (admins + stakeholders) → may act per their workflow rights.

> **Sharing a run never grants workflow rights. The run is a window, not a door.**

---

## 3. Gap analysis — where the code stands today

### 3.1 Track A gaps

| Design requirement | Today | Gap |
|---|---|---|
| Typed edges (data / redirect / conditional) | `graph: Dict[str, List[str]]`, untyped | **No edge grammar at all** |
| Context vector per node | `output: Dict[node_id, ActionDoc]` — one slot | **No second dimension** |
| Node may be re-entered | `is_work_done` = "key present in `output`" — monotonic, irreversible | **Re-entry is structurally impossible** |
| Memoised traversal | 4 BFS loops with no visited set | **Exponential re-visits on a plain DAG** (§6.5) — one appends to a string, one issues a DB call per visit. Not a cycle risk under D2, since `graph` stays acyclic |
| Cycle detection | none anywhere (`grep -i acyclic\|cycle` → nothing) | Kahn's in `coffman_graham` **silently drops** cycle nodes and reports `success` |
| Redirect stack | none | — |
| Per-pair budgets | none | — |
| f_comb / tagged transcript | `collate_action_results` concatenates blocks; `ActionResult` is `{input, output}` with **no provenance tag and no turn number** | Needs tag + turn |
| Signalling ("an edge always says something") | none | — |
| C.R.N. groups | none | — |
| Rerun as vector edit | `empty_downstream_nodes` pops keys off `output` | Needs redefining |
| Continue-run on baseline | `is_continue_run_graph` re-runs a **subgraph** | Different concept, same name — **naming collision to resolve** |
| Precomputed R.N. sets | none | — |

**The four unbounded BFS traversals** (each will hang on a cycle):
- `ActionGraph.empty_downstream_nodes` — `to_visit` list, no visited set
- `ActionGraph.get_subgraph_map` — `deque`, no visited set
- `ActionGroup.build_node_group_hierarchy_prompt` — records `visited_nodes`, **never consults it**
- `ActionGroup.build_node_group_profile_prompt` — same

### 3.2 Track B gaps

| Design requirement | Today | Gap |
|---|---|---|
| Workflow vs run distinction | `ActionGraphDoc` vs `ActionGraphResultDoc` with a frozen copy | ✅ **already correct** |
| Deep-copy share | `UserShareData.clone_action_graph` — clones the graph **and every referenced action**, remaps `nodes` + `node_details[*].data.actionId`, resets `shared_with`/`is_published` | ✅ **already exactly §15's deep copy** |
| Reference share | `shared_with: List[SpaceId \| str]` (str = email) | **Binary — shared or not. No roles, no scopes.** |
| Admin / stakeholder roles | Space roles only: `owner`/`admin`/`member` via `SpaceMemberDoc`; `ADMIN_ROLES = {owner, admin}` | **No per-workflow role** |
| Scoped rights (node / subgraph) | none | — |
| Per-node responder lists | `user_review_required` / `user_input_required` are **booleans on `NodeData`** — no user list | **Anyone with access can answer** |
| Inbox | **nothing** — no notification module exists anywhere in `src/autobots/` | **Build from scratch** |
| Run sharing, automatic | `event_result_sharing.build_owned_or_shared_finds` — owner OR `shared_with` email OR `SpaceId` | Partial; no inheritance from workflow config |
| Run sharing, explicit | `EventResultUpdate.shared_with` | Present, but **no outsider view-only tier** |
| Admin watch-list (view anyone's runs) | none | **New.** Today `SpaceActionGraphs` gives space admins graph access but there is no run-watch concept |
| Stakeholder run visibility is *restricted* | `build_owned_or_shared_finds` returns owner-OR-`shared_with` for everyone alike | Needs the §2.3.1 split — a stakeholder must **not** see runs merely by being a member |
| Back requests | none | — |
| Revocation | none | — |

**An important clarification about `require_permission`.** It looks like an ACL but is not — it is a
**subscription/product entitlement gate** (`Product.PLATFORM` allows everything; `Product.KIWICODE`
matches against a hardcoded `PRODUCT_PERMISSIONS` list). It answers *"does this account's plan include
Workflows:Execute?"*, **not** *"may this user touch this node?"* Track B needs a genuinely new
authorization layer; `require_permission` stays as the outer plan gate and is not extended.

**What `SpaceActionGraphs` does today** matters for Decision D4: any space owner/admin already gets
full read/update/delete/search over **every** graph in the space, with no `user_id` filter. Our role
model must preserve that.

---

## 4. Decisions locked

| # | Question | Decision |
|---|---|---|
| **D1** | "*k* is related to *j*" in the Relevant Subgraph | **Static — related = comparable to *j*** (ancestor **or** descendant). `R.S.(i,j) = (subgraph(i) ∩ ancestors(j)) ∪ descendants(j)`, endpoints inclusive. Incomparable branches off *i* are excluded. Precomputable at build time. Diverges from the §6/§7 diagram (includes node 5) — deliberate, isolated to one function, see **OQ-1**. |
| **D2** | Edge storage | **Additive fields.** `graph` keeps its exact current meaning (the forward/data adjacency). Add optional `redirect_edges` and `conditional_edges`. Zero migration; empty ⇒ K=1 ⇒ byte-identical current behaviour. |
| **D3** ⚠️ | `f_comb` | Entry = `(input, output, node_id, turn_no)`. Tag = start node id on the forward pass, redirecting node id on each redirect, **and the operated node for a user operation** (Part 4.5). Pure addition — no overwrite, no duplicates (identity = `(turn_no, node_id)`). Sort key = `(turn_no, start_rank, node_id)`, start-first **within each turn**. <br><br>⚠️ **"`turn_no` counts per `node_id` from 0" was true through Part 4 and is not true now.** Part 4.5 made numbering **run-wide and monotonic**: `next_user_turn` is the highest turn recorded anywhere plus one, and `next_turn`'s seed became a *floor* so a redirect steps over a number a user operation took. Consequences: a redirect's first turn is **1**, not 0, because the forward pass owns 0; columns are **sparse**; and a number can **repeat across a run's life**, because it is derived from what is *recorded* and a rerun erases records. Identity stays safe — each node's slot is internally unique — but "turn N" does not name a moment across the graph. Six tests that asserted `turns == list(range(n))` now assert uniqueness and ordering. See `AUT-1580_Part4.5_Work_Summary.md` §7. |
| **D4** | Workflow roles vs Spaces | **Space owner/admin ⇒ implicit workflow admin** (preserves `SpaceActionGraphs`). Explicit per-workflow admin/stakeholder grants layer on top and may name users outside the space. Purely additive. |
| **D5** | Redirect-budget exhaustion | **Pause and ask a human.** Status `waiting`, exhausted pair recorded on the run, resume via the existing same-`result_id` path. |
| **D6** | Persistence cost | **Its own part, before the cyclic scheduler** (Part 2). Per-node/per-slot incremental writes; benefits existing DAG runs immediately. |
| **D7** | Start-node identity | **The unique root of the data DAG** (no incoming data edges). If `start_node_id` is set it must agree. 0 or 2+ roots ⇒ validation error. No backfill, no FE change. |
| **D9** | How an edge decides whether to fire | **The node's own action decides**, via a system prompt applied at call time, returning `{text, targets, reason}`. The graph classifies each chosen target — the model never says which *kind* of edge it is. Parse failure falls back to one cheap-model repair, then to not routing (redirect) or to an explicit negative signal (conditional). |
| **D10** | Where the routing prompt lives | **System prompt, at call time only.** Never written into the stored `ActionDoc`, never present in the recorded `input`. The transcript stays the user's content, and reruns of a stored action are unaffected. |
| **D11** | Who decides for a loop node | **The collated result, once.** Iterations run with the normal prompt; one decider call follows collation. Matches Part 1's rule that one node execution is one entry. |
| **D8** | Rerun reset target | **`V_nk := V_n0`** — `n`'s own baseline dimension, not the start node's vector. Reset set is `K(S)` (§1.12): R.N.s in *S*, plus R.N.s outside *S* comparable to one inside. Index 0 ∉ `K(S)` since `start ∉ R.N.`, so the baseline is never a reset target — making the op well-defined and idempotent. |

---

## 5. The phased plan

Two tracks. Track A is strictly ordered — each part depends on the one before. Track B is
independent of Track A and may be interleaved or run in parallel by another pair of hands.

```
TRACK A — architecture                          TRACK B — multiplayer
─────────────────────────────                   ──────────────────────────────
Part 1  Foundation: edge grammar,        DONE   Part 7  Workflow membership
        topology, context-vector model                  + scoped rights

Part 2  Persistence rework               DONE   Part 8  Responder lists, inbox,
                                                        run sharing tiers
Part 3  Cyclic scheduler: redirect stack,DONE
        case (i)/(ii), f_comb, budgets

Part 3.5 Edge identity + lap numbering   ← NEXT
        (revamp; unblocks 4 and 5)

Part 4  Relevant-Subgraph slot propagation
        + the redirect message reaches its target

Part 5  Runtime routing: the model decides
        which edges fire  (+ C.R.N.)

Part 6  User operations as vector manipulation
```

### Part 1 — Foundation *(this branch)*

**Goal:** every model, every invariant and every safety fix that cyclic execution needs, with
**zero behavioural change** to existing graphs. Nothing executes a cycle yet.

- Edge grammar: `EdgeType`, `EdgeSpec`, `redirect_edges`, `conditional_edges` (D2).
- `GraphTopology` — the single source of truth for graph structure: R.N. set, `K`, context-index map,
  ancestors/descendants, precomputed `R.S.(i,j)` (D1), start-node resolution (D7).
- Graph validation on create/update — today there is **none**.
- Context-vector storage model, sparse, with `output` retained as the materialised **baseline mirror**
  so every existing reader keeps working untouched.
- Memoisation of the four unmemoised BFS traversals (a live DAG bug, not cycle-safety — §6.5).
- `coffman_graham` made to **detect and report** a data-edge cycle instead of silently dropping nodes.

Full spec in §6.

### Part 2 — Persistence rework

**Goal:** stop `update_action_graph_result` from doing a full S3 read + full-document write + Mongo
update + Redis bump **once per node and once per loop iteration**. Cycles multiply node executions
and the context vector multiplies document size, so this must land before the scheduler.

- Per-node / per-slot incremental writes instead of whole-document rewrites.
- Drop the read-before-write (the engine already holds the doc in memory).
- Batch flushes at layer/lap boundaries; coalesce the per-iteration writes inside loop nodes.
- Keep the S3-primary / Mongo-lite split and the read-fallback contract intact.
- Benefits every existing DAG run immediately — this part is independently shippable and measurable.

**Exit criteria:** a benchmark graph shows a large reduction in S3 round trips per run with byte-identical
final documents.

### Part 3 — Cyclic scheduler

> **Landed ahead of this part** (see `~/.claude/plans/snappy-riding-unicorn.md`): the ownership
> split is settled and enforced. `result.output[node]` is the **executable** plus a permanent
> baseline mirror; `node_vectors[node]` is the context, written **only when `K > 1`** so a DAG run
> stays byte-identical. All node-result writes now go through
> `node_result_writer.record_node_result` / `forget_node` / `forget_all`, and
> `tests/graphs/test_baseline_mirror.py` fails the build if a direct `result.output[...] =` is
> reintroduced anywhere under `action_graph/`. `context_vector.upsert_entry` was added because a
> loop node reports progressively and `append_entry` would have turned one execution into N
> entries. Three dead functions were deleted (`run_action_with_text_split_and_loop`,
> `run_action_with_file_split_and_loop`, `run_action_with_loop_synchronously`).
>
> So Part 3 supplies a real `NodeRunContext` (tag, turn_no, target_slots) per execution instead of
> `NodeRunContext.for_dag(node)`; it does not need to touch a write site.

**Goal:** a cycle actually executes.

- Replace the layer-walk (`for layer in coffman_graham(...)`) with a **frontier/work-queue scheduler**
  that can re-enter nodes. Data edges still give the forward order; redirect edges re-arm nodes.
- **Redirect stack** with push-on-redirect / pop-on-loop-closure, including the same-context
  condition. Support **multiple coexisting stacks**, one per redirect chain (§1.8).
- **Queue propagation by deep copy, never by reference** (§1.8.1) — the invariant §1.8.2 rests on.
- Fan-in takes **any one** incoming queue (C1) and **asserts the others are equal** (C3). The assert
  is a canary for a broken wave boundary, not a merge point — never reconcile diverging queues.
- **Case (i) / case (ii)** read-write asymmetry (§1.4).
- **`f_comb` within a slot** (D3) — tagged entries, per-`node_id` turn counters, additive merge,
  start-first ordering.
- **Per-(i,j) budgets** with the **pause-and-ask** exhaustion path (D5).
- `is_work_done` generalised from "key present in `output`" to a per-slot, re-armable predicate.

Interim semantics: writes land in **slot k only**; propagation across the turn's full slot set
arrives in Part 4 (§Part 4).
That is already correct for the simplest cycles (a single node re-entered) and keeps this part
reviewable.

### Part 3.5 — Edge identity and lap numbering *(revamp — do this first)*

**Goal:** make an edge, not just a node, a first-class thing the scheduler can name. Parts 4 and 5
both need this, and Part 3 shipped without it.

Part 3 assumed a redirecting node has **one** outgoing redirect edge. Under Part 5 a router node
with three candidate targets is the normal shape, so the assumption stops holding.

- **`RedirectFrame` carries the redirect target.** Today it holds `(node, context_index, turn_no)`
  — who fired, which slot, which lap — but not *which edge*. Without the target there is no way to
  look up `R.S.(i, j)` at the node, which is what Part 4 needs and what a precise upstream check
  would need.
- **Lap numbers become scheduler state, not a count of stored entries.** `_laps_opened` counts
  entries already written to the document. A lap that has been *opened but not yet run* has written
  nothing, so with two edges leaving one node the second read is stale and two laps take the same
  `(turn_no, tag)`. They then land in the same cell and one overwrites the other — measured: six
  laps fired, four recorded.
- **Per-`(edge, lap)` decision state.** The first state in the system keyed by edge rather than by
  node — because one node runs once but can answer differently for each edge leaving it. Part 5
  needs it so `to_input` can leave a declined parent out; see D9 and OQ-9.
- **A precise upstream check becomes possible** — every parent in `R.S.` should hold `(tag, turn)`
  before the child runs. Today the ordering guarantees it and the check would be dead code, so it
  ships as a **canary**, not a gate. It becomes load-bearing the moment anything inside a lap runs
  concurrently.

> **Note on one slot per R.N., not per edge.** Two redirect edges leaving the same node share that
> node's column, because the context vector is indexed by the redirecting node (§1.3). That stays.
> What changes is that their *laps* must be numbered distinctly inside the shared column.

### Part 4 — Relevant-Subgraph slot propagation

**Goal:** a redirect's result reaches the right dimensions of the right nodes.

> ### ⚠️ Corrected 2026-09-02 — this section previously said the opposite
>
> It read: *"push `(V_ij)_new` into `V_kj` for all `k ∈ R.S.(i,j)` — the update walks **down a
> column** (context index *j*), not across a row. Updating a row would collapse the dimensions back
> into DAG behaviour."*
>
> **That is wrong, and it was implemented before it was caught.** Two errors in one sentence:
>
> - **Wrong axis.** It copies one node's output into *other nodes'* cells. A node three hops
>   downstream then holds its grandparent's output, which it has no business seeing — it should
>   only ever know what its own parent handed it. A node's row holds **its own work**; it learns
>   about its parents by *reading their slots*.
> - **Wrong warning.** "Updating a row would collapse the dimensions" is only true of writing the
>   same value into **every** slot — which is case (i), and is meant to collapse them. Writing into
>   the **subset** of slots `R.S.` picks out collapses nothing.
>
> The corrected rule is below. Same relationship to the source material as OQ-1's diagram: the
> prose was wrong, not the design.

#### The rule — which cells a turn writes

When redirect *j* ⇢ *i* fires and opens turn *t*:

- The **dimensions affected** are the context indices of the **R.N.s that lie inside `R.S.(i,j)`**:

  ```
  slots(i, j) = { context_index(r) : r ∈ R.N. ∩ R.S.(i, j) }
  ```

- **Every node that executes during turn *t* writes its own result into all of those slots**,
  tagged `(j, t)`.
- **No node ever writes into another node's slot.** This is a **row** update — one node, several of
  its own dimensions.

Note the write set is a property of the **turn**, not of the node: every participant writes the
same set of slots. It is *not* "the slots this node happens to care about".

**Why those slots.** A slot index *is* a redirecting node. If R.N. *r* lies inside the region this
redirect disturbs, then what happens in the turn is part of what *r*'s dimension should see — *r*
is standing in it. An R.N. on an incomparable branch is genuinely untouched, so its slot stays put.

`GraphTopology.ancestor_rns` and `descendant_rns` were precomputed in Part 1 for exactly this
("precompute the ancestral R.N.s / descendant R.N.s") and have **no other consumer**.

**Worked example** — §1.6's graph, `2 ⇢ 0`. `R.S.(0,2) = {0,1,2,3,4,5}`, and the R.N.s inside it
are `{2, 5}`, so the write set is **slots 1 and 2**. Six nodes each write their own result into
both — twelve cells, every one holding that node's own work. On `5 ⇢ 0`,
`R.S.(0,5) = {0,2,3,4,5}` still contains both R.N.s, so again slots 1 and 2. A graph where each
redirect's `R.S.` contains only its own R.N. cannot tell this rule from "write the current slot",
so **do not use one to test it**.

**Slot ≠ tag.** Slot 2 is node 5's *dimension*, but an entry landing there during a 2-loop is
tagged **"2"** — the tag records what *caused* the execution. 5's dimension legitimately fills with
2-tagged entries, which is exactly how it knows what happened while it was not looping.

**Turn numbering.** One turn per redirect firing, recorded in every affected dimension under the
same `(tag=j, turn=t)` identity — *not* a counter per slot. Two counters would give the same event
different numbers in different dimensions, and since identity is `(turn, tag)` that would make one
execution look like two.

#### Reading — by role, and they differ

- **The redirect target *i*** reads **its own slot** `V_i,ctx(j)` plus **the input *j* handed back
  on redirection**. It does **not** re-read its data parents: they sit upstream of the loop and
  have nothing new to say. This is §1.4's case (ii) literally — `{ V_ik } + k_input → i`.
- **Every other participating node** reads **its data parents' slots** for that dimension, one hop,
  as it always has.

That asymmetry is the whole of it: read narrow (one dimension, one role-appropriate source), write
broad (every dimension the turn disturbed).

- `ActionGraphInputCreator.to_input` must read the **correct slot** for the current context rather
  than always the baseline, and must re-linearise a tagged transcript into prompt text.
- **The redirect message lands here.** Part 5's per-target text ("go back and fix the tone") is not
  a parent's output, so `to_input` cannot reach it today. It **is** the *k_input* above — what the
  redirector hands the target — so the target's read is its home and its reader. Part 5 without
  Part 4 produces a message with nowhere to be read from — which is why the order holds.
- **Resume must complete an outstanding turn, not open a new one (RISK-12).** A crash mid-turn
  currently leaves some nodes holding a turn their siblings never received, and the retry opens the
  *next* turn instead. Harmless while nothing reads a slot; this is the part where `to_input`
  starts reading them, so a half-recorded turn becomes a gap a node actually sees. Needs the open
  frames to be durable — turn → the edge that opened it — which is a design decision to make
  deliberately rather than bolt on. `tests/graphs/test_crash_mid_turn.py` reproduces it and is
  marked `xfail(strict=True)`, so it flips the build the moment it is fixed.

### Part 4.5 — The user operations become first-class *(unplanned; landed before Part 5)*

**Not in the original plan.** It began as the first hour of Part 5 and became its own part, because
tracing the Part 5 spec turned up a chain of defects in the paths a *user* drives — continue-run,
rerun, chat-with-a-node — rather than the paths the scheduler drives. Full account:
`AUT-1580_Part4.5_Work_Summary.md`.

The five things a reader of this document needs to know:

1. **`output[node]` is the baseline, not the latest.** A re-entry writes the dimensions its turn
   disturbs and leaves `output` alone; per-turn values are read from `node_vectors`. Before this,
   `record_node_result` wrote `output[node]` unconditionally, and `assert_baseline_mirror` had been
   reporting **nine broken nodes on a healthy three-turn run** since Part 4 — invisible because the
   guard is a canary and because every test fake returns the same output on every call.

2. **`K == 1` is no longer byte-identical, deliberately.** A DAG keeps a one-slot vector so the
   acyclic and cyclic paths are one code path. The gate is
   `writes_vectors = bool(topology.start_node_id)` — a start node, not a redirect edge, because
   every entry needs a provenance tag and a graph with no single root has none. Measured cost:
   **1.43x** document and S3 volume; execution order and write counts unchanged.

3. **Turn numbers are run-wide** — see the ⚠️ on **D3**.

4. **The three user operations are now first-class, recorded events** with their own tag and turn.
   §4 of the Part 4.5 summary is the reference table for what writes which slots.

5. **`open_continue_run`** is a fifth run-state field: `<root>|<turn_no>`, the same shape as
   `open_turns` and for the same reason. A human gate used to **cancel** a continue-run rather than
   pause it — its identity lived in local variables, so the next run asked every node about turn 0,
   found them all done, and reported `success` having executed nothing.

**What Part 5 must carry forward:** the replacement for the byte-identity property is *a graph with
no redirect edges and no conditional edges opens no turn, charges no budget, and keeps exactly one
dimension.*

### Part 5 — Runtime routing: the model decides which edges fire

**Goal:** an edge fires because the work says so, not because it exists.

Today both edge kinds fire unconditionally — a redirect is bounded only by its budget, and a
conditional edge is inert structure that the engine treats as a plain data edge. So a cycle always
burns its full budget and never converges early. This part makes the decision real.

#### 5.0 Two questions Part 4 handed here, and they are one job — **OQ-7 + OQ-10**

**Do this in the same change as the decider, not after.** The moment 5.1 makes an edge capable of
declining, a node can be re-armed for a turn nothing fed it. Until then the case cannot arise; from
5.1 onward it arises immediately.

**The rule.** A node whose every feeding edge declined this turn **does not run** — no execution, no
`ContextEntry`, no slot propagation. Re-running it would recompute the same output from the same
inputs: a real model call, billed, and with a nondeterministic model an answer that changes for no
reason.

**Where the gate goes — not in `R.S.`** `R.S.(i, j)` is handed to `frontier.rearm()` at the instant
the redirect *fires*, and edges decline *later*, while the turn is running. The information to
narrow by does not exist when `R.S.` is used, so narrowing it is not merely wrong, it is not
possible. Keep `R.S.` as the arming set and gate at **execution** time, in `run_action_graph_v2`
beside the existing `node_already_done` / `upstream_ready` checks: by then the parents have run and
`edge_decisions` holds their verdicts, so the check has everything it needs.

```python
# sketch only - the shape, not the code
fed_by = ActionGraph.contributing_upstream(doc, node, upstream_nodes, run_ctx=run_ctx)
if upstream_nodes and not fed_by:
    continue          # every parent declined: this node takes no part in the turn
```

Note `contributing_upstream` (Part 4, `action_graph.py`) already computes exactly that set for
input composition. This adds the *readiness* half beside it — see the warning below.

**Three things that will cost time if nobody says them:**

- **This is not §1.11's deadlock case, and §1.11 does not forbid it.** That section rules out a node
  *blocking* forever waiting for a message. This node does not block; it is skipped and the wave
  moves on. The distinction is live-vs-skip, not fire-vs-wait.
- **`assert_upstream_turn_recorded` must be given the parents that actually *ran* this turn**,
  not everyone in `R.S.`, or it reports healthy graphs as broken ordering invariants. **The obvious
  guess at the cause is wrong:** a parent that *declined an outgoing edge* still ran and still
  recorded — declining is a fact about the edge, not the node (Part 3.5 §4). The parent holding no
  entry is one **skipped by the rule above**, because every edge feeding *it* declined. The
  tempting fix is to loosen the check; that trades away the thing built to catch a real concurrency
  bug later for silence on a false alarm. It has been quietly correct since Part 3.5 — do not be
  the change that makes it cry wolf.
- **A skipped node's children are fine, and this was checked.** `upstream_ready` asks only whether
  the node recorded anything *for this context*, and case (i) seeded every slot, so the baseline
  satisfies it. The children run and read the skipped node's stored value — correct case-(ii)
  behaviour, not a hole.

**Do not reopen OQ-1 on the back of this.** It is the opposite case: OQ-1's node has *fresh input
waiting* from the redirect target and would be ignoring it; an OQ-7 node has *nothing new at all*.
Full reasoning in §8's OQ-7 row and `AUT-1580_Part4_Work_Summary.md` §2.

#### 5.1 How the decision is made

The node's own action decides, via a **system prompt applied at call time**. It is never written
into the stored action, and never appears in the recorded `input` — see **D10**.

The injected prompt has four labelled parts:

| Section | Content |
|---|---|
| **Role** | This node may route work onward, and is choosing whether to. |
| **Candidates** | `id`, `label`, `description` for each reachable target. Nothing else — config is the author's IP and it bloats the context. |
| **Rules** | Choose only from the listed ids. Choosing none is a valid and expected answer. |
| **Format** | The JSON contract in 5.2, with one worked example. |

Injected **only** into nodes that actually have an outgoing conditional or redirect edge. Every
other node's contract is untouched.

#### 5.2 The output contract

```json
{
  "text": "the node's real output, exactly as it would have been",
  "targets": { "node_b": "why, and what to do", "node_f": null },
  "reason": "one line, for the audit trail"
}
```

- **No `redirection: true/false` field.** The graph already knows which list each target's edge
  lives in. A boolean the model also supplies is a second source of truth that can disagree with
  the first — the same reason `EdgeSpec` carries no `type` (D2).
- **`targets` is a map, not two parallel arrays.** Parallel `redirect_to` / `redirection_text` lists
  go out of sync the first time a model emits three of one and two of the other.
- **Only `DataBlock.text` is touched.** `urls`, `file_ids`, `files`, `tools`, `metadata` and
  `search_data` pass through untouched, so the contract holds for image, file and tool actions too.

#### 5.3 The parse ladder

```
DataBlock.text → extract JSON blocks → parse            → route
                          ↓ fail
                 one cheap-model repair (capped, timeout) → route
                          ↓ fail
                 leave .text as-is, do not route
```

Reuse `ActionTeam._extract_json_blocks` — it already handles ``` fences and bare objects, which is
most of the real-world failure surface. Lift it to shared code rather than writing a third copy.

**The give-up branch differs by edge kind:**

| Edge | Give up means |
|---|---|
| Redirect | do not loop. Fail-closed: the run ends rather than spinning. |
| Conditional | fire it. A parent whose verdict is unknown is treated as having sent its output, which is exactly today's behaviour — so an unparseable decision degrades to the current engine rather than to something new. |

> **This engine cannot deadlock on a declined edge, and §1.11's framing does not apply to it.**
> §1.11 assumes a node blocks waiting for a message. Here a node's readiness is `is_work_done`,
> which asks whether the parent's **output exists** — and a declining parent still ran and still
> produced output. So the downstream node is never blocked.
>
> The real requirement is one step later, and smaller: **`to_input` must leave a declined parent
> out when it composes the input.** That is the whole job of the per-`(edge, lap)` state, and it
> lands in the same function Part 4 is already rewriting.

Note the deliberate divergence from `ActionTeam._parse_leader_response`, which fails **open** and
loops again. A leader that cannot decide should retry; a cycle that cannot decide should stop.

#### 5.4 What each kind does to the queue

| | On fire | On decline |
|---|---|---|
| **Redirect** | re-arm `R.S.(target, node)` — **not** just the target, or the target's descendants never re-run and its new output goes nowhere | nothing |
| **Conditional** | nothing to queue — every node is already in the frontier from lap 0. It changes **readiness**, not membership | record the negative signal |

#### 5.5 Loop nodes: collate, then decide once

A loop node fans out into N iterations. Those iterations get the **normal** prompt — no routing
instructions — and the decision is made once, on the collated result, by a separate call.

```
200 iterations (clean)  →  collate  →  1 decider call  =  201
```

"Inject into the last iteration" is not available: iterations run under `asyncio.gather` with a
semaphore, so there is no defined last one.

One decision per node execution also matches what Part 1 already settled for the transcript —
*one node execution is one entry, not one per `ActionResult`*. Routing and memory then count the
same events.

**Known limitation:** per-row routing ("send the rows that failed validation to the fixer") is not
expressible. That is inherited, not introduced — the context vector stores one entry per execution,
so there is nowhere to put 200 distinct outcomes.

#### 5.6 C.R.N. groups

Unchanged from §1.10, and built on the same signal.

- Groups declared **on the target node**; rendezvous semantics; the target runs **once per round**
  with `f_comb` folding every contribution that round produced.
- Node settings for group membership; validation that members are R.N.s of that target.
- **No timeout machinery** — the negative signal is what makes this safe.

#### 5.7 Validation, cheap and early

`GraphTopology.validate()` already runs warn-only on the write path. Add: **a node with an outgoing
conditional or redirect edge whose action cannot produce text.** Under this design such a node can
never route, and the run will silently never loop. Catch it while someone is still drawing the graph.

### Part 6 — User operations as vector manipulation

**Goal:** rerun / rerun-downstream / continue-run become O(affected slots) vector edits (§1.12).

- **Rerun(S):** empty vectors for nodes in *S*; for `n ∉ S`, set `V_nk := V_n0` (D8) for every
  `k ∈ K(S)` — the **contaminated index set** of §1.12: R.N.s inside *S*, **plus** R.N.s outside *S*
  that are comparable to one inside. Not "every `k ∈ S`".
- **Rerun downstream:** same with `S \ {root}`; recompute `K(S)` from the reduced *S*.
- **Continue run:** read `V_i0` only; **no state change**. Resolve the naming collision with today's
  `is_continue_run_graph` (which means "re-run a subgraph") — likely rename the existing flag.
- Rewrite `empty_downstream_nodes` in these terms.

### Part 7 — Multiplayer A: membership and scoped rights

**Goal:** the authorization layer §15–§16 needs.

- `WorkflowMembership` collection: `(workflow_id, user_id, role: admin|stakeholder, scope, granted_by)`.
- Scope model: `read_only` | `nodes: [node_id]` | `subgraph: root_node_id`.
- `effective_role(user, workflow)` resolution chain, per **D4**:
  1. `workflow.user_id == user.id` → admin
  2. `space_member.role in ADMIN_ROLES` → admin
  3. explicit `WorkflowMembership` → admin | stakeholder
  4. `shared_with` matches (email or `SpaceId`) → stakeholder(read)
  5. otherwise → none
- Enforcement in `UserActionGraphs` / `SpaceActionGraphs` / the API layer. Architecture edits
  (adding/removing nodes and edges) stay **admin-only**; stakeholder writes are validated against
  their scope.
- Formalise the reference-share vs deep-copy split (deep copy already exists and is correct).
- Back requests (§2.4) as a lightweight request record.

### Part 8 — Multiplayer B: responder lists, inbox, run sharing

**Goal:** §17–§18.

- Per-node **responder lists** on user-review / user-input / user-action nodes, with the two
  membership conditions enforced (§2.4).
- **Inbox** — a new module; nothing like it exists today. Pinged by **list membership, not launcher**.
- Surface the **subscription consequence** in the API response so the UI can warn the admin at grant
  time.
- Run sharing: automatic inheritance from workflow config; explicit launcher sharing; and the
  **outsider view-only tier**, which `event_result_sharing` does not currently have.
- **`can_view_run` (§2.3.1)** — the asymmetric visibility rule. Admins may watch **any** admin's or
  stakeholder's runs (opt-in, admin-as-viewer). Stakeholders see a run **only** via a share or via
  responder-list involvement; **stakeholder status and node scope grant no run visibility at all**.
  Enforce in the run list/get/search paths, not just the UI.
- Also delivers the notification channel that **D5's pause-and-ask** budget gate depends on
  (see RISK-3).

---

## 6. Part 1 — implementation specification

Everything below is additive. **No existing field changes meaning. No data migration.**

### 6.1 New: `action_graph_doc_model.py` additions

```python
class EdgeType(str, Enum):
    data        = "data"         # solid  — normal forward edge (the existing `graph`)
    redirect    = "redirect"     # dashed — back edge j ⇢ i, causes re-entry
    conditional = "conditional"  # dotted — forward edge the caller may decline to fire


class EdgeSpec(BaseModel):
    source: str                       # node id
    target: str                       # node id
    id: str | None = None             # frontend (React Flow) edge id, optional
```

On `ActionGraphCreate` and `ActionGraphDoc`:

```python
redirect_edges:    List[EdgeSpec] = []
conditional_edges: List[EdgeSpec] = []
```

On `ActionGraphUpdate` — **must default to `None`, not `[]`**:

```python
redirect_edges:    Optional[List[EdgeSpec]] = None
conditional_edges: Optional[List[EdgeSpec]] = None
```

> **Why this matters.** `CRUDBase.update_one` builds `$set` from
> `doc_update.model_dump().items()` **skipping only `None`**. A field defaulting to `[]` would be
> written on *every* update and silently wipe a graph's redirect edges whenever the frontend saved an
> unrelated change. Every existing field on `ActionGraphUpdate` (`nodes`, `node_details`, `graph`) is
> already `Optional[...] = None` for exactly this reason.

Because `ActionGraphResultDoc.result` **is** an `ActionGraphDoc`, the frozen per-run copy picks the
new fields up automatically — a run keeps executing against the topology it was launched with.

### 6.2 New file: `action_graph/graph_topology.py`

The single source of truth for structure. Built once per run (and once per validation), never
re-derived ad hoc.

```python
@dataclass(frozen=True)
class GraphTopology:
    nodes:             frozenset[str]
    data_adj:          Mapping[str, tuple[str, ...]]   # forward data edges  (from `graph`)
    data_rev:          Mapping[str, tuple[str, ...]]   # parents
    redirect_edges:    tuple[EdgeSpec, ...]            # j ⇢ i
    conditional:       frozenset[tuple[str, str]]      # (source, target)

    start_node_id:     str                             # D7 — unique data-DAG root
    redirecting_nodes: tuple[str, ...]                 # R.N. set, stably sorted.
                                                       #   INVARIANT (§1.7.1): start_node_id ∉ this set
    context_index:     Mapping[str, int]               # R.N. id -> slot 1..K-1 ; baseline = 0.
                                                       #   Never contains start_node_id, so the tag
                                                       #   namespace {start} ∪ R.N. is disjoint and
                                                       #   slot 0 can never alias an R.N. slot
    K:                 int                             # |R.N.| + 1

    # precomputed (§13 + D1)
    descendants:       Mapping[str, frozenset[str]]    # subgraph(n), inclusive
    ancestors:         Mapping[str, frozenset[str]]    # inclusive
    ancestor_rns:      Mapping[str, frozenset[str]]
    descendant_rns:    Mapping[str, frozenset[str]]
    relevant_subgraph: Mapping[tuple[str, str], frozenset[str]]   # (i, j) -> R.S.(i, j)

    @classmethod
    def build(cls, doc: ActionGraphDoc) -> "GraphTopology": ...

    def validate(self) -> list[ValidationIssue]: ...

    @property
    def is_cyclic(self) -> bool:                       # K > 1
        return self.K > 1
```

```python
# D1 — related to j == comparable to j (ancestor OR descendant).
# descendants[j] needs no clipping: j ∈ descendants[i] ⇒ descendants[j] ⊆ descendants[i].
relevant_subgraph[(i, j)] = (descendants[i] & ancestors[j]) | descendants[j]
```

Computed once per redirect edge, so `|R.E.|` entries — not N².

**The load-bearing invariant:** the **data-edge subgraph must remain acyclic**. Cycles are expressed
*only* through `redirect_edges`. This is what lets `coffman_graham`, `invert_map` and every existing
forward traversal keep working unchanged for the forward pass.

### 6.3 Validation (new — there is none today)

`GraphTopology.validate()` returns structured issues; `UserActionGraphs.create` / `.update` raise
`HTTPException(400, ...)` on any of:

| Rule | Rationale |
|---|---|
| Data edges form a DAG | The forward pass and `coffman_graham` require it |
| Exactly one data-DAG root | D7 — the start node's id is the forward-pass provenance tag |
| `start_node_id`, if set, equals that root | Keeps the advisory field honest |
| Every edge endpoint exists in `nodes` / `node_details` | Today a dangling id is silently ignored |
| For each redirect *j* ⇢ *i*: *j* ∈ `descendants[i]`, *j* ≠ *i* | §1.2 — `R.N. ⊆ D.N.` |
| No redirect edge has the **start node as its source** | §1.7.1 — implied by the rule above, but asserted separately so the error message is intelligible ("the start node cannot redirect"). A redirect *to* the root stays legal. |
| No duplicate edges within a list | Keeps `context_index` and budget counters unambiguous |
| Conditional edges exist in `graph` | §1.11 — a conditional edge *is* a data edge with a flag |

> **Rollout.** Some stored graphs may have 2+ roots or dangling ids. Ship validation in
> **warn-only** mode first (log + a `validation_warnings` field on read), measure how many real
> graphs fail, then flip to enforcing. Recorded as **RISK-1**.

### 6.4 Context-vector model

```python
class ContextEntry(BaseModel):
    """One tagged contribution in a slot's transcript (D3)."""
    input:   Dict[str, Any] | None = None
    output:  Dict[str, Any] | None = None
    node_id: str                       # provenance: start node id, or the redirecting node's id
    turn_no: int                       # counter per (slot, node_id), from 0. INDEPENDENT per tag
                                       # and SPARSE: never derived from a global lap counter,
                                       # never padded so every tag has an entry at every turn.


class ContextSlot(BaseModel):
    """V_ni — an ordered, tagged transcript."""
    entries: List[ContextEntry] = []


class NodeContextVector(BaseModel):
    """V_n. Sparse: only populated slots are stored."""
    slots: Dict[str, ContextSlot] = {}   # key = str(context_index); "0" = baseline
```

On `ActionGraphDoc`:

```python
output:        Dict[str, ActionDoc]                    # UNCHANGED — always the BASELINE view
node_vectors:  Dict[str, NodeContextVector] | None = None   # NEW — full V_n per node
```

**The compatibility contract.** `output[node]` is kept as a materialised mirror of
`node_vectors[node].slots["0"]`. Every existing reader is untouched:

- the UI and `GET /action_graphs_results/{id}`
- `ActionGraphInputCreator.to_input` (reads `action_doc.results` / `.output`)
- `is_work_done`, `collate_action_results`, `empty_downstream_nodes`
- `scrub_ip`, the dashboard ROI queries, the LLM run tools

In a DAG, `K == 1`, `node_vectors` holds exactly slot `"0"`, and behaviour is **byte-identical to
today**. §1.12's "the user may chat only with `V_i0`" then falls out for free — that *is* `output`.

Part 1 ships the models and the mirror-maintenance helpers. Part 3 is what starts writing slots
`1..K-1`.

**Sorting helper** (D3), used by Parts 3–4:

```python
def sort_key(entry: ContextEntry, start_node_id: str) -> tuple[int, int, str]:
    return (entry.turn_no, 0 if entry.node_id == start_node_id else 1, entry.node_id)
```

Digit node ids vs a letter start-node id make the explicit rank mandatory — plain lexicographic
ordering would sort the start node **last**.

### 6.5 Memoising the four unmemoised traversals

> **This is *not* cycle-safety work.** Under D2, redirect edges live in their own field and `graph`
> stays the data-edge adjacency, validated acyclic (§6.3). All four of these traverse `result.graph`
> and nothing else, so **a cycle can never reach them** and no termination guardrail is required.
> The reason to fix them is different, and it is a bug that exists **today, on pure DAGs**.

**The actual defect: no memoisation ⇒ exponential re-visits.** Each is a BFS with no `visited` set,
so a node reachable by *N* distinct paths is processed *N* times. A ladder of *k* diamonds costs
2^*k* visits. Two of them do real work on every visit:

| Location | Per-visit work | Consequence |
|---|---|---|
| `ActionGraph.empty_downstream_nodes` | `output.pop(node)` | Idempotent — wasted work only |
| `ActionGraph.get_subgraph_map` | `subgraph[fst] = children` | Idempotent — wasted work only |
| `ActionGroup.build_node_group_hierarchy_prompt` | **appends to a string** | **Correctness bug** — duplicated lines in the prompt. `visited_nodes` is built and then never consulted, so dedup was clearly intended |
| `ActionGroup.build_node_group_profile_prompt` | **a DB query** (`list_owned_or_shared_with`) | **Exponential DB calls**, plus the same duplication |

Fix: give the first two a `visited: set[str]` guard; make the latter two actually *consult* the
`visited_nodes` they already maintain. Real workflow graphs are small, so this is latent rather than
burning — but it is a one-line fix per site and removes a whole class of surprise.

**Three windows where a cyclic `graph` can still reach this code**, which is why the guard is also
cheap insurance rather than pure waste:

1. Validation ships **warn-only** first (RISK-1), so cyclic graphs can exist during that window.
2. **Frozen result docs bypass validation entirely.** `ActionGraphResultDoc.result` is a deep copy
   taken at launch and stored on S3; `empty_downstream_nodes` runs over *that*. Create/update
   validation never retroactively touches an already-stored run.
3. Nothing re-validates graphs already in the collection.

And `coffman_graham`: when `len(ordered_nodes) != len(graph)`, the input contained a cycle. Today it
**silently drops those nodes and the run reports `success`**. Make it raise a typed error, which
`GraphTopology.validate` surfaces as a proper 400.

### 6.6 Test plan

| Test | Asserts |
|---|---|
| Model round-trip | New fields serialise/deserialise; absent ⇒ defaults |
| `ActionGraphUpdate` partial save | Saving only `name` does **not** clear `redirect_edges` (the `None`-default contract) |
| Topology on the §1.6 worked graph | `R.N. == ("2","5")`, `K == 3`, `context_index == {"2":1,"5":2}`, `start_node_id == "-1"` |
| `R.S.` precompute — **degenerate case** | `R.S.(0,2) == {0,1,5,2,3,4}` (all of subgraph(0)) |
| `R.S.` precompute — **discriminating case** | `R.S.(0,5) == {0,5,2,3,4}` — **node 1 excluded** as incomparable to 5. This is the test that fails under the ancestors-only rule; `R.S.(0,2)` alone would not catch it. |
| `R.S.` includes descendants of *j* | Nodes 3 and 4 ∈ `R.S.(0,2)` and ∈ `R.S.(0,5)` — they receive *j*'s recomputed output |
| Validation rejects | data-edge cycle; redirect to a non-descendant; dangling endpoint; 0 roots; 2+ roots; duplicate edge |
| Legacy graph | A doc with none of the new fields ⇒ `K == 1`, `is_cyclic is False`, `data_rev == invert_map(graph)` |
| **Diamond memoisation** | On a k-diamond ladder, each node is processed **once**; assert visit counts are linear, not 2^k |
| **No duplicated hierarchy lines** | `build_node_group_hierarchy_prompt` emits each relationship once on a diamond graph |
| **One DB call per node** | `build_node_group_profile_prompt` issues exactly one `list_owned_or_shared_with` per distinct node |
| `coffman_graham` | Raises on a cyclic input instead of silently dropping nodes |
| Baseline mirror | `output[n]` and `node_vectors[n].slots["0"]` stay consistent |
| **Sparse turn counters** | A transcript holding `(s,0)`, `("2",0)`, `("2",1)`, `("2",2)` — with **no** `(s,1)` — sorts correctly and is accepted. No padding, no gating on the missing start-tag turn. |
| **Independent tag advance** | Appending a `"2"`-tagged entry advances **only** the `"2"` counter; the `"5"` and start counters are untouched |
| **Start tag pinned to turn 0** | `(start, 1)` is unreachable (§1.7.1). Assert every transcript holds exactly one start-tagged entry, at turn 0 |
| **Queue deep copy** | Mutating a child's queue leaves the parent's and every sibling's byte-identical (§1.8.1) — the test that fails if someone passes a reference |
| **Fan-in queue identity** | On a diamond `i→p1→n`, `i→p2→n` with a redirect putting both `p1`,`p2` in `R.S.`, the queues `n` receives from each are equal (§1.8.2) |
| **Incomparable parent sends nothing** | With `p2 ∉ R.S.(i,j)`, `n` receives exactly **one** live queue and reads `p2`'s stored `V_p2` — not a second, divergent queue |
| **`start ∉ R.N.`** | `context_index` never contains `start_node_id`; the tag namespace `{start} ∪ R.N.` is disjoint |
| Redirect targeting the root | `j ⇢ root` is **valid** and re-enters the root — and the resulting entry is tagged `j`, **not** start |
| Regression | `tests/graphs/` and `integration_tests/graphs/` pass **unchanged** |

### 6.7 Definition of done for Part 1

1. All models, `GraphTopology`, and validation land with tests.
2. `make lint` and `make test` are green.
3. **No behavioural change** on any graph without redirect/conditional edges — proven by the
   existing graph test suites passing untouched.
4. A cyclic graph can be **created, stored, read back, and validated** — and cannot hang a worker —
   even though nothing executes a cycle yet.
5. The frontend contract (two new optional array fields) is documented and handed over.

---

## 7. Suggested branch/PR shape for Part 1

| PR | Contents |
|---|---|
| 1 ✅ | Memoise the four unmemoised traversals + `coffman_graham` cycle detection. Standalone bug fixes on today's DAG behaviour, valuable independently of any cyclic work, no new models. **Done** — `tests/graphs/test_graph_traversal_memoisation.py` (12 tests; 5 verified to fail pre-fix). Also fixed a latent aliasing bug: `build_node_group_profile_prompt` was mutating `result.graph` in place. |
| 2 ✅ | `EdgeType` / `EdgeSpec` / `redirect_edges` / `conditional_edges` + the `Optional[...] = None` update contract + model tests. **Done** — `tests/graphs/test_action_graph_edge_grammar.py` (11 tests; the None-default contract verified to fail under a `[]` default). `EdgeSpec` carries **no** `type` field: the list an edge lives in types it. |
| 3 ✅ | `graph_topology.py` + precomputes + `GraphTopology` tests. **Done** — `tests/graphs/test_graph_topology.py` (30 tests). `build()` never raises (validation ships warn-only, so a failing graph must still run) and `start_node_id` is `None` when ambiguous. `R.S.` verified to reject **both** wrong definitions: ancestors-only fails 4 tests, whole-subgraph fails 2. Added `isolated_node` so an unconnected node is not mistaken for a second root. |
| 4 ✅ | Validation wired into `create`/`update`, **warn-only**. **Done** — `tests/graphs/test_action_graph_validation_wiring.py` (12 tests). Gated by the new `ACTION_GRAPH_VALIDATION_ENFORCED` setting (default `False`), so flipping to reject is config, not code. `update` validates the **merged result**, not the patch, and only pays for the extra read when the patch touches topology. Adds `GET /action_graphs/{id}/validation` so the editor can surface warnings during the warn-only phase. |
| 5 ✅ | Context-vector models + baseline-mirror helpers + tests. **Done** — `tests/graphs/test_context_vector.py` (25 tests). `f_comb` implemented as `merge_slots`: pure addition, identity `(turn_no, node_id)`, existing entry wins. Ordering and no-overwrite both verified to fail under the wrong rule (6 and 1 failures respectively). **Correction to §6.4:** `output[n]` is not a mirror of the baseline slot — an entry carries no name/type/config, so an ActionDoc cannot be reconstructed from one. `output` stays where the ActionDoc lives; the baseline slot is a tagged *projection* of the same execution, kept in step by `baseline_matches_action_doc`. |

Small, independently reviewable, each green on its own.

---

## 8. Open questions still outstanding

These do **not** block Part 1. Each is scoped to the part that first needs it.

| # | Question | Needed by | Current stance |
|---|---|---|---|
| **OQ-1** ✅ | **Closed in Part 4 — static, as written in D1. The diagram is wrong, not the rule** (confirmed with the author). The reason is **fresh input**, not an empty cell: node 5 sits directly downstream of the redirect target 0 on an ordinary always-fires data edge, so when 0 re-runs node 5 has new input waiting. Excluding it means 5 ignores that input and node 2 — which joins on **both** 1 and 5 — combines turn *t* from one parent with turn *t−1* from the other. This is §1.5's own justification ("ancestors of *j* re-run because *i* re-runs and feeds down the loop toward *j*"). <br><br>⚠️ *An earlier draft of this row argued the excluded cell is left permanently **empty**. That was wrong and is corrected here: case (i) seeds every slot, so the cell holds the start-tagged baseline and the node reads a **stale** value — which is the documented, intended case-(ii) behaviour for a node outside `R.S.` (see `stacks_from`). The measurement behind the wrong claim filtered entries by the redirector's tag and so hid the baseline entry.* <br><br>Note also that `relevant_subgraph` already had two live consumers before Part 4 (the scheduler's re-run set and Part 3.5's canary), so narrowing was no longer the isolated one-function change this row assumed. | Part 4 | — |
| **OQ-2** | §5's "Why fill every slot in case (i)?" is left open in the notes. | Part 3 | Implement as written. §1.12's rerun rule only makes sense if the baseline is uniform, which is the justification. |
| **OQ-3** 🔸 | **C.R.N. liveness** — §19: the negative signal removes ordinary deadlock, but what about a member never *reached* at all in a round? | Part 5 | **Decided in Part 4.5, not built.** *Unreached ⇒ implicit negative signal, decided at lap drain, never by timeout.* It is decidable because a lap completes before the next begins (`_QueueItem` sorts `(lap, rank, sequence)`, and a redirect arms `lap+1`), so at the moment the target is popped for lap L+1 every member either spoke during lap L or did not run at all. It then composes with OQ-7/OQ-10: **all signals negative — explicit or implicit — means the target does not run this round**, so "unreached ⇒ negative" cannot cause a spurious re-run. <br><br>"Fail the round" was rejected: Part 5's own OQ-7 gate *manufactures* unreached members (a node skipped because everything feeding it declined), so failing would turn the ordinary shape into a run failure — and §1.10's stated purpose for the negative signal is that the group always completes. <br><br>One hole it leaves, for §5.7: a member structurally unreachable from the target never speaks *ever*, and the group silently degenerates. Validation should catch it at draw time — extend §5.6's "members are R.N.s of that target" to "**reachable** R.N.s". |
| **OQ-4** | **Revocation** — nothing describes withdrawing rights or list membership, or what happens to queued inbox items when scope is withdrawn. | Parts 7–8 | Needs a policy before the inbox ships. |
| **OQ-5** | **Deep-copy divergence** — copies are independent with no reconciliation path, so two teams cannot merge improvements. | Part 7 | Accept as designed; flag in UI so users know the copy is one-way. |
| **OQ-6** | Turn counters are **per node_id per slot**. Confirm they do **not** reset on rerun of a subgraph (§1.12 empties vectors — does the counter restart at 0?). | Part 6 | Assume emptying a vector resets its counters, since the transcript it counted is gone. |
| **OQ-7** | Conditional edges and `R.S.`: if a conditional edge declines to fire, does a node reachable **only** through it still take part in the turn? | **Part 5** | **Runtime — it should not take part.** Nothing new reached it, so re-running recomputes the same output from the same inputs: wasted work, a real model call, and a nondeterministic answer change for no reason. <br><br>**This is *not* the same question as OQ-1**, and an earlier draft wrongly fused them. The two are opposites on what matters: OQ-1's node 5 has **fresh input waiting** from the redirect target and would be ignoring it; an OQ-7 node has **nothing new at all**. "Both narrow a set" is the only thing they share. <br><br>**The gate cannot live in `R.S.`** `R.S.` is consumed by `frontier.rearm()` when the redirect *fires*, at the start of the turn; conditional edges decline *during* it, so the information to narrow by does not exist yet. Keep `R.S.` as the arming set and gate at **execution** time: when a node comes up, if every parent that would have fed it this turn declined, skip it — no run, no entry, no propagation. By then the parents have run, so the check has what it needs. <br><br>That makes this a consequence of **OQ-10** rather than a separate decision, so the two are settled together. Two knock-ons for whoever does: `assert_upstream_turn_recorded`'s parent set must use the same notion or the canary fires on healthy graphs the first time an edge declines; and answering OQ-10 "does not run" introduces a **readiness** dimension Part 3.5 was explicit this engine does not have (§1.11's deadlock framing does not apply to a *declining* edge, but it does apply to a node that never ran). A skipped node's children are unaffected in practice — `upstream_ready` asks only whether the node recorded anything for this context, and the case-(i) baseline satisfies it — so the children run and read the stale value, which is correct. |
| **OQ-9** | Where does the per-`(edge, lap)` verdict live, and how does `to_input` read it? Not a readiness question — a declining parent still ran, so nothing blocks. It is an **input-composition** question: the parent must be excluded from the input it would otherwise contribute to. | Part 3.5 / 4 | Record the verdict per `(edge, lap)`; `to_input` skips declined parents. Storage shape undecided. |
| **OQ-10** | What happens when **every** incoming conditional edge to a node declines — does it run on empty input, or not run at all? | Part 5 | **Not run at all**, and **settled together with OQ-7** — that row is the same question reached from the other side, and one rule must answer both or they can disagree. Running on empty costs a billed model call to recompute the same output from the same inputs. Implementation notes, including where the gate goes and the canary knock-on, are in **§Part 5 · 5.0**. |
| **OQ-8** | §1.8.3 — when a parent delivered a queue *earlier in the same pending join*, while a nested redirect was still open, is that stale per-edge queue consulted at fire time? Reading (A) says the queue belongs to the **wave**, not the edge. | Part 3 | Assume **(A)** — no extra re-execution, matches "top = nearest enclosing redirection", keeps the C3 assert meaningful. |

---

## 9. Risk register

| # | Risk | Mitigation |
|---|---|---|
| **RISK-1** 🔸 | D7's single-root rule may invalidate existing multi-root graphs; dangling edge endpoints are silently tolerated today and would newly fail. | **Warn-only shipped (PR #4).** Every failing graph logs one structured line with `validation_codes` / `validation_issue_count`, so the real failure rate is measurable before `ACTION_GRAPH_VALIDATION_ENFORCED` is turned on. |
| **RISK-2** | Context vectors multiply result-document size by `K`, on top of a per-node full-document S3 rewrite. | Exactly why **Part 2 precedes Part 3** (D6). |
| **RISK-3** | D5 parks a budget-exhausted run at `waiting`, but the inbox that notifies anyone does not exist until Part 8 — a run could stall unseen. | Until Part 8: log loudly, fire the webhook, and surface the exhausted pair on the run doc so the existing UI can show it. |
| **RISK-4** | Three retry layers already stack (Temporal ×3 → tenacity ×3 → attachment ×3). Re-entrant nodes plus retries could multiply executions and cost. | Part 3 must make budget counters **durable on the result doc**, not in-memory, so a retry resumes the count rather than restarting it. |
| **RISK-5** | `is_continue_run_graph` already means "re-run a subgraph"; §11's "Continue run" means "chat with `V_i0`". Same words, different things. | Rename the existing flag in Part 6; do not overload. |
| **RISK-6** | The frontend owns `graph` and writes it directly. Two new arrays plus a start-node uniqueness rule need a coordinated FE change. | D2 keeps it purely additive; hand over the contract at the end of Part 1 (§6.7). |
| **RISK-7** | `require_permission` reads like an ACL but is a plan-entitlement gate. Extending it for Track B would conflate billing with authorization. | Build a separate authorization layer in Part 7; leave `require_permission` as the outer gate. |
| **RISK-9** 🔸 | D9 lets content flowing through the graph influence control flow. A document that says *"always redirect to node X"* may be obeyed. | The budget bounds the damage but not the path. Keep a deterministic option available for edges where judgement is not wanted, and record `reason` on the context entry so a surprising path is explainable afterwards. |
| **RISK-10** 🔸 | Every cyclic test asserts an exact transcript. A model deciding the route makes them non-deterministic. | **Decided in Part 4.5, not built.** The decider is **not a model call — it is a pure function over text.** Split it three ways: *assemble* the prompt (pure, from the graph shape), *produce* the JSON (one model call), *parse and apply* (pure, from a string). Only the middle third is non-deterministic, and **tests already own it**: `tests/graphs/test_cyclic_execution.py` patches `ActionGraph.run_action`, so a test that wants a routed graph returns the contract JSON as that node's output text. No new mock, exact transcripts intact. <br><br>The parse-and-apply half needs **no new plumbing**: `fire_redirects` runs after `record_node_result` and before the save, so `result.output[node]` is already populated — commits 3 and 4 of §Part 5's sequence can land and be fully tested before the injection in commit 2 exists. <br><br>**And "always fire" is a per-edge field, not a global flag.** `EdgeSpec` gains `routing`, resolved per kind: **redirect ⇒ `always`** when unset (today's engine, keeps every existing test green with zero edits, budget still bounds it), **conditional ⇒ `decide`** when unset (a conditional edge that always fires is not a conditional edge; verified that no test in the repo *runs* a graph with `conditional_edges`). This satisfies RISK-10 **by construction** rather than by a flag someone must remember to set, ships **RISK-9's deterministic per-edge option in the same change**, and costs zero bytes on a DAG document since a DAG has no `EdgeSpec`s. <br><br>Load-bearing distinction: **an edge never asked is not an edge whose answer was unparseable.** Only a consulted edge reaches §5.3's give-up branch — otherwise "give up ⇒ redirect does not loop" would silently kill every existing cyclic test, whose fake outputs contain no JSON. |
| **RISK-11** | The parse repair adds a second model call on an already slow path. | One attempt, hard timeout. A flaky model must not turn a 3-lap loop into a stall. |
| **RISK-12** ✅ | **Fixed in Part 4.** `open_turns` records `<node>\|<context_index>\|<turn_no>` → the redirect **target** — the one piece that was missing, since `R.S.(i, j)` is keyed by the pair and without it there is no way to name the nodes that owed the turn. On startup any turn its relevant subgraph has not fully recorded re-arms the nodes still owing it, with the frame reconstructed from the key, and the budget is **not** charged again. The record is pruned to what is genuinely outstanding at the end of a run. `test_crash_mid_turn.py`'s two `xfail(strict=True)` tests XPASSed on the fix, their markers are removed and the characterisation test beside them deleted. *Original text:* **A crash mid-turn abandons that turn instead of finishing it.** The turn counter is durable, but the *open frames* are not — nothing records that a turn was handed out and never completed. On the retry the frontier is rebuilt empty, every node reads as done for the forward pass, and the redirecting node fires again taking the **next** number. Measured on `a→b→c→d` with `d ⇢ a`, crashing during turn 0: attempt 1 leaves `a:[0] b:[0] c:[] d:[]`; the retry produces `a:[0,1,2] b:[0,1,2] c:[1,2] d:[1,2]` — **c and d never receive turn 0**, and the abandoned turn still consumed a budget unit, so a crash permanently costs the user one of their turns. | Fix in **Part 4**, which is where a half-recorded turn starts being *read* rather than merely stored. Persist enough to reconstruct an open frame (turn → the edge that opened it); on startup, for any turn whose `R.S.(i,j)` is not fully recorded, re-arm the missing nodes with that frame rather than opening a new turn, and do not re-charge the budget. **Do not roll the partial turn back** — the node really executed and really cost money, and `merge_slots` is explicitly "pure addition… nothing is ever overwritten and nothing is dropped". Note turn numbers need **not** be dense: identity is exact-match `(turn_no, tag)` and ordering only needs them to sort, so an abandoned turn leaving a hole is harmless. |
| **RISK-8** ✅ | **Fixed.** `ActionGraphUpdate.time_saved_per_file` / `dollar_cost_per_hour` defaulted to `0.1` / `50` rather than `None`, so `CRUDBase.update_one` wrote them on every save and a rename silently reset a user's configured dashboard values. Confirmed an oversight: `ActionUpdate` already used `default=None` for its equivalent pair. | Both now `default=None`; `ActionGraphCreate` still supplies `0.1` / `50` at creation. Covered by `test_partial_update_does_not_reset_dashboard_settings` (verified to fail under the old defaults) and `test_create_still_supplies_the_dashboard_defaults`. |
