from typing import Any, Dict, List, Literal, Optional

import supabase_auth
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from starlette.requests import Request
from typing_extensions import Annotated

from src.autobots import SettingsProvider
from src.autobots.action.action.action_doc_model import (
    ActionCreate,
    ActionDoc,
    ActionDocsFound,
    ActionFind,
    ActionUpdate,
)
from src.autobots.action.action.admin_actions import (
    AdminActions,
    CloneActionRequest,
    CloneActionResult,
)
from src.autobots.action.action.common_action_models import DataBlock, Filter, RunSource
from src.autobots.action.action.inbound_blocks import accept_blocks
from src.autobots.action.action.space_actions import SpaceActions
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action.action_result.action_result_doc_model import (
    ActionResultDoc,
    ActionResultUpdate,
    QueuedInputs,
)
from src.autobots.action.action_result.user_action_result import UserActionResult
from src.autobots.action.action_type.action_factory import ActionDataTypes, ActionFactory
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.api.webhook import Webhook
from src.autobots.auth.security import (
    get_access_token,
    get_offline_user_from_access_token,
    get_online_admin_user_from_access_token,
)
from src.autobots.conn.temporal.action.shared import ActionWorkflowInput
from src.autobots.conn.temporal.action.workflow import ActionWorkflow
from src.autobots.conn.temporal.cancellation import cancel_workflow
from src.autobots.conn.temporal.client import get_temporal_client
from src.autobots.conn.temporal.workflow_ids import action_workflow_id
from src.autobots.core.cancellation.run_instructions import RunInstruction, push_instruction
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultStatus, Participation
from src.autobots.llm.tools.tool_factory import ToolFactory
from src.autobots.mcp.mcp_doc_model import MCPFind
from src.autobots.mcp.user_mcp import UserMCP
from src.autobots.product.product_model import (
    ProductResource,
    ResourceOperation,
    require_permission,
)
from src.autobots.space.space_auth import get_optional_space_member, is_space_member_a_admin
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=SettingsProvider.sget().API_ACTIONS, tags=[SettingsProvider.sget().API_ACTIONS]
)

TERMINAL_ACTION_RESULT_STATUSES = {
    EventResultStatus.success,
    EventResultStatus.error,
    EventResultStatus.cancelled,
}


def _extract_consumer_id_from_request(
    request: Request,
    input_payload: Dict[str, Any] | None = None,
    action_result_id: str | None = None,
) -> str | None:
    header_consumer_id = request.headers.get("x-consumer-id")
    if header_consumer_id and header_consumer_id.strip():
        return header_consumer_id.strip()

    if input_payload:
        payload_consumer_id = input_payload.get("consumer_id")
        if payload_consumer_id and str(payload_consumer_id).strip():
            return str(payload_consumer_id).strip()

    if action_result_id and str(action_result_id).strip():
        return str(action_result_id).strip()

    return None


def _bind_consumer_id_to_ctx(
    ctx: Context,
    app_state: AppState,
    request: Request,
    input_payload: Dict[str, Any] | None = None,
    action_result_id: str | None = None,
) -> None:
    consumer_id = _extract_consumer_id_from_request(
        request=request,
        input_payload=input_payload,
        action_result_id=action_result_id,
    )
    if consumer_id:
        ctx.consumer_id = consumer_id
        app_state.action_run_id = consumer_id


@router.get("/types")
async def get_action_types(
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
) -> List[str]:
    return ActionFactory.get_action_types()


@router.get("_types/{action_type}", response_model=Any)
async def get_action_type_objects(
    action_type: ActionType,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
) -> ActionDataTypes:
    data_types = await ActionFactory.get_data_types(action_type)
    return data_types


@router.get("/tools")
async def list_action_tools(
    request: Request,
    llm_type: Literal[
        "", "openai_chat", "openai_responses", "openai_assistant", "gemini", "claude", "grok"
    ] = "",
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
) -> List[str]:
    tools = await ToolFactory.get_tools()
    if llm_type:
        match llm_type:
            case "openai_chat":
                pass
            case "openai_responses":
                tools = ["file_search", "code_interpreter", "web_search"] + tools
            case "openai_assistant":
                pass
            case "gemini":
                tools = ["code_execution", "google_search", "google_maps", "url_context"] + tools
            case "claude":
                tools = [
                    "code_interpreter",
                    "web_fetch_claude",
                    "web_search_claude",
                    "memory_tool_claude",
                ] + tools
    return tools


