from typing import Any, Dict, List, Optional, Set

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.autobots.conn.grok.grok_chat.grok_models import (
    CustomToolSpec,
    GrokChatRequest,
    GrokMessage,
    GrokModelName,
    GrokRole,
    GrokServerToolsConfig,
    GrokTurnRecord,
    # LiveSearchConfig,
    ModelRegistry,
)
from src.autobots.conn.grok.grok_chat.grok_tool_executor import ToolPolicy
from src.autobots.conn.grok.grok_chat.grok_tool_spec_factory import (
    build_custom_tools_from_tools_map,
)


class ToolPolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_sec: Optional[float] = None
    max_arg_bytes: Optional[int] = None
    max_output_chars_for_model: Optional[int] = None

    max_calls_per_chat: Optional[int] = None
    max_total_tool_calls: Optional[int] = None
    max_repeat_same_call: Optional[int] = None

    redact_secrets: Optional[bool] = None

    # NEW caching controls
    cache_enabled: Optional[bool] = None
    cache_ttl_sec: Optional[int] = None
    cache_max_entries: Optional[int] = None
    cache_max_total_bytes: Optional[int] = None
    cache_max_entry_bytes: Optional[int] = None

    # Optional override: mark tool as side-effecting explicitly
    side_effects: Optional[bool] = None


class LiveSearchUserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    parameters: Dict[str, Any] = Field(default_factory=dict)


class AttachmentsUserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    use_file_grok: bool = False
    delete_files_on_complete: bool = False

    file_urls: List[str] = Field(default_factory=list)
    file_ids: List[str] = Field(default_factory=list)
    local_paths: List[str] = Field(default_factory=list)

    image_urls: List[str] = Field(default_factory=list)
    image_file_ids: List[str] = Field(default_factory=list)
    image_local_paths: List[str] = Field(default_factory=list)


class GenerationUserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 2048
    top_p: Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    stop: Optional[List[str]] = None
    response_format: Optional[Dict[str, Any]] = None


class GrokUserConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: GrokModelName = GrokModelName.grok_4_6
    stream: bool = False
    include: List[str] = Field(default_factory=list)

    messages: List[GrokMessage] = Field(default_factory=list)
    system_prompt: Optional[str] = Field(default=None, exclude=True)

    pinned_messages: List[GrokMessage] = Field(default_factory=list)
    turn_history: List[GrokTurnRecord | Dict[str, Any]] = Field(default_factory=list)
    visible_turns: int = Field(default=20, ge=1, le=200)
    chat_turn: int = 0

    history_vector_store_enabled: bool = True
    history_vector_store_id: Optional[str] = None
    history_vector_store_name: Optional[str] = None
    history_vector_store_index_every_n_turns: int = Field(default=20, ge=1, le=200)
    history_vector_store_last_indexed_turn: int = 0

    generation: GenerationUserConfig = Field(default_factory=GenerationUserConfig)
    # live_search: LiveSearchUserConfig = Field(default_factory=LiveSearchUserConfig)
    server_tools: GrokServerToolsConfig = Field(default_factory=GrokServerToolsConfig)
    attachments: AttachmentsUserConfig = Field(default_factory=AttachmentsUserConfig)

    tools: List[str] = Field(default_factory=list)

    # Global side-effect gate
    allow_side_effects: bool = True
    require_confirm_field: str = "confirm"
    

    tool_policy_default: ToolPolicyConfig = Field(default_factory=ToolPolicyConfig)
    tool_policy_overrides: Dict[str, ToolPolicyConfig] = Field(default_factory=dict)

    max_rounds: int = 10
    max_parallel_tools: int = 4

    @model_validator(mode="before")
    @classmethod
    def _migrate_system_prompt_to_messages(cls, data: Any):
        if not isinstance(data, dict):
            return data

        sp = (data.get("system_prompt") or "").strip()
        msgs = data.get("messages")

        if sp:
            if not msgs:
                data["messages"] = [{"role": GrokRole.system, "content": sp}]
            else:
                try:
                    first = msgs[0] if isinstance(msgs, list) and msgs else None
                    first_role = (
                        first.get("role")
                        if isinstance(first, dict)
                        else getattr(first, "role", None)
                    )
                except Exception:
                    first_role = None

                if first_role != GrokRole.system:
                    data["messages"] = [{"role": GrokRole.system, "content": sp}] + list(msgs)

        data.pop("system_prompt", None)
        return data


