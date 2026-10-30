import supabase_auth
from fastapi import APIRouter, Depends, HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots import SettingsProvider
from src.autobots.action_team.action_team.action_team_doc_model import ActionTeamFind
from src.autobots.action_team.action_team_result.action_team_result_model_doc import (
    ActionTeamResultDoc,
    ActionTeamResultDocsFound,
    ActionTeamResultFind,
    ActionTeamResultUpdate,
)
from src.autobots.action_team.action_team_result.space_action_team_result import (
    SpaceActionTeamResults,
)
from src.autobots.action_team.action_team_result.user_action_team_result import UserActionTeamResult
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.space.space_auth import get_optional_space_member, is_space_member_a_admin
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=SettingsProvider.sget().API_ACTION_TEAMS_RESULTS,
    tags=[SettingsProvider.sget().API_ACTION_TEAMS_RESULTS],
)


@router.get("/")
async def list_action_team_results(
    request: Request,
    id: str = None,
    status: EventResultStatus = None,
    is_saved: bool = None,
    action_team_id: str = None,
    action_team_name: str = None,
    batch_id: str = None,
    is_shared: bool = None,
    limit: int = 20,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> ActionTeamResultDocsFound:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    action_team_find = ActionTeamFind(id=action_team_id, name=action_team_name)
    result_find = ActionTeamResultFind(
        id=id, status=status, is_saved=is_saved, result=action_team_find, batch_id=batch_id
    )
    if is_space_admin:
        return await SpaceActionTeamResults(
            space_member=space_member, db=db
        ).list_action_team_results(ctx, result_find, limit, offset)
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    if is_shared:
        result_find.shared_with = user_orm.email
    return await UserActionTeamResult(
        user_orm, db
    ).list_page_owned_or_shared_action_team_result(
        result_find,
        limit,
        offset,
    )


@router.get("/search/text")
async def search_action_team_results(
    request: Request,
    q: str | None = None,
    query: str | None = None,
    limit: int = 10,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> ActionTeamResultDocsFound:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    search_q = (q if q is not None else query) or ""
    search_q = search_q.strip()

    if is_space_admin:
        space_results = SpaceActionTeamResults(space_member=space_member, db=db)
        if search_q:
            return await space_results.search(
                ctx=ctx, query=search_q, limit=limit, offset=offset
            )
        return await space_results.list_action_team_results(
            ctx, ActionTeamResultFind(), limit=limit, offset=offset
        )

    user_orm = UserORM(id=user_res.user.id, space_member=space_member)
    user_action_team_result = UserActionTeamResult(user_orm, db)
    if search_q:
        return await user_action_team_result.search(
            ctx=ctx, query=search_q, limit=limit, offset=offset
        )

    # Fallback to list when query is empty
    return await list_action_team_results(
        request=request,
        limit=limit,
        offset=offset,
        user_res=user_res,
        db=db,
        space_member=space_member,
    )


@router.get("/{id}")
async def get_action_team_result(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> ActionTeamResultDoc:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    if is_space_admin:
        return await SpaceActionTeamResults(
            space_member=space_member, db=db
        ).get_action_team_result(ctx, id)
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    result_doc = await UserActionTeamResult(
        user_orm, db
    ).get_owned_or_shared_action_team_result(ctx, id)
    if result_doc is None:
        raise HTTPException(404, "Action team result not found")
    return result_doc


@router.delete("/{id}")
async def delete_action_team_result(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> ActionTeamResultDoc:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    if is_space_admin:
        return await SpaceActionTeamResults(
            space_member=space_member, db=db
        ).delete_action_team_result(ctx, id)
    user_orm = UserORM(id=user_res.user.id, space_member=space_member)
    user_action_team_result = UserActionTeamResult(user_orm, db)
    result_doc = await user_action_team_result.get_action_team_result(
        ctx=ctx,
        action_team_result_id=id,
    )
    if not result_doc:
        raise HTTPException(404, "Action team result not found")
    deleted = await user_action_team_result.delete_action_team_result(
        id,
    )
    if deleted != 1:
        raise HTTPException(500, "Error in deleting action team result")
    return result_doc



@router.put("/")
async def update_action_team_result(
    request: Request,
    id: str,
    action_team_update: ActionTeamResultUpdate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> ActionTeamResultDoc:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    if is_space_admin:
        return await SpaceActionTeamResults(
            space_member=space_member, db=db
        ).update_action_team_result(ctx, id, action_team_update)
    user_orm = UserORM(id=user_res.user.id, space_member=space_member)
    user_action_team_result = UserActionTeamResult(user_orm, db)
    result_doc = await user_action_team_result.get_action_team_result(
        ctx=ctx,
        action_team_result_id=id,
    )
    if not result_doc:
        raise HTTPException(404, "Action team result not found")
    updated_doc = await user_action_team_result.update_action_team_result(
        ctx=ctx,
        action_team_result_id=id,
        action_team_update=action_team_update,
    )
    if not updated_doc:
        raise HTTPException(500, "Error in updating action team result")
    return updated_doc
    
