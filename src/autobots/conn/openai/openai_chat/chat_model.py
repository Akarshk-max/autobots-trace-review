from enum import Enum
from typing import Any, ClassVar, Dict, Iterable, List, Literal, Optional, Set, Union

import httpx
from openai._types import Body, Headers, Query
from openai.types import ChatModel
from openai.types.chat import (
    ChatCompletionAudioParam,
    ChatCompletionMessageParam,
    ChatCompletionPredictionContentParam,
    ChatCompletionStreamOptionsParam,
    ChatCompletionToolChoiceOptionParam,
    ChatCompletionToolParam,
    completion_create_params,
)
from openai.types.shared import ReasoningEffort
from openai.types.shared_params import Metadata
from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import Self

from src.autobots.conn.openai.openai_common_models import OPENAI_MODELS
from src.autobots.data_model.strip_deprecated_model import StripDeprecatedModel


class Role(str, Enum):
    system = "system"
    user = "user"
    assistant = "assistant"


# DEPRECATED: Custom ReasoningEffort - replaced with OpenAI's ReasoningEffort
# class ReasoningEffort(str, Enum):
#     low = "low"
#     medium = "medium"
#     high = "high"


class OutputType(str, Enum):
    text = "text"
    json = "json"
    markdown = "markdown"


class Message(BaseModel):
    role: Role
    content: Union[str, List[Dict]]


class StructuredOutputSchema(BaseModel):
    """Schema for structured outputs using JSON Schema."""

    type: str = "object"
    properties: Dict[str, Any]
    required: Optional[List[str]] = None
    description: Optional[str] = None
    strict: bool = True


