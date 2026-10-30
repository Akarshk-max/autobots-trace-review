import asyncio
import time

from loguru import logger
from temporalio import activity
from temporalio.exceptions import CancelledError

from src.autobots.action.action.action_doc_model import ActionResult
from src.autobots.action.action.common_action_models import TextObj
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action.action_result.action_result_doc_model import ActionResultUpdate
from src.autobots.action.action_result.user_action_result import UserActionResult
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.api.webhook import Webhook
from src.autobots.conn.temporal.action.shared import ActionWorkflowInput
from src.autobots.conn.temporal.ctx_utils import build_ctx
from src.autobots.conn.temporal.heartbeat import run_with_heartbeat
from src.autobots.conn.temporal.resolve_user import resolve_run_user
from src.autobots.core.cancellation.run_cancellation import RunCancellation
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultStatus, Participation
from src.autobots.llm.chat_snapshot.chat_snapshot import apply_to_result
from src.autobots.run_metrics.run_metrics_writer import RunMetricsWriter
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM


async def _run_single_action(
    ctx: Context,
    app_state: AppState,
    action_input_dict: dict,
    action_result_doc,
    user_action_result: UserActionResult,
    run_as: UserORM,
    db,
    notify: Notify,
    webhook: Webhook | None,
):
    """Run one turn and hand back the run as storage now has it.

    `user_action_result` is the run's owner and decides where the turn is
    written. `run_as` is whose connections, files and token the tools resolve
    against — the same person under `post_as_owner`, the actor under
    `post_as_self`.

    The returned document matters: this write excludes `queued_inputs` so it
    cannot clobber anything queued mid-turn, which means the caller's copy is
    older than the queue by the length of the turn.
    """
    if app_state.cancellation is not None:
        app_state.cancellation.set_snapshot(None)
    run_started_at = time.monotonic()
    try:
        logger.bind(
            ctx=ctx,
            action_run_id=action_result_doc.id,
            action_id=action_result_doc.result.id,
        ).info("Action run started")

        run_action_obj = await run_with_heartbeat(
            ActionFactory.run_action(
                ctx=ctx,
                app_state=app_state,
                action_doc=action_result_doc.result,
                action_input_dict=action_input_dict,
                user=run_as,
                db=db,
                notify=notify,
            ),
            app_state.cancellation,
        )

        if isinstance(run_action_obj, Exception):
            raise run_action_obj

        action_result_doc.status = run_action_obj.status
        action_result_doc.error_message = TextObj()
        action_result_doc.result.config = run_action_obj.config_dict
        action_result_doc.result.input = run_action_obj.input_dict
        action_result_doc.result.output = run_action_obj.output_dict

        if action_result_doc.result.results is None:
            action_result_doc.result.results = []
        if action_result_doc.result.results:
            action_result_doc.result.results[-1].input = run_action_obj.input_dict
            action_result_doc.result.results[-1].output = run_action_obj.output_dict
        else:
            action_result_doc.result.results.append(
                ActionResult(input=run_action_obj.input_dict, output=run_action_obj.output_dict)
            )

        logger.bind(
            ctx=ctx,
            action_run_id=action_result_doc.id,
            action_id=action_result_doc.result.id,
        ).info("Action run success")

    except (CancelledError, asyncio.CancelledError):
        action_result_doc.status = EventResultStatus.cancelled
        action_result_doc.error_message = TextObj(text="Cancelled")
        apply_to_result(ctx, action_result_doc, app_state, notify)
        if action_result_doc.queued_inputs:
            action_result_doc.queued_inputs.inputs = []
            action_result_doc.queued_inputs.version_number += 1
        logger.bind(
            ctx=ctx,
            action_run_id=action_result_doc.id,
            action_id=action_result_doc.result.id,
        ).info("Action run cancelled")
        raise

    except Exception as e:
        action_result_doc.status = EventResultStatus.error
        action_result_doc.error_message = TextObj(text=f"{type(e).__name__}: {e}")
        logger.bind(
            ctx=ctx,
            action_run_id=action_result_doc.id,
            action_id=action_result_doc.result.id,
            error=str(e),
        ).exception("Action run error")
        raise

    finally:
        excluded_fields = set(ActionFactory._EXCLUDED_FIELDS_FROM_UPDATE_DURING_RUN)
        if action_result_doc.status == EventResultStatus.cancelled:
            excluded_fields.discard("queued_inputs")
        action_result_update = ActionResultUpdate(
            **action_result_doc.model_dump(exclude=excluded_fields)
        )
        stored = await user_action_result.update_action_result(
            action_result_doc.id, action_result_update
        )

        logger.bind(
            ctx=ctx,
            action_run_id=action_result_doc.id,
            action_id=action_result_doc.result.id,
        ).info("Action Result updated")

        await RunMetricsWriter(db).record_action_run(
            ctx,
            action_result_doc,
            duration_seconds=time.monotonic() - run_started_at,
            is_root_run=not getattr(app_state, "action_team_result_id", None),
        )

        if webhook:
            await webhook.send(action_result_doc.model_dump())

    return stored


