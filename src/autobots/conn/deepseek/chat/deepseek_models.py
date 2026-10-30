from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class DeepSeekRole(str, Enum):
    system = "system"
    user = "user"
    assistant = "assistant"
    tool = "tool"


class DeepSeekModelName(str, Enum):
    deepseek_v4_flash = "deepseek-v4-flash"
    deepseek_v4_pro = "deepseek-v4-pro"
    deepseek_v4_flash_vision_exp = "deepseek-v4-flash-vision-exp"


class DeepSeekAttachment(BaseModel):
    """
    DeepSeek has no file/image attachment API in chat.
    We treat attachments as "references" that are injected into the message text.
    """
    kind: Literal["url", "path"] = "url"
    value: str
    filename: Optional[str] = None


class DeepSeekFunction(BaseModel):
    name: str
    arguments: Union[str, Dict[str, Any]] = "{}"


class DeepSeekToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: DeepSeekFunction


class DeepSeekMessage(BaseModel):
    role: DeepSeekRole
    content: str = ""

    # Tool messages:
    tool_call_id: Optional[str] = None

    # Assistant tool calls:
    tool_calls: List[DeepSeekToolCall] = Field(default_factory=list)

    # Thinking mode outputs:
    reasoning_content: Optional[str] = None

    # Convenience: non-native attachments
    attachments: List[DeepSeekAttachment] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    def to_openai_dict(self, *, strip_reasoning: bool = False, thinking_enabled: bool = False) -> dict:
        d = {
            "role": self.role,
            "content": self.content or "",
        }

        # tool call fields
        if getattr(self, "tool_calls", None):
            d["tool_calls"] = self.tool_calls

        if getattr(self, "tool_call_id", None):
            d["tool_call_id"] = self.tool_call_id

        # DeepSeek requires the FIELD to exist on assistant messages in thinking mode
        if thinking_enabled and self.role == "assistant":
            if strip_reasoning:
                d["reasoning_content"] = ""   # must exist, can be empty
            else:
                d["reasoning_content"] = self.reasoning_content or ""

        return d


class DeepSeekToolSpec(BaseModel):
    """
    OpenAI-compatible tool definition for chat.completions.create(tools=[...])
    DeepSeek supports:
      - type=function
      - function.name/description/parameters
      - strict (beta)
    """
    type: Literal["function"] = "function"
    function: Dict[str, Any] = Field(default_factory=dict)
    strict: bool = False

    model_config = ConfigDict(extra="forbid")


class DeepSeekResponseFormat(BaseModel):
    type: Literal["text", "json_object"] = "text"


class DeepSeekThinking(BaseModel):
    # DeepSeek "thinking mode" is provided through extra_body when using OpenAI SDK.
    # docs: {"thinking": {"type": "enabled"}} or "disabled"
    type: Literal["enabled", "disabled"] = "disabled"


class DeepSeekUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0


class DeepSeekChatRequest(BaseModel):
    model: Union[DeepSeekModelName, str] = DeepSeekModelName.deepseek_v4_flash
    messages: List[DeepSeekMessage] = Field(default_factory=list)

    # Generation
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 2048
    top_p: Optional[float] = 1.0
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    stop: Optional[List[str]] = None

    # Tools
    tools: List[DeepSeekToolSpec] = Field(default_factory=list)
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None  # "none"|"auto"|"required"| named tool dict

    # Output control
    response_format: Optional[DeepSeekResponseFormat] = None

    # Thinking mode (applies via extra_body)
    thinking: DeepSeekThinking = Field(default_factory=DeepSeekThinking)

    # Streaming
    stream: bool = False

    # Behavior
    strip_reasoning_in_history: bool = True  # recommended default

    # Network / timeouts
    timeout_sec: float = 420.0

    model_config = ConfigDict(extra="forbid")


class DeepSeekChatResult(BaseModel):
    content: str = ""
    model: str = ""

    tool_calls: List[Dict[str, Any]] = Field(default_factory=list)
    reasoning_content: Optional[str] = None
    usage: Optional[DeepSeekUsage] = None

    raw: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="allow")