# OLD ChatReq class - commented out for reference
# class ChatReq(StripDeprecatedModel):
#     messages: List[ChatCompletionMessageParam]
#     model: ChatModel | OPENAI_MODELS | str = "gpt-4.1-mini"
#     audio: Optional[ChatCompletionAudioParam] = None
#     frequency_penalty: Optional[float] = None
#     # function_call: completion_create_params.FunctionCall = None
#     # functions: List[completion_create_params.Function] = None
#     logit_bias: Optional[Dict[str, int]] = None
#     logprobs: Optional[bool] = None
#     max_completion_tokens: Optional[int] = None
#     max_tokens: Optional[int] = Field(default=None, exclude=True)  # deprecated
#     metadata: Optional[Metadata] = None
#     modalities: Optional[List[Literal["text", "audio"]]] = None
#     n: Optional[int] = 1
#     parallel_tool_calls: bool | None = None
#     prediction: Optional[ChatCompletionPredictionContentParam] = None
#     presence_penalty: Optional[float] = None
#     prompt_cache_key: str | None = None
#     reasoning_effort: Optional[ReasoningEffort] = None
#     response_format: Optional[completion_create_params.ResponseFormat] = None
#     safety_identifier: str | None = None
#     seed: Optional[int] = None
#     service_tier: Optional[Literal["auto", "default", "flex", "scale", "priority"]] = None
#     stop: Union[Optional[str], List[str], None] = None
#     store: Optional[bool] = None
#     stream: Optional[Literal[False]] | Literal[True] = False
#     stream_options: Optional[ChatCompletionStreamOptionsParam] = None
#     temperature: Optional[float] = None
#     tool_choice: ChatCompletionToolChoiceOptionParam | None = None
#     tools: List[ChatCompletionToolParam | str] | None = None
#     top_logprobs: Optional[int] = None
#     top_p: Optional[float] = None
#     user: str | None = None
#     verbosity: Optional[Literal["low", "medium", "high"]] = None
#     web_search_options: completion_create_params.WebSearchOptions | None = None
#
#     # deprecated — will never be emitted by model_dump / model_dump_json
#     output_type: Optional[OutputType] = Field(default=None, exclude=True)  # deprecated
#     json_schema: Optional[Dict[str, Any]] = Field(default=None, exclude=True)  # deprecated
#     response_schema: Optional[Union[StructuredOutputSchema, Dict[str, Any]]] = Field(
#         default=None, exclude=True
#     )  # deprecated
#
#     # Use the following arguments if you need to pass additional parameters to the API that aren't available via kwargs.
#     # The extra values given here take precedence over values defined on the client or passed to this method.
#     extra_headers: Headers | None = None
#     extra_query: Query | None = None
#     extra_body: Body | None = None
#     timeout: float | httpx.Timeout | None = 900
#     stream : Optional[bool] = False
#
#     _deprecated_fields: ClassVar[Set[str]] = {
#         "output_type",
#         "json_schema",
#         "response_schema",
#         "max_tokens",
#     }
#
#     model_config = ConfigDict(arbitrary_types_allowed=True)
#
#     def model_dump(self, *args, **kwargs):
#         # updating Iterable to list, as Iterable is not compatible with model_dump
#         for i in range(len(self.messages)):
#             if isinstance(self.messages[i], dict):
#                 for message_field in self.messages[i]:
#                     message_field_value = self.messages[i][message_field]
#                     if (
#                         isinstance(message_field_value, Iterable)
#                         and not isinstance(message_field_value, (str, dict, list))
#                         and isinstance(message_field, str)
#                     ):
#                         self.messages[i][message_field] = list(message_field_value)
#         data = super().model_dump(*args, **kwargs)
#         return data
#
#     @classmethod
#     def model_validate(
#         cls,
#         obj: Any,
#         *,
#         strict: bool | None = None,
#         from_attributes: bool | None = None,
#         context: Any | None = None,
#         by_alias: bool | None = None,
#         by_name: bool | None = None,
#     ) -> Self:
#         # updating Iterable to list, as Iterable is not compatible with model_dump
#         data = super().model_validate(
#             obj, strict=strict, from_attributes=from_attributes, context=context
#         )
#         # updating Iterable to list, as Iterable is not compatible with model_dump
#         for i in range(len(data.messages)):
#             if isinstance(data.messages[i], dict):
#                 for message_field in data.messages[i]:
#                     message_field_value = data.messages[i][message_field]
#                     if (
#                         isinstance(message_field_value, Iterable)
#                         and not isinstance(message_field_value, (str, dict, list))
#                         and isinstance(message_field, str)
#                     ):
#                         data.messages[i][message_field] = list(message_field_value)
#         return data
#
#     def get_structured_output_format(self) -> Optional[Dict[str, Any]]:
#         """
#         Returns a properly formatted structured output configuration for OpenAI API
#         """
#         if self.json_schema:
#             return {"type": "json_schema", "schema": self.json_schema, "strict": True}
#         elif self.response_schema:
#             schema = self.response_schema
#             if isinstance(schema, StructuredOutputSchema):
#                 schema = schema.model_dump(exclude_none=True)
#             return {"type": "json_schema", "schema": schema, "strict": True}
#         elif self.output_type == OutputType.json:
#             return {"type": "json_object"}
#         return None


