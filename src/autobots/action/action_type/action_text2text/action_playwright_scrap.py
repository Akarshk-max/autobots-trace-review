import asyncio
import json
import time
from typing import Any, Dict, List, Optional, Type, Union

import httpx
from loguru import logger
from pydantic import BaseModel, Field

from src.autobots import SettingsProvider
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import (
    ActionABC,
    ActionConfigCreateType,
    ActionConfigType,
    ActionConfigUpdateType,
    ActionInputType,
    ActionOutputType,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.browser_task.intent.models import ScrapeIntentRequest
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class PlaywrightTimings(BaseModel):
    elapsed_ms: Optional[int] = None


class PlaywrightLink(BaseModel):
    href: str
    text: Optional[str] = None


class PlaywrightArtifact(BaseModel):
    kind: Optional[str] = None
    label: Optional[str] = None

    # S3-backed artifact
    s3_url: Optional[str] = None
    mime: Optional[str] = None
    byte_length: Optional[int] = None
    sha256: Optional[str] = None

    # provenance
    source_url: Optional[str] = None
    selector: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)

    # optional inline payload
    data: Any = None

    model_config = {"extra": "allow"}


class PlaywrightPageResult(BaseModel):
    page_no: int
    url: str
    main_content: Optional[str] = None
    links: Optional[List[PlaywrightLink]] = None
    metadata: Optional[Dict[str, Any]] = None

    model_config = {"extra": "allow"}


class PlaywrightError(BaseModel):
    type: Optional[str] = None
    message: Optional[str] = None

    model_config = {"extra": "allow"}


class PlaywrightJobResult(BaseModel):
    ok: bool
    status: str

    job_id: Optional[str] = None
    request_id: Optional[str] = None
    url: Optional[str] = None
    final_url: Optional[str] = None
    policy_id: Optional[str] = None
    attempt: Optional[int] = None
    mode: Optional[str] = None

    timings: Optional[PlaywrightTimings] = None
    budgets_used: Optional[Dict[str, int]] = None

    # Non-pagination success
    main_content: Optional[str] = None
    links: Optional[List[PlaywrightLink]] = None
    metadata: Optional[Dict[str, Any]] = None

    # Pagination success
    pages: Optional[List[PlaywrightPageResult]] = None
    links_merged: Optional[List[PlaywrightLink]] = None

    # Artifacts from modules
    artifacts: Optional[List[PlaywrightArtifact]] = None

    # Failure or blocked
    error: Optional[PlaywrightError] = None
    diagnostics: Optional[Dict[str, Any]] = None

    # Policy fallback path
    policy_chain_tried: Optional[List[str]] = None
    final_policy: Optional[str] = None

    model_config = {"extra": "allow"}


class PlaywrightScrapeResult(BaseModel):
    request_id: Optional[str] = None
    ok: bool = True
    results: List[PlaywrightJobResult] = Field(default_factory=list)

    model_config = {"extra": "allow"}

class PlaywrightScrapeConfig(BaseModel):
    intent: ScrapeIntentRequest

