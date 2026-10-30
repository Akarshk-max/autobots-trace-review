import re
from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.space_id import OptionalSpaceId
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.data_model.user_id import UserId

# MCP CONSTANT

MCP_START_CONSTANT: str = "mcp_"

# openai rejects any other server name
_SERVER_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


class MCPCreate(BaseModel):
    server_name: str = Field(
        ...,
        description="The name of the model context server. Must start with a letter and contain only letters, digits, underscores, or hyphens.",
    )
    server_type: Literal["SSE", "StreamableHttp"]
    server_url: str = Field(..., description="The URL of the model context server.")
    description: Optional[str] = Field(
        None, description="A brief description of the model context server."
    )
    allowed_tools: Optional[list[str]] = Field(
        default=None,
        description="A list of tool names that are permitted to use this model context server.",
    )
    request_headers: Optional[Dict[str, str]] = Field(
        default=None,
        description="Optional HTTP headers to include in requests (e.g., Authorization, Content-Type).",
    )
    oauth_token: Optional[str] = Field(
        default=None,
        description="Optional OAuth token for authenticating with the model context server.",
    )
    auth: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Encrypted envelope containing request_headers and oauth_token.",
    )

    @field_validator("server_name")
    @classmethod
    def validate_server_name(cls, v: str) -> str:
        if not _SERVER_NAME_PATTERN.match(v):
            raise ValueError(
                "server_name must start with a letter and contain only letters, digits, underscores, or hyphens"
            )
        return v


class MCPAddInput(MCPCreate):
    """Input for enable_mcp tool. Inherits from MCPCreate but makes server_type and server_url
    optional to allow server_name-only lookups for pre-registered MCPs."""

    server_type: Optional[Literal["SSE", "StreamableHttp"]] = Field(
        default=None,
        description="Transport protocol (required for new MCPs): 'SSE' or 'StreamableHttp'.",
    )
    server_url: Optional[str] = Field(
        default=None,
        description="The URL of the MCP server endpoint (required for new MCPs).",
    )


class MCPFind(BaseModel):
    id: Optional[str] = Field(default=None)  # , alias='_id')
    server_name: Optional[str] = None
    server_type: Optional[Literal["SSE", "StreamableHttp"]] = None
    server_url: Optional[str] = None
    description: Optional[str] = None
    allowed_tools: Optional[list[str]] = None
    request_headers: Optional[Dict[str, str]] = None
    oauth_token: Optional[str] = None
    auth: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class MCPDocFind(MCPFind, OptionalSpaceId):
    user_id: Optional[str] = None


class MCPDocCreate(UserId, OptionalSpaceId, MCPCreate, CreatedAt, UpdatedAt):
    pass


class MCPUpdate(BaseModel):
    server_name: Optional[str] = None
    server_type: Optional[Literal["SSE", "StreamableHttp"]] = None
    server_url: Optional[str] = None
    description: Optional[str] = None
    allowed_tools: Optional[list[str]] = None
    request_headers: Optional[Dict[str, str]] = None
    oauth_token: Optional[str] = None
    auth: Optional[Dict[str, Any]] = None


class MCPDocUpdate(MCPUpdate, UpdatedAt):
    id: str
    user_id: str


# class MCPLiteDoc(BaseModel):
#     id: str = Field(..., alias='_id')
#     server_name : str
#     server_type : Literal["SSE","StreamableHttp"]
#     server_url : str
#     description : Optional[str] = None
#     allowed_tools : Optional[list[str]] = None
#     Request_headers : Optional[Dict[str, str]] = None
#     user_id: str
#     created_at: DateTime
#     updated_at: DateTime


class MCPDoc(MCPDocCreate):
    __collection__ = "MCPs"
    id: str = Field(..., alias="_id")

    model_config = ConfigDict(populate_by_name=True)


class MCPDocsFound(DocFindPage):
    docs: list[MCPDoc]
