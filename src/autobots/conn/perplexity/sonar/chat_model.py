import re
from datetime import datetime
from typing import Annotated, Any, Dict, List, Literal

from pydantic import BaseModel, ConfigDict, Field

PerplexityRole = Literal["system", "user", "assistant"]
SearchType = Literal["fast", "pro", "auto"]
SearchMode = Literal["web", "academic", "sec"]
SearchContextSize = Literal["low", "medium", "high"]
SearchRecency = Literal["hour", "day", "week", "month", "year"]
StreamMode = Literal["full", "concise"]
ReasoningEffort = Literal["minimal", "low", "medium", "high"]
AsyncStatus = Literal["CREATED", "IN_PROGRESS", "COMPLETED", "FAILED"]

_ALLOWED_IMAGE_MIME_SUBTYPES = {"png", "jpeg", "webp", "gif"}
_ALLOWED_IMAGE_FORMAT_FILTERS = {"gif", "jpeg", "png", "webp"}
_ALLOWED_FILE_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt", ".rtf"}
_MAX_IMAGE_BYTES = 50 * 1024 * 1024
_MAX_FILE_BYTES = 50 * 1024 * 1024
_MAX_FILES_PER_REQUEST = 30


class PerplexityTextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class PerplexityImageURLValue(BaseModel):
    url: str


class PerplexityFileURLValue(BaseModel):
    url: str


class PerplexityImageURLPart(BaseModel):
    type: Literal["image_url"] = "image_url"
    image_url: PerplexityImageURLValue


class PerplexityFileURLPart(BaseModel):
    type: Literal["file_url"] = "file_url"
    file_url: PerplexityFileURLValue


PerplexityContentPart = Annotated[
    PerplexityTextPart | PerplexityImageURLPart | PerplexityFileURLPart,
    Field(discriminator="type"),
]
PerplexityMessageContent = str | List[PerplexityContentPart]


class PerplexityMessageParam(BaseModel):
    role: PerplexityRole
    content: PerplexityMessageContent


class PerplexityWebSearchUserLocation(BaseModel):
    country: str | None = None
    region: str | None = None
    city: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class PerplexityWebSearchOptions(BaseModel):
    search_context_size: SearchContextSize | None = None
    search_type: SearchType | None = None
    user_location: PerplexityWebSearchUserLocation | None = None


class PerplexityResponseFormat(BaseModel):
    type: str
    json_schema: Dict[str, Any] | None = None


class PerplexityMediaResponseOverrides(BaseModel):
    return_videos: bool | None = None


class PerplexityMediaResponse(BaseModel):
    overrides: PerplexityMediaResponseOverrides | None = None


class PerplexitySearchMetadata(BaseModel):
    search_type_used: str | None = None
    classification_reason: str | None = None


class PerplexitySearchResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    title: str | None = None
    url: str | None = None
    date: str | None = None
    last_updated: str | None = None
    snippet: str | None = None
    source: str | None = None


class PerplexityImageResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    image_url: str | None = None
    origin_url: str | None = None
    title: str | None = None
    width: int | None = None
    height: int | None = None
    format: str | None = None


class PerplexityVideoResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    url: str | None = None
    thumbnail_url: str | None = None
    title: str | None = None
    duration: float | None = None


class PerplexityUsageCost(BaseModel):
    input_tokens_cost: float | None = None
    output_tokens_cost: float | None = None
    citation_tokens_cost: float | None = None
    reasoning_tokens_cost: float | None = None
    request_cost: float | None = None
    search_queries_cost: float | None = None
    total_cost: float | None = None


