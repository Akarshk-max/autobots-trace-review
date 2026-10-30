from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, HttpUrl

Mode = Literal["http", "rendered"]

class ScreenshotPlan(BaseModel):
    enabled: bool = False
    selectors: List[str] = Field(default_factory=list)
    full_page: bool = False
    max_images: int = 6

class PdfPlan(BaseModel):
    enabled: bool = False
    page: bool = True

class DownloadPlan(BaseModel):
    enabled: bool = False
    click_selectors: List[str] = Field(default_factory=list)
    max_files: int = 3
    timeout_ms: int = 15000
    max_bytes: int = 20_000_000

class NetworkCapturePlan(BaseModel):
    enabled: bool = False
    resource_types: List[str] = Field(default_factory=lambda: ["xhr", "fetch"])
    url_contains: List[str] = Field(default_factory=list)
    max_items: int = 10
    capture_bodies: bool = True
    max_body_bytes: int = 2_000_000
    upload_large_bodies: bool = True
    upload_always: bool = False
    settle_ms: int = 0

class TablePlan(BaseModel):
    enabled: bool = False
    selectors: List[str] = Field(default_factory=list)
    max_tables: int = 100
    max_rows: int = 200
    max_scrolls: int = 20
    scroll_delta: int = 800
    start_count: int = 1
    end_count: int = 1
    settle_ms: int = 250

class ChartPlan(BaseModel):
    enabled: bool = False
    selectors: List[str] = Field(default_factory=list)
    max_charts: int = 3
    screenshot_canvas: bool = True

class AuthPlan(BaseModel):
    enabled: bool = False
    login_url: Optional[HttpUrl] = None
    credentials_url: Optional[HttpUrl] = None
    username_selector: Optional[str] = None
    password_selector: Optional[str] = None
    submit_selector: Optional[str] = None
    success_indicator_selector: Optional[str] = None
    timeout_ms: int = 30000
    extract_storage_state: bool = False

class BrowserPlan(BaseModel):
    screenshots: ScreenshotPlan = Field(default_factory=ScreenshotPlan)
    pdf: PdfPlan = Field(default_factory=PdfPlan)
    downloads: DownloadPlan = Field(default_factory=DownloadPlan)
    network: NetworkCapturePlan = Field(default_factory=NetworkCapturePlan)
    tables: TablePlan = Field(default_factory=TablePlan)
    charts: ChartPlan = Field(default_factory=ChartPlan)
    auth: AuthPlan = Field(default_factory=AuthPlan)


class ContextHints(BaseModel):
    """
    Safe, limited context hints:
    - user_agent / locale / extra headers (e.g., Accept-Language)
    """
    user_agent: Optional[str] = None
    locale: Optional[str] = None
    timezone_id: Optional[str] = None
    extra_http_headers: Dict[str, str] = Field(default_factory=dict)


class NetworkRules(BaseModel):
    block_resource_types: List[str] = Field(default_factory=lambda: ["image", "font", "media"])
    allow_hosts: Optional[List[str]] = None
    block_hosts: Optional[List[str]] = None


class Budgets(BaseModel):
    max_total_ms: int = 20_000
    max_nav_ms: int = 9_000
    max_requests: int = 120
    max_redirects: int = 10
    max_chars: int = 50_000
    max_snippet_chars: int = 1200


class StepPlan(BaseModel):
    mode: Mode
    nav_timeout_ms: int
    ready: Literal["auto", "domcontentloaded", "networkidle", "selector"] = "auto"
    selectors_any: List[str] = Field(default_factory=list)
    selectors_all: List[str] = Field(default_factory=list)


class Expectations(BaseModel):
    min_text_length_hint: Optional[int] = None
    contains_any_text_hint: List[str] = Field(default_factory=list)
    contains_any_selectors_hint: List[str] = Field(default_factory=list)
    block_signals_hint: List[str] = Field(default_factory=list)
    allow_empty: bool = False


class NormalizePlan(BaseModel):
    trim_whitespace: bool = True
    collapse_newlines: bool = True
    unicode_normalize: bool = False
    strip_scripts_styles: bool = True


class ExtractionPlan(BaseModel):
    tasks: List[str] = Field(default_factory=list)  # ["main_content","links","metadata"]
    content_selectors: List[str] = Field(default_factory=list)
    exclude_selectors: List[str] = Field(default_factory=list)
    link_scope_selectors: List[str] = Field(default_factory=list)
    links_limit: int = 200
    allow_external_links: bool = True
    dedupe_links: bool = True
    keep_query_params: bool = False
    image_selectors: List[str] = Field(default_factory=list)
    image_scope_selectors: List[str] = Field(default_factory=list)
    images_limit: int = 200
    include_background_images: bool = False

    normalize: NormalizePlan = Field(default_factory=NormalizePlan)


class PaginationPlan(BaseModel):
    mode: Literal["none", "next_button", "page_param"] = "none"
    max_pages: int = 1
    next_selectors: List[str] = Field(default_factory=list)
    page_param_name: Optional[str] = None  # for page_param
    stop_when_hint: Optional[str] = None


class JobPlan(BaseModel):
    job_id: str
    request_id: str
    url: HttpUrl
    domain: str
    policy_id: str
    storage_state_url: Optional[str] = None

    steps: List[StepPlan]
    budgets: Budgets
    network: NetworkRules
    context: ContextHints = Field(default_factory=ContextHints)

    extraction: ExtractionPlan
    expectations: Expectations
    pagination: PaginationPlan = Field(default_factory=PaginationPlan)

    browser: BrowserPlan = Field(default_factory=BrowserPlan)
    max_attempts: int = 2


class ExecutionPlan(BaseModel):
    request_id: str
    jobs: List[JobPlan]