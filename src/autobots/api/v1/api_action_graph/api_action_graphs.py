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
from src.autobots.action_graph.action_graph.action_graph_doc_model import (
    ActionGraphCreate,
    ActionGraphDoc,
    ActionGraphDocsFound,
    ActionGraphFind,
    ActionGraphSharedWithDocFind,
    ActionGraphUpdate,
)
from src.autobots.action_graph.action_graph.action_group import ActionGroup
from src.autobots.action_graph.action_graph.graph_validation import (
    ActionGraphValidation,
    describe_validation,
)
from src.autobots.action_graph.action_graph.space_action_graph import SpaceActionGraphs
from src.autobots.action_graph.action_graph.user_action_graph import UserActionGraphs
from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultCreate,
    ActionGraphResultDoc,
)
from src.autobots.action_graph.action_graph_result.user_action_graph_result import (
    UserActionGraphResult,
)
from src.autobots.api.webhook import Webhook
from src.autobots.auth.security import get_access_token, get_offline_user_from_access_token
from src.autobots.conn.temporal.client import get_temporal_client
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.app_state import AppState
from src.autobots.event_result.event_result_model import EventResultStatus, EventType
from src.autobots.product.product_model import (
    ProductResource,
    ResourceOperation,
    require_permission,
)
from src.autobots.space.space_auth import get_optional_space_member, is_space_member_a_admin
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=SettingsProvider.sget().API_ACTION_GRAPHS,
    tags=[SettingsProvider.sget().API_ACTION_GRAPHS],
)


@router.post("/")
async def create_action_graph(
    action_graph_create: ActionGraphCreate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Workflows, ResourceOperation.Create)),

) -> ActionGraphDoc:
    try:
        user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
        resp = await UserActionGraphs(user=user_orm, db=db).create(
            action_graph_create,
       )
        return resp
    except HTTPException:
        raise  # a validation 400 must not be reported as a 500
    except Exception as e:
        logger.error(str(e))
        raise HTTPException(500)


@router.get("/")
async def list_action_graphs(
    request: Request,
    id: str = None,
    name: str = None,
    version: float = None,
    is_saved: bool = None,
    is_published: bool = None,
    is_published_by_others: bool = None,
    is_shared: bool = None,
    limit: int = 100,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Workflows, ResourceOperation.Read)),
) -> ActionGraphDocsFound:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    find = ActionGraphFind(
        id=id, name=name, version=version, is_saved=is_saved, is_published=is_published
   )
    if is_space_admin:
        return await SpaceActionGraphs(space_member=space_member, db=db).list_action_graphs(
            ctx, find, limit, offset
       )
    or_find = None
    if is_shared:
        or_find = [ActionGraphSharedWithDocFind(shared_with=user_orm.email)]
        if name:
            or_find[0].name = name
        if id:
            or_find[0].id = id
        if version is not None:
            or_find[0].version = version
        if is_saved is not None:
            or_find[0].is_saved = is_saved
    action_docs = await UserActionGraphs(user=user_orm, db=db).list_owned_or_shared(
        find,
        or_find,
        limit,
        offset,
   )
    return action_docs


@router.get("/{id}")
async def get_action_graph(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Workflows, ResourceOperation.Read)),
) -> ActionGraphDoc:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    if is_space_admin:
        action_doc = await SpaceActionGraphs(
            space_member=space_member, db=db
        ).get_action_graph(ctx, id)
    else:
        action_doc = await UserActionGraphs(user=user_orm, db=db).get_owned_or_shared(
            ctx=ctx,
            action_graph_id=id,
       )
    if not action_doc:
        raise HTTPException(404, detail="Action graph not found")
    return action_doc


@router.get("/{id}/validation")
async def get_action_graph_validation(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Workflows, ResourceOperation.Read)),
) -> ActionGraphValidation:
    """Structural problems with a stored graph, without attempting a write.

    Validation is warn-only, so a graph can be saved and still be reported here.
    """
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    if is_space_admin:
        action_doc = await SpaceActionGraphs(
            space_member=space_member, db=db
        ).get_action_graph(ctx, id)
    else:
        action_doc = await UserActionGraphs(user=user_orm, db=db).get_owned_or_shared(
            ctx=ctx,
            action_graph_id=id,
        )
    if not action_doc:
        raise HTTPException(404, detail="Action graph not found")
    return describe_validation(action_doc)