@router.get("/mcp_tools")
async def list_mcp_action_tools(
    request: Request,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> List[str]:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump())
    mcps = await UserMCP(user=user_orm, db=db).list_mcps(
        ctx=ctx, mcp_find=MCPFind(), limit=100, offset=0
    )
    list_mcps = []
    for mcp in mcps.docs:
        mcp_server_new_name = "mcp_" + mcp.server_name
        list_mcps.append(mcp_server_new_name)
    return list_mcps


@router.post("/")
async def create_action(
    request: Request,
    action_create: ActionCreate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Agents, ResourceOperation.Create)),
) -> ActionDoc:
    ctx = request.state.context
    app_state:AppState = request.state.app_state
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    action_doc = await UserActions(user=user_orm, db=db).create_action(
        ctx=ctx,
        app_state=app_state,
        action_create=action_create,
    )
    return action_doc


@router.get("/")
async def list_actions(
    request: Request,
    id: str = None,
    name: str = None,
    version: float = None,
    type: ActionType = None,
    is_saved: bool = None,
    is_published: bool = None,
    is_shared: bool = None,
    limit: int = 100,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Agents, ResourceOperation.Read)),
) -> ActionDocsFound:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    action_find = ActionFind(
        id=id, name=name, version=version, type=type, is_saved=is_saved, is_published=is_published
    )
    if is_space_admin:
        return await SpaceActions(space_member=space_member, db=db).list_actions(
            ctx, action_find, limit, offset
        )
    if is_shared:
        action_find.shared_with = user_orm.email
    user_actions = UserActions(user=user_orm, db=db)
    action_docs = await user_actions.list_owned_or_shared_with(
        action_find,
        limit,
        offset,
    )
    return action_docs


@router.get("/system_actions/")
async def list_system_actions(
    request: Request,
    filter: Annotated[Filter, Depends(Filter)],
    id: str = None,
    name: str = None,
    version: float = None,
    type: ActionType = None,
    limit: int = 100,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    _perm=Depends(require_permission(ProductResource.Agents, ResourceOperation.Read)),
) -> ActionDocsFound:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump())
    action_find = ActionFind(id=id, name=name, version=version, type=type)
    user_actions = UserActions(user=user_orm, db=db)
    logger.bind(ctx=ctx).info(
        f"Filter : {filter}"
    )
    action_docs = await user_actions.list_system_actions(
        ctx=ctx, action_find=action_find, limit=limit, offset=offset, filter=filter,
    )
    return action_docs


@router.get("/{id}")
async def get_action(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Agents, ResourceOperation.Read)),
) -> ActionDoc:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    if is_space_admin:
        action_doc = await SpaceActions(space_member=space_member, db=db).get_action(ctx, id)
    else:
        action_doc = await UserActions(user=user_orm, db=db).get_owned_or_shared_or_system_actions(
            ctx=ctx,
            action_id=id,
            empty_shared_action_config=True,
        )
    if not action_doc:
        raise HTTPException(404, detail="Action not found")
    return action_doc


@router.get("/search/text")
async def search_actions(
    request: Request,
    q: str | None = None,
    query: str | None = None,
    limit: int = 10,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Agents, ResourceOperation.Read)),
) -> ActionDocsFound:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    # Backwards compatible: clients may send `query` (old) or `q` (new).
    search_q = (q if q is not None else query) or ""
    search_q = search_q.strip()

    if is_space_admin:
        space_actions = SpaceActions(space_member=space_member, db=db)
        if search_q != "":
            return await space_actions.search(ctx=ctx, query=search_q, limit=limit, offset=offset)
        return await space_actions.list_actions(ctx, ActionFind(), limit=limit, offset=offset)

    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    user_actions = UserActions(user=user_orm, db=db)
    if search_q != "":
        return await user_actions.search(query=search_q, limit=limit, offset=offset)

    # Keep existing behavior when query is empty (backwards compatibility).
    return await list_actions(
        request=request,
        is_shared=True,
        limit=limit,
        offset=offset,
        user_res=user_res,
        db=db,
        space_member=space_member,
    )


