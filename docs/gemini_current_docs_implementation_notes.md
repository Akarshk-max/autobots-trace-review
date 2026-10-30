# Gemini current docs deep dive — implementation notes

_Last reviewed: 2026-05-19_

## Why this note exists

This is an implementation-focused summary of the **current official Gemini documentation** intended to help when redesigning or rebuilding the Gemini connector/action layer.

The goal is **not** to mirror Google SDK examples directly. The goal is to extract the design constraints, production caveats, and architectural implications that matter for `src/autobots/...`.

---

## Executive summary

### Recommended production baseline

If we want a **robust, generalized, production-grade Gemini action** today, the safest baseline is:

1. Use **Gemini API `generateContent`** as the primary production surface.
2. Use **API version `v1`** by default for stability.
3. Use **stable 2.5 models** as the default production family:
   - `gemini-2.5-flash` = default general-purpose agent/chat model
   - `gemini-2.5-pro` = heavy reasoning / code / long-context tasks
   - `gemini-2.5-flash-lite` = cheap, very high-throughput workloads
4. Keep **conversation state in our app**, not in Gemini.
5. Keep the **tool loop explicit in our action layer**, even though parts of the SDK can auto-run some function flows.
6. Treat Gemini file handling as **two separate systems**:
   - short-lived uploaded files
   - durable indexed retrieval stores
7. Build a **capability + incompatibility registry** so the action planner can choose compatible feature sets before making a request.

### Key design conclusion

Gemini currently gives us a strong set of primitives, but the primitives are **not automatically a clean architecture**. To make Gemini reliable, we should wrap it in a Claude-like internal design with:

- canonical action state
- bounded turn history
- explicit tool/result persistence
- explicit file lifecycle tracking
- feature gating by model + API surface
- stream/non-stream normalization
- provider-agnostic abstractions

---

## 1) Current Gemini API surfaces that matter

## 1.1 `generateContent` API

This is the core stable request/response API.

### Why it matters
- It is the most mature and safest default path for production.
- It supports standard generation, streaming, tools, multimodal input, token counting, files, and caching.
- It is **stateless** from the app’s perspective: if we want multi-turn correctness, we must own and persist conversation state ourselves.

### Architectural implication
For a generalized action, `generateContent` should be the **default engine**.

---

## 1.2 Streaming via `streamGenerateContent`

Streaming uses the **same request shape** as `generateContent`, but the response is delivered as **SSE chunks**.

### Architectural implication
This is ideal for a provider adapter pattern:
- one normalized request builder
- one non-stream execution path
- one stream event adapter
- one shared final-response normalizer

We should avoid building a completely separate “streaming-only Gemini architecture”.

---

## 1.3 Interactions API

The Interactions API is now described by Google as the **new standard primitive for new projects**, optimized for:
- agentic workflows
- server-side state management
- typed execution steps
- background tasks
- multimodal multi-turn flows

However, the same docs also say:
- it is **Beta**
- features/schemas may change
- for **production workloads**, continue to use the standard `generateContent` API for stable deployments

### Architectural implication
We should treat Interactions as:
- an important **future-facing path**
- worth designing around conceptually
- **not yet** the safest core production backend for this connector rewrite

### Suggested stance
- Build our generalized action around **our own canonical state model**.
- Add a provider backend interface so Gemini can later swap from `generateContent` to `Interactions` without rewriting the whole action.

---

## 2) API versioning policy

The official docs distinguish:
- `v1` = stable
- `v1beta` = early features, may break, not recommended for production if breaking changes are unacceptable

The SDKs default to `v1beta` unless configured otherwise.

### Production rule
Default to **`v1`**.

### Exception rule
Only opt into **`v1beta`** behind explicit feature flags when a required feature does not exist in `v1`.

### Architectural implication
The Gemini connector should have:
- `api_version` as a first-class config field
- feature gating based on `(api_version, model, requested_capability)`

---

## 3) Stable model baseline

The current stable 2.5 family gives us the most useful production baseline.

