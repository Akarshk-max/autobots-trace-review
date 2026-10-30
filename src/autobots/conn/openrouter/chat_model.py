from typing import Any, Dict, Iterable, List, Optional, Union

import httpx
from openai._types import Body, Headers, Query
from openai.types.chat import (
    ChatCompletionAudioParam,
    ChatCompletionPredictionContentParam,
    ChatCompletionStreamOptionsParam,
    ChatCompletionToolChoiceOptionParam,
    ChatCompletionToolParam,
    completion_create_params,
)
from openai.types.shared_params import Metadata
from pydantic import BaseModel, ConfigDict, Field

from src.autobots.conn.openai.openai_chat.chat_model import OutputType, StructuredOutputSchema
from src.autobots.data_model.strip_deprecated_model import StripDeprecatedModel


class OpenRouterGeneratedFileRef(BaseModel):
    s3_url: str
    file_id: str | None = None
    file_name: str | None = None


class OpenRouterTurnRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn: int
    messages: List[Dict[str, Any]] = Field(default_factory=list)


class OpenRouterStoredToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_call_id: str
    name: str
    chat_turn: int
    step_index: int

    input: Dict[str, Any] = Field(default_factory=dict)
    output_text: str | None = None
    is_error: bool = False

    urls: List[str] = Field(default_factory=list)
    generated_files: List[OpenRouterGeneratedFileRef | Dict[str, Any]] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)


class OpenRouterChatReq(StripDeprecatedModel):

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    # We store messages as plain dicts to remain compatible with OpenRouter extensions
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    model: str = "openai/gpt-4o"

    audio: ChatCompletionAudioParam | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    temperature: float | None = None
    top_p: float | None = None
    n: int | None = 1
    stop: Union[str, List[str], None] = None
    seed: int | None = None

    max_tokens: int | None = None
    max_completion_tokens: int | None = None

    # Tool calling
    parallel_tool_calls: bool | None = None
    tool_choice: ChatCompletionToolChoiceOptionParam | None = None
    tools: List[ChatCompletionToolParam | str | Dict[str, Any]] | None = None

    # Streaming
    stream: bool = False
    stream_options: ChatCompletionStreamOptionsParam | None = None

    # Prediction / response formatting
    prediction: ChatCompletionPredictionContentParam | None = None
    response_format: completion_create_params.ResponseFormat | None = None

    output_type: OutputType | None = Field(default=None, exclude=True)
    json_schema: Dict[str, Any] | None = Field(default=None, exclude=True)
    response_schema: StructuredOutputSchema | Dict[str, Any] | None = Field(
        default=None, exclude=True
    )
    structured_output_strict: bool = Field(default=True, exclude=True)
    structured_output_name: str = Field(default="structured_output", min_length=1, exclude=True)
    metadata: Metadata | None = None
    user: str | None = None


    session_id: str | None = None
    # Request-level overrides
    timeout: float | httpx.Timeout | None = None
    extra_headers: Headers | None = None
    extra_query: Query | None = None
    extra_body: Body | None = None


    # Default behavior: enabled.
    context_compression_enabled: bool = True

    reasoning_exclude: bool = True
    visible_turns: int = Field(default=5, ge=1, le=200)
    include_previous_tool_messages: bool = True
    previous_tool_messages_turns: int | None = Field(default=5, ge=1, le=200)

    pinned_messages: List[Dict[str, Any]] = Field(default_factory=list)
    turn_history: List[OpenRouterTurnRecord | Dict[str, Any]] = Field(default_factory=list)
    chat_turn: int = 0

    tool_results: List[OpenRouterStoredToolResult | Dict[str, Any]] = Field(default_factory=list)
    tool_results_history: List[OpenRouterStoredToolResult | Dict[str, Any]] = Field(default_factory=list)

    current_turn_urls: List[str] = Field(default_factory=list)
    all_turn_urls: List[str] = Field(default_factory=list)

    vector_store_enabled: bool = True
    vector_store_id: str | None = None
    vector_store_name: str | None = None
    vector_store_manifest: List[Dict[str, Any]] = Field(default_factory=list)
    current_turn_vector_store_urls: List[str] = Field(default_factory=list)

    history_vector_store_enabled: bool = True
    history_vector_store_id: str | None = None
    history_vector_store_name: str | None = None
    history_vector_store_index_every_n_turns: int = Field(default=5, ge=1, le=200)
    history_vector_store_last_indexed_turn: int = 0

    finalize_stream: bool = True

    generated_files: List[OpenRouterGeneratedFileRef | Dict[str, Any]] = Field(default_factory=list)

    def model_dump(self, *args, **kwargs):
        for i in range(len(self.messages)):
            if isinstance(self.messages[i], dict):
                for message_field in self.messages[i]:
                    message_field_value = self.messages[i][message_field]
                    if (
                        isinstance(message_field_value, Iterable)
                        and not isinstance(message_field_value, (str, dict, list))
                        and isinstance(message_field, str)
                    ):
                        self.messages[i][message_field] = list(message_field_value)
        return super().model_dump(*args, **kwargs)

    def get_structured_output_format(self) -> Optional[Dict[str, Any]]:
        if self.json_schema:
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": self.structured_output_name,
                    "schema": self.json_schema,
                    "strict": self.structured_output_strict,
                },
            }

        if self.response_schema:
            schema: Any = self.response_schema
            if isinstance(schema, StructuredOutputSchema):
                schema = schema.model_dump(exclude_none=True, exclude={"strict"})
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": self.structured_output_name,
                    "schema": schema,
                    "strict": self.structured_output_strict,
                },
            }

        if self.output_type == OutputType.json:
            return {"type": "json_object"}

        return None

    def to_openrouter_api_payload(self) -> dict[str, Any]:
        payload = self.model_dump(exclude_none=True)
        for key in (
            "context_compression_enabled",
            "reasoning_exclude",
            "session_id",
            "visible_turns",
            "include_previous_tool_messages",
            "previous_tool_messages_turns",
            "pinned_messages",
            "turn_history",
            "chat_turn",
            "tool_results",
            "tool_results_history",
            "current_turn_urls",
            "all_turn_urls",
            "vector_store_enabled",
            "vector_store_id",
            "vector_store_name",
            "vector_store_manifest",
            "current_turn_vector_store_urls",
            "history_vector_store_enabled",
            "history_vector_store_id",
            "history_vector_store_name",
            "history_vector_store_index_every_n_turns",
            "history_vector_store_last_indexed_turn",
            "finalize_stream",
            "generated_files",
        ):
            payload.pop(key, None)
        return payload