@router.put("/{id}")
async def update_action(
    request: Request,
    id: str,
    action_update: ActionUpdate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Agents, ResourceOperation.Update)),
) -> ActionDoc:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    app_state = request.state.app_state
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    if is_space_admin:
        action_doc = await SpaceActions(space_member=space_member, db=db).update_action(
            ctx=ctx,
            app_state=app_state,
            action_id=id,
            action_update=action_update,
            user=user_orm,
        )
    else:
        action_doc = await UserActions(user=user_orm, db=db).update_action(
            ctx=ctx,
            app_state=app_state,
            action_id=id,
            action_update=action_update,
        )
    return action_doc


@router.delete("/{id}")
async def delete_action(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Agents, ResourceOperation.Delete)),
) -> ActionDoc:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    if is_space_admin:
        action_doc = await SpaceActions(space_member=space_member, db=db).delete_action(ctx, id)
        return action_doc
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    user_actions = UserActions(user=user_orm, db=db)
    action_doc = await user_actions.get_action(
        id,
    )
    if action_doc is None:
        raise HTTPException(400, "Action not found")
    deleted_count = await user_actions.delete_action(
        id,
    )
    if deleted_count != 1:
        raise HTTPException(500, "Error in deleting action")
    return action_doc


# @router.post("/{id}/run")
# async def run_action(
#     request: Request,
#     id: str,
#     input: Dict[str, Any],
#     action_result_id: str = None,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     token: str = Depends(get_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> Any:
#     ctx = request.state.context
#     _bind_consumer_id_to_ctx(
#         ctx=ctx,
#         request=request,
#         input_payload=input,
#         action_result_id=action_result_id,
#     )
#     user_orm = UserORM(**user_res.user.model_dump())
#     user_orm.access_token = token
#     resp = await UserActions(user=user_orm, db=db).run_action_v1(
#         ctx, id, input, action_result_id=action_result_id
#     )
#     match resp:
#         case ActionDoc():
#             return resp
#         case HTTPException():
#             raise HTTPException(
#                 status_code=resp.status_code, detail=f"Secret Not Created, {resp.detail}"
#             )
#         case AppException():
#             raise HTTPException(detail=resp.detail, status_code=resp.http_status)
#         case _:
#             raise HTTPException(
#                 detail="Error in Action run", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
#             )


# @router.post("/{id}/run_stream", response_class=StreamingResponse)
# async def run_action_stream(
#     request: Request,
#     id: str,
#     input: Dict[str, Any],
#     action_result_id: str = None,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> StreamingResponse:
#     ctx = request.state.context
#     _bind_consumer_id_to_ctx(
#         ctx=ctx,
#         request=request,
#         input_payload=input,
#         action_result_id=action_result_id,
#     )
#     user_orm = UserORM(**user_res.user.model_dump())

#     # The generator returned by your service. It must be: AsyncIterable[bytes]
#     gen = UserActions(user=user_orm, db=db).run_action_stream(ctx, id, input, action_result_id)
#     final_response: Any = None

#     # Wrap to handle disconnects & optional cleanup
#     async def stream_wrapper() -> AsyncIterator[bytes]:
#         nonlocal final_response
#         agen = gen.__aiter__()
#         try:
#             async for chunk in agen:
#                 # chunk = await agen.__anext__()
#                 try:
#                     obj = json.loads(chunk.decode("utf-8"))
#                     if obj.get("event") == "user-actions-run-action-stream":
#                         # capture for server-side use (log/persist/webhook)
#                         final_response = obj.get("error")
#                         if final_response is None:
#                             final_response = ActionDoc.model_validate(obj.get("result"))
#                             if final_response is None:
#                                 raise Exception("No return-value in final response")
#                         else:
#                             raise Exception(str(final_response))

#                 except Exception as e:
#                     raise e
#                 if await request.is_disconnected():
#                     break
#                 yield chunk
#             if final_response is not None:
#                 # send one last "final" frame (optional if your chunks are SSE JSON lines)
#                 final_obj = {
#                     "event": "final-stream-response",
#                     "response": final_response.model_dump(mode="json"),
#                 }
#                 try:
#                     # NDJSON: one JSON object per line, newline terminated
#                     yield (json.dumps(final_obj, ensure_ascii=False) + "\n").encode("utf-8")
#                 except Exception:
#                     raise
#         except Exception as e:
#             yield (
#                 json.dumps({"event": "error-stream", "error": str(e)}, ensure_ascii=False) + "\n"
#             ).encode("utf-8")