## 3.1 `gemini-2.5-flash`
Best default for:
- low-latency general chat
- reasoning + tools
- production agent workloads
- high-volume but not ultra-cheap workloads

## 3.2 `gemini-2.5-pro`
Best for:
- hard reasoning
- code/math/STEM
- long-context analysis
- codebases / document-heavy tasks

## 3.3 `gemini-2.5-flash-lite`
Best for:
- cheapest high-throughput flows
- lightweight extraction/classification
- simple chat / routing / pre-processing steps

## Shared capability baseline across stable 2.5 models
The docs currently show support for all of the following on the stable 2.5 family:
- input context up to **1,048,576 tokens**
- output up to **65,536 tokens**
- caching
- code execution
- file search
- function calling
- Google Maps grounding
- Google Search grounding
- structured outputs
- thinking
- URL context

### Architectural implication
A generalized action can target the 2.5 family as a common baseline and then do **per-model tuning**, rather than maintaining completely separate feature architectures for Pro/Flash/Flash-Lite.

---

## 4) Conversation / turn-state design

This is the most important design difference vs Claude.

## 4.1 Gemini `generateContent` is stateless
Google’s docs explicitly note that the Gemini API is stateless, and the model does not automatically retain thought context from previous turns.

### Architectural implication
We must own:
- conversation history
- turn boundaries
- tool call IDs
- tool responses
- thought signatures
- generated intermediate content
- retrieval/file references

In other words, Gemini **needs** a Claude-style internal state layer.

---

## 4.2 Thought signatures are not optional if we manually manage history
For thinking models, Gemini may return `thoughtSignature` values in content parts.

Important implications from the docs:
- if we receive thought signatures and we are re-sending history manually, we should pass them back exactly as received
- for Gemini 3, function-calling flows can fail with 4xx validation errors if required thought signatures are omitted
- for Gemini 2.5, thought signatures are relevant when thinking is enabled and function declarations are used
- Google SDKs can handle this automatically **only if we preserve the full prior model response / use their managed flow**

### Production design rule
We should **persist raw Gemini parts**, not only flattened text.

### Canonical turn model should keep
At minimum, each turn should retain:
- role
- ordered parts
- raw function call parts
- raw function response parts
- part IDs when present
- thought signatures when present
- tool metadata when present
- citations / grounding metadata when present

### Strong recommendation
Do **not** reduce Gemini turns to plain text too early.

Treat each turn as structured content.

---

## 4.3 Why a Claude-like turn architecture is still the right design
Even though Gemini is stateless, our connector should still maintain:
- pinned/system prompt state
- visible runtime turn window
- canonical turn history store
- bounded working-set messages
- persisted tool result history
- persisted file/retrieval manifests

This will let us normalize Gemini and Claude behind the same internal architecture.

---

## 5) Tooling model in Gemini

Gemini’s docs draw a very clear distinction between:
- **built-in tools** managed by Google
- **custom tools / function calling** managed by us

That distinction should become a core part of our connector design.

---

## 5.1 Built-in tool flow
For built-in tools such as:
- Google Search grounding
- Google Maps grounding
- Code execution
- URL context
- File Search

Gemini executes the tool entirely on Google’s side and returns the final grounded answer in one API flow.

### Architectural implication
For built-in tools, our action does **not** need to run an external local tool loop.

Instead, our job is to:
- choose compatible built-in tools
- configure them correctly
- preserve provider metadata/citations/tool context from the response

---

## 5.2 Custom tool flow (function calling)
For custom tools:
1. we send tool/function declarations
2. Gemini may return a structured function call with a unique `id`
3. we execute the function in our environment
4. we send back a `functionResponse` with the **same `id`**
5. Gemini uses that result to continue or finalize

### Architectural implication
For custom tools, we need a full explicit loop with:
- call detection
- argument validation
- tool execution
- result normalization
- correlation by function-call ID
- retry/error policy
- persistence of tool results into turn history

This is very similar to Claude’s explicit tool loop and should be modeled that way.

---

