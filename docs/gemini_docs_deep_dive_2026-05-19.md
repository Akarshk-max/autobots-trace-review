# Gemini current documentation deep dive for production connector design

_Last verified: 2026-05-19_

## Purpose

This note captures the current **official Gemini documentation** in a way that is directly useful for implementing or rewriting a production-grade Gemini connector/action.

The goal is not to restate every API example. The goal is to extract the design constraints that matter for architecture:

- how turns/history actually work
- how thought signatures affect turn persistence
- how custom tools differ from built-in tools
- how Files API differs from File Search
- what streaming really implies
- what is model-gated vs preview-only
- what should be mirrored from the Claude connector architecture

---

## Executive summary

### 1) Gemini API is stateless

The Gemini API does **not** retain server-side chat state for standard `generateContent` requests. SDK chat helpers are conveniences on top of the same API. In practice, the **full conversation history must be sent again on every follow-up turn**.

**Implementation consequence:**
We should copy Claude's idea that runtime `messages/history` must be treated as a first-class architectural concern. We cannot rely on Gemini to remember anything unless we resend it.

### 2) Thought signatures are a real protocol concern

For thinking models, Gemini may return `thoughtSignature` values in parts of the model response. These signatures preserve reasoning context across multi-step interactions.

- With **Gemini 3**, function-calling signatures are **mandatory** to return in the next request or the API can reject the request with 4xx/400 errors.
- With **Gemini 2.5**, function-call-related signatures are less strict, but still important enough that the safest production posture is to preserve them whenever present.
- The official Google GenAI SDK can handle this automatically **if you append the full model response object back into history**.

**Implementation consequence:**
A Gemini connector should never persist only flattened assistant text if tool use is involved. It must persist **full structured parts** for the current turn, or at minimum a lossless normalized representation that preserves signatures and IDs.

### 3) Built-in tools and custom function calling are not the same feature

Gemini has two different tool families:

1. **Custom tools / function calling** — tool schemas supplied by the app.
2. **Built-in tools** — Google-managed tools such as Google Search, URL Context, Code Execution, File Search, Maps, Computer Use.

These are not universally combinable across models/modes.

**Implementation consequence:**
Do not model Gemini tools as one flat list with one execution policy. We need a tool taxonomy:

- custom function tools
- built-in server-side tools
- tool-combination capability flags
- model / provider / mode gating

### 4) Files API and File Search are separate systems

Gemini has:

- **Files API**: upload reusable media/documents to reference in prompts
- **File Search**: managed RAG/indexing system that imports, chunks, embeds, and retrieves documents

These solve different problems and must be modeled separately.

**Implementation consequence:**
A production Gemini connector should separate:

- prompt file attachments
- retrieval/index state
- URL-context inputs
- tool-generated multimodal outputs

Trying to collapse all of these into one "file handling" path will become brittle.

### 5) Streaming should be treated as a transport mode, not a different product flow

Streaming uses the same request semantics as standard generation, but the connector must parse **incremental structured parts**, not just text.

**Implementation consequence:**
Streaming adapter logic should accumulate:

- partial text
- function/tool parts
- code execution parts
- thought signatures
- final structured content object

This is the same lesson as Claude.

---

## Recommended implementation baseline

If we want a **robust generalized production connector**, the safest baseline is:

- **Primary production models:** `gemini-2.5-pro`, `gemini-2.5-flash`
- **Preview-only path:** Gemini 3-specific features behind explicit flags
- **SDK preference:** official Google GenAI SDK whenever possible
- **Provider abstraction:** separate backends for
  - Gemini Developer API
  - Vertex AI

Why this baseline:

- Gemini 2.5 Pro and Flash are documented as stable current workhorse models.
- Gemini 3 introduces important features, but several tool-combination features are explicitly marked preview-only and Gemini-3-only.
- If we use the official SDK and keep full structured history, thought-signature handling gets much simpler.

---

## Core interaction model

## Standard API

Primary request style:

- `generateContent`
- `streamGenerateContent` (SSE)

The request carries `contents`, which is the current conversation history plus the latest user turn.

## SDK chat

SDK chat is a convenience layer only.

Important practical rule from docs:

- multi-turn chat still uses `generateContent` underneath
- the full history is still sent each turn

### Architecture implication

For our connector we should assume:

