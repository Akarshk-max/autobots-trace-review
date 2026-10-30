import asyncio

from fastapi import HTTPException
from loguru import logger
from temporalio import activity
from temporalio.exceptions import CancelledError

from src.autobots.action.action.common_action_models import TextObj
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action_graph.action_graph.action_graph import ActionGraph
from src.autobots.action_graph.action_graph.node_result_writer import forget_all
from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultUpdate,
)
from src.autobots.action_graph.action_graph_result.user_action_graph_result_prev import (
    UserActionGraphResult,
)
from src.autobots.api.webhook import Webhook
from src.autobots.conn.temporal.action_graph.shared import ActionGraphWorkflowInput
from src.autobots.conn.temporal.ctx_utils import build_ctx
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.app_state import AppState
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user import User
from src.autobots.user.user_orm_model import UserORM

HEARTBEAT_INTERVAL_SECONDS = 15


async def _run_with_heartbeat(coro):
    task = asyncio.ensure_future(coro)
    try:
        while not task.done():
            activity.heartbeat("executing")
            await asyncio.wait({task}, timeout=HEARTBEAT_INTERVAL_SECONDS)
        return task.result()
    except (CancelledError, asyncio.CancelledError):
        task.cancel()
        try:
            await task
        except (CancelledError, asyncio.CancelledError, Exception):
            pass
        raise CancelledError("Activity cancelled during execution")


@activity.defn
async def execute_action_graph_activity(payload: ActionGraphWorkflowInput) -> dict:
    db = next(get_mongo_db())
    ctx = build_ctx(
        user_id=payload.user_id,
        trace_id=payload.trace_id,
    )

    user_orm = UserORM(id=payload.user_id)
    user_doc = await User.get(ctx=ctx, user=user_orm, id=payload.user_id, db=db)
    user_orm = UserORM.model_validate(user_doc.model_dump())
    user_orm.access_token = payload.access_token

    user_actions = UserActions(user_orm, db)
    user_action_graph_result = UserActionGraphResult(user_orm, db)
    webhook = Webhook.model_validate(payload.webhook) if payload.webhook else None

    action_graph_result_doc = await user_action_graph_result.get_action_graph_result(
        ctx=ctx,
        action_graph_result_id=payload.action_graph_result_id,
        scrub_ip=False,
    )
    if not action_graph_result_doc:
        logger.bind(ctx=ctx).error(
            f"Action graph result {payload.action_graph_result_id} not found"
        )
        return {"status": "error", "detail": "action_graph_result not found"}

    if activity.is_cancelled():
        raise CancelledError("Activity cancelled before graph execution")

    app_state = AppState(
        action_graph_id=payload.action_graph_id,
        action_graph_result_id=payload.action_graph_result_id,
        action_graph_node_id=payload.action_graph_node_id,
    )

    notify = Notify(topic=payload.action_graph_result_id)

    if payload.action_graph_node_id and payload.is_rerun_graph:
        await ActionGraph.empty_downstream_nodes(
            ctx=ctx,
            action_graph_node_id=payload.action_graph_node_id,
            action_graph_result_doc=action_graph_result_doc,
            is_empty_downstream_only=payload.is_empty_downstream_only,
        )
    elif not payload.action_graph_node_id and payload.is_rerun_graph:
        forget_all(action_graph_result_doc)

    try:
        if payload.action_graph_node_id and not payload.is_rerun_graph and not payload.is_continue_run_graph:
            result = await _run_with_heartbeat(
                ActionGraph.run_node(
                    ctx=ctx,
                    app_state=app_state,
                    user_actions=user_actions,
                    action_graph_input_dict=payload.input,
                    action_graph_result_doc=action_graph_result_doc,
                    action_graph_node_id=payload.action_graph_node_id,
                    user_action_graph_result=user_action_graph_result,
                    notify=notify,
                    webhook=webhook,
                )
            )
        else:
            result = await _run_with_heartbeat(
                ActionGraph.run_action_graph_v2(
                    ctx=ctx,
                    app_state=app_state,
                    user_actions=user_actions,
                    action_graph_input_dict=payload.input,
                    action_graph_result_doc=action_graph_result_doc,
                    action_graph_node_id=payload.action_graph_node_id,
                    is_continue_run_graph=payload.is_continue_run_graph,
                    user_action_graph_result=user_action_graph_result,
                    notify=notify,
                    webhook=webhook,
                )
            )
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Action graph execution failed: {e}")
        try:
            action_graph_result_update = ActionGraphResultUpdate(
                status=EventResultStatus.error,
                error_message=TextObj(text=str(e)),
            )
            await user_action_graph_result.update_action_graph_result(
                ctx=ctx,
                action_graph_result_id=payload.action_graph_result_id,
                action_graph_update=action_graph_result_update,
            )
            if webhook:
                await webhook.send(action_graph_result_doc.model_dump())
        except Exception:
            logger.bind(ctx=ctx).exception("Error updating result to error status")
        raise

    if result.status == EventResultStatus.error:
        error_msg = result.error_message.text if result.error_message else "Action Graph run failed"
        raise HTTPException(status_code=500, detail=error_msg)

    return {
        "status": "success",
        "action_graph_id": payload.action_graph_id,
        "action_graph_result_id": payload.action_graph_result_id,
    }
