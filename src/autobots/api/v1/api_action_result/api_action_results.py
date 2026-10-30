import supabase_auth
from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots import SettingsProvider
from src.autobots.action.action.action_doc_model import ActionFind
from src.autobots.action.action.inbound_blocks import accept_blocks
from src.autobots.action.action_result.action_result_doc_model import (
    ActionResultDoc,
    ActionResultDocsFound,
    ActionResultFind,
    ActionResultUpdate,
    QueuedInputs,
)
from src.autobots.action.action_result.space_action_result import SpaceActionResults
from src.autobots.action.action_result.user_action_result import UserActionResult
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.app_state import AppState
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.space.space_auth import get_optional_space_member, is_space_member_a_admin
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=SettingsProvider.sget().API_ACTION_RESULTS,
    tags=[SettingsProvider.sget().API_ACTION_RESULTS],
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


@router.get("/{action_result_id}/queue")
async def get_action_queue(
    request: Request,
    action_result_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> QueuedInputs:
    """Get queued inputs for an action result."""
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump())
    user_action_result = UserActionResult(user_orm, db)
    
    result_doc = await user_action_result.get_owned_or_shared_action_result(
        action_result_id, ctx
    )
    if not result_doc:
        raise HTTPException(404, "Action result not found")
    
    logger.bind(
        ctx=ctx,
        action_result_id=action_result_id,
        queue_size=len(result_doc.queued_inputs.inputs) if result_doc.queued_inputs else 0,
    ).info("Queue retrieved")
    
    return result_doc.queued_inputs or QueuedInputs()


