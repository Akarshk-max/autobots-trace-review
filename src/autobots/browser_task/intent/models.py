from enum import Enum
from typing import Any, List, Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from src.autobots.browser_task.intent.normalize import (
    DEFAULT_INTENT_VERSION,
    DEFAULT_POLICY_ID,
    MAX_LABEL_LEN,
    MAX_TAG_LEN,
    MAX_TAGS,
    MAX_TARGETS,
    MAX_TASKS,
    MAX_TRACE_ID_LEN,
    MAX_WAIT_HINT_MS,
    MIN_WAIT_HINT_MS,
    clip_int,
    sanitize_hint_strings,
    sanitize_selector_list,
)


class ScreenshotSpec(BaseModel):
    enabled: bool = False
    selectors: List[str] = Field(default_factory=list)
    full_page: bool = False
    max_images: int = Field(default=6, ge=0, le=20)
    model_config = {"extra": "ignore"}

class PdfSpec(BaseModel):
    enabled: bool = False
    page: bool = True
    model_config = {"extra": "ignore"}

class DownloadSpec(BaseModel):
    enabled: bool = False
    click_selectors: List[str] = Field(default_factory=list)
    max_files: int = Field(default=3, ge=0, le=20)
    timeout_ms: int = Field(default=15000, ge=1000, le=120000)
    max_bytes: int = Field(default=20_000_000, ge=0, le=200_000_000)  # 200MB cap
    model_config = {"extra": "ignore"}

class NetworkCaptureSpec(BaseModel):
    enabled: bool = False
    resource_types: List[str] = Field(default_factory=lambda: ["xhr", "fetch"])
    url_contains: List[str] = Field(default_factory=list)
    max_items: int = Field(default=10, ge=0, le=200)
    capture_bodies: bool = True
    max_body_bytes: int = Field(default=2_000_000, ge=0, le=50_000_000)
    upload_large_bodies: bool = True
    upload_always: bool = False
    settle_ms: int = Field(default=0, ge=0, le=5000)
    model_config = {"extra": "ignore"}

class TableSpec(BaseModel):
    enabled: bool = False
    selectors: List[str] = Field(default_factory=list)
    max_tables: int = Field(default=100, ge=0, le=500)
    max_rows: int = Field(default=200, ge=1, le=5000)
    max_scrolls: int = Field(default=20, ge=0, le=200)
    scroll_delta: int = Field(default=800, ge=50, le=5000)
    settle_ms: int = Field(default=250, ge=0, le=2000)
    # Time to wait before auto-detecting tables (allows JS-rendered tables to load)
    pre_detect_settle_ms: int = Field(default=2000, ge=0, le=10000)
    # Retry detection if no tables found (useful for slow-loading tables)
    retry_detect: bool = True
    # Maximum number of retry attempts (each retry doubles the wait time)
    max_retries: int = Field(default=2, ge=0, le=5)
    start_count: int = Field(default=1, ge=1, le=5000)
    end_count: int = Field(default=1, ge=1, le=5000)
    model_config = {"extra": "ignore"}

class ChartSpec(BaseModel):
    enabled: bool = False
    selectors: List[str] = Field(default_factory=list)  # svg root or container selector
    max_charts: int = Field(default=3, ge=0, le=30)
    screenshot_canvas: bool = True
    model_config = {"extra": "ignore"}