@router.get("/search/text")
async def search_action_graphs(
    request: Request,
    q: str | None = None,
    query: str | None = None,
    limit: int = 10,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Workflows, ResourceOperation.Read)),
) -> ActionGraphDocsFound:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    # Backwards compatible: clients may send `query` (old) or `q` (new).
    search_q = (q if q is not None else query) or ""
    search_q = search_q.strip()

    if is_space_admin:
        space_graphs = SpaceActionGraphs(space_member=space_member, db=db)
        if search_q:
            return await space_graphs.search(ctx=ctx, query=search_q, limit=limit, offset=offset)
        return await space_graphs.list_action_graphs(
            ctx, ActionGraphFind(), limit=limit, offset=offset
       )

    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    user_action_graphs = UserActionGraphs(user=user_orm, db=db)
    if search_q != "":
        return await user_action_graphs.search(
            query=search_q,
            limit=limit,
            offset=offset,
       )

    # Keep existing behavior when query is empty (backwards compatibility).
    return await list_action_graphs(
        request=request,
        is_shared=True,
        limit=limit,
        offset=offset,
        user_res=user_res,
        db=db,
        space_member=space_member,
   )


@router.put("/{id}")
async def update_action_graph(
    request: Request,
    id: str,
    action_graph_update: ActionGraphUpdate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Workflows, ResourceOperation.Update)),
) -> ActionGraphDoc:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    app_state: AppState = request.state.app_state
    if is_space_admin:
        action_doc = await SpaceActionGraphs(
            space_member=space_member, db=db
        ).update_action_graph(ctx, id, action_graph_update)
    else:
        user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
        action_doc = await UserActionGraphs(user=user_orm, db=db).update( app_state, id, action_graph_update)
    return action_doc


@router.delete("/{id}")
async def delete_action_graph(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Workflows, ResourceOperation.Delete)),
) -> ActionGraphDoc:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    if is_space_admin:
        action_doc = await SpaceActionGraphs(
            space_member=space_member, db=db
        ).delete_action_graph(ctx, id)
        return action_doc
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    user_action_graphs = UserActionGraphs(user=user_orm, db=db)
    action_doc = await user_action_graphs.get_owned(id)
    if action_doc is None:
        raise HTTPException(404, "Action not found")
    deleted_count = await user_action_graphs.delete(id)
    if deleted_count != 1:
        raise HTTPException(500, "Error in deleting action")
    return action_doc


# @router.post("/{id}/run")
# async def run_action_graph(
#         id: str,
#         input: TextObj,
#         user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#         db: Database = Depends(get_mongo_db)
# ) -> Dict[str, Any]:
#     user_orm = UserORM(id=UUID(user_res.user.id))
#     resp = await UserActionGraphs(user=user_orm, db=db).run(id, input, db)
#     return resp


@router.post("/{id}/async_run")
async def async_run_action_graph(
    request: Request,
    id: str,
    input: DataBlock,
    background_tasks: BackgroundTasks,
    action_graph_result_id: Optional[str] = None,
    action_graph_node_id: Optional[str] = None,
    is_rerun_graph: bool = False,
    is_continue_run_graph: bool = False,
    is_empty_downstream_only: bool = False,
    webhook: Webhook | None = None,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Workflows, ResourceOperation.Execute)),
) -> ActionGraphResultDoc | None:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    app_state: AppState = request.state.app_state
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    user_orm.access_token = token
    # On rerun, execution uses the graph stored in the existing result doc — skip fetch
    action_graph_doc = None
    if is_space_admin and not action_graph_result_id:
        action_graph_doc = await SpaceActionGraphs(
            space_member=space_member, db=db
        ).get_action_graph(ctx, id)
    user_actions = UserActions(user_orm, db)
    user_action_graph_result = UserActionGraphResult(user_orm, db)

    # Nothing else stamps a graph turn, so without this the author, its time and
    # its source are whatever the client put on the block.
    (input,) = await accept_blocks(
        db,
        [input],
        ctx=ctx,
        app_state=app_state,
        sender=user_orm,
        run_id=action_graph_result_id,
    )

    if action_graph_result_id:
        # Authorises the caller, then returns a service bound to the run's owner,
        # which is what keeps a participant's turn in the owner's conversation.
        _, user_action_graph_result = await user_action_graph_result.authorize_turn(
            run_id=action_graph_result_id, db=db, ctx=ctx
        )

    app_state.action_graph_id = id
    app_state.action_graph_result_id = action_graph_result_id
    app_state.action_graph_node_id = action_graph_node_id

    temporal_client = await get_temporal_client()
    input = input.stamped_by(str(user_orm.id), source=RunSource.api)
    resp = await UserActionGraphs(user=user_orm, db=db).run_in_background(
        ctx=ctx,
        app_state=app_state,
        user_actions=user_actions,
        user_action_graph_result=user_action_graph_result,
        action_graph_id=id,
        input=input,
        action_graph_result_id=action_graph_result_id,
        action_graph_node_id=action_graph_node_id,
        is_rerun_graph=is_rerun_graph,
        is_continue_run_graph=is_continue_run_graph,
        is_empty_downstream_only=is_empty_downstream_only,
        background_tasks=background_tasks,
        webhook=webhook,
        action_graph_doc=action_graph_doc,
        temporal_client=temporal_client,
        access_token=token,
   )
    return resp