#     return StreamingResponse(
#         stream_wrapper(),
#         media_type="application/x-ndjson",
#         headers={
#             # good SSE hygiene
#             "Cache-Control": "no-cache",
#             "Connection": "keep-alive",
#             "X-Accel-Buffering": "no",  # disable buffering on some proxies
#         },
#     )


@router.post("/{id}/async_run")
async def async_run_action(
    request: Request,
    id: str,
    input: DataBlock,
    background_tasks: BackgroundTasks,
    action_result_id: str = None,
    webhook: Webhook | None = None,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    token: str = Depends(get_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Agents, ResourceOperation.Execute)),
) -> ActionResultDoc:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    app_state:AppState = request.state.app_state
    _bind_consumer_id_to_ctx(
        ctx=ctx,
        app_state=app_state,
        request=request,
        input_payload=input.model_dump(exclude_none=True),
        action_result_id=action_result_id,
    )
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    user_orm.access_token = token  # Attach the access token to the user ORM
    action_doc = None
    if is_space_admin:
        action_doc = await SpaceActions(space_member=space_member, db=db).get_action(ctx, id)
    if action_doc is None:
        user_actions = UserActions(user_orm, db)
        action_doc = await user_actions.get_owned_or_shared_or_system_actions(
            ctx=ctx,
            action_id=id,
            empty_shared_action_config=False,
        )
    user_action_result = UserActionResult(user_orm, db)

    # Before the queue-append branch below, which returns without reaching the
    # rest of this function. Before authorization too, which is safe only
    # because the mention check is scope-bound - a cheaper lookup against
    # `shared_with` would make this an existence oracle for run ids.
    (input,) = await accept_blocks(
        db,
        [input],
        ctx=ctx,
        app_state=app_state,
        sender=user_orm,
        run_id=action_result_id,
    )

    # Check if action_result_id is provided and handle queue logic
    if action_result_id:
        # Authorizes the caller, then returns the run and a service bound to its
        # owner. Everything below writes through `user_action_result`, so this
        # rebinding is what keeps a participant's turn in the owner's storage.
        existing_result_doc, user_action_result = await user_action_result.authorize_turn(
            run_id=action_result_id, db=db, ctx=ctx
        )
        if existing_result_doc is not None:
            logger.bind(
                ctx=ctx,
                action_result_id=action_result_id,
                db_status=existing_result_doc.status,
            ).info("Queue decision: checking status")
            if existing_result_doc.status == EventResultStatus.request_cancel:
                raise HTTPException(
                    status_code=409,
                    detail="Cancellation in progress for this run; retry once it is cancelled",
                )

            # If status is processing, queue the input instead of starting a new run
            if existing_result_doc.status == EventResultStatus.processing:
                if existing_result_doc.queued_inputs is None:
                    existing_result_doc.queued_inputs = QueuedInputs(inputs=[], total_count=0)
                existing_result_doc.queued_inputs.inputs.append(input)
                existing_result_doc.queued_inputs.total_count += 1
                existing_result_doc.queued_inputs.version_number += 1
                await user_action_result.update_action_result(
                    action_result_id,
                    ActionResultUpdate(queued_inputs=existing_result_doc.queued_inputs),
                )
                logger.bind(
                    ctx=ctx,
                    action_result_id=action_result_id,
                    queue_size=len(existing_result_doc.queued_inputs.inputs),
                ).info("Input queued while action is processing")
                return existing_result_doc
            
            # Get action_doc from existing result if not found
            if action_doc is None:
                action_doc = existing_result_doc.result

    if action_doc is None:
        logger.bind(ctx=ctx, action_id=id).error("Action as well as Action Result doc not found")
        raise HTTPException(404, "Action not found")

    input_dict = input.model_dump(exclude_none=True)


    app_state.action_id = action_doc.id
    app_state.action_name = action_doc.name
    app_state.action_run_id = action_result_id

    action_result_doc = await user_action_result.init(
        app_state=app_state,
        action_result_id=action_result_id,
        action_doc=action_doc,
        action_input_dict=input_dict,
        db=db,
        ctx=ctx,
    )

    if not ctx.consumer_id and action_result_doc is not None:
        ctx.consumer_id = action_result_doc.id
    if action_result_doc is not None and not ctx.action_run_name:
        ctx.action_run_name = getattr(action_result_doc, "name", None)

    app_state.action_run_id = action_result_doc.id
    app_state.action_run_name = action_result_doc.name

    temporal_client = await get_temporal_client()
    workflow_input = ActionWorkflowInput(
        action_id=action_doc.id,
        action_run_id=action_result_doc.id,
        user_id=action_result_doc.user_id,
        space_id=action_result_doc.space_id,
        actor_user_id=str(user_orm.id),
        trace_id=ctx.trace_id,
        consumer_id=ctx.consumer_id,
        # It is the actor's token, so it travels only when the run resolves the
        # actor's resources. Running as the owner it would read the wrong
        # person's data, and the actions needing one fail as scheduled runs do.
        access_token=(
            token
            if action_result_doc.user_id == str(user_orm.id)
            or action_result_doc.participation == Participation.post_as_self
            else None
        ),
        input=input_dict,
        webhook=webhook.model_dump(mode="json") if webhook else None,
    )
    await temporal_client.start_workflow(
        ActionWorkflow.run,
        workflow_input,
        id=action_workflow_id(action_result_doc.id),
        task_queue=SettingsProvider.sget().TEMPORAL_TASK_QUEUE,
    )
    return action_result_doc


