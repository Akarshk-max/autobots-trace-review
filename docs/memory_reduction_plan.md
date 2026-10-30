# Plan: Reduce Application Memory Profile

## Context

The application is deployed on ECS with:
- **2 uvicorn workers per task**, 3 desired tasks (6 total workers)
- **4096 CPU / 8192 MB RAM per task** — so ~4GB available per worker
- `ddtrace-run` wrapping adds overhead

At startup, `action_map.py` (76 imports) and `tool_routes.py` (130+ imports) eagerly import **every** action class and tool, which transitively pulls in heavy libraries like pandas, chromadb, numpy, python-docx, python-pptx, pymupdf, and more — all before serving a single request. Each worker duplicates this overhead.

**Estimated baseline per worker**: 400-800MB (mostly from eager transitive imports)
**With 2 workers per task**: 800MB-1.6GB of startup overhead per 8GB task, leaving less headroom for actual request processing

---

## Key Findings

### P0 — Eager imports of heavy libraries at startup

These files are imported at module load time via `action_map.py` and `tool_routes.py`, pulling heavy deps into every worker:

| Library | Est. Memory | Imported via (at startup) |
|---------|------------|---------------------------|
| **pandas** | ~150-300MB | `action_csv_merge.py:11`, `action_api.py:14`, `action_text2text_api.py:9`, `openai_responses.py:13`, `openai_responses_v2.py:13`, `file_converter.py:11`, `base64_encoder_decoder_extended.py:8`, `csv_merge.py:8`, `merge_pdf.py:7`, `action_csv_perquet.py:6`, `action_parquet_inspector.py:16` |
| **chromadb + numpy** | ~200-300MB | `action_multimodal_llm_chat_with_vector_search_openai.py` → `conn/chroma/chroma.py:12` |
| **python-docx + python-pptx** | ~30-50MB | `file_converter.py:12,15` (via `tool_routes.py:82`) |
| **onnxruntime** | ~100-200MB | Transitive via chromadb |

### P1 — PDF Parser OOM under heavy use

`src/autobots/action/action_type/action_text2text/action_text2text_pdf_parser.py`:

1. **O(n²) string concatenation** — `text += page.get_text()` (lines 116, 140) copies the entire accumulated string on every page. A 500-page PDF creates ~500 intermediate string copies.
2. **No temp file cleanup** — `NamedTemporaryFile(delete=False)` at line 92, but the downloaded PDF is **never deleted** after processing. Repeated calls leak disk/memory.
3. **No PDF size limit** — will download and process arbitrarily large PDFs (hundreds of MB).
4. **Sync processing blocks event loop** — `extract_text_from_pdf` (line 112) is synchronous, called directly without `asyncio.to_thread`. Large PDFs block the worker entirely.
5. **pymupdf imported at module level** (line 5) — adds ~100MB to startup memory per worker.

### P2 — Action graph loop memory accumulation (file split + loop OOM)

`src/autobots/action_graph/action_graph/action_graph.py`:

The concurrent loop executor (`run_action_with_loop_concurrently`, line ~2860) has three compounding memory issues:

1. **`pending` dict grows unbounded (line 2904)** — Workers complete out of order. The `pending` dict holds all completed-but-not-yet-sequenced results. If item #0 is slow, items #1 through #9999 all accumulate in `pending` waiting for #0 to finish. The queue has maxsize=22, but `pending` has no limit.

2. **`collated` ActionDoc grows unbounded (lines 2118, 2126)** — `_append_to_collated_action_doc()` does:
   - `collated.output["blocks"].extend(blocks)` — appends all output blocks from every iteration
   - `collated.results.extend(next_result.results)` — appends all result objects
   - `collated.input[f"text_{idx}"]` — stores every input text
   - For 10,000 iterations at ~5-10KB per result = 50-100MB minimum in the collated doc

3. **Full collated doc persisted on every drain (lines 2967-2997)** — `flush_collated()` serializes the *entire* growing collated ActionDoc to MongoDB on each drain. As the doc grows, each DB write gets larger.

**Also in `src/autobots/conn/excel/read_and_split.py`:**
- Line 216: Non-xlsx files load entire file via `pd.read_excel()` — no streaming
- Line 238: `split_excel_into_batches()` calls `list()` on the generator, materializing all batches into memory at once