## 5.3 Function calling modes
The docs currently expose function calling modes such as:
- `AUTO`
- `VALIDATED`
- `ANY`
- `NONE`

### Design use
- `AUTO`: default when using only function declarations; use for normal agent/chat flows
- `VALIDATED`: use when combining function calling with other constrained behaviors or tool combinations
- `ANY`: useful for forced function-router / planner steps
- `NONE`: useful for temporary suppression without removing tool defs

### Architectural implication
Our generalized action should expose tool mode as part of planner/runtime config, not bury it inside provider-specific code.

---

## 5.4 SDK automatic function calling
The docs note that the Python SDK can automatically:
- convert Python functions into declarations
- detect function calls
- execute them
- send the result back
- return the final text

### Recommendation
Do **not** use SDK auto-function-calling as the primary architecture for Autobots.

It is fine for prototypes, but for a production generalized action we want:
- explicit state persistence
- cross-provider symmetry
- consistent tracing / auditing
- unified error handling
- deterministic history storage

So we should keep function calling explicit in our app runtime, even if the SDK offers shortcuts.

---

## 6) Built-in tool compatibility and incompatibility rules

This is critical for robust request planning.

## 6.1 Search grounding
Google Search grounding:
- works on stable 2.5 models
- returns grounding metadata/citations
- can be used with other built-in tools such as code execution and URL context

### Design implication
Search grounding should be modeled as one built-in tool family with citation-preserving output.

---

## 6.2 URL context
URL context:
- lets Gemini read specific URLs
- uses a two-step retrieval path (internal index cache first, live fetch fallback)
- supports up to **20 URLs per request**
- supports up to **34 MB per URL**
- requires public URLs
- does **not** support localhost/private/tunneling URLs

Important limitation from the docs:
- URL context with function calling is currently unsupported in the general Gemini API tool flow

### Design implication
For a production planner, URL context should usually live in a **built-in-tool-only request mode**, not mixed with custom function calls.

---

## 6.3 File Search
File Search is Gemini’s managed RAG path.

Key documented characteristics:
- imports/chunks/indexes documents into a File Search store
- raw uploaded files are temporary
- indexed search-store data remains until manually deleted
- max file size per document: **100 MB**
- recommended store size: under **20 GB** for latency
- store total quota depends on user tier

Important limitation from the docs:
- File Search cannot be combined with other tools like Google Search grounding / URL context / etc.

### Design implication
File Search should be treated as a **separate retrieval mode**, not just another tool we casually add to the same request.

This is very important.

For production we should think of Gemini requests as falling into mutually exclusive families:
- built-in search/url-context family
- file-search RAG family
- custom function-calling family
- preview tool-combination family (Gemini 3 only)

---

## 6.4 Built-in + custom tool combination
Google now documents a preview flow where built-in tools and function calling can be combined in one generation.

But the important caveats are:
- it is **Preview**
- it is supported for **Gemini 3 models only**
- it requires explicit context circulation behavior (`include_server_side_tool_invocations`)
- it relies on preserving tool_call, tool_response, and thought-signature context

### Production recommendation
Do **not** make this the default architecture for the Gemini rewrite.

Instead:
- design the state model so it **can** support it later
- keep it behind a feature flag / experimental backend
- do not base the stable connector on preview-only Gemini 3 combination behavior

---

## 7) File lifecycle model

This is one of the biggest areas where a robust internal abstraction is needed.

## 7.1 Files API = reusable, but short-lived
The Files API supports uploading media for reuse across requests, but the docs state:
- files are automatically deleted after **48 hours**
- project storage limit is **20 GB**
- per-file max is **2 GB**
- files can be referenced during that time, but you cannot download the stored file back from Gemini

### Design implication
Gemini uploaded files are **temporary runtime assets**, not durable long-term storage.

So our connector must distinguish:
- source file URL / canonical storage we own
- temporary Gemini file handle
- durable retrieval/index handle if we import into File Search

This is exactly the kind of separation Claude already has and Gemini needs.

---