@router.post("/{id}/async_run_loop")
async def async_run_action_loop(
    request: Request,
    id: str,
    input: List[DataBlock],
    background_tasks: BackgroundTasks,
    action_result_id: str = None,
    webhook: Webhook | None = None,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Agents, ResourceOperation.Execute)),
) -> List[ActionResultDoc]:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    app_state:AppState = request.state.app_state
    _bind_consumer_id_to_ctx(
        ctx=ctx,
        app_state=app_state,
        request=request,
        input_payload=None,
        action_result_id=action_result_id,
    )
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    user_orm.access_token = token
    action_doc = None
    if is_space_admin:
        action_doc = await SpaceActions(space_member=space_member, db=db).get_action(ctx, id)
    if action_doc is None:
        user_actions = UserActions(user_orm, db)
        action_doc = await user_actions.get_owned_or_shared_or_system_actions(
            ctx=ctx,
            action_id=id,
            empty_shared_action_config=False,
        )
    if action_doc is None:
        raise HTTPException(404, "Action not found")

    user_action_result = UserActionResult(user_orm, db)
    input = [input_.stamped_by(str(user_orm.id), source=RunSource.api) for input_ in input]
    action_result_doc_list = []
    app_state.action_id = action_doc.id
    app_state.action_name = action_doc.name
    app_state.action_run_id = action_result_id

    temporal_client = await get_temporal_client()

    for input_ in input:
        input_dict = input_.model_dump(exclude_none=True)
        action_result_doc = await user_action_result.init(
            app_state=app_state,
            action_result_id=action_result_id,
            action_doc=action_doc,
            action_input_dict=input_dict,
            db=db,
            ctx=ctx,
        )

        if not ctx.consumer_id and action_result_doc is not None:
            ctx.consumer_id = action_result_doc.id
        if action_result_doc is not None and not ctx.action_run_name:
            ctx.action_run_name = getattr(action_result_doc, "name", None)

        app_state.action_run_id = action_result_doc.id
        app_state.action_run_name = action_result_doc.name

        workflow_input = ActionWorkflowInput(
            action_id=action_doc.id,
            action_run_id=action_result_doc.id,
            user_id=action_result_doc.user_id,
            space_id=action_result_doc.space_id,
            actor_user_id=str(user_orm.id),
            trace_id=ctx.trace_id,
            consumer_id=action_result_doc.id,
            access_token=token,
            input=input_dict,
            webhook=webhook.model_dump(mode="json") if webhook else None,
        )
        await temporal_client.start_workflow(
            ActionWorkflow.run,
            workflow_input,
            id=action_workflow_id(action_result_doc.id),
            task_queue=SettingsProvider.sget().TEMPORAL_TASK_QUEUE,
        )
        action_result_doc_list.append(action_result_doc)

    return action_result_doc_list


