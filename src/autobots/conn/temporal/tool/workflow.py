from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, is_cancelled_exception

from src.autobots.conn.temporal.ctx_utils import build_ctx
from src.autobots.conn.temporal.tool.activity import (
    execute_tool_activity,
    finalize_tool_run_activity,
)
from src.autobots.conn.temporal.tool.shared import ToolFinalizeInput, ToolWorkflowInput
from src.autobots.core.heartbeat.running_executions import HeartBeatRunRegistry
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.app_state import AppState


@workflow.defn
class ToolWorkflow:
    @workflow.run
    async def run(self, payload: ToolWorkflowInput) -> dict:
        ctx = build_ctx(
            user_id=payload.user_id,
            trace_id=payload.trace_id,
            consumer_id=payload.tool_run_id,
        )
        app_state = AppState()
        HeartBeatRunRegistry.get_instance().register(
            ctx=ctx,
            app_state=app_state,
            runnable_id=payload.tool_run_id,
            runnable_type=AppCode.TOOL_FACTORY.value,
        )
        try:
            return await workflow.execute_activity(
                execute_tool_activity,
                payload,
                start_to_close_timeout=timedelta(minutes=payload.start_to_close_timeout_minutes),
                heartbeat_timeout=timedelta(seconds=60),
                # Tools are not idempotent: a retried api_call or create_action runs twice.
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except ActivityError as e:
            if is_cancelled_exception(e):
                raise
            await workflow.execute_activity(
                finalize_tool_run_activity,
                ToolFinalizeInput(
                    tool_run_id=payload.tool_run_id,
                    user_id=payload.user_id,
                    reason=str(e),
                ),
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise
        finally:
            HeartBeatRunRegistry.get_instance().deregister(
                ctx=ctx, app_state=app_state, runnable_id=payload.tool_run_id
            )
