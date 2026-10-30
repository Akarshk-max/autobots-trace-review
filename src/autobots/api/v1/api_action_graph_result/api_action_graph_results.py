import supabase_auth
from fastapi import APIRouter, Depends, HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots import SettingsProvider
from src.autobots.action_graph.action_graph.action_graph_doc_model import ActionGraphFind
from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultDoc,
    ActionGraphResultDocsFound,
    ActionGraphResultFind,
    ActionGraphResultUpdate,
)
from src.autobots.action_graph.action_graph_result.space_action_graph_result import (
    SpaceActionGraphResults,
)
from src.autobots.action_graph.action_graph_result.user_action_graph_result import (
    UserActionGraphResult,
)
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.space.space_auth import get_optional_space_member, is_space_member_a_admin
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=SettingsProvider.sget().API_ACTION_GRAPHS_RESULTS,
    tags=[SettingsProvider.sget().API_ACTION_GRAPHS_RESULTS],
)


# @router.post("/")
# async def create_action_result(
#         action_doc: ActionDoc,
#         user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#         db: Database = Depends(get_mongo_db)
# ) -> ActionResultDoc:
#     user_orm = UserORM(id=UUID(user_res.user.id))
#     action_result = ActionResultCreate(status=EventResultStatus.processing, result=action_doc)
#     action_result_doc = await UserActionResult(user_orm, db).create_action_result(action_result)
#     return action_result_doc


@router.get("/")
async def list_action_graph_result(
    request: Request,
    id: str = None,
    status: EventResultStatus = None,
    is_saved: bool = None,
    action_graph_id: str = None,
    action_graph_name: str = None,
    action_graph_version: float = None,
    batch_id: str = None,
    is_shared: bool = None,
    limit: int = 20,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> ActionGraphResultDocsFound:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    action_graph_find = ActionGraphFind(
        id=action_graph_id, name=action_graph_name, version=action_graph_version
    )
    action_result_find = ActionGraphResultFind(
        id=id,
        status=status,
        is_saved=is_saved,
        result=action_graph_find,
        batch_id=batch_id,
    )
    if is_space_admin:
        action_result_docs = await SpaceActionGraphResults(
            space_member=space_member, db=db
        ).list_action_graph_results(ctx, action_result_find, limit, offset)
        return action_result_docs
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    if is_shared:
        action_result_find.shared_with = user_orm.email
    user_action_graph_result = UserActionGraphResult(user_orm, db)
    action_result_docs = await user_action_graph_result.list_page_owned_or_shared_action_graph_result(
        action_result_find,
        limit,
        offset,
    )
    return action_result_docs


@router.get("/{id}")
async def get_action_graph_result(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> ActionGraphResultDoc:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    if is_space_admin:
        action_result_doc = await SpaceActionGraphResults(
            space_member=space_member, db=db
        ).get_action_graph_result(ctx, id)
    else:
        user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
        action_result_doc = await UserActionGraphResult(
            user_orm, db
        ).get_owned_or_shared_action_graph_result(ctx, id)
    if action_result_doc is None:
        raise HTTPException(404, "Action graph result not found")
    return action_result_doc


@router.get("/search/text")
async def search_action_graph_results(
    request: Request,
    q: str | None = None,
    query: str | None = None,
    limit: int = 10,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> ActionGraphResultDocsFound:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    # Backwards compatible: clients may send `query` (old) or `q` (new).
    search_q = (q if q is not None else query) or ""
    search_q = search_q.strip()

    if is_space_admin:
        space_results = SpaceActionGraphResults(space_member=space_member, db=db)
        if search_q != "":
            docs = await space_results.search(
                ctx=ctx, query=search_q, limit=limit, offset=offset
            )
        else:
            docs = await space_results.list_action_graph_results(
                ctx, ActionGraphResultFind(), limit=limit, offset=offset
            )
        return docs

    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    user_action_graph_results = UserActionGraphResult(user=user_orm, db=db)
    if search_q != "":
        docs = await user_action_graph_results.search(
            query=search_q,
            limit=limit,
            offset=offset,
        )
        return docs

    docs = await list_action_graph_result(
        request=request,
        limit=limit,
        offset=offset,
        user_res=user_res,
        db=db,
        space_member=space_member,
    )
    return docs


@router.put("/")
async def update_action_graph_result(
    request: Request,
    id: str,
    action_graph_result_update: ActionGraphResultUpdate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> ActionGraphResultDoc:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    if is_space_admin:
        updated_doc = await SpaceActionGraphResults(
            space_member=space_member, db=db
        ).update_action_graph_result(ctx, id, action_graph_result_update)
    else:
        user_orm = UserORM(id=user_res.user.id, space_member=space_member)
        user_action_graph_result = UserActionGraphResult(user_orm, db)
        updated_doc = await user_action_graph_result.update_action_graph_result(
            ctx,
            id,
            action_graph_result_update,
        )
    return updated_doc


@router.delete("/{id}")
async def delete_action_graph_result(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> ActionGraphResultDoc:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    if is_space_admin:
        action_result = await SpaceActionGraphResults(
            space_member=space_member, db=db
        ).delete_action_graph_result(ctx, id)
        return action_result
    user_orm = UserORM(id=user_res.user.id, space_member=space_member)
    user_action_result = UserActionGraphResult(user_orm, db)
    action_result = await user_action_result.get_action_graph_result(
        ctx=ctx,
        action_graph_result_id=id,
    )
    if not action_result:
        raise HTTPException(404, "Action Result not found")
    deleted = await UserActionGraphResult(user_orm, db).delete_action_graph_result(
        id,
    )
    if deleted != 1:
        raise HTTPException(500, "Error in deleting Action Result")
    return action_result