@router.post("/{id}/batch_run")
async def batch_run_action_graph(
    request: Request,
    id: str,
    input: DataBlock,
    background_tasks: BackgroundTasks,
    action_graph_result_id: Optional[str] = None,
    action_graph_node_id: Optional[str] = None,
    is_rerun_graph: bool = False,
    is_empty_downstream_only: bool = False,
    batch_size: Optional[int] = Query(default=None, ge=1, le=100),
    webhook: Webhook | None = None,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Workflows, ResourceOperation.Execute)),
) -> list[ActionGraphResultDoc]:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    app_state: AppState = request.state.app_state
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    user_orm.access_token = token
    # On rerun, execution uses the graph stored in the existing result doc — skip fetch
    action_graph_doc = None
    if is_space_admin and not action_graph_result_id:
        action_graph_doc = await SpaceActionGraphs(
            space_member=space_member, db=db
        ).get_action_graph(ctx, id)
    user_actions = UserActions(user_orm, db)
    user_action_graph_result = UserActionGraphResult(user_orm, db)

    app_state.action_graph_id = id
    app_state.action_graph_result_id = action_graph_result_id
    app_state.action_graph_node_id = action_graph_node_id
    temporal_client = await get_temporal_client()
    input = input.stamped_by(str(user_orm.id), source=RunSource.api)
    resp = await UserActionGraphs(user=user_orm, db=db).batch_run_in_background(
        ctx=ctx,
        app_state=app_state,
        user_actions=user_actions,
        user_action_graph_result=user_action_graph_result,
        action_graph_id=id,
        input=input,
        action_graph_result_id=action_graph_result_id,
        action_graph_node_id=action_graph_node_id,
        is_rerun_graph=is_rerun_graph,
        is_empty_downstream_only=is_empty_downstream_only,
        background_tasks=background_tasks,
        webhook=webhook,
        batch_size=batch_size,
        action_graph_doc=action_graph_doc,
        temporal_client=temporal_client,
        access_token=token,
    )
    return resp


@router.post("/{action_graph_id}/run_group_chat")
async def run_group_chat(
    request: Request,
    action_graph_id: str,
    action_group_input: DataBlock,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Workflows, ResourceOperation.Execute)),
) -> ActionGraphResultDoc:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    app_state: AppState = request.state.app_state
    try:
        user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
        logger.bind(ctx=ctx).info(
            f"Running action graph group chat for action graph id: {action_graph_id}"
       )
        user_action_graphs = UserActionGraphs(user=user_orm, db=db)
        user_actions = UserActions(user=user_orm, db=db)
        user_action_graph_result = UserActionGraphResult(user=user_orm, db=db)
        action_graph_doc = None
        if is_space_admin:
            action_graph_doc = await SpaceActionGraphs(
                space_member=space_member, db=db
            ).get_action_graph(ctx, action_graph_id)
        if not action_graph_doc:
            action_graph_doc = await user_action_graphs.get_owned_or_shared(
                ctx=ctx,
                action_graph_id=action_graph_id,
           )
        if not action_graph_doc:
            logger.bind(ctx=ctx).info(f"Action graph not found for id: {action_graph_id}")
            raise HTTPException(404, detail="Action graph not found")
        logger.bind(ctx=ctx).info(f"Fetched action graph doc: {action_graph_doc}")
        action_group_input_dict = action_group_input.model_dump(exclude_none=True)
        action_graph_doc.input = action_group_input_dict
        action_graph_result_create = ActionGraphResultCreate(
            type=EventType.action_graph_run,
            result=action_graph_doc,
            status=EventResultStatus.processing,
       )
        logger.bind(ctx=ctx).info(
            f"Created action graph result create: {action_graph_result_create}"
       )
        action_graph_result_doc: ActionGraphResultDoc = (
            await user_action_graph_result.create_action_graph_result(
                ctx=ctx,
                action_graph_result=action_graph_result_create,
           )
       )
        logger.bind(ctx=ctx).info(f"Created action graph result doc: {action_graph_result_doc}")
        app_state.action_graph_id = action_graph_id
        app_state.action_graph_result_id = action_graph_result_doc.id
        action_group_result = await ActionGroup.run_action_group(
            ctx=ctx,
            app_state=app_state,
            action_graph_input_dict=action_group_input_dict,
            action_graph_result_doc=action_graph_result_doc,
            user_actions=user_actions,
            user_action_graph_result=user_action_graph_result,
       )
        logger.bind(ctx=ctx).info(
            f"final action graph result doc after group chat run: {action_group_result}"
       )
        return action_group_result

    except Exception as e:
        logger.bind(ctx=ctx).exception(str(e))
        raise HTTPException(500, f"Error in running action graph group chat: {str(e)}")