### P3 — Daytona action OOM during repeated status polling

`src/autobots/action/action_type/action_text2text/action_text2text_daytona.py` and `src/autobots/conn/daytona/daytona_client.py`:

When the Daytona action runs a script and the LLM polls `operation="status"` every few minutes:

1. **Sandbox cache grows unbounded (daytona_client.py:63)** — `_sandbox_cache: dict` caches SDK sandbox objects with no eviction. Each poll calls `_get_sandbox_sync()` which caches the object. Never pruned except on explicit `delete_sandbox`.

2. **`server_sync` downloads entire artifacts into memory (daytona_client.py:624-656)** — `download_file_bytes()` loads entire files into memory. The base64 fallback creates strings ~1.33x file size. A 100MB CSV artifact = 133MB+ in-process memory.

3. **`server_sync` runs on every completed-status poll (lines 718-764)** — Re-downloads files from sandbox each time status is polled if artifacts appear "missing". No flag to skip once reconciled.

4. **`log_tail` compounds across polls (line 659)** — On poll failure: `log_tail = (job.log_tail or "") + "\n[WARN]..."` grows the string each poll, persists to MongoDB, then re-read next poll.

### P4 — OpenAI Responses input list grows unbounded per conversation

`src/autobots/conn/openai/openai_responses/openai_responses.py` (and v2, stream variants):

`responses_req.input` is appended to on every tool-call turn (lines 638-650, 1140-1158). In multi-turn conversations with many tool calls, this list accumulates all previous outputs + retry messages + tool results. 20 tool calls can produce hundreds of input items with large payloads.

### P5 — Ingestion retry queue can grow unbounded

`src/autobots/ingestion/realtime_indexer.py:329` — `_retry_queue` defaultdict accumulates failed documents. Poison-pill documents that keep failing stay in queue.

### P6 — Unbounded in-memory buffers

- `pty_session_manager.py`: `asyncio.Queue()` created without `maxsize` for PTY output
- `terminal_connection_manager.py`: `_pending_futures` dict can grow unbounded on timeouts
- `grok_common.py:128`: Unbounded `asyncio.Queue()` for sync-to-async bridge

### P7 — Dead code with dangerous imports

- `agentic_data_provider.py` imports `torch` + `transformers` and loads a model at module level (lines 73-77) — currently dead code (not imported anywhere) but dangerous if ever imported accidentally
- `semantic_data_provider.py` imports `spacy` + `sentence-transformers` — also dead code

---

## Implementation Plan

### Phase 1: Lazy imports in action modules (highest impact)

Convert top-level `import pandas as pd` to function-level imports in all files that are loaded at startup. This is the single highest-impact change since pandas is imported ~11 times through the startup chain.

**Files to modify** (move `import pandas as pd` inside functions that use it):

1. `src/autobots/action/action_type/action_text2text/action_csv_merge.py`
2. `src/autobots/action/action_type/action_text2text/action_api.py`
3. `src/autobots/action/action_type/action_text2text/action_text2text_api.py`
4. `src/autobots/conn/openai/openai_responses/openai_responses.py`
5. `src/autobots/conn/openai/openai_responses/openai_responses_v2.py`
6. `src/autobots/files/file_converter.py` (also lazy-import `docx`, `pptx`)
7. `src/autobots/llm/tools/base64_encoder_decoder_extended.py`
8. `src/autobots/llm/tools/csv_merge.py`
9. `src/autobots/llm/tools/merge_pdf.py`
10. `src/autobots/llm/tools/action_csv_perquet.py`
11. `src/autobots/llm/tools/action_parquet_inspector.py`

### Phase 2: Lazy import chromadb

Convert chromadb import to lazy in the action that pulls it into startup:

**Files to modify:**

1. `src/autobots/conn/chroma/chroma.py` — move `import chromadb`, `import numpy` inside class methods
2. `src/autobots/action/action_type/action_text2text/action_multimodal_llm_chat_with_vector_search_openai.py` — lazy import of chroma types

### Phase 3: Fix PDF Parser memory issues

**File:** `src/autobots/action/action_type/action_text2text/action_text2text_pdf_parser.py`