class ActionPlaywrightScrape(
    ActionABC[
        PlaywrightScrapeConfig,
        PlaywrightScrapeConfig,
        PlaywrightScrapeConfig,
        DataBlock,
        DataBlocks,
    ]
):
    type = ActionType.playwright_scrape

    _MAX_RETRIES = 3
    _BACKOFF_BASE_SECONDS = 1.0
    _STREAM = True

    _MAX_TEXT_CHARS_PER_JOB_BLOCK = 50_000

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return PlaywrightScrapeConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return PlaywrightScrapeConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return PlaywrightScrapeConfig

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @staticmethod
    def get_description() -> str:
        """
        This description is used by LLMs to generate correct config.
        Keep it explicit and contract-stable.
        """
        return r"""
PLAYWRIGHT SCRAPE INTENT CONFIG
==============================
This action sends a structured scraping intent to the internal Playwright scraping service.
Request contract (IMPORTANT)
------------------------------
The POST body is ALWAYS:

{
  "intent": <ScrapeIntentRequest JSON>
}

The "intent" describes WHAT to extract. The backend decides HOW to execute it.

Minimal example
---------------
{
  "intent": {
    "intent_version": "1",
    "targets": [
      { "url": "https://example.com" }
    ],
    "tasks": [
      { "kind": "main_content" },
      {"kind":"images"} # only use when needed to extract image
    ],
    "policy_id": "default"
  }
}
Example: Input config for downloading a file if user asked for downloading it.Use always js-heavy whenever downloading content from downloadable url.
{
  "intent": {
    "intent_version": "1",
    "strict_mode": false,
    "targets": [
      { "url": "<URL>" }
    ],
    "extraction": {
      "browser": {
        "downloads": { "enabled": true, "click_selectors": [ "a.download", "button.export" ] }
      }
    },
    "policy_id": "js-heavy"
  }
}
------------------------------------------------------------
INTENT STRUCTURE (ScrapeIntentRequest)
------------------------------------------------------------

intent_version: str
  Current version: "1"

strict_mode: bool
  If true: unknown fields are rejected , Don't use this.
  If false: unknown fields are ignored (recommended). Always keep it false.

targets: List[TargetUrl] (REQUIRED)
  Each target becomes a separate scraping job.
  Target format:
    {
      "url": "https://example.com",
      "label": "optional string",
      "headers_hint": { "accept_language": "en-US,en;q=0.9" },
      "storage_state_url": "https://.../storage_state.json"
    }

tasks: List[TaskSpec] (OPTIONAL)
  If omitted, server defaults apply:
    main_content, links(limit=200), metadata

  Task kinds:
    - main_content
    - links (supports "limit")
    - metadata
    - images

  Example:
    [{"kind":"links","limit":200}]

navigation (OPTIONAL)
  load_profile: cheap | balanced | deep | auto
  page_kind: auto | article | product | listing | profile | docs | landing | checkout

  wait:
    ready: auto | domcontentloaded | networkidle | selector

  pagination:
    mode: none | next_button | page_param
    max_pages_hint: 1..200
    next_selectors_hint: ["a.next", ...]
    page_param_name: "page"
    stop_when_hint: "same_content" | "no_next_button" | "no_new_links" (hint)

extraction (OPTIONAL)
  content:
    selectors_hint
    exclude_selectors_hint
    prefer_text_over_tables
    remove_boilerplate

  links:
    scope_selectors_hint
    allow_external
    dedupe
    keep_query_params

  images:
    selectors_hint:
        CSS or XPath selectors for image elements or containers
    scope_selectors_hint:
        Limits image discovery to a section of the page (recommended)
    images_limit:
        Max number of images to return
    include_background_images:
        If true, extracts CSS background-image URLs
        REQUIRED for modern CMS and marketing sites (DataCamp, Medium, Notion, etc.)

  normalize:
    trim_whitespace
    collapse_newlines
    unicode_normalize
    strip_scripts_styles

  browser (OPTIONAL extras)
    screenshots: { enabled, selectors, full_page, max_images }
    pdf: { enabled, page }
    downloads: { enabled, click_selectors, max_files, timeout_ms, max_bytes }
    network: { enabled, resource_types, url_contains, max_items, capture_bodies, ... }
    tables: { enabled, selectors,start_count,end_count, max_tables, max_rows, max_scrolls, ... }
    charts: { enabled, selectors, max_charts, screenshot_canvas }

expectations (OPTIONAL)
  min_text_length_hint
  contains_any_text_hint
  contains_any_selectors_hint
  block_signals_hint
  allow_empty

output (OPTIONAL)
  format: json | markdown
  verbosity: compact | normal | verbose
  include_raw_html: never | on_failure | always
  include_debug: bool

policy_id (OPTIONAL)
  default | js-heavy | stealth-lite


XPath selectors
---------------
All selector fields support XPath when prefixed with:
  xpath=//...

Valid in:
  - content.selectors_hint
  - content.exclude_selectors_hint
  - links.scope_selectors_hint
  - images.selectors_hint
  - images.scope_selectors_hint
  - browser.tables.selectors
  - browser.charts.selectors
  - browser.screenshots.selectors

Examples:
  "xpath=//article"
  "xpath=//article//p"
  "xpath=//div[@id='content']"

Note:
For downloads , charts , network  , tables , screenshots , pdf always use "js-heavy" or "stealth-lite" policy_id only.
In table extraction you can provide start_count and end_count to extract all the table page by page.
If target url is related to documentation sites than default is best policy_id.
If target url is related to wikipedia or similar sites than js-heavy is best policy_id.
If default and js-heavy both failed with timeout related error than try with stealth-lite policy_id.
"""
    
    def _normalize_intent(
        self, intent: Union[ScrapeIntentRequest, Dict[str, Any]]
    ) -> Dict[str, Any]:
        if isinstance(intent, ScrapeIntentRequest):
            intent_dict = intent.model_dump(mode="json", exclude_none=True)
        else:
            intent_dict = dict(intent or {})

        intent_dict["intent_version"] = "1"
        intent_dict["strict_mode"] = False

        return intent_dict


    def _make_payload(self, intent_dict: Dict[str, Any]) -> Dict[str, Any]:
        return {"intent": intent_dict}

    def _dedupe_preserve_order(self, xs: List[str]) -> List[str]:
        seen: set[str] = set()
        out: List[str] = []
        for x in xs:
            if not x:
                continue
            if x in seen:
                continue
            seen.add(x)
            out.append(x)
        return out

    def _result_to_datablocks(self, res: PlaywrightScrapeResult) -> DataBlocks:
        blocks: List[DataBlock] = []

        blocks.append(
            DataBlock(
                text=res.model_dump_json(exclude_none=True),
                metadata={"kind": "playwright_scrape_raw", "request_id": res.request_id},
            )
        )

        # Blocks 1: per-job
        for jr in res.results:
            urls: List[str] = []

            job_meta: Dict[str, Any] = {
                "kind": "playwright_job_result",
                "ok": jr.ok,
                "status": jr.status,
                "job_id": jr.job_id,
                "request_id": jr.request_id,
                "url": jr.url,
                "final_url": jr.final_url,
                "policy_id": jr.policy_id,
                "attempt": jr.attempt,
                "mode": jr.mode,
                "policy_chain_tried": jr.policy_chain_tried,
                "final_policy": jr.final_policy,
                "timings": (jr.timings.model_dump(exclude_none=True) if jr.timings else None),
                "budgets_used": jr.budgets_used,
                "error": (jr.error.model_dump(exclude_none=True) if jr.error else None),
                "diagnostics": jr.diagnostics,
                "artifacts": [a.model_dump(exclude_none=True) for a in (jr.artifacts or [])],
            }

            text = ""

            if jr.pages:
                # Pagination path: take first few pages' main content as text summary
                parts: List[str] = []
                for p in jr.pages[:5]:
                    if p.main_content:
                        parts.append(p.main_content)
                    if p.links:
                        urls.extend([link.href for link in p.links if link and link.href])

                text = "\n\n".join([t for t in parts if t])[: self._MAX_TEXT_CHARS_PER_JOB_BLOCK]

                # keep detailed pages in metadata for consumers
                job_meta["pages"] = [p.model_dump(exclude_none=True) for p in jr.pages]

                if jr.links_merged:
                    urls.extend([link.href for link in jr.links_merged if link and link.href])
                    job_meta["links_merged"] = [link.model_dump(exclude_none=True) for link in jr.links_merged]

            else:
                # Non-pagination path
                if jr.main_content:
                    text = (jr.main_content or "")[: self._MAX_TEXT_CHARS_PER_JOB_BLOCK]

                if jr.links:
                    urls.extend([link.href for link in jr.links if link and link.href])
                    job_meta["links"] = [link.model_dump(exclude_none=True) for link in jr.links]

                if jr.metadata:
                    job_meta["metadata"] = jr.metadata

            if not text and not jr.ok:
                # Failure summary fallback
                et = jr.error.type if jr.error else "unknown"
                em = jr.error.message if jr.error else "failed"
                text = f"[playwright_scrape_failed] type={et} message={em}"

            blocks.append(
                DataBlock(
                    text=text,
                    urls=self._dedupe_preserve_order(urls),
                    metadata={k: v for k, v in job_meta.items() if v is not None},
                )
            )

        return DataBlocks(blocks=blocks)

    async def run_action(
        self,
        ctx: Context,
        app_state: AppState,
        action_input: DataBlock,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:
        """
        Standard Action runtime path.
        Uses self.action_config (already created/validated by framework).
        """
        try:
            payload = self._normalize_intent(self.action_config.intent)
            # payload = self._make_payload(intent_dict)

            result = await self._execute_with_retry(ctx=ctx, payload=payload, notify=notify)
            res_model = PlaywrightScrapeResult.model_validate(result)

            return self._result_to_datablocks(res_model)

        except Exception:
            logger.bind(ctx=ctx).exception("Playwright scrape action failed")
            raise

    async def run_tool(
        self,
        action_config: PlaywrightScrapeConfig,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> PlaywrightScrapeResult | Exception:
        start_monotonic = time.monotonic()

        try:
            config = await self.create_config(
                ctx=ctx,
                app_state=app_state,
                config_create=action_config,
                user=self.user,
                db=self.db,
            )

            # logger.bind(ctx=ctx).info(f"config before normalization {config}")

            payload = self._normalize_intent(config.intent)

            # logger.bind(ctx=ctx).info(f"config after normalization {payload}")

            result = await self._execute_with_retry(
                ctx=ctx,
                payload=payload,
                notify=notify,
            )

            elapsed_s = time.monotonic() - start_monotonic

            logger.bind(
                ctx=ctx,
                elapsed_seconds=round(elapsed_s, 3),
            ).info("Playwright scrape completed")

            return PlaywrightScrapeResult.model_validate(result)

        except Exception as e:
            elapsed_s = time.monotonic() - start_monotonic

            logger.bind(
                ctx=ctx,
                elapsed_seconds=round(elapsed_s, 3),
            ).exception("Playwright scrape failed")

            return e

    # HTTP call with retry
    async def _execute_with_retry(
        self,
        ctx: Context,
        payload: Dict[str, Any],
        notify: Optional[Notify],
    ) -> Dict[str, Any]:
        api_base = SettingsProvider.sget().AUTOBOTS_PLAYWRIGHT_API_URL.rstrip("/")
        endpoint = f"{api_base}/playwright/scrape"

        headers = {
            "Authorization": f"Bearer {self.user.access_token}",
            "Content-Type": "application/json",
        }

        attempt = 0

        while True:
            try:
                async with httpx.AsyncClient(timeout=600) as client:
                    if self._STREAM:
                        async with client.stream(
                            "POST",
                            endpoint,
                            json=payload,
                            headers=headers,
                        ) as response:
                            response.raise_for_status()

                            collected = ""
                            async for chunk in response.aiter_text():
                                collected += chunk
                                if notify:
                                    notify(ctx, chunk)

                            # Best-effort: if backend returns SSE-ish frames, join data lines.
                            collected_stripped = collected.replace("\r\n", "\n").lstrip()
                            if collected_stripped.startswith("data:") or "\n\ndata:" in collected_stripped:
                                data_lines: List[str] = []
                                for line in collected.replace("\r\n", "\n").split("\n"):
                                    if line.startswith("data:"):
                                        data_lines.append(line[len("data:") :].strip())
                                collected = "".join(data_lines)

                            return json.loads(collected)

                    # non-stream
                    response = await client.post(
                        endpoint,
                        json=payload,
                        headers=headers,
                    )
                    response.raise_for_status()
                    return response.json()

            except Exception as e:
                attempt += 1

                if attempt > self._MAX_RETRIES:
                    logger.bind(ctx=ctx).error("Playwright scrape failed after retries")
                    raise

                sleep_seconds = self._BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                logger.bind(ctx=ctx).warning(
                    "Retry attempt={} sleeping={}s error={}",
                    attempt,
                    sleep_seconds,
                    str(e),
                )
                await asyncio.sleep(sleep_seconds)