async def _process_queued_inputs(
    ctx: Context,
    app_state: AppState,
    action_result_doc,
    user_action_result: UserActionResult,
    run_as: UserORM,
    db,
    notify: Notify,
    webhook: Webhook | None,
) -> None:
    while True:
        if not (
            action_result_doc.status == EventResultStatus.success
            and action_result_doc.queued_inputs
            and action_result_doc.queued_inputs.inputs
        ):
            return

        next_input = action_result_doc.queued_inputs.inputs.pop(0).model_dump(exclude_none=True)
        action_result_doc.queued_inputs.processed_count += 1
        action_result_doc.queued_inputs.version_number += 1
        action_result_doc.status = EventResultStatus.processing
        action_result_doc.result.input = next_input
        action_result_doc.result.output = {}
        action_result_doc.result.results.append(ActionResult(input=next_input))

        queue_update = ActionResultUpdate(
            queued_inputs=action_result_doc.queued_inputs,
            result=action_result_doc.result,
            status=action_result_doc.status,
        )
        await user_action_result.update_action_result(action_result_doc.id, queue_update)

        logger.bind(
            ctx=ctx,
            action_run_id=action_result_doc.id,
            queue_remaining=len(action_result_doc.queued_inputs.inputs),
            processed_count=action_result_doc.queued_inputs.processed_count,
        ).info("Processing next queued input")

        try:
            action_result_doc = await _run_single_action(
                ctx=ctx,
                app_state=app_state,
                action_input_dict=next_input,
                action_result_doc=action_result_doc,
                user_action_result=user_action_result,
                run_as=run_as,
                db=db,
                notify=notify,
                webhook=webhook,
            )
        except (CancelledError, asyncio.CancelledError):
            raise
        except Exception as e:
            logger.bind(
                ctx=ctx,
                action_run_id=action_result_doc.id,
                error=str(e),
            ).exception("Error in continue_run")
            return


@activity.defn
async def execute_action_activity(payload: ActionWorkflowInput) -> dict:
    db = next(get_mongo_db())
    ctx = build_ctx(
        user_id=payload.user_id,
        actor_user_id=payload.actor_user_id,
        trace_id=payload.trace_id,
        consumer_id=payload.consumer_id or payload.action_run_id,
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

    user_action_result = UserActionResult(user_orm, db)
    webhook = Webhook.model_validate(payload.webhook) if payload.webhook else None

    action_result_doc = await user_action_result.get_action_result(payload.action_run_id)
    if not action_result_doc:
        logger.bind(ctx=ctx).error(f"Action result {payload.action_run_id} not found")
        return {"status": "error", "detail": "action_result not found"}

    # Whose connections, files and token the tools resolve against. The owner
    # unless the run was shared for people to post as themselves — both inputs
    # are already here, so nothing new travels on the payload.
    run_as = user_orm
    if (
        action_result_doc.participation == Participation.post_as_self
        and payload.actor_user_id
        and payload.actor_user_id != payload.user_id
    ):
        run_as = await resolve_run_user(
            ctx=ctx,
            db=db,
            user_id=payload.actor_user_id,
            space_id=payload.space_id,
            access_token=payload.access_token,
        )

    action_doc = action_result_doc.result
    if not action_doc:
        user_actions = UserActions(user_orm, db)
        action_doc = await user_actions.get_owned_or_shared_or_system_actions(
            ctx=ctx, action_id=payload.action_id, empty_shared_action_config=False
        )

    if not action_doc:
        logger.bind(ctx=ctx).error(f"Action {payload.action_id} not found")
        return {"status": "error", "detail": "action not found"}

    if activity.is_cancelled():
        raise CancelledError("Activity cancelled before action execution")

    if action_result_doc.status == EventResultStatus.request_cancel:
        logger.bind(ctx=ctx, action_run_id=payload.action_run_id).info(
            "Cancellation already requested for this run; not starting it"
        )
        await user_action_result.update_action_result(
            payload.action_run_id, ActionResultUpdate(status=EventResultStatus.cancelled)
        )
        return {"status": "cancelled", "action_run_id": payload.action_run_id}

    app_state = AppState(
        action_id=payload.action_id,
        action_run_id=payload.action_run_id,
        action_name=action_doc.name,
    )
    app_state.set_cancellation(RunCancellation(run_id=payload.action_run_id))

    notify = Notify(topic=payload.action_run_id)

    action_result_doc = await _run_single_action(
        ctx=ctx,
        app_state=app_state,
        action_input_dict=payload.input,
        action_result_doc=action_result_doc,
        user_action_result=user_action_result,
        run_as=run_as,
        db=db,
        notify=notify,
        webhook=webhook,
    )

    await _process_queued_inputs(
        ctx=ctx,
        app_state=app_state,
        action_result_doc=action_result_doc,
        user_action_result=user_action_result,
        run_as=run_as,
        db=db,
        notify=notify,
        webhook=webhook,
    )

    return {
        "status": "success",
        "action_id": payload.action_id,
        "action_run_id": payload.action_run_id,
    }