@router.put("/{action_result_id}/queue")
async def update_action_queue(
    request: Request,
    action_result_id: str,
    queued_inputs: QueuedInputs,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> QueuedInputs:
    """Update queued inputs for an action result with optimistic locking."""
    ctx = request.state.context
    app_state: AppState = request.state.app_state
    user_orm = UserORM(**user_res.user.model_dump())
    user_action_result = UserActionResult(user_orm, db)
    
    # Editing the queue is posting to the run, so it takes the same gate — and
    # rebinds to the owner, whose storage the write has to land in.
    result_doc, user_action_result = await user_action_result.authorize_turn(
        run_id=action_result_id, db=db, ctx=ctx
    )
    if not result_doc.queued_inputs:
        raise HTTPException(404, "No queue exists for this action result")
    
    # Version check for optimistic locking
    if result_doc.queued_inputs.version_number != queued_inputs.version_number:
        raise HTTPException(409, "Version mismatch - queue was modified")
    
    # Reorder and removal send the whole list back, so a block that already
    # carries an author keeps it. Editing moves text, so its mentions are
    # rechecked even though the block is not new.
    result_doc.queued_inputs.inputs = await accept_blocks(
        db,
        queued_inputs.inputs,
        ctx=ctx,
        app_state=app_state,
        sender=user_orm,
        run_id=action_result_id,
        keep_existing_author=True,
    )
    result_doc.queued_inputs.total_count = (
        result_doc.queued_inputs.processed_count + len(queued_inputs.inputs)
    )
    result_doc.queued_inputs.version_number += 1
    
    await user_action_result.update_action_result(
        action_result_id,
        ActionResultUpdate(queued_inputs=result_doc.queued_inputs)
    )
    
    logger.bind(
        ctx=ctx,
        action_result_id=action_result_id,
        version=result_doc.queued_inputs.version_number,
    ).info("Queue updated")
    
    return result_doc.queued_inputs


@router.get("/")
async def list_action_result(
    request: Request,
    id: str = None,
    status: EventResultStatus = None,
    is_saved: bool = None,
    action_id: str = None,
    action_name: str = None,
    action_version: float = None,
    action_type: ActionType = None,
    batch_id: str = None,
    is_shared: bool = None,
    limit: int = 100,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> ActionResultDocsFound:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    action_find = ActionFind(
        id=action_id, name=action_name, version=action_version, type=action_type
    )
    action_result_find = ActionResultFind(
        id=id,
        status=status,
        is_saved=is_saved,
        result=action_find,
        batch_id=batch_id,
    )
    if is_space_admin:
        return await SpaceActionResults(space_member=space_member, db=db).list_action_results(
            ctx, action_result_find, limit, offset
        )
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    if is_shared:
        action_result_find.shared_with = user_orm.email
    user_action_result = UserActionResult(user_orm, db)
    action_result_docs = await user_action_result.list_owned_or_shared_action_results(
        action_result_find,
        limit,
        offset,
    )
    return action_result_docs


@router.get("/{id}")
async def get_action_result(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> ActionResultDoc:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    if is_space_admin:
        action_result_doc = await SpaceActionResults(
            space_member=space_member, db=db
        ).get_action_result(ctx, id)
    else:
        user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
        action_result_doc = await UserActionResult(
            user_orm, db
        ).get_owned_or_shared_action_result(id, ctx)
    if action_result_doc is None:
        raise HTTPException(status_code=404, detail="Action result not found")
    # Avoid leaking internal configuration details
    if (
        action_result_doc
        and action_result_doc.result
        and getattr(action_result_doc.result, "config", None)
    ):
        action_result_doc.result.config = {}
    return action_result_doc


@router.post("/{id}/branch")
async def branch_action_result(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> ActionResultDoc:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    if is_space_admin:
        action_result_doc = await SpaceActionResults(space_member=space_member, db=db).branch(
            ctx=ctx, result_id=id, actor_user_id=str(user_res.user.id)
        )
    else:
        user_orm = UserORM(id=user_res.user.id, space_member=space_member)
        action_result_doc = await UserActionResult(user_orm, db).branch(source_id=id, ctx=ctx)
    if (
            action_result_doc
            and action_result_doc.result
            and getattr(action_result_doc.result, "config", None)
        ):
            action_result_doc.result.config = {}
    return action_result_doc


@router.get("/search/text")
async def search_action_results(
    request: Request,
    q: str | None = None,
    query: str | None = None,
    limit: int = 10,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> ActionResultDocsFound:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    # Backwards compatible: clients may send `query` (old) or `q` (new).
    search_q = (q if q is not None else query) or ""
    search_q = search_q.strip()

    if is_space_admin:
        space_results = SpaceActionResults(space_member=space_member, db=db)
        if search_q != "":
            docs = await space_results.search(
                ctx=ctx, query=search_q, limit=limit, offset=offset
            )
        else:
            docs = await space_results.list_action_results(
                ctx, ActionResultFind(), limit=limit, offset=offset
            )
        return docs

    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    user_action_results = UserActionResult(user=user_orm, db=db)

    if search_q != "":
        docs = await user_action_results.search(
            query=search_q,
            limit=limit,
            offset=offset,
        )
        return docs

    docs = await list_action_result(
        request=request,
        limit=limit,
        offset=offset,
        user_res=user_res,
        db=db,
        space_member=space_member,
    )
    return docs


@router.put("/")
async def update_action_result(
    request: Request,
    id: str,
    action_graph_result_update: ActionResultUpdate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> ActionResultDoc:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    if is_space_admin:
        updated_doc = await SpaceActionResults(
            space_member=space_member, db=db
        ).update_action_result(ctx, id, action_graph_result_update)
    else:
        user_orm = UserORM(id=user_res.user.id, space_member=space_member)
        user_action_result = UserActionResult(user_orm, db)
        updated_doc = await user_action_result.update_action_result(
            id,
            action_graph_result_update,
        )
    return updated_doc


@router.delete("/{id}")
async def delete_action_result(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> ActionResultDoc:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    if is_space_admin:
        action_result = await SpaceActionResults(
            space_member=space_member, db=db
        ).delete_action_result(ctx, id)
        return action_result
    user_orm = UserORM(id=user_res.user.id, space_member=space_member)
    user_action_result = UserActionResult(user_orm, db)

    # For delete, do not require the full S3 payload to exist; allow deleting broken new-style records.
    try:
        action_result = await user_action_result.get_action_result(id)
    except HTTPException as e:
        # Tightened behavior: new-style records with missing S3 payload raise 404. Allow delete anyway.
        if e.status_code == 404 and str(e.detail) == "Action result payload missing in S3":
            action_result = await user_action_result.get_action_result_mongo(
                action_result_id=id,
            )
        else:
            raise

    if not action_result:
        raise HTTPException(404, "Action Result not found")

    deleted = await user_action_result.delete_action_result(id)
    if deleted != 1:
        raise HTTPException(500, "Error in deleting Action Result")
    return action_result