- the canonical store is **our persisted history**, not the provider
- chat sessions are client-side convenience only
- any trimming / turn windowing / replay strategy belongs to our connector, not Gemini

---

## Turn persistence design for Gemini

To make Gemini production-safe, turn storage should mirror Claude conceptually.

## What must be persisted

For each turn we should persist a normalized but lossless record containing:

- `role`
- `parts[]`
- `functionCall` parts
- `functionResponse` parts
- `tool_call` / `tool_response` style built-in tool parts when present
- `executable_code` parts
- `code_execution_result` parts
- `thoughtSignature`
- IDs attached to tool/function/code parts
- any citation / grounding metadata we need later

## Why full part preservation matters

Gemini documentation makes two critical points:

1. The API is stateless.
2. Thought signatures may be required for next-turn correctness.

That means flattening response parts into plain assistant text is architecturally unsafe for:

- function calling
- code execution
- Gemini 3 tool-combination flows
- reasoning continuity

## Suggested Gemini turn-store contract

A Gemini turn record should look conceptually like:

```text
TurnRecord
- turn_index
- provider (gemini_api | vertex)
- model
- request_contents[]
- response_content
- usage_metadata
- built_in_tool_events[]
- custom_tool_events[]
- thought_signatures[]
- generated_artifacts[]
- timestamps / status
```

### Strong recommendation

If we use the official SDK, store the **full response object normalized to JSON** wherever possible, not just extracted text.

---

## Thought signatures: the most important Gemini-specific protocol rule

## What they are

Thought signatures are encrypted representations of the model's internal reasoning state.

## Operational rules

### Gemini 3

When Gemini 3 returns function calls:

- the first function call part in a step carries the signature
- in sequential multi-step tool use, **each step's first function call** must preserve its signature
- if the current turn is replayed without required signatures, the API can return a **400 / validation error**

### Gemini 2.5

Docs explicitly say Gemini 2.5 behaves differently:

- if there are function calls, Gemini 2.5 places the signature in the **first part regardless of type**
- returning it is described as optional, unlike Gemini 3

### Streaming edge case

Docs also warn that with streaming, a signature may arrive in a **final chunk with empty text**.

## Implementation rules we should adopt

1. **Always preserve all signatures if present**, regardless of model.
2. **Never rebuild tool-call history from scratch** if we already have the exact provider response.
3. **Streaming parser must inspect all chunks**, including empty-text chunks.
4. If we support manual REST-style history, we need explicit signature-circulation logic.
5. If we use SDK chat + full response replay, we can simplify the implementation substantially.

---

## Tool architecture in Gemini

## Tool families

### A) Custom tools (function calling)

These are functions we declare via schemas.

Gemini function calling supports modes:

- `AUTO`
- `VALIDATED`
- `ANY`
- `NONE`

### B) Built-in tools

The tools overview currently lists:

- Google Search
- Google Maps
- Code Execution
- URL Context
- Computer Use (Preview)
- File Search

### Key design observation

Gemini built-in tools are **server-side tools**. They are not executed by our process, but their context and responses may still need to be circulated in conversation history.

That means Gemini's tool architecture naturally wants **two execution planes**:

1. **provider-managed execution plane** (built-in tools)
2. **application-managed execution plane** (function calling)

This is very similar to the split Claude already has between:

- server-side tool results
- custom tool execution in our runtime

---

## Function calling flow

The canonical function-calling loop is:

1. Send prompt + function declarations.
2. Model returns `functionCall` part(s).
3. App executes the function.
4. App sends back a `functionResponse` part.
5. Model continues or finishes.

### Design requirements

A robust implementation must preserve:

- function name
- function args
- function call ID
- original part ordering
- thought signature placement
- function response content

## Multimodal function responses

Current docs say this is available for **Gemini 3 series**.

Gemini 3 can accept multimodal content nested inside a `functionResponse`, including:

- images
- PDFs
- plain text docs

This is useful if our app-side tool returns a generated PDF/image/document that the model should reason over in the very next step.

### Architecture implication

We should design the Gemini tool-result pipeline to support:

- plain structured JSON function responses
- optional multimodal attachments in function responses

But we should gate multimodal function responses behind:

- `model_family == gemini_3`
- explicit capability flag

---

## Built-in tool combination rules (important)

This area is one of the most important implementation constraints.

## What the current docs imply

### Search + other built-in tools

The Google Search page says Search can be used with other built-in tools like:

