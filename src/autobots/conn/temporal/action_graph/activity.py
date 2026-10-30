
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
from src.autobots.action_graph.action_graph_result.user_action_graph_result import (
    UserActionGraphResult,
)
from src.autobots.api.webhook import Webhook
from src.autobots.conn.temporal.action_graph.shared import ActionGraphWorkflowInput
from src.autobots.conn.temporal.ctx_utils import build_ctx
from src.autobots.conn.temporal.heartbeat import run_with_heartbeat
from src.autobots.conn.temporal.resolve_user import resolve_run_user
from src.autobots.core.cancellation.run_cancellation import RunCancellation
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.app_state import AppState
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.server_sent_events.server_sent_events import Notify


@activity.defn
async def execute_action_graph_activity(payload: ActionGraphWorkflowInput) -> dict:
    db = next(get_mongo_db())
    ctx = build_ctx(
        user_id=payload.user_id,
        actor_user_id=payload.actor_user_id,
        trace_id=payload.trace_id,
    )

    # The owner, deliberately: the run, its files and its connections are all
    # theirs. `payload.actor_user_id` says who asked, and is for attribution.
    user_orm = await resolve_run_user(
        ctx=ctx,
        db=db,
        user_id=payload.user_id,
        space_id=payload.space_id,
        access_token=payload.access_token,
    )

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
    app_state.set_cancellation(RunCancellation(run_id=payload.action_graph_result_id))

    notify = Notify(topic=payload.action_graph_result_id)

    # An approval belongs to the output it was given for, so an operation that replaces a node's
    # output withdraws it. Each branch names its own set - see `ActionGraph.withdraw_approvals`.
    # This must stay in step with `_run_retry_as_background_task`, which is the same dispatch for
    # the non-Temporal path.
    if payload.action_graph_node_id and payload.is_rerun_graph:
        emptied = await ActionGraph.empty_downstream_nodes(
            ctx=ctx,
            action_graph_node_id=payload.action_graph_node_id,
            action_graph_result_doc=action_graph_result_doc,
            is_empty_downstream_only=payload.is_empty_downstream_only,
        )
        ActionGraph.withdraw_approvals(
            ctx,
            action_graph_result_doc=action_graph_result_doc,
            nodes=emptied,
            reason="rerun",
        )
    elif not payload.action_graph_node_id and payload.is_rerun_graph:
        # No withdrawal here, deliberately - this drives an unfinished run to completion, and
        # clearing the approvals would park it at the same gate forever. See the long note on the
        # matching branch in `action_graph.py`.
        forget_all(action_graph_result_doc)

    elif payload.action_graph_node_id and not payload.is_rerun_graph and payload.is_continue_run_graph:
        # Only for a NEW continue-run; a resume must not withdraw them again. And the subtree
        # MINUS the root, because the root is skipped and its approval still describes what its
        # children read.
        if not getattr(action_graph_result_doc.result, "open_continue_run", None):
            ActionGraph.withdraw_approvals(
                ctx,
                action_graph_result_doc=action_graph_result_doc,
                nodes=ActionGraph._subtree_of(
                    payload.action_graph_node_id, action_graph_result_doc.result.graph or {}
                )
                - {payload.action_graph_node_id},
                reason="continue-run",
            )
    try:
        if payload.action_graph_node_id and not payload.is_rerun_graph and not payload.is_continue_run_graph:
            result = await run_with_heartbeat(
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
                ),
                app_state.cancellation,
            )
        else:
            result = await run_with_heartbeat(
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
                ),
                app_state.cancellation,
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
