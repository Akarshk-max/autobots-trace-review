from datetime import timedelta

from temporalio import workflow
from src.autobots.conn.temporal.action.activity import execute_action_activity
from src.autobots.conn.temporal.action.shared import ActionWorkflowInput
from src.autobots.conn.temporal.ctx_utils import build_ctx
from src.autobots.conn.temporal.workflow_retry import (
    execute_activity_retry_on_heartbeat_timeout,
)
from src.autobots.core.heartbeat.running_executions import HeartBeatRunRegistry
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.app_state import AppState


@workflow.defn
class ActionWorkflow:
    @workflow.run
    async def run(self, payload: ActionWorkflowInput) -> dict:
        ctx = build_ctx(
            user_id=payload.user_id,
            trace_id=payload.trace_id,
            consumer_id=payload.consumer_id or payload.action_run_id,
        )
        app_state = AppState(
            action_id=payload.action_id,
            action_run_id=payload.action_run_id,
        )

        HeartBeatRunRegistry.get_instance().register(
            ctx=ctx,
            app_state=app_state,
            runnable_id=payload.action_run_id,
            runnable_type=AppCode.ACTION.value,
        )

        try:
            return await execute_activity_retry_on_heartbeat_timeout(
                execute_action_activity,
                payload,
                start_to_close_timeout=timedelta(hours=10),
                heartbeat_timeout=timedelta(seconds=60),
                max_attempts=3,
            )
        finally:
            HeartBeatRunRegistry.get_instance().deregister(
                ctx=ctx,
                app_state=app_state,
                runnable_id=payload.action_run_id,
            )