- Code Execution
- URL Context

### Built-in + custom tools

There is now a separate **tool-combination** page documenting built-in + custom-tool combinations, but it is explicitly:

- **Preview**
- **Gemini 3 only**

and requires `include_server_side_tool_invocations=True`.

### URL Context limitation page

The URL Context page still states that tool use like URL Context / Google Search **with function calling is currently unsupported**.

## Practical interpretation

The safest production reading is:

- **Gemini 2.5 standard path:** built-in tools and custom function calling should be treated as **not safely combinable** in one generalized standard connector flow.
- **Gemini 3 preview path:** built-in + custom-tool combination exists, but is preview-only and should be behind an explicit feature gate.

### Architecture rule

We should encode a capability matrix such as:

```text
supports_custom_functions
supports_built_in_search
supports_built_in_url_context
supports_built_in_code_execution
supports_file_search
supports_built_in_plus_custom_combination
supports_multimodal_function_response
requires_strict_signature_return
```

Do not infer these at runtime from one doc page. Keep an explicit registry.

---

## Files architecture: treat as 3 different systems

Gemini docs force us to separate file handling into three distinct paths.

## 1) Files API (prompt attachments)

The Files API lets us upload media/documents separately from the prompt so they can be reused across requests.

Operations include:

- upload
- get metadata
- list
- delete

Some files, especially video, may need polling until the file state becomes `ACTIVE`.

### Use this for

- reusable prompt inputs
- PDFs / images / text / audio / video that should be directly referenced in prompts

### Do not confuse this with

- File Search indexing
- URL Context
- custom-tool outputs

---

## 2) File Search (managed RAG)

File Search is a separate managed retrieval system.

It:

- imports / chunks / indexes data
- creates embeddings
- retrieves relevant context for the model

Important current docs notes:

- Audio and video are not currently supported.
- Storage is free.
- Query-time embedding generation is free.
- You pay at indexing time for embeddings and then normal model token costs.

### Implementation consequence

This should map to a separate subsystem in the connector, not the same path as prompt attachments.

Suggested state shape:

```text
GeminiFileSearchState
- store_name
- indexed_documents[]
- ingest_operations[]
- metadata_filters
- last_sync_status
```

---

## 3) URL Context (public URL ingestion)

URL Context is another separate tool.

It is for public URLs the model should fetch and analyze directly.

Current documented limitations include:

- max 20 URLs per request
- max 34 MB retrieved content per URL
- public URLs only
- no localhost/private network/tunneling URLs
- unsupported for audio/video files
- unsupported for paywalled content and Google Workspace docs

### Implementation consequence

We should not route arbitrary URLs through Files API or File Search by default. We need a planner/router:

- public web page / PDF URL => URL Context or explicit downloader path
- private / authenticated files => our downloader + Files API / app-managed upload
- corpus / RAG use case => File Search

---

## A critical design distinction: Files API vs File Search vs URL Context

This is the most important Gemini file-handling rule.

| Need | Correct Gemini mechanism |
|---|---|
| Reuse media/documents across prompts | Files API |
| Managed retrieval over your corpus | File Search |
| Read public web pages or PDFs directly from URLs | URL Context |
| Let model use current web info broadly | Google Search |

If we blur these together in one connector, the system will be hard to reason about and difficult to debug.

---

## Code execution architecture

Gemini's code execution tool is provider-managed Python execution.

The model can:

- emit executable Python code
- run it in the provider environment
- receive `code_execution_result`
- continue iterating until it produces final text

The docs also show response parts like:

- `text`
- `executable_code`
- `code_execution_result`
- optional image outputs in some examples

### Architecture implication

Code execution should be treated like a **server-side built-in tool**, not like our custom app tool.

We should preserve its structured parts in history exactly the same way we preserve function calls.

### If code execution is enabled

Connector state should track at least:

- code execution enabled flag
- executable code parts emitted
- execution result parts emitted
- artifacts/images if present
- thought signatures / IDs tied to the turn

---

## Streaming architecture

## What docs say

- `streamGenerateContent` uses SSE and the same request semantics as standard generation.
- SDK chat also supports streaming.
- Multi-turn streaming still resends full history.
- Thought signatures may arrive in a final chunk even if text is empty.

## Architecture implication

A streaming adapter should be able to accumulate:

