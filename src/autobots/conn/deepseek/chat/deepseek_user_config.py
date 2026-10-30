from typing import Any, Dict, List, Optional, Set, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.autobots.conn.deepseek.chat.deepseek_models import (
    DeepSeekChatRequest,
    DeepSeekMessage,
    DeepSeekModelName,
    DeepSeekResponseFormat,
    DeepSeekThinking,
    DeepSeekToolSpec,
)
from src.autobots.conn.deepseek.chat.deepseek_tool_executor import ToolPolicy
from src.autobots.conn.deepseek.chat.deepseek_tool_spec_factory import build_deepseek_tools


# this is very much similar to all the llm
class GenerationUserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 2048
    top_p: Optional[float] = 1.0
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    stop: Optional[List[str]] = None



class OutputUserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # JSON output:
    json_mode: bool = False  # if true -> response_format={"type":"json_object"}


class ThinkingUserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False  # if true - thinking.type="enabled"

# tool policy config model 
class ToolPolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_sec: Optional[float] = None
    max_arg_bytes: Optional[int] = None
    max_output_chars_for_model: Optional[int] = None

    max_calls_per_chat: Optional[int] = None
    max_total_tool_calls: Optional[int] = None
    max_repeat_same_call: Optional[int] = None

    redact_secrets: Optional[bool] = None

    cache_enabled: Optional[bool] = None
    cache_ttl_sec: Optional[int] = None
    cache_max_entries: Optional[int] = None
    cache_max_total_bytes: Optional[int] = None
    cache_max_entry_bytes: Optional[int] = None

    side_effects: Optional[bool] = None

# this is the main config which will use as config for creating action of deepseek
class DeepSeekUserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: Union[DeepSeekModelName, str] = DeepSeekModelName.deepseek_v4_flash
    stream: bool = False

    messages: List[DeepSeekMessage] = Field(default_factory=list)
    system_prompt: Optional[str] = Field(default=None, exclude=True)

    generation: GenerationUserConfig = Field(default_factory=GenerationUserConfig)
    thinking: ThinkingUserConfig = Field(default_factory=ThinkingUserConfig)
    output: OutputUserConfig = Field(default_factory=OutputUserConfig)
    tools: List[str] = Field(default_factory=list)

    # strict mode as mentioned in official documentation of deepseek
    strict_mode_tool_calls: bool = False

    # global side-effect gate
    allow_side_effects: bool = False
    require_confirm_field: str = "confirm"


    # Tool policies
    tool_policy_default: ToolPolicyConfig = Field(default_factory=ToolPolicyConfig)
    tool_policy_overrides: Dict[str, ToolPolicyConfig] = Field(default_factory=dict)

    max_rounds: int = 10
    strip_reasoning_in_history: bool = False

    @model_validator(mode="before")
    @classmethod
    def _migrate_system_prompt_to_messages(cls, data: Any):
        if not isinstance(data, dict):
            return data

        sp = (data.get("system_prompt") or "").strip()
        msgs = data.get("messages")

        if sp:
            if not msgs:
                data["messages"] = [{"role": "system", "content": sp}]
            else:
                first = msgs[0] if isinstance(msgs, list) and msgs else None
                first_role = first.get("role") if isinstance(first, dict) else getattr(first, "role", None)
                if first_role != "system":
                    data["messages"] = [{"role": "system", "content": sp}] + list(msgs)

        data.pop("system_prompt", None)
        return data


class CompiledToolPolicies(BaseModel):
    model_config = ConfigDict(extra="allow")
    default: ToolPolicy
    overrides: Dict[str, ToolPolicy] = Field(default_factory=dict)


class CompiledPlan(BaseModel):
    model_config = ConfigDict(extra="allow")

    req: DeepSeekChatRequest
    resolved_model: str
    tools: List[DeepSeekToolSpec] = Field(default_factory=list)
    tool_names: Set[str] = Field(default_factory=set)
    policies: CompiledToolPolicies