## 7.2 `files.register` for GCS-backed registration
The API reference also exposes `files.register`, which registers Google Cloud Storage URIs with the FileService without copying the file.

### Why this matters
For production systems already using GCS, this is much better than treating Gemini’s temporary file store as canonical storage.

### Design implication
If we ever do a cloud-native Gemini path, the preferred file architecture should be:
- our durable object storage (S3/GCS/etc.) remains canonical
- provider-specific registered/uploaded file refs are derived/transient

---

## 7.3 File Search store = durable retrieval memory
File Search stores are the durable side of Gemini retrieval:
- raw file objects may expire
- indexed search-store data persists until deleted

### Design implication
This maps nicely to an internal separation between:
- transient uploaded file manifest
- durable retrieval-store manifest

That mirrors Claude’s split between uploaded files and vector-store state.

---

## 8) Code execution design implications

Gemini provides built-in code execution that:
- runs **Python only**
- is managed server-side
- is available on the current stable 2.5 family
- is primarily meant for reasoning/computation/data-processing workflows

The docs also warn that:
- enabling code execution can regress some other types of output quality
- the model cannot act like a general artifact-producing media environment

### Architectural implication
We should treat Gemini code execution as:
- a **reasoning tool**
- not a substitute for a durable sandbox/filesystem runtime
- not equivalent to Claude’s richer generated-file/container workflow

### Production rule
If we need:
- durable generated artifacts
- editable workspace state
- file-by-file mutation lifecycle
- strong code execution observability

then Gemini built-in code execution alone is probably **not enough**.

That suggests our generalized architecture should keep a separate notion of:
- provider-native code execution
- external/autobots-managed execution environments

---

## 9) Streaming design implications

## 9.1 Standard Gemini streaming
The docs describe `streamGenerateContent` as SSE-based streaming using the same request shape as standard generation.

### Good architecture match
We should build a provider adapter that normalizes streamed output into internal events like:
- text_delta
- thinking_delta (if available)
- tool_call_started
- tool_result_received
- citation_delta / grounding update
- final_response
- error

## 9.2 Interactions streaming
Interactions streaming is richer and typed, with event types like `content.delta` and `interaction.complete`.

### Architectural implication
If we design our stream abstraction well now, we can support both:
- `generateContent` SSE streaming
- future Interactions streaming

without rewriting upper layers.

## 9.3 Structured output streaming
The structured output docs explicitly note that structured outputs can be streamed as valid partial JSON chunks.

### Design implication
Our stream layer should support two output modes:
- text stream
- structured-json stream accumulator

---

## 10) Thinking, token counting, and caching

## 10.1 Thinking control
Current docs distinguish:
- Gemini 2.5 uses `thinkingBudget`
- Gemini 3 uses `thinkingLevel`

### Design implication
Thinking config must be model-family aware.

We should not expose a single raw provider knob directly to the upper app layer.

Instead use an internal abstraction such as:
- `thinking_mode = off | low | medium | high | adaptive`
- provider resolver maps that to `thinkingBudget` or `thinkingLevel`

---

## 10.2 Token counting
Gemini provides token counting support and docs note that token accounting includes:
- text
- images / files / other modalities
- tools/function calling overhead

### Design implication
We should have a Gemini token preflight helper similar in spirit to Claude’s token counting helper.

It should support:
- exact provider-side counting when available
- heuristic fallback when needed
- request-size policy before sending

---

## 10.3 Caching
Gemini currently documents:
- implicit caching on most models
- explicit caching for `generateContent`
- Interactions API does not yet support explicit caching, though `previous_interaction_id` helps implicit caching

### Production recommendation
For the first production Gemini rewrite:
- rely on implicit caching first
- keep explicit caching behind a separate feature module
- do not make the action architecture depend on explicit caching availability

---

## 11) Interactions API — what to do with it now

## What is attractive about it
Interactions has several things we actually want architecturally:
- server-side state with `previous_interaction_id`
- typed execution steps
- explicit stored interaction objects
- built-in support for agentic workflows
- streaming of structured events