- text deltas
- structured parts
- function call parts
- tool call / tool response parts
- code execution parts
- thought signatures
- finish reason / usage metadata

### Strong recommendation

For Gemini, streaming should produce:

1. incremental user-facing updates
2. one final normalized `Content` object for persistence

That is exactly the same high-level architecture Claude uses, and we should keep that pattern.

---

## Token counting and context design

## Token counting

Gemini provides `count_tokens` and also `usage_metadata` on generation responses.

Useful fields include:

- prompt token count
- output token count
- cached content token count
- thoughts token count
- total token count

## Multimodal tokenization

Docs currently state:

- small images (<=384 px both dimensions) count as 258 tokens
- larger images are tiled into 768x768 regions at 258 tokens each
- video tokens are counted at 263 tokens/second
- audio tokens are counted at 32 tokens/second

### Architecture implication

We should keep a Gemini token preflight helper similar to Claude's token-count preflight.

That helper should be able to:

- call `count_tokens`
- estimate next-turn size with full replayed history
- warn when multimodal inputs are likely to explode cost/latency
- integrate with any future history compaction strategy

---

## Context caching

Gemini now has two caching paths:

- implicit caching
- explicit caching

For Gemini 2.5 and newer models, implicit caching is on by default.

Current docs note minimum token counts for implicit caching like:

- Gemini 2.5 Flash: 1024
- Gemini 2.5 Pro: 4096

### Architecture implication

Our connector should support a caching policy object, but unlike Claude we should avoid overcomplicating it initially.

Suggested rollout:

1. **Phase 1:** observe cache-related usage metadata only
2. **Phase 2:** enable explicit caching for large repeated corpora
3. **Phase 3:** layer caching policy on top of long-context / retrieval workflows

---

## Long context

Current model docs say both:

- `gemini-2.5-pro` input limit: 1,048,576 tokens
- `gemini-2.5-flash` input limit: 1,048,576 tokens

The long-context guide also explicitly pushes a different architectural mindset:

- long context reduces the need for aggressive summarization / dropping / RAG in some workflows
- but caching is the primary optimization for repeated large contexts

### Architecture implication

For Gemini we should not copy Claude's exact compaction strategy mechanically.

Instead:

- keep bounded turn history support
- add token preflight
- prefer replaying more history when within budget
- combine with explicit caching for repeated heavy context
- use File Search only when retrieval is actually needed, not as the default answer to every large context problem

---

## Vertex AI differences that matter for architecture

If we support Vertex as a Gemini provider, we should explicitly model it as a different transport/provider backend.

## Important file-input difference

Vertex documentation describes `FileData.fileUri` using:

- `gs://...` URIs
- public HTTP URLs
- public/owned YouTube URLs (one per request)

with MIME types specified alongside the file URI.

So Vertex file input is **URI-oriented**, not Files-API-object-oriented.

## Function response difference

Vertex function-calling reference also explicitly documents:

- `functionCall`
- `functionResponse`
- optional `parts` inside a function response
- `inline_data`
- `file_data`

### Architecture implication

Provider abstraction should separate at least:

- auth
- endpoint/client
- file transport strategy
- model naming / resolution
- provider-specific request sanitization

But the higher-level state model can still be unified.

---

## Capability matrix we should encode in code

Recommended fields for a Gemini model registry:

```text
provider: gemini_api | vertex
model_name
family
stable | preview
input_token_limit
output_token_limit
supports_function_calling
supports_code_execution
supports_google_search
supports_url_context
supports_file_search
supports_maps
supports_computer_use
supports_built_in_plus_custom_combination
supports_multimodal_function_response
supports_thinking
thought_signature_mode
supports_explicit_caching
supports_implicit_caching
supports_streaming
supports_sdk_chat
```

### Suggested initial values

#### gemini-2.5-pro
- stable
- 1M context
- function calling: yes
- code execution: yes
- google search: yes
- url context: yes
- file search: yes
- built-in + custom combination: **no (treat as unsupported in standard production path)**
- multimodal function response: no
- thinking: yes

#### gemini-2.5-flash
- stable
- 1M context
- function calling: yes
- code execution: yes
- google search: yes
- url context: yes
- file search: **docs conflict; treat cautiously / feature flag / test-gate**
- built-in + custom combination: no
- multimodal function response: no
- thinking: yes