class CompiledToolPolicies(BaseModel):
    model_config = ConfigDict(extra="allow")
    default: ToolPolicy
    overrides: Dict[str, ToolPolicy] = Field(default_factory=dict)


class CompiledPlan(BaseModel):
    model_config = ConfigDict(extra="allow")

    req: GrokChatRequest
    resolved_model: str
    custom_tool_specs: List[CustomToolSpec] = Field(default_factory=list)
    policies: CompiledToolPolicies
    custom_tool_names: Set[str] = Field(default_factory=set)


def _merge_policy(base: ToolPolicy, override: ToolPolicyConfig) -> ToolPolicy:
    p = base.model_copy(deep=True)
    for k, v in (override.model_dump(exclude_none=True) or {}).items():
        setattr(p, k, v)
    return p


def compile_grok_plan(user_cfg: GrokUserConfig, registry: ModelRegistry) -> CompiledPlan:
    resolved_model = registry.resolve(user_cfg.model)

    custom_specs = build_custom_tools_from_tools_map(
        tool_names=user_cfg.tools,
        forbid_additional=True,
        on_missing="error",
    )
    custom_names = {t.name for t in (custom_specs or [])}

    # live_search = LiveSearchConfig(
    #     enabled=bool(user_cfg.live_search.enabled),
    #     parameters=LiveSearchParametersConfig(**(user_cfg.live_search.parameters or {})),
    # )

    req = GrokChatRequest(
        model=user_cfg.model,
        stream=user_cfg.stream,
        include=list(user_cfg.include or []),
        # live_search=live_search,
        server_tools=user_cfg.server_tools,
        messages=list(user_cfg.messages or []),
        temperature=user_cfg.generation.temperature,
        max_tokens=user_cfg.generation.max_tokens,
        top_p=user_cfg.generation.top_p,
        frequency_penalty=user_cfg.generation.frequency_penalty,
        presence_penalty=user_cfg.generation.presence_penalty,
        stop=user_cfg.generation.stop,
        response_format=user_cfg.generation.response_format,
        tools=list(user_cfg.tools or []),
        custom_tools=custom_specs,
        use_file_grok=user_cfg.attachments.use_file_grok,
        delete_files_on_complete=user_cfg.attachments.delete_files_on_complete,
        file_urls=list(user_cfg.attachments.file_urls or []),
        file_ids=list(user_cfg.attachments.file_ids or []),
        local_paths=list(user_cfg.attachments.local_paths or []),
        image_urls=list(user_cfg.attachments.image_urls or []),
        image_file_ids=list(user_cfg.attachments.image_file_ids or []),
        image_local_paths=list(user_cfg.attachments.image_local_paths or []),
    )

    # Vision check
    has_images = any([req.image_urls, req.image_file_ids, req.image_local_paths])
    if has_images and not registry.supports_vision(resolved_model):
        raise ValueError(
            f"Model '{resolved_model}' is not vision-capable but images were provided."
        )

    # Base tool policy
    base = ToolPolicy(
        timeout_sec=1200.0,
        max_arg_bytes=50_000,
        max_output_chars_for_model=15_000,
        max_calls_per_chat=10,
        max_total_tool_calls=30,
        max_repeat_same_call=2,
        side_effects=True,
        allow_side_effects=bool(user_cfg.allow_side_effects),
        require_confirm_field=user_cfg.require_confirm_field or "confirm",
        redact_secrets=True,
    )
    base = _merge_policy(base, user_cfg.tool_policy_default)

    # ensure global gates override
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
        custom_tool_specs=custom_specs,
        policies=CompiledToolPolicies(default=base, overrides=overrides),
        custom_tool_names=custom_names,
        max_parallel_tools=int(user_cfg.max_parallel_tools or 4),
    )