## Why we should still be careful
The docs also explicitly say:
- Beta
- schema can change
- for production workloads, use `generateContent`
- some `generateContent` features are still unavailable there

## Recommended strategy
Design **for** Interactions, but build **on** `generateContent` first.

Practical meaning:
- our canonical internal state model should look more like an `Interaction`
- but the Gemini provider backend should still use `generateContent` for stable production delivery

---

## 12) Recommended generalized action design for Gemini

Below is the architecture I would implement.

## 12.1 Canonical internal state (provider-agnostic)

Have one strongly typed state object that stores at least:
- model
- api surface (`generateContent` vs `interactions`)
- api version (`v1` / `v1beta`)
- system / pinned instructions
- canonical turn history
- working-set messages for current send
- raw provider parts per turn
- thought signatures
- custom tool call records
- built-in tool metadata
- grounding metadata/citations
- transient uploaded file manifest
- durable retrieval/file-search manifest
- current-turn URLs / attachments
- stream state snapshot
- usage/token metrics

This is the Gemini equivalent of what Claude already does well.

---

## 12.2 Capability registry

Add a Gemini model capability registry that knows, for each model family:
- supports_thinking
- thinking_style (`budget` vs `level`)
- supports_function_calling
- supports_structured_output
- supports_code_execution
- supports_search_grounding
- supports_url_context
- supports_file_search
- supports_tool_combination_preview
- supports_interactions_api
- supported_api_versions

And separately, a compatibility matrix for request composition.

Example:
- `file_search + google_search` => reject
- `url_context + function_calling` => reject in standard mode
- `built_in + custom` => allow only on Gemini 3 preview path with combination flag

---

## 12.3 Tool family planner

Before each request, classify the intended tool plan into one family:

### Family A — Custom function calling
Use when we need app-managed tools.

### Family B — Built-in grounding/tools
Use when we only need native search/maps/url/code-execution behavior.

### Family C — File Search RAG
Use when retrieval over indexed user docs is the main task.

### Family D — Preview tool-combination
Only for Gemini 3 preview and behind feature flag.

This planner will prevent a lot of invalid requests before they happen.

---

## 12.4 File lifecycle abstraction

Keep separate concepts for:
- `source_file_ref` — our canonical source URL / blob / object store ref
- `gemini_file_ref` — short-lived uploaded or registered Gemini file handle
- `file_search_store_ref` — durable indexed retrieval resource
- `turn_attachment_ref` — how this file entered the current turn

Do not collapse these into one list of URLs.

---

## 12.5 Explicit custom-tool loop

Even if SDK helpers exist, our runtime should explicitly:
- inspect response parts
- detect function calls
- validate tool args
- execute tools
- store tool results
- add function responses with matching IDs
- preserve thought signatures/raw parts
- continue until final answer

This is the production-safe way to make Gemini symmetric with Claude.

---

## 12.6 Streaming adapter

Implement one Gemini streaming adapter that converts provider events into internal events.

Target event types:
- `assistant_text_delta`
- `assistant_structured_json_delta`
- `thinking_delta`
- `built_in_tool_activity`
- `custom_tool_call_requested`
- `custom_tool_result_ack`
- `final_usage`
- `stream_error`

---

## 12.7 Request sanitization

Add a Gemini request sanitizer before every send.

Responsibilities:
- validate supported tool combinations
- remove unsupported config combinations for chosen model/API surface
- map internal thinking config to correct provider shape
- enforce API version policy
- ensure required IDs/signatures are present for multi-turn/manual history
- reject requests that would rely on expired temporary file handles

This should be a first-class helper, not scattered inline logic.

---

## 13) Production guardrails

## Must-do guardrails
1. Default to `v1`.
2. Default to `generateContent`.
3. Keep Gemini history canonical in our DB/config, not only in SDK chat objects.
4. Persist raw structured parts, not just flattened text.
5. Persist function call IDs and function responses.
6. Persist thought signatures when present.
7. Split file state into transient vs durable.
8. Validate tool compatibility before sending.
9. Add token preflight/counting.
10. Keep preview-only features behind feature flags.

