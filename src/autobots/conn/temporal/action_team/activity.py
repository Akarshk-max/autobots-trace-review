
import time

from loguru import logger
from temporalio import activity
from temporalio.exceptions import CancelledError

from src.autobots.action.action.common_action_models import TextObj
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action_team.action_team.action_team import ActionTeam
from src.autobots.action_team.action_team_result.action_team_result_model_doc import (
    ActionTeamResultUpdate,
)
from src.autobots.action_team.action_team_result.user_action_team_result import (
    UserActionTeamResult,
)
from src.autobots.api.webhook import Webhook
from src.autobots.conn.temporal.action_team.shared import ActionTeamWorkflowInput
from src.autobots.conn.temporal.ctx_utils import build_ctx
from src.autobots.conn.temporal.heartbeat import run_with_heartbeat
from src.autobots.conn.temporal.resolve_user import resolve_run_user
from src.autobots.core.cancellation.run_cancellation import RunCancellation
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.app_state import AppState
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.run_metrics.run_metrics_writer import RunMetricsWriter
from src.autobots.server_sent_events.server_sent_events import Notify


@activity.defn
async def execute_action_team_activity(payload: ActionTeamWorkflowInput) -> dict:
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
    user_action_team_result = UserActionTeamResult(user_orm, db)
    webhook = Webhook.model_validate(payload.webhook) if payload.webhook else None

    action_team_result_doc = await user_action_team_result.get_action_team_result(
        ctx=ctx,
        action_team_result_id=payload.action_team_result_id,
    )
    if not action_team_result_doc:
        logger.bind(ctx=ctx).error(
            f"Action team result {payload.action_team_result_id} not found"
        )
        return {"status": "error", "detail": "action_team_result not found"}

    if activity.is_cancelled():
        raise CancelledError("Activity cancelled before team execution")

    app_state = AppState(
        action_team_id=payload.action_team_id,
        action_team_result_id=payload.action_team_result_id,
    )
    app_state.set_cancellation(RunCancellation(run_id=payload.action_team_result_id))

    notify = Notify(topic=payload.action_team_result_id)
    run_started_at = time.monotonic()

    try:
        await run_with_heartbeat(
            ActionTeam._execute_team_loop(
                ctx=ctx,
                app_state=app_state,
                user_actions=user_actions,
                input_dict=payload.input,
                action_team_result_doc=action_team_result_doc,
                user_action_team_result=user_action_team_result,
                notify=notify,
                webhook=webhook,
            ),
            app_state.cancellation,
        )
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Action team execution failed: {e}")
        try:
            action_team_result_update = ActionTeamResultUpdate(
                status=EventResultStatus.error,
                error_message=TextObj(text=f"{type(e).__name__}: {e}"),
            )
            await user_action_team_result.update_action_team_result(
                ctx=ctx,
                action_team_result_id=payload.action_team_result_id,
                action_team_update=action_team_result_update,
            )
            action_team_result_doc.status = EventResultStatus.error
            action_team_result_doc.error_message = TextObj(text=f"{type(e).__name__}: {e}")
            await RunMetricsWriter(user_actions.db).record_action_team_run(
                ctx,
                action_team_result_doc,
                duration_seconds=time.monotonic() - run_started_at,
            )
            if webhook:
                await webhook.send(action_team_result_doc.model_dump())
        except Exception:
            logger.bind(ctx=ctx).exception("Error updating result to error status")
        raise

    return {
        "status": "success",
        "action_team_id": payload.action_team_id,
        "action_team_result_id": payload.action_team_result_id,
    }
