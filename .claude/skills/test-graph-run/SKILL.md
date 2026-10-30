---
name: test-graph-run
description: End-to-end test an action graph's async run against the live stack — simple run, resume, rerun, rerun-downstream, continue-run, and cyclic turns. Use when verifying run behaviour changes (AUT-1580 parts), or when a run "worked" and you need to prove what actually happened to each node.
user-invocable: true
---

# Test an action graph's async run, end to end

`make test` proves the engine in-process. This proves it through the real HTTP + Temporal + S3
path, which is where the interesting failures live.

**The whole difficulty is that "it ran" is not observable from the response.** `POST /async_run`
returns immediately with a run document that has not executed yet, and the final document looks
much the same whether a node re-ran or was skipped. You have to watch the run *while it happens*.

---

## 0. Prerequisites

Stack up — see the **`dev-async`** skill, which is the authority:

| Process | Port | Command |
|---|---|---|
| Temporal dev server | 7233 (UI 8233) | `temporal server start-dev --db-filename /tmp/temporal.db` |
| `autobots-runner` | 8005 | `make dev-runner` — wait for `curl localhost:8005/health` → `polling: true` |
| Main API | 8000 | `make dev` |

Redis (6379) must be up. `ENV=local` is required in `.env.local`.

**Token:** `~/.kiwi/tokens.json` → `access_token`. Check `expires_at` before a long session. Never
print it. If it is missing, ask the user to mint one; do not handle their password.

**Shutting down:** `make dev` runs uvicorn with `--workers 3 --reload`, so killing the port holder
is not enough — the reloader respawns it. Kill the tree:
```bash
pkill -f 'uvicorn --loop asyncio src.autobots.main:app'; pkill -9 -f 'src.autobots.main:app'
```
Leave Redis alone if it was up before you started.

---

## 1. The two facts everything else depends on

**The re-execution signals are OPPOSITE.**

| | `len(results)` | reliable signal |
|---|---|---|
| **rerun** | 1 → *gone* → 1. **Delta zero or negative** | the node **leaves** `output` and **re-enters** it |
| **continue-run** | 1 → 2. **Delta positive** | the count **grows** |

A before/after snapshot can distinguish continue-run but **cannot** distinguish a rerun from
nothing happening at all. You must poll.

**Never verify a re-run by comparing `updated_at` or output text.** Those timestamps belong to the
action *definition* and never change per execution; many actions are deterministic and emit
identical text. This has fooled three separate attempts on this project.

---

## 2. The harness

Two things it must get right, both learned the hard way:

- **Seed it with a pre-request baseline.** Starting from an empty `seen` makes every pre-existing
  node look like it just appeared — which reads as a re-execution that never happened.
- **One run-document GET costs ~2.3s** (it reads the whole document from S3), so a serial loop
  samples every ~2.7s however small the sleep. Nodes with no action re-execute inside that window.
  Use staggered concurrent pollers when you need sub-second resolution.

