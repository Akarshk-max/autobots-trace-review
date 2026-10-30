import json
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.autobots.conn.grok.live_search.live_search_models import LiveSearchParametersConfig


class GrokModelName(str, Enum):
    grok_4_6 = "grok-4.6"
    grok_4_5 = "grok-4.5"
    grok_4_3 = "grok-4.3"
    grok_4_20_0309_reasoning = "grok-4.20-0309-reasoning"
    grok_4_20_0309_non_reasoning = "grok-4.20-0309-non-reasoning"
    grok_4_20_multi_agent_0309 = "grok-4.20-multi-agent-0309"
    grok_build_0_1 = "grok-build-0.1"
    grok_code_fast_1 = "grok-code-fast-1"
    grok_2_1212 = "grok-2-1212"
    grok_2_vision_1212 = "grok-2-vision-1212"


class GrokRole(str, Enum):
    system = "system"
    user = "user"
    assistant = "assistant"


# Model registry
# This pydantic model handles capabalities for the grok models
class ModelInfo(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    aliases: List[str] = Field(default_factory=list)
    input_modalities: List[int] = Field(default_factory=list)  # 1=text, 2=image
    output_modalities: List[int] = Field(default_factory=list)
    max_prompt_length: int = 131072


class ModelRegistry(BaseModel):
    language_models: List[ModelInfo] = Field(default_factory=list)
    image_generation_models: List[ModelInfo] = Field(default_factory=list)

    def resolve(self, name_or_alias: str) -> str:
        needle = (name_or_alias or "").strip()
        if not needle:
            raise ValueError("Model name is empty")

        for m in self.language_models + self.image_generation_models:
            if m.name == needle:
                return m.name

        for m in self.language_models + self.image_generation_models:
            if needle in (m.aliases or []):
                return m.name

        return needle

    def get(self, name_or_alias: str) -> Optional[ModelInfo]:
        resolved = self.resolve(name_or_alias)
        for m in self.language_models + self.image_generation_models:
            if m.name == resolved:
                return m
        return None

    def supports_vision(self, name_or_alias: str) -> bool:
        m = self.get(name_or_alias)
        if not m:
            return False
        return 2 in (m.input_modalities or [])

    def max_prompt(self, name_or_alias: str, default: int = 131072) -> int:
        m = self.get(name_or_alias)
        return int(getattr(m, "max_prompt_length", default) or default)


def _parse_modalities(val: Any) -> List[int]:
    if val is None:
        return []
    if isinstance(val, list):
        return [int(x) for x in val if str(x).isdigit()]
    if isinstance(val, str):
        s = val.strip()
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [int(x) for x in parsed if str(x).isdigit()]
        except Exception:
            pass
        out: List[int] = []
        for ch in s:
            if ch.isdigit():
                out.append(int(ch))
        return out
    return []


# this is just i build from official website
# this is url - https://github.com/xai-org/xai-sdk-python/blob/main/examples/aio/models.py
# we can build same thing just tweaking few things in given code and this will be we can get easily
DEFAULT_MODEL_REGISTRY = ModelRegistry(
    language_models=[
        ModelInfo(
            name="grok-2-1212",
            aliases=["grok-2", "grok-2-latest"],
            input_modalities=_parse_modalities("[1]"),
            output_modalities=_parse_modalities("[1]"),
            max_prompt_length=131072,
        ),
        ModelInfo(
            name="grok-2-vision-1212",
            aliases=["grok-2-vision", "grok-2-vision-latest"],
            input_modalities=_parse_modalities("[1, 2]"),
            output_modalities=_parse_modalities("[1]"),
            max_prompt_length=32768,
        ),
        ModelInfo(
            name="grok-build-0.1",
            aliases=["grok-code-fast-1", "grok-code-fast", "grok-code-fast-1-0825"],
            input_modalities=_parse_modalities("[1, 2]"),
            output_modalities=_parse_modalities("[1]"),
            max_prompt_length=256000,
        ),
        ModelInfo(
            name="grok-4.20-0309-reasoning",
            aliases=["grok-4.20", "grok-4.20-reasoning", "grok-4.20-reasoning-latest"],
            input_modalities=_parse_modalities("[1, 2]"),
            output_modalities=_parse_modalities("[1]"),
            max_prompt_length=1_000_000,
        ),
        ModelInfo(
            name="grok-4.20-0309-non-reasoning",
            aliases=["grok-4.20-non-reasoning", "grok-4.20-non-reasoning-latest"],
            input_modalities=_parse_modalities("[1, 2]"),
            output_modalities=_parse_modalities("[1]"),
            max_prompt_length=1_000_000,
        ),
        ModelInfo(
            name="grok-4.20-multi-agent-0309",
            input_modalities=_parse_modalities("[1, 2]"),
            output_modalities=_parse_modalities("[1]"),
            max_prompt_length=1_000_000,
        ),
        ModelInfo(
            name="grok-4.3",
            aliases=["grok-4.3-latest"],
            input_modalities=_parse_modalities("[1, 2]"),
            output_modalities=_parse_modalities("[1]"),
            max_prompt_length=1_000_000,
        ),
        ModelInfo(
            name="grok-4.5",
            aliases=["grok-4.5-latest", "grok-build-latest"],
            input_modalities=_parse_modalities("[1, 2]"),
            output_modalities=_parse_modalities("[1]"),
            max_prompt_length=500_000,
        ),
        ModelInfo(
            name="grok-4.6",
            input_modalities=_parse_modalities("[1, 2]"),
            output_modalities=_parse_modalities("[1]"),
            max_prompt_length=500_000,
        ),
    ]
)


# Attachments Pydantic Models for files and images both combinly


class GrokAttachment(BaseModel):
    """
    Attachments are always uploaded to xAI Files and referenced via file_id in chat.
    For images, just attach the image file_id and use a vision-capable model.
    """

    kind: Literal["file_id", "url", "path"] = "url"
    value: str
    filename: Optional[str] = None

    # If None, we auto-detect from extension for url/path.
    # is given url image or not
    is_image: Optional[bool] = None
    detail: Literal["high", "low"] = "low"


class GrokMessage(BaseModel):
    role: GrokRole
    content: str = ""
    attachments: List[GrokAttachment] = Field(default_factory=list)
    model_config = ConfigDict(arbitrary_types_allowed=True)


class GrokTurnRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    turn: int
    messages: List[GrokMessage] = Field(default_factory=list)


class GrokServerToolsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    web_search: bool = False
    x_search: bool = False
    code_execution: bool = True


# Live search integration pydantic model
class LiveSearchConfig(BaseModel):
    enabled: bool = False
    parameters: LiveSearchParametersConfig = Field(default_factory=LiveSearchParametersConfig)


# Tools
class CustomToolSpec(BaseModel):
    """
    Client-side function calling tool:
    Grok returns tool_calls, we execute, then respond with tool_result().
    """

    name: str
    description: str = ""
    parameters_schema: Dict[str, Any] = Field(default_factory=dict)
    handler: Any = Field(default=None, exclude=True)

    model_config = ConfigDict(arbitrary_types_allowed=True)


# Request/Response
class GrokChatRequest(BaseModel):
    model: GrokModelName = GrokModelName.grok_4_6
    messages: List[GrokMessage] = Field(default_factory=list)

    # Generation params
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 2048
    top_p: Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    stop: Optional[List[str]] = None

    # Streaming
    stream: bool = False
    include: List[str] = Field(default_factory=list)  # e.g. ["verbose_streaming"]

    # Live search (SearchParameters)
    live_search: LiveSearchConfig = Field(default_factory=LiveSearchConfig)
    server_tools: GrokServerToolsConfig = Field(default_factory=GrokServerToolsConfig)

    # Custom tools (client-side)
    tools: List[str] = Field(default_factory=list)  # this for adding tool name as list of str
    custom_tools: List[CustomToolSpec] = Field(default_factory=list)

    # file_mode
    use_file_grok: bool = False
    delete_files_on_complete: bool = False

    # Convenience attachment lists (auto-attached to last user msg)
    file_urls: List[str] = Field(default_factory=list)
    file_ids: List[str] = Field(default_factory=list)
    local_paths: List[str] = Field(default_factory=list)

    # Optional: explicitly image convenience lists
    image_urls: List[str] = Field(default_factory=list)
    image_file_ids: List[str] = Field(default_factory=list)
    image_local_paths: List[str] = Field(default_factory=list)

    # Structured output (keep flexible but it will be always optional)
    response_format: Optional[Dict[str, Any]] = None

    timeout_sec: Optional[float] = 420.0
    model_config = ConfigDict(arbitrary_types_allowed=True)


# Pydantic model for getting usages of grok model mainly token counts after tool calling
# so that we can optimise the tool calling gracefully without going too harshly
class GrokUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    num_sources_used: Optional[int] = None


# Pydantic model for output of grok model
class GrokChatResult(BaseModel):
    content: str = ""
    model: str = ""
    citations: List[str] = Field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = Field(default_factory=list)
    usage: Optional[GrokUsage] = None
    raw: Dict[str, Any] = Field(default_factory=dict)