## Do-not-do list
1. Do **not** treat the Files API as durable storage.
2. Do **not** assume built-in tools and custom tools are universally combinable.
3. Do **not** flatten model turns to plain text if we intend to support thinking/tool continuity.
4. Do **not** rely on SDK automatic function calling as the only execution path.
5. Do **not** make File Search “just another tool” in a generic tools array without incompatibility checks.
6. Do **not** adopt Interactions as the only production backend yet.

---

## 14) Suggested implementation order

### Phase 1 — stable production baseline
- `generateContent`
- API `v1`
- stable 2.5 model registry
- explicit custom function-calling loop
- built-in Google Search / URL context / code execution support
- token counting + request sanitization
- streaming adapter
- transient file manifest

### Phase 2 — durable retrieval
- File Search store abstraction
- durable retrieval manifest
- planner family for retrieval mode
- import/index/query lifecycle

### Phase 3 — advanced/preview paths
- Gemini 3 preview combination mode
- built-in + custom tool combination
- structured outputs with tools where supported
- Interactions API backend

---

## 15) Proposed decision for the rewrite

If we want the Gemini rewrite to become **Claude-grade stable**, the right strategy is:

- copy Claude’s **internal architecture pattern**
- do **not** copy Gemini SDK examples as architecture
- wrap Gemini in a strong internal model of:
  - turns
  - tools
  - files
  - retrieval
  - streaming
  - capabilities
  - context budgets

The provider should be a backend implementation of our architecture, not the architecture itself.

---

## 16) Official docs index (for implementation)

### Core API
- Gemini API reference: https://ai.google.dev/api
- API versions explained: https://ai.google.dev/gemini-api/docs/api-versions
- Interactions API: https://ai.google.dev/gemini-api/docs/interactions

### Models
- All Gemini models: https://ai.google.dev/gemini-api/docs/models
- Gemini 2.5 Flash: https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash
- Gemini 2.5 Flash-Lite: https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash-lite
- Gemini 2.5 Pro: https://ai.google.dev/gemini-api/docs/models/gemini-2.5-pro

### Tools
- Using tools with Gemini API: https://ai.google.dev/gemini-api/docs/tools
- Function calling: https://ai.google.dev/gemini-api/docs/function-calling
- Combine built-in tools and function calling: https://ai.google.dev/gemini-api/docs/tool-combination
- Grounding with Google Search: https://ai.google.dev/gemini-api/docs/google-search
- URL context: https://ai.google.dev/gemini-api/docs/url-context
- Code execution: https://ai.google.dev/gemini-api/docs/code-execution
- File Search: https://ai.google.dev/gemini-api/docs/file-search

### Files / tokens / thinking / output
- Files API: https://ai.google.dev/gemini-api/docs/files
- Token counting: https://ai.google.dev/gemini-api/docs/tokens
- Thinking: https://ai.google.dev/gemini-api/docs/thinking
- Thought signatures: https://ai.google.dev/gemini-api/docs/thought-signatures
- Structured outputs: https://ai.google.dev/gemini-api/docs/structured-output
- Context caching: https://ai.google.dev/gemini-api/docs/caching

---

## 17) Short answer for our connector design

### What to copy from Claude
- canonical action state
- explicit turn history
- explicit tool history
- explicit file manifests
- capability registry
- request sanitization
- normalized stream adapter

### What to copy from Gemini docs
- exact provider constraints
- exact tool incompatibilities
- exact ID / signature rules
- exact file lifetime behavior
- exact API version policy

### What not to copy from Gemini docs
- SDK shortcuts as architecture
- preview-only flows as production defaults

---

## 18) Current recommendation to discuss next

If we proceed to implementation planning, the most sensible target is:

**Build a new Gemini action around `generateContent` + `v1` + stable 2.5 models + explicit internal state/tool loop, while designing the abstractions so Gemini 3 preview combination mode and Interactions API can be added later without redesigning the action.**
