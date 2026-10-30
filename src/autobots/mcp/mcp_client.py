import hashlib
import json
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from loguru import logger
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Tool as MCPTool
from openai.types.chat import ChatCompletionToolParam
from pydantic import BaseModel, ConfigDict, Field

from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.context import Context

MCP_TOOL_NAME_SEPARATOR = "__"

MCP_MAX_TOOL_NAME_LEN = 64

_INVALID_TOOL_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")


class MCPToolBinding(BaseModel):
    """Links a namespaced tool name back to the MCP server that can execute it.

    Built while resolving tools and carried through the chat loop so a tool call can
    be dispatched without re-reading the MCP document.
    """

    model_config = ConfigDict(extra="forbid")

    server_name: str
    server_url: str
    server_type: str | None = None
    oauth_token: str | None = None
    request_headers: dict[str, str] | None = None
    mcp_tool_name: str = Field(description="Tool name as known by the MCP server.")


class MCPClientWrapper:
    """Connects to remote MCP servers and adapts their tools to OpenAI function-calling.

    Used by OpenRouter, which exposes no server-side MCP support: we act as the MCP
    client ourselves, advertise the remote tools as ordinary functions, and execute
    tool calls against the MCP server when the model invokes them.

    Known limitation: a fresh session is established per operation. Remote MCP servers
    that retain state between tool calls are therefore unsupported.
    """

    @staticmethod
    @asynccontextmanager
    async def connect(
        ctx: Context,
        server_url: str,
        server_type: str | None = None,
        oauth_token: str | None = None,
        request_headers: dict[str, str] | None = None,
    ) -> AsyncIterator[ClientSession]:
        """Open an initialised MCP session using the transport named by server_type.

        server_type follows MCPCreate: "SSE" or "StreamableHttp". Unknown values fall
        back to StreamableHttp, the transport that supersedes SSE in the MCP spec.
        """
        headers: dict[str, str] = {str(k): str(v) for k, v in (request_headers or {}).items()}
        if oauth_token:
            headers.setdefault("Authorization", f"Bearer {oauth_token}")

        transport = str(server_type or "").strip().lower()

        if transport == "sse":
            async with sse_client(url=server_url, headers=headers) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        else:
            # streamablehttp_client yields a third element (session id accessor).
            async with streamablehttp_client(url=server_url, headers=headers) as (
                read,
                write,
                _get_session_id,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    @staticmethod
    def build_tool_name(server_name: str, mcp_tool_name: str) -> str:
        """Namespace an MCP tool name so it is unique and API-legal.

        OpenAI-compatible function names allow only [A-Za-z0-9_-] and at most 64
        characters; overlong names are truncated with a hash suffix so that distinct
        tools cannot collapse onto the same identifier.
        """
        safe_server = _INVALID_TOOL_NAME_CHARS.sub("_", str(server_name or "mcp"))
        safe_tool = _INVALID_TOOL_NAME_CHARS.sub("_", str(mcp_tool_name or "tool"))
        name = f"{safe_server}{MCP_TOOL_NAME_SEPARATOR}{safe_tool}"

        if len(name) <= MCP_MAX_TOOL_NAME_LEN:
            return name

        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        return f"{name[: MCP_MAX_TOOL_NAME_LEN - len(digest) - 1]}_{digest}"

    @staticmethod
    def to_chat_completion_tool_param(
        mcp_tool: MCPTool,
        server_name: str,
    ) -> ChatCompletionToolParam:
        """Adapt an MCP tool to an OpenAI ChatCompletionToolParam.

        MCP inputSchema is already JSON Schema, so it maps onto function.parameters
        directly.
        """
        parameters = mcp_tool.inputSchema or {"type": "object", "properties": {}}

        return ChatCompletionToolParam(
            type="function",
            function={
                "name": MCPClientWrapper.build_tool_name(server_name, mcp_tool.name),
                "description": (mcp_tool.description or f"MCP tool {mcp_tool.name}")[:1024],
                "parameters": parameters,
            },
        )

    @staticmethod
    def filter_allowed_tools(
        mcp_tools: list[MCPTool],
        allowed_tools: list[str] | None,
    ) -> list[MCPTool]:
        """Restrict tools to allowed_tools when that allowlist is configured."""
        if not allowed_tools:
            return list(mcp_tools)
        allowed = {str(t).strip() for t in allowed_tools if str(t).strip()}
        return [t for t in mcp_tools if t.name in allowed]

    @staticmethod
    async def list_tools(
        ctx: Context,
        server_url: str,
        server_type: str | None = None,
        oauth_token: str | None = None,
        request_headers: dict[str, str] | None = None,
        allowed_tools: list[str] | None = None,
    ) -> list[MCPTool]:
        """Return the remote tools exposed by an MCP server, honouring allowed_tools."""
        async with MCPClientWrapper.connect(
            ctx=ctx,
            server_url=server_url,
            server_type=server_type,
            oauth_token=oauth_token,
            request_headers=request_headers,
        ) as session:
            response = await session.list_tools()
            return MCPClientWrapper.filter_allowed_tools(response.tools, allowed_tools)

    @staticmethod
    def stringify_tool_result(result: Any) -> str:
        """Flatten a CallToolResult into the plain text the chat loop expects."""
        content = getattr(result, "content", None)

        if content is None:
            structured = getattr(result, "structuredContent", None)
            if structured is not None:
                return json.dumps(structured, default=str, ensure_ascii=False)
            return str(result)

        parts: list[str] = []
        for block in content if isinstance(content, list) else [content]:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(str(text))
                continue
            if hasattr(block, "model_dump"):
                parts.append(json.dumps(block.model_dump(exclude_none=True), default=str, ensure_ascii=False))
            else:
                parts.append(str(block))

        return "\n".join(p for p in parts if p)

    @staticmethod
    async def call_tool(
        ctx: Context,
        server_url: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        server_type: str | None = None,
        oauth_token: str | None = None,
        request_headers: dict[str, str] | None = None,
    ) -> str:
        """Invoke a tool on an MCP server and return its result as text.

        Errors flagged by the server are prefixed with "error :" to match the
        convention the chat loops use to detect failed tool calls.
        """
        async with MCPClientWrapper.connect(
            ctx=ctx,
            server_url=server_url,
            server_type=server_type,
            oauth_token=oauth_token,
            request_headers=request_headers,
        ) as session:
            result = await session.call_tool(tool_name, arguments or {})

        text = MCPClientWrapper.stringify_tool_result(result)

        if getattr(result, "isError", False):
            logger.bind(ctx=ctx, app_code=AppCode.MCP, tool=tool_name).warning(
                "MCP tool reported an error"
            )
            return f"error : {text}"

        return text

    @staticmethod
    async def call_tool_from_binding(
        ctx: Context,
        binding: MCPToolBinding,
        arguments: dict[str, Any] | None = None,
    ) -> str:
        """Execute the MCP tool described by a binding resolved earlier in the turn."""
        return await MCPClientWrapper.call_tool(
            ctx=ctx,
            server_url=binding.server_url,
            tool_name=binding.mcp_tool_name,
            arguments=arguments,
            server_type=binding.server_type,
            oauth_token=binding.oauth_token,
            request_headers=binding.request_headers,
        )