@router.post("/async_run")
async def async_run_action_doc(
    request: Request,
    action: ActionCreate,
    input: DataBlock,
    background_tasks: BackgroundTasks,
    webhook: Webhook | None = None,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Agents, ResourceOperation.Execute)),
) -> ActionResultDoc:
    ctx: Context = request.state.context
    app_state:AppState = request.state.app_state
    _bind_consumer_id_to_ctx(
        ctx=ctx,
        app_state=app_state,
        request=request,
        input_payload=input.model_dump(exclude_none=True),
        action_result_id=None,
    )
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    user_orm.access_token = token
    input = input.stamped_by(str(user_orm.id), source=RunSource.api)
    action_doc = ActionDoc(
        _id=action.type + "_" + ctx.trace_id,
        user_id=user_orm.id,
        **action.model_dump(),
    )
    user_action_result = UserActionResult(user_orm, db)
    app_state.action_id = action_doc.id
    app_state.action_name = action_doc.name

    action_result_doc = await user_action_result.init(
        app_state=app_state,
        action_result_id=None,
        action_doc=action_doc,
        action_input_dict=input.model_dump(exclude_none=True),
        db=db,
        ctx=ctx,
    )

    if not ctx.consumer_id and action_result_doc is not None:
        ctx.consumer_id = action_result_doc.id
    if action_result_doc is not None and not ctx.action_run_name:
        ctx.action_run_name = getattr(action_result_doc, "name", None)

    app_state.action_run_id = action_result_doc.id
    app_state.action_run_name = action_result_doc.name

    temporal_client = await get_temporal_client()
    workflow_input = ActionWorkflowInput(
        action_id=action_doc.id,
        action_run_id=action_result_doc.id,
        user_id=action_result_doc.user_id,
        space_id=action_result_doc.space_id,
        actor_user_id=str(user_orm.id),
        trace_id=ctx.trace_id,
        consumer_id=ctx.consumer_id,
        access_token=token,
        input=input.model_dump(exclude_none=True),
        webhook=webhook.model_dump(mode="json") if webhook else None,
    )
    await temporal_client.start_workflow(
        ActionWorkflow.run,
        workflow_input,
        id=action_workflow_id(action_result_doc.id),
        task_queue=SettingsProvider.sget().TEMPORAL_TASK_QUEUE,
    )
    return action_result_doc


@router.post("/{id}/batch_run")
async def batch_run_action(
    request: Request,
    id: str,
    input: DataBlock,
    background_tasks: BackgroundTasks,
    action_result_id: str = None,
    batch_size: Optional[int] = Query(default=None, ge=1, le=45),
    webhook: Webhook | None = None,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Agents, ResourceOperation.Execute)),
) -> List[ActionResultDoc]:
    is_space_admin: bool = is_space_member_a_admin(space_member)
    ctx = request.state.context
    app_state:AppState = request.state.app_state
    _bind_consumer_id_to_ctx(
        ctx=ctx,
        app_state=app_state,
        request=request,
        input_payload=input.model_dump(exclude_none=True),
        action_result_id=action_result_id,
    )
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    user_orm.access_token = token
    user_actions = UserActions(user_orm, db)
    user_action_result = UserActionResult(user_orm, db)

    action_doc = None
    if is_space_admin:
        action_doc = await SpaceActions(space_member=space_member, db=db).get_action(ctx, id)
    if action_doc is None:
        action_doc = await user_actions.get_owned_or_shared_or_system_actions(
            ctx=ctx,
            action_id=id,
            empty_shared_action_config=False,
        )
    if action_doc is None:
        raise HTTPException(404, "Action not found")

    input = input.stamped_by(str(user_orm.id), source=RunSource.api)

    if input.metadata.process_url_in_text:
        input.process_url_in_text(ctx)
        input.metadata.process_url_in_text = False
        logger.bind(ctx=ctx, urls_found=len(input.urls)).info(
            "Processed URLs from text for batch run and disabled re-extraction"
        )

    app_state.action_id = action_doc.id
    app_state.action_name = action_doc.name
    app_state.action_run_id = action_result_id

    temporal_client = await get_temporal_client()
    results = await user_actions.batch_run_action_in_background(
        ctx=ctx,
        app_state=app_state,
        action_doc=action_doc,
        action_input=input,
        user_action_result=user_action_result,
        action_result_id=action_result_id,
        db=db,
        background_tasks=background_tasks,
        webhook=webhook,
        batch_size=batch_size,
    )

    for result_doc in results:
        workflow_input = ActionWorkflowInput(
            action_id=action_doc.id,
            action_run_id=result_doc.id,
            user_id=result_doc.user_id,
            space_id=result_doc.space_id,
            actor_user_id=str(user_orm.id),
            trace_id=ctx.trace_id,
            consumer_id=result_doc.id,
            access_token=token,
            input=result_doc.result.input,
            webhook=webhook.model_dump(mode="json") if webhook else None,
        )
        await temporal_client.start_workflow(
            ActionWorkflow.run,
            workflow_input,
            id=action_workflow_id(result_doc.id),
            task_queue=SettingsProvider.sget().TEMPORAL_TASK_QUEUE,
        )

    return results


