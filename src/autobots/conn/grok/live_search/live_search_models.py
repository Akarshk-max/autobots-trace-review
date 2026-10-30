from datetime import datetime
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


# Exceptions classes
class XAILiveSearchError(Exception):
    """Base exception for live search operations."""


class XAILiveSearchClientError(XAILiveSearchError):
    """Raised when client creation/usage fails."""


class XAILiveSearchRequestError(XAILiveSearchError):
    """Raised when request/config is invalid."""


class XAILiveSearchRunError(XAILiveSearchError):
    """Raised when live search fails."""


SearchMode = Literal["off", "auto", "on"]


# Pydantic models for live search grok classes
class WebSourceConfig(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    type: Literal["web"] = "web"
    country: Optional[str] = None
    excluded_websites: list[str] = Field(default_factory=list)


class NewsSourceConfig(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    type: Literal["news"] = "news"
    country: Optional[str] = None
    excluded_websites: list[str] = Field(default_factory=list)


class XSourceConfig(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    type: Literal["x"] = "x"
    included_x_handles: list[str] = Field(default_factory=list)
    excluded_x_handles: list[str] = Field(default_factory=list)
    post_favorite_count: Optional[int] = None


class RSSSourceConfig(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    type: Literal["rss"] = "rss"
    links: list[str] = Field(default_factory=list)


SearchSourceConfig = Annotated[
    Union[WebSourceConfig, NewsSourceConfig, XSourceConfig, RSSSourceConfig],
    Field(discriminator="type"),
]


class LiveSearchParametersConfig(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    mode: SearchMode = "auto"
    return_citations: bool = True

    from_date: Optional[datetime] = None
    to_date: Optional[datetime] = None

    sources: Optional[list[SearchSourceConfig]] = None


class LiveSearchRequest(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    prompt: str
    model: str = "grok-4.3"
    search_parameters: LiveSearchParametersConfig = Field(
        default_factory=LiveSearchParametersConfig
    )


class LiveSearchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    request: LiveSearchRequest
    created_at: datetime = Field(default_factory=datetime.utcnow)

    content: str
    citations: list[str] = Field(default_factory=list)

    num_sources_used: Optional[int] = None


class LiveSearchStreamResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    request: LiveSearchRequest
    created_at: datetime = Field(default_factory=datetime.utcnow)

    content: str
    chunks: list[str] = Field(default_factory=list)

    citations: list[str] = Field(default_factory=list)
    num_sources_used: Optional[int] = None
