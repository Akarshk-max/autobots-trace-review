from typing import Any, Dict

import supabase_auth
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from starlette.requests import Request

from src.autobots import SettingsProvider
from src.autobots.action.action_result.action_result_doc_model import ActionResultDoc
from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultDoc,
)
from src.autobots.action_team.action_team_result.action_team_result_model_doc import (
    ActionTeamResultDoc,
)
from src.autobots.integrations.app_types import AppTypes
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM
from src.autobots.webhook.user_webhook import UserWebhook
from src.autobots.webhook.webhook_model import WebhookCreate, WebhookDoc, WebhookFind, WebhookUpdate
from src.autobots.data_model.app_state import AppState

router = APIRouter(
    prefix=SettingsProvider.sget().API_WEBHOOKS, tags=[SettingsProvider.sget().API_WEBHOOKS]
)

webhook_event_router = APIRouter(
    prefix=SettingsProvider.sget().API_WEBHOOKS_EVENTS,
    tags=[SettingsProvider.sget().API_WEBHOOKS],
)


# class ZendeskEvent(BaseModel):
#     event: str
#     data: dict


# @router.post("/zendesk")
# async def zendesk_webhook(
#     request: Request,
#     event: Dict[str, Any],
#     background_tasks: BackgroundTasks,
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ):
#     ctx = request.state.context
#     # Process the incoming Zendesk event
#     logger.bind(ctx=ctx).info(f"zendesk webhook: {event}")
#     pattern = r"https?://\S+"
#     urls = re.findall(pattern, event["comment"])
#     url = urls[0] if urls else None
#     logger.bind(ctx=ctx).info(f"zendesk webhook work url: {url}")
#     if url is not None:
#         input = DataBlock(text=url)
#         user_orm = UserORM(id=SettingsProvider.sget().SYSTEM_USER)
#         user_actions = UserActions(user_orm, db)
#         user_action_graph_result = UserActionGraphResult(user_orm, db)
#         print(str(background_tasks))
#         resp = await UserActionGraphs(user=user_orm, db=db).run_in_background(
#             ctx=ctx,
#             user_actions=user_actions,
#             user_action_graph_result=user_action_graph_result,
#             action_graph_id="66bb0bc17dd55c16afde29bb",
#             input=input,
#             background_tasks=background_tasks,
#         )
#         logger.bind(
#             ctx=ctx,
#             graph_result_id=resp.id,
#             action_graph_id=resp.result.id,
#             action_graph_name=resp.result.name,
#         ).info(f"zendesk webhook end: {event}")
#     return {}


@router.post("/")
async def create_webhook(
    request: Request,
    create: WebhookCreate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> WebhookDoc:
    ctx = request.state.context
    user = UserORM(**user_res.user.model_dump(), space_member=space_member)
    webhook_doc = await UserWebhook.create(
        ctx=ctx,
        db=db,
        user=user,
        webhook_create=create,
        path_prefix=router.prefix,
    )
    return webhook_doc


@router.put("/")
async def update_webhook(
    request: Request,
    id: str,
    update: WebhookUpdate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> WebhookDoc:
    ctx = request.state.context
    user = UserORM(**user_res.user.model_dump(), space_member=space_member)
    webhook_doc = await UserWebhook.update(
        ctx=ctx,
        db=db,
        user=user,
        id=id,
        webhook_update=update,
    )
    return webhook_doc


@router.get("/{id}")
async def get_webhook(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> WebhookDoc:
    ctx = request.state.context
    user = UserORM(**user_res.user.model_dump(), space_member=space_member)
    webhook_doc = await UserWebhook.get(ctx=ctx, db=db, user=user, id=id)
    if webhook_doc is None:
        raise HTTPException(404, "Webhook not found")
    return webhook_doc


@router.get("/")
async def list_webhook(
    request: Request,
    webhook_id: str | None = None,
    runnable_id: str | None = None,
    app_type: AppTypes | None = None,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> DocFindPage:
    ctx = request.state.context
    find = WebhookFind(webhook_id=webhook_id, runnable_id=runnable_id, app_type=app_type)
    user = UserORM(**user_res.user.model_dump(), space_member=space_member)
    webhook_doc = await UserWebhook.list(
        ctx=ctx,
        db=db,
        user=user,
        webhook_find=find,
    )
    return webhook_doc


@router.delete("/{id}")
async def delete_webhook(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> int:
    ctx = request.state.context
    user = UserORM(**user_res.user.model_dump(), space_member=space_member)
    delete_result = await UserWebhook.delete(
        ctx=ctx,
        db=db,
        user=user,
        id=id,
    )
    return delete_result.deleted_count


@webhook_event_router.post("/{id}")
async def run_webhook_event(
    request: Request,
    id: str,
    event: Dict[str, Any],
    background_tasks: BackgroundTasks,
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> list[ActionGraphResultDoc] | ActionResultDoc | ActionTeamResultDoc | None:
    ctx = request.state.context
    app_state: AppState = request.state.app_state
    try:
        # Check for webhook challenge first
        challenge_resp = await UserWebhook.handle_challenge(
            ctx=ctx, db=db, webhook_id=id, event=event
        )
        if challenge_resp is not None:
            return challenge_resp

        # Normal flow
        webhook = await UserWebhook.run_event(
            ctx=ctx, app_state=app_state, db=db, webhook_id=id, event=event, background_tasks=background_tasks
        )
        return webhook
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.bind(
            ctx=ctx,
            webhook_id=id,
        ).error(f"Error in running webhook event: {e}")
        raise HTTPException(status_code=500, detail=f"Error in running webhook event: {e}")


router.include_router(webhook_event_router)