1. **Replace string concatenation with list join** — collect page texts in a list, `"\n".join()` at the end (O(n) vs O(n²))
2. **Add temp file cleanup** — wrap processing in `try/finally` with `os.unlink(temp_path)`
3. **Add PDF size limit** — check `Content-Length` header during download, reject PDFs over a threshold (e.g. 100MB)
4. **Run sync extraction in thread** — wrap `extract_text_from_pdf` with `asyncio.to_thread()` so it doesn't block the event loop
5. **Lazy-import pymupdf** — move `import pymupdf` from module level into the functions that use it

### Phase 4: Fix action graph loop memory accumulation

**File:** `src/autobots/action_graph/action_graph/action_graph.py`

**Issue A — `pending` dict grows unbounded (line 2904)**
Workers complete out of order. The `pending` dict holds all completed-but-not-yet-sequenced results. With 10,000 iterations and 11 concurrent workers, if item #0 is slow, items #1-#9999 all accumulate in `pending`.

**Issue B — `collated` ActionDoc grows unbounded (lines 2118, 2126)**
`_append_to_collated_action_doc()` appends all output blocks and results from every iteration into a single in-memory object. 10,000 iterations = 50-100MB+ in one ActionDoc.

**Issue C — Full collated doc persisted on every drain (lines 2967-2997)**
`flush_collated()` writes the entire growing collated ActionDoc to MongoDB on every drain cycle.

**Fixes:**
1. **Add backpressure** — when `len(pending) > worker_count * 4`, pause the producer until pending drains
2. **Stream results to storage** — write drained results to separate documents or S3 instead of accumulating in `collated.output["blocks"]`
3. **Drop input text from collated** — `collated.input[f"text_{append_index}"]` is redundant; remove or cap it

**Also fix in `src/autobots/conn/excel/read_and_split.py`:**
- Line 216: Non-xlsx files load entire file via `pd.read_excel()` — add size check
- Line 238: `split_excel_into_batches()` calls `list()` on generator, materializing all batches at once — remove the `list()` wrapper

### Phase 5: Fix Daytona action memory leaks during polling

**Files:**
- `src/autobots/conn/daytona/daytona_client.py`
- `src/autobots/action/action_type/action_text2text/action_text2text_daytona.py`

**Fixes:**
1. **Bound `_sandbox_cache`** — Replace unbounded dict (line 63) with LRU cache (max ~50 entries)
2. **Stream artifact downloads to disk** — When SDK streaming fails, write bytes directly to file instead of holding full bytes object in memory
3. **Cap `log_tail` length** — Truncate to max 50KB before persisting to prevent compounding growth
4. **Skip redundant `server_sync`** — Add `server_synced: bool` flag to job doc; skip re-download on subsequent polls

### Phase 6: Bound unbounded queues/buffers

**Files to modify:**

1. `src/autobots/terminal/pty_session_manager.py` — add `maxsize=1000` to `asyncio.Queue()` calls
2. `src/autobots/terminal/terminal_connection_manager.py` — add TTL/cleanup for stale `_pending_futures`
3. `src/autobots/conn/grok/grok_chat/grok_common.py` — add `maxsize` to Queue

### Phase 7: Remove dead code with dangerous imports

**Files to delete or guard:**

1. `src/autobots/datastore/agentic_data_provider.py` — module-level `torch` + model loading. Delete if unused, or add `if TYPE_CHECKING:` guard
2. `src/autobots/datastore/semantic_data_provider.py` — module-level `spacy` + `sentence-transformers`. Same treatment

### Phase 8: Deployment awareness (no code changes needed)

- Workers already right-sized at 2 per task (ECS config)
- Memory limit already set at 8192MB per task
- Consider monitoring: if memory still tight after Phases 1-4, could reduce `desired` from 3→2 and increase `task_memory`

---

## Verification

1. **Measure before/after**: Run `python -c "import tracemalloc; tracemalloc.start(); from src.autobots.main import app; print(tracemalloc.get_traced_memory())"` to compare startup memory
2. **Run test suite**: `make test` to ensure no regressions from lazy imports
3. **Smoke test**: `make dev` and hit key endpoints (action creation, LLM chat, CSV merge, vector search) to verify lazy imports work on first use
4. **Check import errors**: Verify no `NameError` from moved imports by running affected action types
