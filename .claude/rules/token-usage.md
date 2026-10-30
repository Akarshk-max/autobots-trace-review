# Token Usage Rules

Applies to `src/autobots/token_usage/` and any connector that records LLM usage.

- Record one `LlmCallUsage` per API call, inside the connector's tool loop, next to
  the existing `snapshot_*` call. One document is written per `ActionFactory.run_action`
  invocation, so a whole tool loop collapses into one row with the per-call breakdown.
- Instrument every billed call, including the extra "finalize after max loop" request
  that several connectors make once the tool loop is exhausted.
- Never let recording break a run. `TokenUsageService._record` swallows its own errors
  and every normalizer tolerates a missing or malformed usage payload.
- `input_tokens` on our model always means input billed at the full rate, excluding
  cache reads and cache writes. Providers disagree, so normalize per provider:
  - **Anthropic** reports `input_tokens` already excluding both cache fields, and gives
    no total, so the total is
    `input_tokens + cache_read_input_tokens + cache_creation_input_tokens + output_tokens`.
  - **OpenRouter** reports `prompt_tokens` including `prompt_tokens_details.cached_tokens`,
    so cached tokens are subtracted out.
  - **Gemini** reports `prompt_token_count` including `cached_content_token_count`, and
    reports `thoughts_token_count` *outside* `candidates_token_count`, so output is
    `candidates + thoughts`.
  - **Grok** reports `prompt_tokens` including cached tokens, and `reasoning_tokens`
    inside `completion_tokens`.
  - **DeepSeek** uses the OpenAI Chat shape, but reports cache hits as a top-level
    `prompt_cache_hit_tokens` that is already inside `prompt_tokens`, so it is subtracted out.
    Reasoning sits in `completion_tokens_details.reasoning_tokens`, inside `completion_tokens`.
  - **Perplexity** reports `prompt_tokens` / `completion_tokens` / `total_tokens` with no
    cache fields, a top-level `reasoning_tokens`, and a `cost` object whose `total_cost` is
    the USD figure we store. `citation_tokens` and `num_search_queries` stay in `raw` only.
  - **OpenAI Chat** uses the same shape as OpenRouter. **OpenAI Responses** uses the same
    semantics under different names: `input_tokens` / `input_tokens_details.cached_tokens`
    / `output_tokens_details.reasoning_tokens`.
- OpenRouter returns usage on streamed responses without any request flag; the old
  `usage: {include: true}` body field is deprecated and must not be added back. OpenAI
  still needs `stream_options.include_usage` set on streaming requests or the chunks carry
  no usage at all.
- Prefer the provider's own total when it publishes one; compute it ourselves only where
  the provider gives none (Anthropic) or where its total omits a component.
- Every row stores `provider` **and** `surface`. They differ only for OpenAI, where
  Responses and Chat bill to one provider but report usage under different field names.
  `SURFACE_REGISTRY` in `token_usage_service.py` is the single place that pairs a surface
  with its provider and normalizer; adding a provider is one enum value and one row there.
- `cost` is the provider's own figure when it reports one (OpenRouter, Perplexity). For
  every other provider it is computed per call in `TokenUsageService.record` from the
  normalized token counts and `model_pricing/model_prices.json`. Exact counts times
  published rates is a fact, not an estimate, so there is no cost-source flag.
- `price_for` matches a row's model to a pricing key by exact name, else by the longest
  key that is a prefix ending at a `-`, `@` or `:` boundary, scoped to the provider. So a
  dated or Vertex name finds its base price, but a different version number does not.
  A model with no price gets `null` and one warning, never `0` and never a neighbour's rate.
  Aggregations report `null`, never `0`, for rows that carry no cost.
- Nothing about token usage is written into `ActionResultDoc`. Run, graph and user totals
  are aggregations over `TokenUsages`, served by `GET /v1/token_usage/{run_id}` and
  `GET /v1/token_usage/source/{source_id}`.
- Every row carries `source` and `source_id`, so nothing lands unattributable. A graph or
  team run is made of action runs, so both are `source=action` and are found through their
  typed result ids; `ingestion`, `knowledge_base` and `sandbox` are their own sources.
- A team leader and a graph node run through `run_action_v1` without an `action_result_id`,
  so they have no action run of their own and `action_run_id` stays null. `source_id` falls
  back to the graph or team result id, so the containing run is still findable. `_store`
  warns when a row lands with no `source_id` at all.
- Outside an action run, start a collector on a **child** `AppState` carrying the source,
  and flush it in a `finally`. A child gives each concurrent unit of work its own collector.
- `AppState.child_context()` resets the containing-run ids unless they are passed as
  kwargs. Any call site that spawns a child *inside* a graph or team run must pass those
  ids through, or the child's rows lose their graph or team attribution entirely.
