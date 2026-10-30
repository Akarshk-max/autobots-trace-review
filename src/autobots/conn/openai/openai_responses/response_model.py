from typing import Any, Dict, List, Literal, Optional, Union

from openai.types import Metadata, Reasoning
from openai.types.responses import (
    ResponseIncludable,
    ResponseInputParam,
    ResponsePromptParam,
    ResponseTextConfigParam,
    ToolParam,
    response_create_params,
)
from pydantic import BaseModel, model_validator

from src.autobots.conn.openai.openai_responses.openai_responses_models import RESPONSES_MODELS


def is_iterator_like(obj: Any) -> bool:
    """True for generators, ValidatorIterator, etc."""
    return (
        hasattr(obj, "__iter__")
        and not isinstance(obj, (str, bytes, dict, list, tuple, BaseModel))
        and not hasattr(obj, "__len__")
    )


def convert_iterators_to_lists(obj: Any) -> Any:
    if is_iterator_like(obj):
        try:
            return [convert_iterators_to_lists(v) for v in obj]
        except Exception:
            return list(obj)  # fallback
    elif isinstance(obj, BaseModel):
        for name, value in obj.__dict__.items():
            setattr(obj, name, convert_iterators_to_lists(value))
        return obj
    elif isinstance(obj, dict):
        return {k: convert_iterators_to_lists(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [convert_iterators_to_lists(v) for v in obj]
    elif hasattr(obj, "__dict__"):  # handle custom types
        for k, v in vars(obj).items():
            setattr(obj, k, convert_iterators_to_lists(v))
        return obj
    else:
        return obj


class OpenaiResponsesReq(BaseModel):
    background: Optional[bool] | None = None
    context_management: Optional[List[response_create_params.ContextManagement]] | None = [
        {"type": "compaction", "compact_threshold": 200000}
    ]
    include: Optional[List[ResponseIncludable]] | None = None
    input: Union[str, ResponseInputParam]  # | None = None
    instructions: Optional[str] | None = None
    max_output_tokens: Optional[int] | None = None
    max_tool_calls: Optional[int] | None = None
    metadata: Optional[Metadata] | None = None
    model: RESPONSES_MODELS = "gpt-5.2"
    parallel_tool_calls: Optional[bool] | None = None
    previous_response_id: Optional[str] | None = None
    prompt: Optional[ResponsePromptParam] | None = None
    reasoning: Optional[Reasoning] | None = Reasoning(effort="medium")
    service_tier: Optional[Literal["auto", "default", "flex", "scale", "priority"]] | None = None
    store: Optional[bool] | None = None
    stream: Optional[Literal[False]] | Literal[True] | None = None
    temperature: Optional[float] | None = None
    text: ResponseTextConfigParam | None = None
    tool_choice: response_create_params.ToolChoice | None = None
    tools: List[ToolParam | str] | None = None
    top_logprobs: Optional[int] | None = None
    top_p: Optional[float] | None = None
    truncation: Optional[Literal["auto", "disabled"]] | None = None
    user: str | None = None
    # Use the following arguments if you need to pass additional parameters to the API that aren't available via kwargs.
    # The extra values given here take precedence over values defined on the client or passed to this method.
    # extra_headers: Headers | None = None,
    # extra_query: Query | None = None,
    # extra_body: Body | None = None,
    # timeout: float | None = None,
    extra_headers: Dict[str, str] | None = None
    extra_query: Dict[str, str] | None = None
    extra_body: Dict[str, str] | None = None
    timeout: float | None = None

    @model_validator(mode="after")
    def convert_iterators(self):
        _ = convert_iterators_to_lists(self)
        return self


# class AzureOpenaiResponsesReq(OpenaiResponsesReq):
#     model: OPENAI_MODELS = "o3-pro"

# model: OPENAI_MODELS = "model-router"
# messages: List[Dict[str, Any]]
# temperature: float | None = None
# top_p: float | None = None
# n: int | None = None
# stream: bool = False
# stop: str | List[str] | None = None
# max_tokens: int | None = None
# presence_penalty: float | None = None
# frequency_penalty: float | None = None
# logit_bias: Dict[str, float] | None = None
# user: str | None = None
# response_format: Dict[str, str] | None = None
# seed: int | None = None
# tools: List[Dict[Any, Any]] | None = None
# tool_choice: str | Dict[str, Any] | None = None
# # Responses API specific parameters
# previous_response_id: str | None = None
# truncation: Literal["auto", "disabled"] | None = None
# store: bool | None = None
# web_search_settings: Dict[str, Any] | None = None
# file_search_settings: Dict[str, Any] | None = None
# computer_use_settings: Dict[str, Any] | None = None


class ResponseUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ResponseToolCall(BaseModel):
    id: str
    type: str = "function"
    function: Dict[str, str]


class ResponseMessage(BaseModel):
    role: Literal["assistant", "system", "user", "tool"]
    content: str | None
    tool_calls: List[ResponseToolCall] | None = None
    name: str | None = None
    function_call: Dict[str, str] | None = None


class ResponseChoice(BaseModel):
    index: int
    message: ResponseMessage
    finish_reason: str | None


class WebSearchSource(BaseModel):
    domain: str
    url: str
    title: str | None = None
    extract: str | None = None


class WebSearchResult(BaseModel):
    search_query: str
    results: List[WebSearchSource]


class FileSearchResult(BaseModel):
    file_id: str
    file_name: str
    extract: str


class ComputerUseAction(BaseModel):
    type: str
    parameters: Dict[str, Any]


class ToolResult(BaseModel):
    tool_call_id: str
    web_search: WebSearchResult | None = None
    file_search: List[FileSearchResult] | None = None
    computer_use: ComputerUseAction | None = None


# class Response(BaseModel):
#     id: str
#     object: str = "response"
#     created: int
#     model: str
#     choices: List[ResponseChoice]
#     usage: ResponseUsage
#     system_fingerprint: str | None = None
#     tool_results: List[ToolResult] | None = None
#     previous_response_id: str | None = None