@router.post("/{action_result_id}/cancel")
async def cancel_action_run(
    request: Request,
    action_result_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    token: str = Depends(get_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Agents, ResourceOperation.Execute)),
) -> ActionResultDoc:
    ctx: Context = request.state.context
    app_state: AppState = request.state.app_state
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    user_orm.access_token = token
    user_action_result = UserActionResult(user_orm, db)

    action_result_doc = await user_action_result.get_action_result(
        action_result_id=action_result_id,
        ctx=ctx,
    )
    if action_result_doc is None:
        raise HTTPException(404, "Action Result not found")

    if action_result_doc.status in TERMINAL_ACTION_RESULT_STATUSES:
        return action_result_doc

    app_state.action_run_id = action_result_doc.id
    app_state.action_run_name = action_result_doc.name

    if action_result_doc.status == EventResultStatus.request_cancel:
        return action_result_doc

    action_result_update = ActionResultUpdate(status=EventResultStatus.request_cancel)
    if action_result_doc.queued_inputs is not None:
        action_result_doc.queued_inputs.inputs = []
        action_result_doc.queued_inputs.version_number += 1
        action_result_update.queued_inputs = action_result_doc.queued_inputs

    cancelling_doc = await user_action_result.update_action_result(
        action_result_id,
        action_result_update,
        ctx=ctx,
    )

    if not await cancel_workflow(action_workflow_id(action_result_id)):
        logger.bind(ctx=ctx, action_result_id=action_result_id).info(
            "No running workflow; finalising cancellation"
        )
        return await user_action_result.update_action_result(
            action_result_id,
            ActionResultUpdate(status=EventResultStatus.cancelled),
            ctx=ctx,
        )

    logger.bind(ctx=ctx, action_result_id=action_result_id).info("Action run cancellation requested")
    return cancelling_doc


class InstructionAccepted(BaseModel):
    action_result_id: str
    accepted: bool = True


@router.post("/{action_result_id}/instruct", status_code=202)
async def instruct_action_run(
    request: Request,
    action_result_id: str,
    instruction: RunInstruction,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.Agents, ResourceOperation.Execute)),
) -> InstructionAccepted:
    ctx: Context = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    user_action_result = UserActionResult(user_orm, db)

    action_result_doc, _ = await user_action_result.authorize_turn(
        run_id=action_result_id, db=db, ctx=ctx
    )
    if action_result_doc.status != EventResultStatus.processing:
        raise HTTPException(
            status_code=409,
            detail=f"Run is not processing (status={action_result_doc.status.value}); "
            "use async_run to add a turn",
        )

    await push_instruction(action_result_id, instruction)
    logger.bind(ctx=ctx, action_result_id=action_result_id).info("Run instruction accepted")
    return InstructionAccepted(action_result_id=action_result_id)


####################################
# Admin paths
# get_online_admin_user_from_access_token
####################################
@router.post("/clone")
async def clone_action(
    request: Request,
    clone_req: CloneActionRequest,
    user_res: supabase_auth.UserResponse = Depends(get_online_admin_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> dict[str, list[CloneActionResult]]:
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    response = await AdminActions.clone_action(ctx=ctx, user=user_orm, db=db, clone_req=clone_req)
    return response
