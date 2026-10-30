import asyncio

from loguru import logger
from temporalio import activity
from temporalio.exceptions import CancelledError

from src.autobots.action.action.common_action_models import TextObj
from src.autobots.api.webhook import Webhook
from src.autobots.conn.temporal.ctx_utils import build_ctx
from src.autobots.conn.temporal.heartbeat import run_with_heartbeat
from src.autobots.conn.temporal.resolve_user import resolve_run_user
from src.autobots.conn.temporal.tool.shared import ToolFinalizeInput, ToolWorkflowInput
from src.autobots.core.cancellation.run_cancellation import RunCancellation
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.app_state import AppState
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.llm.tools.tool_factory import ToolFactory
from src.autobots.llm.tools.tool_result.tool_result_doc_model import ToolResultUpdate, ToolRun
from src.autobots.llm.tools.tool_result.user_tool_result import UserToolResult
from src.autobots.user.user import User
from src.autobots.user.user_orm_model import UserORM


@activity.defn
async def execute_tool_activity(payload: ToolWorkflowInput) -> dict:
    db = next(get_mongo_db())
    ctx = build_ctx(
        user_id=payload.user_id,
        trace_id=payload.trace_id,
        consumer_id=payload.tool_run_id,
    )

    user_orm = await resolve_run_user(
        ctx=ctx,
        db=db,
        user_id=payload.user_id,
        space_id=payload.space_id,
        access_token=payload.access_token,
    )

    user_tool_result = UserToolResult(user_orm, db)
    webhook = Webhook.model_validate(payload.webhook) if payload.webhook else None

    logger.bind(
        ctx=ctx,
        code=AppCode.TOOL_FACTORY,
        tool_name=payload.tool_name,
        tool_run_id=payload.tool_run_id,
    ).info(f"Running tool in runner: {payload.tool_name}")

    app_state = AppState()
    app_state.set_cancellation(RunCancellation(run_id=payload.tool_run_id))

    try:
        tool_output = await run_with_heartbeat(
            ToolFactory(user=user_orm, db=db).run_tool(
                ctx=ctx,
                app_state=app_state,
                tool_name=payload.tool_name,
                tool_args=payload.tool_args,
                output_text_to_file_char_limit=payload.output_text_to_file_char_limit,
                raise_on_error=True,
            ),
            app_state.cancellation,
        )
        tool_result_update = ToolResultUpdate(
            status=EventResultStatus.success,
            result=ToolRun(
                tool_name=payload.tool_name,
                tool_args=payload.tool_args,
                output=tool_output.model_dump(),
            ),
        )
    except (CancelledError, asyncio.CancelledError):
        await user_tool_result.update_tool_result(
            payload.tool_run_id,
            ToolResultUpdate(
                status=EventResultStatus.cancelled, error_message=TextObj(text="Cancelled")
            ),
        )
        logger.bind(
            ctx=ctx, code=AppCode.TOOL_FACTORY, tool_run_id=payload.tool_run_id
        ).info("Tool run cancelled")
        raise

    except Exception as e:
        logger.bind(
            ctx=ctx,
            code=AppCode.TOOL_FACTORY,
            tool_name=payload.tool_name,
            tool_run_id=payload.tool_run_id,
        ).exception(f"Tool run failed: {str(e)}")
        tool_result_update = ToolResultUpdate(
            status=EventResultStatus.error,
            error_message=TextObj(text=str(e)),
        )

    tool_result_doc = await user_tool_result.update_tool_result(
        payload.tool_run_id, tool_result_update
    )

    if webhook:
        await webhook.send(tool_result_doc.model_dump())

    return tool_result_doc.model_dump(mode="json")


@activity.defn
async def finalize_tool_run_activity(payload: ToolFinalizeInput) -> dict:
    """Settle a run whose activity died before it could write a terminal status."""
    db = next(get_mongo_db())
    ctx = build_ctx(user_id=payload.user_id, consumer_id=payload.tool_run_id)

    user_orm = UserORM(id=payload.user_id)
    user_doc = await User.get(ctx=ctx, user=user_orm, id=payload.user_id, db=db)
    user_orm = UserORM.model_validate(user_doc.model_dump())

    user_tool_result = UserToolResult(user_orm, db)
    settled_doc = await user_tool_result.settle_tool_result(
        payload.tool_run_id,
        ToolResultUpdate(
            status=EventResultStatus.stuck, error_message=TextObj(text=payload.reason)
        ),
    )
    if settled_doc is None:
        return {"settled": False}

    logger.bind(
        ctx=ctx, code=AppCode.TOOL_FACTORY, tool_run_id=payload.tool_run_id
    ).warning(f"Tool run left unfinished, marked stuck: {payload.reason}")
    return {"settled": True, "status": settled_doc.status.value}
