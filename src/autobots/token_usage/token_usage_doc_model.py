from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.space_id import OptionalSpaceId
from src.autobots.data_model.user_id import UserId
from src.autobots.event_result.event_result_model import EventResultStatus


class TokenUsageSource(str, Enum):
    """The world a row came from. A graph or team run is made of action runs, so
    both are `action` here and are found through their typed result ids instead."""

    action = "action"
    ingestion = "ingestion"
    knowledge_base = "knowledge_base"
    sandbox = "sandbox"


class LlmProvider(str, Enum):
    claude = "claude"
    openrouter = "openrouter"
    openai = "openai"
    gemini = "gemini"
    grok = "grok"
    deepseek = "deepseek"
    perplexity = "perplexity"


class LlmSurface(str, Enum):

    claude = "claude"
    openrouter = "openrouter"
    gemini = "gemini"
    grok = "grok"
    openai_responses = "openai_responses"
    openai_chat = "openai_chat"
    deepseek = "deepseek"
    perplexity = "perplexity"


class TokenCounts(BaseModel):
    input_tokens: int = Field(
        default=0,
        description="Input tokens billed at the full rate, excluding cache reads and cache writes.",
    )
    output_tokens: int = 0
    cached_input_tokens: int = Field(
        default=0, description="Input tokens served from the provider's prompt cache."
    )
    cache_write_tokens: int = Field(
        default=0, description="Input tokens written into the provider's prompt cache."
    )
    reasoning_tokens: int = Field(
        default=0, description="Reasoning tokens, already counted inside output_tokens."
    )
    total_tokens: int = 0
    cost: Optional[float] = Field(
        default=None, description="Cost in USD as reported by the provider, when it reports one."
    )


class LlmCallUsage(TokenCounts):
    loop: int
    model: str
    raw: Dict[str, Any] = Field(default_factory=dict)


class TokenUsageDocCreate(UserId, OptionalSpaceId, TokenCounts, CreatedAt):
    provider: LlmProvider
    surface: Optional[LlmSurface] = None
    model: str
    status: EventResultStatus = EventResultStatus.success

    source: TokenUsageSource = TokenUsageSource.action
    source_id: Optional[str] = Field(
        default=None, description="The unit of work this usage belongs to, within its source."
    )

    action_id: Optional[str] = None
    action_run_id: Optional[str] = None
    action_name: Optional[str] = None
    turn: Optional[int] = None

    action_graph_id: Optional[str] = None
    action_graph_result_id: Optional[str] = None
    action_graph_node_id: Optional[str] = None

    action_team_id: Optional[str] = None
    action_team_result_id: Optional[str] = None

    api_call_count: int = 0
    calls: List[LlmCallUsage] = Field(default_factory=list)


class TokenUsageDoc(TokenUsageDocCreate):
    __collection__ = "TokenUsages"

    id: str = Field(..., alias="_id")

    model_config = ConfigDict(populate_by_name=True)


class TokenUsageLiteDoc(OptionalSpaceId, BaseModel):
    id: str = Field(..., alias="_id")
    user_id: str
    provider: LlmProvider
    surface: Optional[LlmSurface] = None
    model: str
    status: EventResultStatus
    source: TokenUsageSource = TokenUsageSource.action
    source_id: Optional[str] = None
    action_id: Optional[str] = None
    action_run_id: Optional[str] = None
    turn: Optional[int] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    cost: Optional[float] = None
    api_call_count: int = 0
    created_at: Any = None

    model_config = ConfigDict(populate_by_name=True)


class TokenUsageDocFind(BaseModel):
    id: Optional[str] = None
    user_id: Optional[str] = None
    space_id: Optional[str] = None
    source: Optional[TokenUsageSource] = None
    source_id: Optional[str] = None
    provider: Optional[LlmProvider] = None
    model: Optional[str] = None
    action_id: Optional[str] = None
    action_run_id: Optional[str] = None
    action_graph_result_id: Optional[str] = None
    action_team_result_id: Optional[str] = None
    status: Optional[EventResultStatus] = None


class TokenUsageDocUpdate(BaseModel):
    id: str
    user_id: str


class TokenUsageDocsFound(DocFindPage):
    docs: List[TokenUsageLiteDoc]
    totals: TokenCounts = Field(default_factory=TokenCounts)
    api_call_count: int = 0
    record_count: int = 0
    by_provider: List["TokenUsageProviderTotals"] = Field(default_factory=list)


class TokenUsageRunType(str, Enum):
    action_run = "action_run"
    action_graph_run = "action_graph_run"
    action_team_run = "action_team_run"


RUN_TYPE_TO_ID_FIELD = {
    TokenUsageRunType.action_run: "action_run_id",
    TokenUsageRunType.action_graph_run: "action_graph_result_id",
    TokenUsageRunType.action_team_run: "action_team_result_id",
}


class TokenUsageProviderTotals(TokenCounts):
    provider: LlmProvider
    api_call_count: int = 0
    record_count: int = 0


class TokenUsageSourceTotals(TokenCounts):
    source: TokenUsageSource
    source_id: str
    api_call_count: int = 0
    record_count: int = 0
    by_provider: List[TokenUsageProviderTotals] = Field(default_factory=list)
    records: List[TokenUsageLiteDoc] = Field(default_factory=list)


class TokenUsageRunTotals(TokenCounts):
    run_id: str
    run_type: TokenUsageRunType
    api_call_count: int = 0
    record_count: int = 0
    by_provider: List[TokenUsageProviderTotals] = Field(default_factory=list)
    records: List[TokenUsageLiteDoc] = Field(default_factory=list)