class PerplexityUsage(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    search_context_size: str | None = None
    citation_tokens: int | None = None
    num_search_queries: int | None = None
    reasoning_tokens: int | None = None
    cost: PerplexityUsageCost | None = None


class PerplexityChatReq(BaseModel):
    model: str = "sonar-pro"
    messages: List[PerplexityMessageParam] = Field(default_factory=list)

    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop: str | List[str] | None = None
    stream_mode: StreamMode | None = None
    reasoning_effort: ReasoningEffort | None = None
    language_preference: str | None = None

    # Sonar search controls
    search_mode: SearchMode | None = None
    search_domain_filter: List[str] | None = None
    search_recency_filter: SearchRecency | None = None
    search_after_date_filter: str | None = None
    search_before_date_filter: str | None = None
    last_updated_after_filter: str | None = None
    last_updated_before_filter: str | None = None
    search_language_filter: List[str] | None = None
    enable_search_classifier: bool | None = None
    disable_search: bool | None = None
    web_search_options: PerplexityWebSearchOptions | None = None

    # Rich response controls
    return_images: bool | None = None
    return_related_questions: bool | None = None
    image_domain_filter: List[str] | None = None
    image_format_filter: List[str] | None = None
    media_response: PerplexityMediaResponse | None = None

    # Structured outputs
    response_format: PerplexityResponseFormat | Dict[str, Any] | None = None

    # Internal app state
    citations: bool = True

    @staticmethod
    def _ensure_date_format(value: str, field_name: str) -> None:
        try:
            datetime.strptime(value, "%m/%d/%Y")
        except ValueError as exc:
            raise ValueError(f"{field_name} must use MM/DD/YYYY format") from exc

    @staticmethod
    def _is_https_url(value: str) -> bool:
        return value.startswith("https://")

    @staticmethod
    def _estimate_base64_bytes(value: str) -> int | None:
        clean = re.sub(r"\s+", "", value or "")
        if not clean:
            return None

        if clean.startswith("data:"):
            parts = clean.split(",", 1)
            if len(parts) != 2:
                return None
            clean = parts[1]

        if not re.fullmatch(r"[A-Za-z0-9+/=]+", clean):
            return None

        padding = len(clean) - len(clean.rstrip("="))
        return max((len(clean) * 3) // 4 - padding, 0)

    @staticmethod
    def _validate_image_url_or_data_uri(value: str) -> None:
        if PerplexityChatReq._is_https_url(value):
            return

        match = re.match(r"^data:image/([a-zA-Z0-9.+-]+);base64,", value)
        if not match:
            raise ValueError(
                "image_url.url must be an https URL or a data:image/{png|jpeg|webp|gif};base64,... URI"
            )

        subtype = match.group(1).lower()
        if subtype not in _ALLOWED_IMAGE_MIME_SUBTYPES:
            raise ValueError("image_url data URI must use one of: png, jpeg, webp, gif")

        size_bytes = PerplexityChatReq._estimate_base64_bytes(value)
        if size_bytes is not None and size_bytes > _MAX_IMAGE_BYTES:
            raise ValueError("Each image must be <= 50MB")

    @staticmethod
    def _validate_file_url_or_base64(value: str) -> None:
        if value.startswith("data:"):
            raise ValueError(
                "file_url.url must be either an https URL or raw base64 without a data: prefix"
            )

        if PerplexityChatReq._is_https_url(value):
            lowered = value.lower()
            if not any(ext in lowered for ext in _ALLOWED_FILE_EXTENSIONS):
                return
            return

        size_bytes = PerplexityChatReq._estimate_base64_bytes(value)
        if size_bytes is None:
            raise ValueError(
                "file_url.url must be either an https URL or raw base64 without a data: prefix"
            )
        if size_bytes > _MAX_FILE_BYTES:
            raise ValueError("Each file must be <= 50MB")

    def sanitize_for_model(self) -> None:
        if not self.messages:
            raise ValueError("Perplexity Sonar requires at least one message")

        if self.max_tokens is not None and not (0 < self.max_tokens <= 128000):
            raise ValueError("max_tokens must be between 1 and 128000")

        if self.temperature is not None and not (0 <= self.temperature <= 2):
            raise ValueError("temperature must be between 0 and 2")

        if self.top_p is not None and not (0 <= self.top_p <= 1):
            raise ValueError("top_p must be between 0 and 1")

        if self.stream_mode is not None and not self.stream:
            raise ValueError("stream_mode can only be used when stream=True")

        if self.stop is not None:
            stop_values = [self.stop] if isinstance(self.stop, str) else list(self.stop)
            if not stop_values:
                raise ValueError("stop cannot be empty")
            if any(not isinstance(x, str) or not x for x in stop_values):
                raise ValueError("stop must contain non-empty strings only")

        if self.language_preference is not None and not re.fullmatch(r"[a-z]{2}", self.language_preference):
            raise ValueError(
                "language_preference must be a lowercase ISO 639-1 code, for example 'en'"
            )

        if self.search_domain_filter:
            if len(self.search_domain_filter) > 20:
                raise ValueError("search_domain_filter supports at most 20 entries")

            has_allow = any(not value.startswith("-") for value in self.search_domain_filter if value)
            has_deny = any(value.startswith("-") for value in self.search_domain_filter if value)
            if has_allow and has_deny:
                raise ValueError(
                    "search_domain_filter must use either allowlist OR denylist mode, not both"
                )
            if any(not value or value in {"-", " -"} for value in self.search_domain_filter):
                raise ValueError("search_domain_filter entries must be non-empty")

        if self.search_language_filter:
            if len(self.search_language_filter) > 10:
                raise ValueError("search_language_filter supports at most 10 entries")
            invalid_languages = [
                x for x in self.search_language_filter if not re.fullmatch(r"[a-z]{2}", x or "")
            ]
            if invalid_languages:
                raise ValueError(
                    "search_language_filter entries must be lowercase ISO 639-1 codes such as 'en' or 'fr'"
                )

        if self.image_domain_filter:
            if len(self.image_domain_filter) > 10:
                raise ValueError("image_domain_filter supports at most 10 entries")
            if not self.return_images:
                raise ValueError("image_domain_filter requires return_images=True")

        if self.image_format_filter:
            if len(self.image_format_filter) > 10:
                raise ValueError("image_format_filter supports at most 10 entries")
            if not self.return_images:
                raise ValueError("image_format_filter requires return_images=True")

            normalized_formats = []
            for value in self.image_format_filter:
                normalized = (value or "").strip().lower().lstrip(".")
                if normalized not in _ALLOWED_IMAGE_FORMAT_FILTERS:
                    raise ValueError(
                        "image_format_filter values must be one of: gif, jpeg, png, webp"
                    )
                normalized_formats.append(normalized)
            self.image_format_filter = normalized_formats

        date_fields = {
            "search_after_date_filter": self.search_after_date_filter,
            "search_before_date_filter": self.search_before_date_filter,
            "last_updated_after_filter": self.last_updated_after_filter,
            "last_updated_before_filter": self.last_updated_before_filter,
        }
        for field_name, value in date_fields.items():
            if value:
                self._ensure_date_format(value, field_name)

        if self.search_recency_filter and any(date_fields.values()):
            raise ValueError("search_recency_filter cannot be combined with absolute date filters")

        if self.web_search_options and self.web_search_options.user_location:
            location = self.web_search_options.user_location
            if location.latitude is not None and not (-90 <= location.latitude <= 90):
                raise ValueError("user_location.latitude must be between -90 and 90")
            if location.longitude is not None and not (-180 <= location.longitude <= 180):
                raise ValueError("user_location.longitude must be between -180 and 180")
            if (location.latitude is not None or location.longitude is not None) and not location.country:
                raise ValueError("user_location.country is required when latitude/longitude are provided")

        search_type = self.web_search_options.search_type if self.web_search_options else None
        if search_type == "pro" and not self.stream:
            raise ValueError("Perplexity Pro Search requires stream=True")

        file_parts_count = 0
        for message in self.messages:
            content = message.content
            if isinstance(content, str):
                continue

            for part in content:
                if isinstance(part, PerplexityTextPart):
                    if not part.text:
                        raise ValueError("text parts must contain non-empty text")
                    continue

                if isinstance(part, PerplexityImageURLPart):
                    self._validate_image_url_or_data_uri(part.image_url.url)
                    continue

                if isinstance(part, PerplexityFileURLPart):
                    file_parts_count += 1
                    self._validate_file_url_or_base64(part.file_url.url)
                    continue

        if file_parts_count > _MAX_FILES_PER_REQUEST:
            raise ValueError("At most 30 file_url parts are allowed per request")


class PerplexityChatResult(BaseModel):
    text: str = ""
    citations: List[str] = Field(default_factory=list)
    search_results: List[PerplexitySearchResult] = Field(default_factory=list)
    images: List[PerplexityImageResult] = Field(default_factory=list)
    videos: List[PerplexityVideoResult] = Field(default_factory=list)
    related_questions: List[str] = Field(default_factory=list)
    usage: PerplexityUsage | None = None
    search_metadata: PerplexitySearchMetadata | None = None
    finish_reason: str | None = None
    model: str | None = None
    raw_response: Dict[str, Any] | None = None


class PerplexityAsyncRequestSummary(BaseModel):
    id: str
    model: str
    created_at: int
    status: AsyncStatus
    started_at: int | None = None
    completed_at: int | None = None
    failed_at: int | None = None


class PerplexityAsyncListResult(BaseModel):
    requests: List[PerplexityAsyncRequestSummary] = Field(default_factory=list)
    next_token: str | None = None
    raw_response: Dict[str, Any] | None = None


class PerplexityAsyncRequestResult(BaseModel):
    id: str
    model: str
    created_at: int
    status: AsyncStatus
    started_at: int | None = None
    completed_at: int | None = None
    failed_at: int | None = None
    response: PerplexityChatResult | None = None
    raw_response: Dict[str, Any] | None = None