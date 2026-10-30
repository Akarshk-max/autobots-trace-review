from typing import Optional

import supabase_auth
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from starlette.requests import Request

from src.autobots import SettingsProvider
from src.autobots.action.action.common_action_models import DataBlock, RunSource
from src.autobots.action.action.inbound_blocks import accept_blocks
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action_team.action_team.action_team_doc_model import (
    ActionTeamCreate,
    ActionTeamDoc,
    ActionTeamDocsFound,
    ActionTeamFind,
    ActionTeamUpdate,
)
from src.autobots.action_team.action_team.space_action_teams import SpaceActionTeams
from src.autobots.action_team.action_team.user_action_team import UserActionTeams
from src.autobots.action_team.action_team_result.action_team_result_model_doc import (
    ActionTeamResultDoc,
)
from src.autobots.action_team.action_team_result.user_action_team_result import UserActionTeamResult
from src.autobots.api.webhook import Webhook
from src.autobots.auth.security import get_access_token, get_offline_user_from_access_token
from src.autobots.conn.temporal.client import get_temporal_client
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.app_state import AppState
from src.autobots.product.product_model import (
    ProductResource,
    ResourceOperation,
    require_permission,
)
from src.autobots.space.space_auth import get_optional_space_member, is_space_member_a_admin
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=SettingsProvider.sget().API_ACTION_TEAMS,
    tags=[SettingsProvider.sget().API_ACTION_TEAMS],
)


@router.post("/")
async def create_action_team(
    request: Request,
    action_team_create: ActionTeamCreate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Teams, ResourceOperation.Create)),

) -> ActionTeamDoc:
    ctx = request.state.context
    try:
        user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
        for member in action_team_create.members:
            member.member_id = str(gen_uuid())
        logger.bind(ctx=ctx).info(f"Creating action team with members: {action_team_create.members}")
        resp = await UserActionTeams(user=user_orm, db=db).create(
            ctx,
            action_team_create,
       )
        return resp
    except Exception as e:
        logger.error(str(e))
        raise HTTPException(500, detail=str(e))


@router.get("/")
async def list_action_teams(
    request: Request,
    id: str = None,
    name: str = None,
    is_saved: bool = None,
    is_published: bool = None,
    is_shared: bool = None,
    limit: int = 100,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Teams, ResourceOperation.Read)),

) -> ActionTeamDocsFound:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    find = ActionTeamFind(id=id, name=name, is_saved=is_saved, is_published=is_published)
    if is_space_admin:
        action_team_docs = await SpaceActionTeams(
            space_member=space_member, db=db
        ).list_action_teams(ctx, find, limit, offset)
    else:
        user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
        if is_shared:
            find.shared_with = user_orm.email
        action_team_docs = await UserActionTeams(user=user_orm, db=db).list_owned_or_shared(
            ctx,
            find,
            limit,
            offset,
       )
    return action_team_docs


@router.get("/search/text")
async def search_action_teams(
    request: Request,
    q: str | None = None,
    query: str | None = None,
    limit: int = 10,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Teams, ResourceOperation.Read)),

) -> ActionTeamDocsFound:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    search_q = (q if q is not None else query) or ""
    search_q = search_q.strip()

    if is_space_admin:
        space_teams = SpaceActionTeams(space_member=space_member, db=db)
        if search_q:
            return await space_teams.search(
                ctx=ctx, query=search_q, limit=limit, offset=offset
           )
        return await space_teams.list_action_teams(
            ctx, ActionTeamFind(), limit=limit, offset=offset
       )

    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    user_action_teams = UserActionTeams(user=user_orm, db=db)
    if search_q:
        return await user_action_teams.search(
            ctx=ctx, query=search_q, limit=limit, offset=offset
       )

    # Fallback to list when query is empty
    return await list_action_teams(
        request=request,
        is_shared=True,
        limit=limit,
        offset=offset,
        user_res=user_res,
        db=db,
        space_member=space_member,
   )


@router.get("/{id}")
async def get_action_team(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Teams, ResourceOperation.Read)),

) -> ActionTeamDoc:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    if is_space_admin:
        action_team_doc = await SpaceActionTeams(
            space_member=space_member, db=db
        ).get_action_team(ctx, id)
    else:
        user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
        action_team_doc = await UserActionTeams(user=user_orm, db=db).get_owned_or_shared(ctx, id)
    if not action_team_doc:
        raise HTTPException(404, detail="Action team not found")
    return action_team_doc