class AuthSpec(BaseModel):
    enabled: bool = False
    login_url: Optional[HttpUrl] = Field(default=None, description="URL of the login page")
    credentials_url: Optional[HttpUrl] = Field(
        default=None,
        description="URL to JSON file containing credentials (username, password, etc.)"
    )
    username_selector: Optional[str] = Field(default=None, max_length=256)
    password_selector: Optional[str] = Field(default=None, max_length=256)
    submit_selector: Optional[str] = Field(default=None, max_length=256)
    success_indicator_selector: Optional[str] = Field(default=None, max_length=256)
    timeout_ms: int = Field(default=30000, ge=5000, le=120000)
    extract_storage_state: bool = Field(default=False, description="If true, generates a storage state JSON (cookies/origins) upon successful login")
    model_config = {"extra": "ignore"}

    @field_validator("username_selector", "password_selector", "submit_selector", "success_indicator_selector", mode="before")
    @classmethod
    def _sanitize_selectors(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        if not isinstance(v, str):
            return None
        s = v.strip()
        return s if s else None

class BrowserExtractionSpec(BaseModel):
    screenshots: ScreenshotSpec = Field(default_factory=ScreenshotSpec)
    pdf: PdfSpec = Field(default_factory=PdfSpec)
    downloads: DownloadSpec = Field(default_factory=DownloadSpec)
    network: NetworkCaptureSpec = Field(default_factory=NetworkCaptureSpec)
    tables: TableSpec = Field(default_factory=TableSpec)
    charts: ChartSpec = Field(default_factory=ChartSpec)
    auth: AuthSpec = Field(default_factory=AuthSpec)
    model_config = {"extra": "ignore"}

class ImagesExtractionSpec(BaseModel):
    selectors_hint: List[str] = Field(default_factory=list)
    scope_selectors_hint: List[str] = Field(default_factory=list)
    images_limit: int = Field(default=200, ge=0, le=5000)
    include_background_images: bool = False

    model_config = {"extra": "ignore"}

    @field_validator("selectors_hint", "scope_selectors_hint", mode="before")
    @classmethod
    def _sanitize_selectors(cls, v: Any) -> List[str]:
        return sanitize_selector_list(v)

class TaskKind(str, Enum):
    main_content = "main_content"
    links = "links"
    metadata = "metadata"
    images = "images"


class OutputFormat(str, Enum):
    json = "json"
    text = "text"
    markdown = "markdown"


class Verbosity(str, Enum):
    compact = "compact"
    normal = "normal"
    verbose = "verbose"


class IncludeRawHtml(str, Enum):
    never = "never"
    on_failure = "on_failure"
    always = "always"


class LoadProfile(str, Enum):
    cheap = "cheap"
    balanced = "balanced"
    deep = "deep"
    auto = "auto"


class PageKind(str, Enum):
    auto = "auto"
    article = "article"
    product = "product"
    listing = "listing"
    profile = "profile"
    docs = "docs"
    landing = "landing"
    checkout = "checkout"


class ReadyCondition(str, Enum):
    auto = "auto"
    domcontentloaded = "domcontentloaded"
    networkidle = "networkidle"
    selector = "selector"


class PaginationMode(str, Enum):
    none = "none"
    next_button = "next_button"
    page_param = "page_param"


class HeadersHint(BaseModel):
    """
    Very limited safe header hints.
    Keep this intentionally tiny (avoid arbitrary headers/cookies/tokens).
    """
    accept_language: Optional[str] = Field(default=None, description="e.g. en-US")

    model_config = {"extra": "ignore"}


class TargetUrl(BaseModel):
    url: HttpUrl
    label: Optional[str] = Field(default=None, max_length=MAX_LABEL_LEN)
    headers_hint: Optional[HeadersHint] = None
    storage_state_url: Optional[HttpUrl] = Field(
        default=None,
        description="Optional per-target Playwright storage-state JSON URL. Overrides root storage_state_url.",
    )

    model_config = {"extra": "ignore"}


class TaskSpec(BaseModel):
    kind: TaskKind
    limit: Optional[int] = Field(default=None, ge=1, le=10_000, description="Used by links task only")
    model_config = {"extra": "ignore"}

    @model_validator(mode="after")
    def _validate_kind_options(self) -> "TaskSpec":
        if self.kind != TaskKind.links and self.limit is not None:
            # ignore silently (lenient) - compiler can decide too
            self.limit = None
        return self


class WaitSpec(BaseModel):
    ready: ReadyCondition = ReadyCondition.auto
    selectors_any: List[str] = Field(default_factory=list)
    selectors_all: List[str] = Field(default_factory=list)
    max_wait_hint_ms: Optional[int] = Field(default=None)

    model_config = {"extra": "ignore"}

    @field_validator("max_wait_hint_ms", mode="before")
    @classmethod
    def _clip_wait_hint(cls, v: Any) -> int | None:
        if v is None:
            return None
        return clip_int(v, min_v=MIN_WAIT_HINT_MS, max_v=MAX_WAIT_HINT_MS, default=5000)

    @field_validator("selectors_any", "selectors_all", mode="before")
    @classmethod
    def _sanitize_selectors(cls, v: Any) -> List[str]:
        return sanitize_selector_list(v)


class PaginationSpec(BaseModel):
    mode: PaginationMode = PaginationMode.none
    max_pages_hint: Optional[int] = Field(default=None, ge=1, le=200)
    next_selectors_hint: List[str] = Field(default_factory=list)
    page_param_name: Optional[str] = Field(default=None, max_length=32)
    stop_when_hint: Optional[str] = Field(
        default=None,
        description="Hint string like: no_new_links / no_next_button / same_content",
    )

    model_config = {"extra": "ignore"}

    @field_validator("next_selectors_hint", mode="before")
    @classmethod
    def _sanitize_next_selectors(cls, v: Any) -> List[str]:
        return sanitize_selector_list(v)


class NavigationSpec(BaseModel):
    load_profile: LoadProfile = LoadProfile.auto
    page_kind: PageKind = PageKind.auto
    wait: Optional[WaitSpec] = None
    pagination: PaginationSpec = Field(default_factory=PaginationSpec)

    model_config = {"extra": "ignore"}


class ContentExtractionSpec(BaseModel):
    selectors_hint: List[str] = Field(default_factory=list)
    exclude_selectors_hint: List[str] = Field(default_factory=list)
    prefer_text_over_tables: Optional[bool] = None
    remove_boilerplate: Optional[bool] = True

    model_config = {"extra": "ignore"}

    @field_validator("selectors_hint", "exclude_selectors_hint", mode="before")
    @classmethod
    def _sanitize_selectors(cls, v: Any) -> List[str]:
        return sanitize_selector_list(v)


class LinksExtractionSpec(BaseModel):
    scope_selectors_hint: List[str] = Field(default_factory=list)
    allow_external: bool = True
    dedupe: bool = True
    keep_query_params: bool = False

    model_config = {"extra": "ignore"}

    @field_validator("scope_selectors_hint", mode="before")
    @classmethod
    def _sanitize_scope_selectors(cls, v: Any) -> List[str]:
        return sanitize_selector_list(v)


class NormalizeSpec(BaseModel):
    trim_whitespace: bool = True
    collapse_newlines: bool = True
    unicode_normalize: bool = False
    strip_scripts_styles: bool = True

    model_config = {"extra": "ignore"}


class ExtractionSpec(BaseModel):
    content: ContentExtractionSpec = Field(default_factory=ContentExtractionSpec)
    links: LinksExtractionSpec = Field(default_factory=LinksExtractionSpec)
    normalize: NormalizeSpec = Field(default_factory=NormalizeSpec)
    browser: BrowserExtractionSpec = Field(default_factory=BrowserExtractionSpec)
    images: ImagesExtractionSpec = Field(default_factory=ImagesExtractionSpec)
    model_config = {"extra": "ignore"}


class ExpectationsSpec(BaseModel):
    language_hint: Optional[str] = Field(default=None, description="e.g. en, hi")
    min_text_length_hint: Optional[int] = Field(default=None, ge=0, le=200_000)
    contains_any_text_hint: List[str] = Field(default_factory=list)
    contains_any_selectors_hint: List[str] = Field(default_factory=list)
    block_signals_hint: List[str] = Field(default_factory=list)
    allow_empty: bool = False

    model_config = {"extra": "ignore"}

    @field_validator("contains_any_text_hint", "block_signals_hint", mode="before")
    @classmethod
    def _sanitize_hint_strings(cls, v: Any) -> List[str]:
        return sanitize_hint_strings(v)

    @field_validator("contains_any_selectors_hint", mode="before")
    @classmethod
    def _sanitize_selector_strings(cls, v: Any) -> List[str]:
        return sanitize_selector_list(v)


class OutputSpec(BaseModel):
    format: OutputFormat = OutputFormat.json
    verbosity: Verbosity = Verbosity.normal
    include_raw_html: IncludeRawHtml = IncludeRawHtml.never
    include_debug: bool = False

    model_config = {"extra": "ignore"}


class ClientRef(BaseModel):
    trace_id: Optional[str] = Field(default=None, max_length=MAX_TRACE_ID_LEN)
    conversation_id: Optional[str] = Field(default=None, max_length=MAX_TRACE_ID_LEN)
    source: Optional[str] = Field(default=None, max_length=64)
    tags: List[str] = Field(default_factory=list)

    model_config = {"extra": "ignore"}

    @field_validator("tags", mode="before")
    @classmethod
    def _sanitize_tags(cls, v: Any) -> List[str]:
        if not v:
            return []
        out: List[str] = []
        seen: set[str] = set()
        for raw in v:
            if not isinstance(raw, str):
                continue
            s = raw.strip()
            if not s:
                continue
            if len(s) > MAX_TAG_LEN:
                s = s[:MAX_TAG_LEN]
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
            if len(out) >= MAX_TAGS:
                break
        return out


class ScrapeIntentRequest(BaseModel):
    intent_version: str = Field(default=DEFAULT_INTENT_VERSION)
    strict_mode: bool = Field(default=False, description="If true, reject unknown fields.")
    targets: List[TargetUrl] = Field(..., min_length=1, max_length=MAX_TARGETS)
    tasks: List[TaskSpec] = Field(default_factory=list, max_length=MAX_TASKS)
    navigation: NavigationSpec = Field(default_factory=NavigationSpec)
    extraction: ExtractionSpec = Field(default_factory=ExtractionSpec)
    expectations: ExpectationsSpec = Field(default_factory=ExpectationsSpec)
    output: OutputSpec = Field(default_factory=OutputSpec)
    policy_id: str = Field(default=DEFAULT_POLICY_ID, min_length=1, max_length=64)
    client_ref: Optional[ClientRef] = None
    storage_state_url: Optional[HttpUrl] = Field(
        default=None,
        description="Optional global Playwright storage-state JSON URL (cookies/origins). Used for all targets unless a target overrides it.",
    )
    # We always "ignore" extras here; strictness is enforced by parser.py.
    model_config = {"extra": "ignore"}

    @model_validator(mode="after")
    def _ensure_tasks_default(self) -> "ScrapeIntentRequest":
        # If LLM sends no tasks, choose safe defaults:
        if not self.tasks:
            self.tasks = [
                TaskSpec(kind=TaskKind.main_content),
                TaskSpec(kind=TaskKind.links, limit=200),
                TaskSpec(kind=TaskKind.metadata),
            ]
        return self

    @field_validator("intent_version")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        # keep simple for now; allow "1"
        v = str(v).strip()
        if not v:
            return DEFAULT_INTENT_VERSION
        return v
