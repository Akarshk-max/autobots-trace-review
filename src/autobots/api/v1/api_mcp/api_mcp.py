from typing import Dict, Literal

import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from starlette import status
from starlette.requests import Request

from src.autobots import SettingsProvider
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.mcp.mcp_doc_model import MCPCreate, MCPDoc, MCPDocsFound, MCPFind, MCPUpdate
from src.autobots.mcp.user_mcp import UserMCP
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(prefix=SettingsProvider.sget().API_MCPs, tags=[SettingsProvider.sget().API_MCPs])


@router.post("/")
async def create_mcp(
    request: Request,
    mcp_create: MCPCreate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> MCPDoc:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    action_doc = await UserMCP(user=user_orm, db=db).create_mcp(ctx=ctx, mcp_create=mcp_create, decrypt=False)
    action_doc.auth = {}
    return action_doc


@router.get("/{mcp_id}")
async def get_mcp(
    request: Request,
    mcp_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> MCPDoc:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    mcp_doc = await UserMCP(user=user_orm, db=db).get_mcp(ctx=ctx, mcp_id=mcp_id, decrypt=False)
    if not mcp_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"MCP with id {mcp_id} not found",
        )
    mcp_doc.auth = {}
    return mcp_doc


@router.get("/")
async def list_mcps(
    request: Request,
    server_name: str = None,
    server_type: Literal["SSE", "StreamableHttp"] = None,
    server_url: str = None,
    description: str = None,
    request_headers: Dict[str, str] = None,
    oauth_token: str = None,
    limit: int = 100,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> MCPDocsFound:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    mcp_find = MCPFind(
        server_name=server_name,
        server_type=server_type,
        server_url=server_url,
        description=description,
        request_headers=request_headers,
        oauth_token=oauth_token,
    )
    mcp_docs = await UserMCP(user=user_orm, db=db).list_mcps(
        ctx=ctx, mcp_find=mcp_find, limit=limit, offset=offset, decrypt=False
    )
    # Wipe auth from all docs in the response
    for doc in mcp_docs.docs:
        doc.auth = {}
    return mcp_docs


@router.put("/{mcp_id}")
async def update_mcp(
    request: Request,
    mcp_id: str,
    mcp_update: MCPUpdate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> MCPDoc:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    mcp_doc = await UserMCP(user=user_orm, db=db).update_mcp(
        ctx=ctx, mcp_id=mcp_id, mcp_update=mcp_update, decrypt=False
    )
    if not mcp_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"MCP with id {mcp_id} not found",
        )
    mcp_doc.auth = {}
    return mcp_doc


@router.delete("/{mcp_id}")
async def delete_mcp(
    request: Request,
    mcp_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> MCPDoc:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    mcp_doc = await UserMCP(user=user_orm, db=db).get_mcp(ctx=ctx, mcp_id=mcp_id)
    if mcp_doc is None:
        raise HTTPException(400, f"MCP with {mcp_id}  not found")
    deleted_count = await UserMCP(user=user_orm, db=db).delete_mcp(ctx=ctx, mcp_id=mcp_id)
    if deleted_count != 1:
        raise HTTPException(500, "Error in deleting action")
    mcp_doc.auth = {}
    return mcp_doc


@router.get("/by_name/{mcp_server_name}")
async def get_mcp_by_server_name(
    request: Request,
    mcp_server_name: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> MCPDoc:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    mcp_doc = await UserMCP(user=user_orm, db=db).get_mcp_by_server_name(
        ctx=ctx, mcp_server_name=mcp_server_name, decrypt=False
    )
    if not mcp_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"MCP with server name {mcp_server_name} not found",
        )
    mcp_doc.auth = {}
    return mcp_doc