def _merge_policy(base: ToolPolicy, override: ToolPolicyConfig) -> ToolPolicy:
    p = base.model_copy(deep=True)
    for k, v in (override.model_dump(exclude_none=True) or {}).items():
        setattr(p, k, v)
    return p

# this is added to resolve an error i got earlier for getting right model name from enum
def _normalize_model(m: Union[str, DeepSeekModelName]) -> DeepSeekModelName:
    if isinstance(m, DeepSeekModelName):
        return m

    s = str(m).strip().lower()

    alias_map = {
        "deepseek_v4_flash": DeepSeekModelName.deepseek_v4_flash,
        "deepseek-v4-flash": DeepSeekModelName.deepseek_v4_flash,
        "deepseek_v4_pro": DeepSeekModelName.deepseek_v4_pro,
        "deepseek-v4-pro": DeepSeekModelName.deepseek_v4_pro,
        "deepseek_v4_flash_vision_exp": DeepSeekModelName.deepseek_v4_flash_vision_exp,
        "deepseek-v4-flash-vision-exp": DeepSeekModelName.deepseek_v4_flash_vision_exp,
    }

    if s in alias_map:
        return alias_map[s]

    raise ValueError(f"Unknown DeepSeek model: {m}")


def compile_deepseek_plan(user_cfg: DeepSeekUserConfig, *, tools_map: Dict[str, Any]) -> CompiledPlan:
    normalized_model = _normalize_model(user_cfg.model)
    resolved_model = normalized_model.value


    # Build tool specs from TOOLS_MAP
    tool_specs: List[DeepSeekToolSpec] = []
    tool_names: Set[str] = set()

    if user_cfg.tools:
        tool_specs = build_deepseek_tools(
            tool_names=user_cfg.tools,
            tools_map=tools_map,
            strict=user_cfg.strict_mode_tool_calls,
        )
        tool_names = {
            t.function.get("name")
            for t in tool_specs
            if isinstance(t.function, dict)
        }

    # response_format
    response_format = DeepSeekResponseFormat(type="json_object") if user_cfg.output.json_mode else None

    # thinking
    thinking = DeepSeekThinking(type="enabled" if user_cfg.thinking.enabled else "disabled")

    req = DeepSeekChatRequest(
        model=normalized_model,
        stream=user_cfg.stream,
        messages=list(user_cfg.messages or []),

        temperature=user_cfg.generation.temperature,
        max_tokens=user_cfg.generation.max_tokens,
        top_p=user_cfg.generation.top_p,
        frequency_penalty=user_cfg.generation.frequency_penalty,
        presence_penalty=user_cfg.generation.presence_penalty,
        stop=user_cfg.generation.stop,

        tools=tool_specs,
        response_format=response_format,
        thinking=thinking,
        strip_reasoning_in_history=bool(user_cfg.strip_reasoning_in_history),
        timeout_sec=420.0,
    )

    # Base policy
    base = ToolPolicy(
        timeout_sec=1200.0,
        max_arg_bytes=50_000,
        max_output_chars_for_model=15_000,
        max_calls_per_chat=10,
        max_total_tool_calls=30,
        max_repeat_same_call=2,
        side_effects=False,
        allow_side_effects=bool(user_cfg.allow_side_effects),
        require_confirm_field=user_cfg.require_confirm_field or "confirm",
        redact_secrets=True,
        cache_enabled=False,
    )

    base = _merge_policy(base, user_cfg.tool_policy_default)

    # enforce global gates
    base.allow_side_effects = bool(user_cfg.allow_side_effects)
    base.require_confirm_field = user_cfg.require_confirm_field or "confirm"

    overrides: Dict[str, ToolPolicy] = {}
    for tname, cfg in (user_cfg.tool_policy_overrides or {}).items():
        p = _merge_policy(base, cfg)
        p.allow_side_effects = bool(user_cfg.allow_side_effects)
        p.require_confirm_field = user_cfg.require_confirm_field or "confirm"
        overrides[tname] = p

    return CompiledPlan(
        req=req,
        resolved_model=resolved_model,
        tools=tool_specs,
        tool_names=tool_names,
        policies=CompiledToolPolicies(default=base, overrides=overrides),
    )