@router.put("/{id}")
async def update_action_team(
    request: Request,
    id: str,
    action_team_update: ActionTeamUpdate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Teams, ResourceOperation.Update)),

) -> ActionTeamDoc:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    for members in action_team_update.members or []:
        if not members.member_id:
            members.member_id = str(gen_uuid())
    if is_space_admin:
        action_team_doc = await SpaceActionTeams(
            space_member=space_member, db=db
        ).update_action_team(ctx, id, action_team_update)
    else:
        user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
        action_team_doc = await UserActionTeams(user=user_orm, db=db).update(
            ctx, id, action_team_update
       )
    return action_team_doc


@router.delete("/{id}")
async def delete_action_team(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    _perm=Depends(require_permission(ProductResource.Teams, ResourceOperation.Delete)),

    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> ActionTeamDoc:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    if is_space_admin:
        action_team_doc = await SpaceActionTeams(
            space_member=space_member, db=db
        ).delete_action_team(ctx, id)
        return action_team_doc
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    user_action_teams = UserActionTeams(user=user_orm, db=db)
    action_team_doc = await user_action_teams.get_owned(ctx, id)
    if action_team_doc is None:
        raise HTTPException(404, "Action team not found")
    deleted_count = await user_action_teams.delete(ctx, id)
    if deleted_count != 1:
        raise HTTPException(500, "Error in deleting action team")
    return action_team_doc


@router.post("/{id}/async_run")
async def async_run_action_team(
    request: Request,
    id: str,
    input: DataBlock,
    background_tasks: BackgroundTasks,
    action_team_result_id: str = None,
    webhook: Webhook | None = None,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Teams, ResourceOperation.Execute)),
) -> ActionTeamResultDoc | None:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    app_state: AppState = request.state.app_state
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    user_orm.access_token = token
    action_team_doc = None
    if is_space_admin:
        action_team_doc = await SpaceActionTeams(
            space_member=space_member, db=db
        ).get_action_team(ctx, id)
    user_actions = UserActions(user_orm, db)
    user_action_team_result = UserActionTeamResult(user_orm, db)

    # Nothing else stamps a team turn, so without this the author, its time and
    # its source are whatever the client put on the block.
    (input,) = await accept_blocks(
        db,
        [input],
        ctx=ctx,
        app_state=app_state,
        sender=user_orm,
        run_id=action_team_result_id,
    )

    if action_team_result_id:
        # Authorises the caller, then returns a service bound to the run's owner.
        # `init` reads and writes the run through it, so the rebinding is what
        # keeps a participant's turn in the owner's conversation rather than
        # looking for the owner's run under the participant's own id.
        _, user_action_team_result = await user_action_team_result.authorize_turn(
            run_id=action_team_result_id, db=db, ctx=ctx
        )

    app_state.action_team_id = id
    app_state.action_team_result_id = action_team_result_id

    temporal_client = await get_temporal_client()
    input = input.stamped_by(str(user_orm.id), source=RunSource.api)
    return await UserActionTeams(user=user_orm, db=db).run_in_background(
        ctx=ctx,
        app_state=app_state,
        user_actions=user_actions,
        user_action_team_result=user_action_team_result,
        action_team_id=id,
        input=input,
        action_team_result_id=action_team_result_id,
        background_tasks=background_tasks,
        webhook=webhook,
        action_team_doc=action_team_doc,
        temporal_client=temporal_client,
        access_token=token,
    )
    
    


@router.post("/{id}/batch_run")
async def batch_run_action_team(
    request: Request,
    id: str,
    input: DataBlock,
    background_tasks: BackgroundTasks,
    batch_size: Optional[int] = Query(default=None, ge=1, le=100),
    action_team_result_id: str = None,
    webhook: Webhook | None = None,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Teams, ResourceOperation.Execute)),
) -> list[ActionTeamResultDoc]:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    app_state: AppState = request.state.app_state
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    user_orm.access_token = token
    action_team_doc = None
    if is_space_admin:
        action_team_doc = await SpaceActionTeams(
            space_member=space_member, db=db
        ).get_action_team(ctx, id)
    user_actions = UserActions(user_orm, db)
    user_action_team_result = UserActionTeamResult(user_orm, db)

    app_state.action_team_id = id
    app_state.action_team_result_id = action_team_result_id

    temporal_client = await get_temporal_client()
    input = input.stamped_by(str(user_orm.id), source=RunSource.api)
    return await UserActionTeams(user=user_orm, db=db).batch_run_in_background(
        ctx=ctx,
        app_state=app_state,
        user_actions=user_actions,
        user_action_team_result=user_action_team_result,
        action_team_id=id,
        input=input,
        action_team_result_id=action_team_result_id,
        background_tasks=background_tasks,
        webhook=webhook,
        batch_size=batch_size,
        action_team_doc=action_team_doc,
        temporal_client=temporal_client,
        access_token=token,
    )