```python
"""Poll a run document and record what actually happened to each node."""
import json, os, time, threading
import httpx

TOK = json.load(open(os.path.expanduser("~/.kiwi/tokens.json")))["access_token"]
H = {"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"}
API = "http://localhost:8000/v1"
GRAPHS  = f"{API}/action_graphs"
RESULTS = f"{API}/action_graphs_results"     # NOT /action_graph_results - that 404s

def fetch(c, rid):
    r = c.get(f"{RESULTS}/{rid}", headers=H)
    return r.json() if r.status_code == 200 else None

def snapshot(doc):
    """node -> (present_in_output, len(results))."""
    out = ((doc or {}).get("result") or {}).get("output") or {}
    return {n: (True, len((d or {}).get("results") or [])) for n, d in out.items()}

def fast_poll(rid, seconds=300, workers=6, stagger=0.4, baseline=None, quiet_for=8.0):
    """Staggered concurrent pollers - effective resolution ~= `stagger`, not fetch latency."""
    samples, lock, stop = [], threading.Lock(), threading.Event()

    def worker(offset):
        time.sleep(offset)
        with httpx.Client(timeout=60.0) as c:
            while not stop.is_set():
                t = time.time()
                doc = fetch(c, rid)
                if doc is not None:
                    with lock:
                        samples.append((t, snapshot(doc), doc))
                time.sleep(0.05)

    for i in range(workers):
        threading.Thread(target=worker, args=(i * stagger,), daemon=True).start()

    start = last_change = time.time()
    seen = dict(baseline) if baseline else {}      # <- the baseline is not optional
    transitions, final = [], None
    while time.time() - start < seconds:
        time.sleep(0.2)
        with lock:
            batch = sorted(samples, key=lambda s: s[0]); samples.clear()
        for ts, snap, doc in batch:
            final = doc
            for n in set(snap) | set(seen):
                if seen.get(n) != snap.get(n):
                    now = snap.get(n)
                    transitions.append((round(ts - start, 1), n,
                                        "gone" if now is None else f"results={now[1]}"))
                    last_change = time.time()
            seen = snap
        if final and final.get("status") in ("success", "error", "waiting", "cancelled") \
           and time.time() - last_change > quiet_for:
            break
    stop.set()
    return transitions, final
```

---

## 3. The run modes, and exactly what to assert

All are `POST {GRAPHS}/{graph_id}/async_run`. **The body must be `{"input": {...}}`** — the
endpoint has two body params (`input` and `webhook`), so FastAPI embeds them and a bare `DataBlock`
gets a 422. Everything else is a **query param**.

| Mode | Query params | Assert |
|---|---|---|
| **Simple run** | *(none)* | every node reaches `results >= 1`; status `success` |
| **Resume** a `waiting` run | `action_graph_result_id` | already-done nodes are **not** re-executed; the blocked node now runs |
| **Rerun (whole graph)** | `action_graph_result_id`, `is_rerun_graph=true` | every node with an action: `gone` → `results=N`. **Delta 0** |
| **Rerun downstream** | `+ action_graph_node_id=<n>` | `<n>` **and** its descendants emptied; upstream untouched |
| **Rerun downstream only** | `+ is_empty_downstream_only=true` | descendants emptied, **`<n>` itself NOT** emptied |
| **Continue run** | `action_graph_result_id`, `action_graph_node_id=<n>`, `is_continue_run_graph=true` | **nothing** emptied; `len(results)` **grows**; `<n>` itself skipped (it is the subtree root) |

```python
# rerun
c.post(f"{GRAPHS}/{gid}/async_run", headers=H,
       params={"action_graph_result_id": rid, "is_rerun_graph": "true"},
       json={"input": {"text": "..."}})

# continue run
c.post(f"{GRAPHS}/{gid}/async_run", headers=H,
       params={"action_graph_result_id": rid, "action_graph_node_id": node,
               "is_continue_run_graph": "true"},
       json={"input": {"text": "..."}})
```

### Reading the transitions

```
rerun:            0.4s  A  gone          <- emptied
                  2.0s  A  results=1     <- refilled     => delta 0, PASS
continue-run:     4.3s  C  results=2     <- grew, never gone => PASS
```

---

## 4. Cyclic graphs (K > 1)

Everything above still applies. Additionally, on the final run document under `result`:

| Field | Expect on a cyclic run |
|---|---|
| `node_vectors` | populated; each node's slot holds **only that node's own** input/output |
| `redirect_budgets` | `"<target>\|<source>"` → `fired` ≤ `limit` |
| `turn_counters` | `"<node>\|<context_index>"` → turns handed out |
| `edge_decisions` | `None` until Part 5 writes verdicts |
| `open_turns` | `None` once every turn completed; non-empty means a turn was abandoned |

Checks worth making:

- **A node holds only its own work.** Every entry in `node_vectors[n]` must be `n`'s output — never
  a parent's or a grandparent's. This is the defect Part 4 shipped first and caught by reading a
  prompt.