# NEW ChatReq class with OpenAI SDK alignment
class ChatReq(StripDeprecatedModel):
    messages: List[ChatCompletionMessageParam]
    model: ChatModel | OPENAI_MODELS | str = "gpt-5.2"
    audio: Optional[ChatCompletionAudioParam] = None
    frequency_penalty: Optional[float] = None
    # function_call: completion_create_params.FunctionCall = None
    # functions: List[completion_create_params.Function] = None
    logit_bias: Optional[Dict[str, int]] = None
    logprobs: Optional[bool] = None
    max_completion_tokens: Optional[int] = None
    max_tokens: Optional[int] = Field(default=None, exclude=True)  # deprecated
    metadata: Optional[Metadata] = None
    modalities: Optional[List[Literal["text", "audio"]]] = None
    n: Optional[int] = 1
    parallel_tool_calls: bool | None = None
    prediction: Optional[ChatCompletionPredictionContentParam] = None
    presence_penalty: Optional[float] = None
    prompt_cache_key: str | None = None
    prompt_cache_retention: Optional[Literal["in_memory", "24h"]] = (
        None  # NEW: Added from OpenAI SDK
    )
    reasoning_effort: Optional[ReasoningEffort] = "medium"  # UPDATED: Now uses OpenAI's ReasoningEffort, default medium
    response_format: Optional[completion_create_params.ResponseFormat] = None
    safety_identifier: str | None = None
    seed: Optional[int] = None
    service_tier: Optional[Literal["auto", "default", "flex", "scale", "priority"]] = None
    stop: Union[Optional[str], List[str], None] = None
    store: Optional[bool] = None
    stream: Optional[Literal[False]] | Literal[True] = False
    stream_options: Optional[ChatCompletionStreamOptionsParam] = None
    temperature: Optional[float] = None
    tool_choice: ChatCompletionToolChoiceOptionParam | None = None
    tools: List[ChatCompletionToolParam | str] | None = None
    top_logprobs: Optional[int] = None
    top_p: Optional[float] = None
    user: str | None = None
    verbosity: Optional[Literal["low", "medium", "high"]] = None
    web_search_options: completion_create_params.WebSearchOptions | None = None

    # deprecated — will never be emitted by model_dump / model_dump_json
    output_type: Optional[OutputType] = Field(default=None, exclude=True)  # deprecated
    json_schema: Optional[Dict[str, Any]] = Field(default=None, exclude=True)  # deprecated
    response_schema: Optional[Union[StructuredOutputSchema, Dict[str, Any]]] = Field(
        default=None, exclude=True
    )  # deprecated

    # Use the following arguments if you need to pass additional parameters to the API that aren't available via kwargs.
    # The extra values given here take precedence over values defined on the client or passed to this method.
    extra_headers: Headers | None = None
    extra_query: Query | None = None
    extra_body: Body | None = None
    timeout: float | httpx.Timeout | None = 900
    stream: Optional[bool] = False

    _deprecated_fields: ClassVar[Set[str]] = {
        "output_type",
        "json_schema",
        "response_schema",
        "max_tokens",
    }

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def model_dump(self, *args, **kwargs):
        # updating Iterable to list, as Iterable is not compatible with model_dump
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
        data = super().model_dump(*args, **kwargs)
        return data

    @classmethod
    def model_validate(
        cls,
        obj: Any,
        *,
        strict: bool | None = None,
        from_attributes: bool | None = None,
        context: Any | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        # updating Iterable to list, as Iterable is not compatible with model_dump
        data = super().model_validate(
            obj, strict=strict, from_attributes=from_attributes, context=context
        )
        # updating Iterable to list, as Iterable is not compatible with model_dump
        for i in range(len(data.messages)):
            if isinstance(data.messages[i], dict):
                for message_field in data.messages[i]:
                    message_field_value = data.messages[i][message_field]
                    if (
                        isinstance(message_field_value, Iterable)
                        and not isinstance(message_field_value, (str, dict, list))
                        and isinstance(message_field, str)
                    ):
                        data.messages[i][message_field] = list(message_field_value)
        return data

    def get_structured_output_format(self) -> Optional[Dict[str, Any]]:
        """
        Returns a properly formatted structured output configuration for OpenAI API
        """
        if self.json_schema:
            return {"type": "json_schema", "schema": self.json_schema, "strict": True}
        elif self.response_schema:
            schema = self.response_schema
            if isinstance(schema, StructuredOutputSchema):
                schema = schema.model_dump(exclude_none=True)
            return {"type": "json_schema", "schema": schema, "strict": True}
        elif self.output_type == OutputType.json:
            return {"type": "json_object"}
        return None


class Choice(BaseModel):
    index: int
    message: Message
    finish_reason: str


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatRes(BaseModel):
    id: str
    object: str = None
    created: int
    model: str
    choices: List[Choice]
    usage: Usage
