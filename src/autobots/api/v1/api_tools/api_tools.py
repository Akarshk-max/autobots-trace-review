from datetime import datetime
from typing import Any, Dict, List

import supabase_auth
from fastapi import APIRouter, Depends, HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots import SettingsProvider
from src.autobots.api.webhook import Webhook
from src.autobots.auth.security import get_access_token, get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.llm.tools.tool_result.tool_result_doc_model import (
    ToolResultDoc,
    ToolResultDocsFound,
    ToolResultFind,
    ToolRunFind,
)
from src.autobots.llm.tools.tool_service import (
    ToolArgsInvalid,
    ToolConfig,
    ToolConfigUnavailable,
    ToolDescription,
    ToolNotFound,
    ToolRunInFlight,
    ToolRunNotFound,
    ToolService,
)
from src.autobots.product.product_model import (
    ProductResource,
    ResourceOperation,
    require_permission,
)
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=SettingsProvider.sget().API_TOOLS,
    tags=[SettingsProvider.sget().API_TOOLS],
)


@router.get("/")
async def list_tools(
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    _perm=Depends(require_permission(ProductResource.Agents, ResourceOperation.Read)),
) -> List[str]:
    return ToolService.list_tool_names()


@router.get("/runs")
async def list_tool_runs(
    tool_name: str = None,
    status: EventResultStatus = None,
    created_at_gte: datetime = None,
    created_at_lte: datetime = None,
    limit: int = 20,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Agents, ResourceOperation.Read)),
) -> ToolResultDocsFound:
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    tool_result_find = ToolResultFind(
        status=status,
        created_at_gte=created_at_gte,
        created_at_lte=created_at_lte,
        result=ToolRunFind(tool_name=tool_name) if tool_name else None,
    )
    return await ToolService.list_tool_runs(
        user=user_orm, db=db, tool_result_find=tool_result_find, limit=limit, offset=offset
    )


@router.get("/runs/{tool_run_id}")
async def get_tool_run(
    tool_run_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Agents, ResourceOperation.Read)),
) -> ToolResultDoc:
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    try:
        return await ToolService.get_tool_run(user=user_orm, db=db, tool_run_id=tool_run_id)
    except ToolRunNotFound:
        raise HTTPException(404, f"Tool run not found: {tool_run_id}")


@router.delete("/runs/{tool_run_id}")
async def delete_tool_run(
    tool_run_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Agents, ResourceOperation.Delete)),
) -> ToolResultDoc:
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    try:
        return await ToolService.delete_tool_run(user=user_orm, db=db, tool_run_id=tool_run_id)
    except ToolRunNotFound:
        raise HTTPException(404, f"Tool run not found: {tool_run_id}")
    except ToolRunInFlight:
        raise HTTPException(409, "Tool run is still in flight; cancel it before deleting")


@router.post("/runs/{tool_run_id}/cancel")
async def cancel_tool_run(
    tool_run_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Agents, ResourceOperation.Execute)),
) -> ToolResultDoc:
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    try:
        return await ToolService.cancel_tool_run(user=user_orm, db=db, tool_run_id=tool_run_id)
    except ToolRunNotFound:
        raise HTTPException(404, f"Tool run not found: {tool_run_id}")


@router.get("/{tool_name}/description")
async def get_tool_description(
    tool_name: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    _perm=Depends(require_permission(ProductResource.Agents, ResourceOperation.Read)),
) -> ToolDescription:
    try:
        return ToolService.get_tool_description(tool_name)
    except ToolNotFound:
        raise HTTPException(404, f"Tool not found: {tool_name}")


@router.get("/{tool_name}/config")
async def get_tool_config(
    tool_name: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    _perm=Depends(require_permission(ProductResource.Agents, ResourceOperation.Read)),
) -> ToolConfig:
    try:
        return ToolService.get_tool_config(tool_name)
    except ToolNotFound:
        raise HTTPException(404, f"Tool not found: {tool_name}")
    except ToolConfigUnavailable:
        raise HTTPException(404, f"Tool has no input schema: {tool_name}")


@router.post("/{tool_name}/run")
async def run_tool(
    request: Request,
    tool_name: str,
    tool_args: Dict[str, Any],
    webhook: Webhook | None = None,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Agents, ResourceOperation.Execute)),
) -> ToolResultDoc:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    user_orm.access_token = token

    try:
        return await ToolService.run_tool(
            ctx=ctx,
            user=user_orm,
            db=db,
            tool_name=tool_name,
            tool_args=tool_args,
            webhook=webhook.model_dump(mode="json") if webhook else None,
        )
    except ToolNotFound:
        raise HTTPException(404, f"Tool not found: {tool_name}")
    except ToolArgsInvalid as e:
        raise HTTPException(422, f"Invalid tool_args: {str(e)}")