- **A turn writes every dimension it disturbed.** On a graph whose `R.S.` contains more than one
  R.N., one turn writes more than one slot. **A graph where each redirect's `R.S.` holds only its
  own R.N. cannot test this** — it collapses to "write the current slot" and passes under the wrong
  rule too.
- **The budget bounds the loop.** `fired` never exceeds `limit`; exhaustion parks the run at
  `waiting`, and raising the limit + re-running the same `result_id` resumes it.
- **Turn numbers are not dense.** A node only runs the turns whose `R.S.` contains it, so `[0,2,4]`
  is correct. Do not assert `list(range(n))`.

### K == 1 must stay byte-identical

The regression check that matters for every AUT-1580 part. Create a **DAG** through the API and
assert all five cyclic fields are `null` — on create, on fetch, after a partial update, and after
every run mode:

```python
for f in ("node_vectors","redirect_budgets","turn_counters","edge_decisions","open_turns"):
    assert doc["result"].get(f) is None, f
```

---

## 5. Gates, which will stop your run before you get to any of this

Many real graphs carry human gates on `node_details[n].data`:

- `user_input_required` — blocks **the node itself**
- `user_review_required` — blocks the reviewed node's **children**, not the node

Either parks the run at `waiting`. **Approving on the graph does not reach an existing run** — the
run carries its own `result.node_details` snapshot and a rerun deliberately skips fetching the
graph. Write the approval to the **run**:

```python
doc = fetch(c, rid)
nd = json.loads(json.dumps(doc["result"]["node_details"]))     # deep copy
nd[node]["data"]["user_input_done"] = True
nd[node]["data"]["user_review_done"] = True
c.put(f"{RESULTS}/", headers=H, params={"id": rid},
      json={"result": {**doc["result"], "node_details": nd}})
```

Then resume with `action_graph_result_id` and **no** rerun flags. That is the resume for every
parked operation — a plain run empties nothing and steps over whatever already finished, and a
parked continue-run is picked up from the document rather than from the request.

`is_rerun_graph=true` with **no node id is not a resume** — it calls `forget_all` and re-executes
the whole graph. The end state can look identical, so check the node's `results` count leaving and
re-entering `output` rather than the final document.

**Only two operations clear `user_input_done` / `user_review_done`:** a rerun from a node (exactly
the nodes it emptied), and a *new* continue-run (the subtree minus its root). A chat, a resumed
continue-run and a rerun with no node id all leave them standing — so after those, a gate you
approved earlier is still approved, and a node's children are not re-blocked.

**Continue-run on a `waiting` run is refused with 405** — deliberately, because such a run has
nodes that never produced output. Do not "fix" it.

**A review can be approved before the node has run**, and it is honoured. Handy for driving a run
to completion in one pass; useless as evidence a human read anything.

---

## 6. Traps

- **Don't conclude from a missing log line.** The rerun/empty logic is duplicated in
  `conn/temporal/action_graph/activity.py` *without* the logging `_run_as_background_task` has.
  Grepping the runner log for `Empty all Nodes` returns nothing even when `forget_all` ran. Check
  the code path.
- **A node with no action (`actionId: ""`) re-executes faster than you can sample.** Its emptying is
  real and persisted but the window is sub-0.4s. Absence of evidence here is not evidence of
  absence — say so rather than reporting a failure.
- **Results prefix is `/v1/action_graphs_results`.** The wrong one 404s, and a harness that treats
  an unreadable document as empty will report "the run has no nodes".
- **Activity failures surface in the runner log, not the API log.**
- **`temporal task-queue describe --task-queue autobots-tasks`** — no pollers listed means the
  runner is down; the API accepts the request and the run sits in the backlog forever.

---

## 7. CRUD, before any of this

If run behaviour looks wrong, rule out the boring cause first. `POST /` → `GET /{id}` → `GET /` →
`GET /search/text` → `GET /{id}/validation` → `PUT /{id}` → `DELETE /{id}`, then `GET` → 404.

Known and out of scope: **`PUT /v1/action_graphs/{id}` returns 500 for a missing id** where `GET`
and `DELETE` correctly return 404.
