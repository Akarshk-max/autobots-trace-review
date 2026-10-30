from typing import Any, Literal

from anthropic import AsyncAnthropic
from loguru import logger
from pydantic import BaseModel, ConfigDict

from src.autobots.conn.claude.model_registry import ClaudeProvider
from src.autobots.data_model.context import Context


class ClaudeTokenCountPreflightConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    hard_fail_on_error: bool = False
    log_payload: bool = False
    minimum_messages: int = 1


class ClaudeTokenCountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    messages: list[dict[str, Any]]
    system: str | list[dict[str, Any]] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: dict[str, Any] | None = None
    thinking: dict[str, Any] | None = None
    context_management: dict[str, Any] | None = None
    betas: list[str] | None = None


class ClaudeTokenCountResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exact: bool
    input_tokens: int
    original_input_tokens: int | None = None
    provider: ClaudeProvider
    source: Literal["api", "heuristic", "skipped"]
    request_shape: ClaudeTokenCountRequest | None = None
    error: str | None = None


class ClaudeTokenCountingHelper:
    @staticmethod
    def _normalize(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, dict):
            return {str(k): ClaudeTokenCountingHelper._normalize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [ClaudeTokenCountingHelper._normalize(v) for v in value]
        if hasattr(value, "model_dump"):
            return ClaudeTokenCountingHelper._normalize(value.model_dump(exclude_none=True))
        return value

    @classmethod
    def build_request(cls, chat_req: Any) -> ClaudeTokenCountRequest:
        payload = ClaudeTokenCountRequest(
            model=str(chat_req.model),
            messages=cls._normalize(chat_req.messages) or [],
            system=cls._normalize(getattr(chat_req, "system", None)),
            tools=cls._normalize(getattr(chat_req, "tools", None)),
            tool_choice=cls._normalize(getattr(chat_req, "tool_choice", None)),
            thinking=cls._normalize(getattr(chat_req, "thinking", None)),
            context_management=cls._normalize(getattr(chat_req, "context_management", None)),
            betas=cls._normalize(getattr(chat_req, "betas", None)),
        )
        return payload

    @staticmethod
    def _heuristic_tokens_from_text(text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // 4)

    @classmethod
    def _heuristic_count(cls, request: ClaudeTokenCountRequest) -> int:
        total = 0

        if isinstance(request.system, str):
            total += cls._heuristic_tokens_from_text(request.system)
        elif isinstance(request.system, list):
            for block in request.system:
                total += cls._heuristic_tokens_from_text(str(block.get("text", "")))

        for message in request.messages:
            content = message.get("content")
            if isinstance(content, str):
                total += cls._heuristic_tokens_from_text(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        total += cls._heuristic_tokens_from_text(str(block.get("text", "")))
                        total += cls._heuristic_tokens_from_text(str(block.get("thinking", "")))
                        total += cls._heuristic_tokens_from_text(str(block.get("title", "")))
                        total += cls._heuristic_tokens_from_text(str(block.get("content", "")))

        for tool in request.tools or []:
            total += cls._heuristic_tokens_from_text(str(tool))

        if request.tool_choice:
            total += cls._heuristic_tokens_from_text(str(request.tool_choice))

        if request.thinking:
            total += cls._heuristic_tokens_from_text(str(request.thinking))

        if request.context_management:
            total += cls._heuristic_tokens_from_text(str(request.context_management))

        if request.betas:
            total += cls._heuristic_tokens_from_text(str(request.betas))

        return total

    @classmethod
    async def count(
        cls,
        *,
        client: AsyncAnthropic,
        ctx: Context,
        chat_req: Any,
    ) -> ClaudeTokenCountResult:
        provider = getattr(chat_req, "provider", ClaudeProvider.ANTHROPIC)
        request = cls.build_request(chat_req)

        config = getattr(chat_req, "token_count_preflight", None)
        if config is None:
            config = ClaudeTokenCountPreflightConfig()

        if not config.enabled:
            return ClaudeTokenCountResult(
                exact=False,
                input_tokens=0,
                original_input_tokens=None,
                provider=provider,
                source="skipped",
                request_shape=None,
            )

        if len(request.messages) < config.minimum_messages:
            return ClaudeTokenCountResult(
                exact=False,
                input_tokens=0,
                original_input_tokens=None,
                provider=provider,
                source="skipped",
                request_shape=request,
            )

        if provider != ClaudeProvider.ANTHROPIC:
            heuristic = cls._heuristic_count(request)
            logger.bind(ctx=ctx).info(
                "Claude token preflight skipped for provider={} using heuristic tokens={}",
                provider.value,
                heuristic,
            )
            return ClaudeTokenCountResult(
                exact=False,
                input_tokens=heuristic,
                original_input_tokens=None,
                provider=provider,
                source="heuristic",
                request_shape=request,
                error=f"Token count API not used for provider={provider.value}",
            )

        if config.log_payload:
            logger.bind(ctx=ctx).debug(
                "Claude token preflight payload={}",
                request.model_dump(exclude_none=True),
            )

        use_beta_count_tokens = bool(request.context_management or request.betas)
        request_payload = request.model_dump(exclude_none=True)
        if use_beta_count_tokens and "betas" not in request_payload:
            request_payload["betas"] = []

        # Anthropic count_tokens does not support "server tools" (built-in tools like
        # code_execution_* / web_fetch_* / web_search_* / memory_*). These are represented
        # by tool dicts containing a "type" key.
        #
        # To keep token preflight robust, strip server tools for the count_tokens call
        # and conservatively add their heuristic footprint back to the returned count.
        server_tools_stripped: list[dict[str, Any]] = []
        token_adjustment = 0
        raw_tools = request_payload.get("tools")
        if isinstance(raw_tools, list) and raw_tools:
            kept_tools: list[Any] = []
            for tool in raw_tools:
                tool_type = str( tool.get("type") or "" )
                if tool_type.startswith("code_execution_") or tool_type.startswith("web_fetch_") or tool_type.startswith("web_search_") or tool_type.startswith("memory_"):
                    server_tools_stripped.append(tool)
                else:
                    kept_tools.append(tool)

            if server_tools_stripped:
                if kept_tools:
                    request_payload["tools"] = kept_tools
                else:
                    request_payload.pop("tools", None)

                # If tool_choice forces a specific server tool, remove it for count_tokens.
                tool_choice = request_payload.get("tool_choice")
                server_tool_names = {
                    str(item.get("name"))
                    for item in server_tools_stripped
                    if isinstance(item, dict) and item.get("name")
                }
                if (
                    isinstance(tool_choice, dict)
                    and tool_choice.get("type") == "tool"
                    and tool_choice.get("name") in server_tool_names
                ):
                    request_payload.pop("tool_choice", None)
                    token_adjustment += cls._heuristic_tokens_from_text(str(tool_choice))

                token_adjustment += cls._heuristic_tokens_from_text(str(server_tools_stripped))
                logger.bind(ctx=ctx).info(
                    "Claude token preflight: stripped unsupported server tools for count_tokens. "
                    "server_tools={} kept_tools={} token_adjustment={}",
                    len(server_tools_stripped),
                    len(kept_tools),
                    token_adjustment,
                )

        try:
            response = None
            if use_beta_count_tokens:
                if not (
                    hasattr(client, "beta")
                    and hasattr(client.beta, "messages")
                    and hasattr(client.beta.messages, "count_tokens")
                ):
                    raise AttributeError("AsyncAnthropic.beta.messages.count_tokens is not available")

                response = await client.beta.messages.count_tokens(**request_payload)
            else:
                if not hasattr(client.messages, "count_tokens"):
                    raise AttributeError("AsyncAnthropic.messages.count_tokens is not available")

                request_payload.pop("betas", None)
                request_payload.pop("context_management", None)
                response = await client.messages.count_tokens(**request_payload)

            input_tokens = int(getattr(response, "input_tokens", 0) or 0)
            if token_adjustment:
                input_tokens += int(token_adjustment)
            response_context_management = getattr(response, "context_management", None)
            original_input_tokens = None
            if response_context_management is not None:
                if hasattr(response_context_management, "model_dump"):
                    response_context_management = response_context_management.model_dump(exclude_none=True)
                if isinstance(response_context_management, dict):
                    raw_original = response_context_management.get("original_input_tokens")
                    if raw_original is not None:
                        original_input_tokens = int(raw_original)

            logger.bind(ctx=ctx).info(
                "Claude token preflight exact_input_tokens={} original_input_tokens={} model={} beta_api={}",
                input_tokens,
                original_input_tokens,
                request.model,
                use_beta_count_tokens,
            )

            return ClaudeTokenCountResult(
                exact=True,
                input_tokens=input_tokens,
                original_input_tokens=original_input_tokens,
                provider=provider,
                source="api",
                request_shape=request,
            )
        except Exception as exc:
            heuristic = cls._heuristic_count(request)
            logger.bind(ctx=ctx).warning(
                "Claude token preflight failed, falling back to heuristic. error={} heuristic_tokens={} beta_api={}",
                str(exc),
                heuristic,
                use_beta_count_tokens,
            )
            if config.hard_fail_on_error:
                raise

            return ClaudeTokenCountResult(
                exact=False,
                input_tokens=heuristic,
                original_input_tokens=None,
                provider=provider,
                source="heuristic",
                request_shape=request,
                error=str(exc),
            )