#### gemini-3-* preview path
- preview
- built-in + custom combination: yes, preview-only
- multimodal function response: yes
- stricter thought-signature handling: yes

---

## Documentation inconsistencies worth encoding as guardrails

These are important for implementation planning.

### Inconsistency 1: File Search support for Gemini 2.5 Flash

- The **Gemini 2.5 Flash model page** lists File Search as supported.
- The **File Search supported-models section** currently lists Gemini 2.5 Pro and 2.5 Flash-Lite, but not 2.5 Flash.

**Practical rule:**
Do not rely only on doc scraping for capability truth. Keep a feature flag and add runtime integration tests.

### Inconsistency 2: built-in tools + function calling

- URL Context docs say tool use with function calling is currently unsupported.
- Tool-combination docs say built-in + custom tool combinations exist in preview for Gemini 3 only.

**Practical rule:**
Treat built-in + custom combination as a **Gemini 3 preview feature only**, not as the default generalized Gemini behavior.

---

## What a Claude-style Gemini rewrite should look like

The Claude architecture should be copied at the **design level**, with Gemini-specific protocol differences layered in.

## Suggested module split

### 1) `chat_model_gemini.py`
Owns the canonical request/state model.

Should include:

- messages/history
- turn history
- visible turns
- tool config
- built-in tool config
- files state
- file search state
- url context state
- generated artifacts
- usage metadata
- thought-signature preservation fields

### 2) `model_registry_gemini.py`
Owns model/provider capability flags.

### 3) `gemini_chat.py`
Owns execution loop:

- request sanitization
- send / stream
- parse parts
- function-call loop
- built-in tool context circulation
- code-execution handling
- final response normalization

### 4) `tool_factory_gemini.py`
Owns custom function declaration generation and app-side tool execution.

### 5) file modules split by concern
Instead of one mixed file module, use separate concepts:

- prompt files
- file search store
- url context routing
- tool-result multimodal packaging

### 6) `streaming_adapter_gemini.py`
Accumulate text + structured parts + signatures into final persisted content.

### 7) `token_counting_gemini.py`
Use `count_tokens` and usage metadata for preflight and observability.

---

## Practical implementation rules

1. **Persist full structured model responses, not just text.**
2. **Always preserve IDs and thought signatures.**
3. **Separate custom tools from built-in tools.**
4. **Separate Files API from File Search from URL Context.**
5. **Treat Gemini 3 combination features as preview-only.**
6. **Prefer official SDK over hand-built REST when possible.**
7. **Keep provider abstraction clean between Gemini API and Vertex AI.**
8. **Add runtime capability tests because docs are not perfectly consistent.**

---

## Suggested rollout plan for a robust generalized Gemini action

### Phase 1 — Stable production baseline

- `gemini-2.5-pro` and `gemini-2.5-flash`
- standard generate + streaming
- custom function calling only
- built-in tools only in isolated, explicit modes
- Files API support
- token preflight + usage logging
- turn history + signature preservation

### Phase 2 — Retrieval + URL tooling

- File Search support
- URL Context support
- Google Search support
- explicit routing rules among Files / File Search / URL Context / Search

### Phase 3 — Preview advanced path

- Gemini 3 tool-combination support
- multimodal function responses
- stricter signature validation path

### Phase 4 — Vertex parity

- Vertex provider backend
- URI/file transport adaptation
- provider-specific integration tests

---

## Official sources used for this note

- https://ai.google.dev/gemini-api/docs/text-generation
- https://ai.google.dev/gemini-api/docs/thought-signatures
- https://ai.google.dev/gemini-api/docs/function-calling
- https://ai.google.dev/gemini-api/docs/code-execution
- https://ai.google.dev/api/files
- https://ai.google.dev/gemini-api/docs/file-search
- https://ai.google.dev/gemini-api/docs/google-search
- https://ai.google.dev/gemini-api/docs/url-context
- https://ai.google.dev/gemini-api/docs/tokens
- https://ai.google.dev/gemini-api/docs/caching
- https://ai.google.dev/gemini-api/docs/long-context
- https://ai.google.dev/gemini-api/docs/tool-combination
- https://ai.google.dev/gemini-api/docs/models/gemini-2.5-pro
- https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash
- https://docs.cloud.google.com/vertex-ai/generative-ai/docs/model-reference/inference
- https://docs.cloud.google.com/vertex-ai/generative-ai/docs/model-reference/function-calling
