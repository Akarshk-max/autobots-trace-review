import asyncio
import inspect
from typing import Any

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, ValidationError

from src.autobots.action.action.common_action_models import TextObj
from src.autobots.conn.temporal.cancellation import cancel_workflow
from src.autobots.conn.temporal.client import get_temporal_client
from src.autobots.conn.temporal.tool.shared import ToolWorkflowInput
from src.autobots.conn.temporal.tool.workflow import ToolWorkflow
from src.autobots.conn.temporal.workflow_ids import tool_workflow_id
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.llm.tools.tool_result.tool_result_doc_model import (
    TERMINAL_TOOL_RUN_STATUSES,
    ToolResultCreate,
    ToolResultDoc,
    ToolResultDocsFound,
    ToolResultFind,
    ToolResultUpdate,
    ToolRun,
)
from src.autobots.llm.tools.tool_result.user_tool_result import UserToolResult
from src.autobots.llm.tools.tools_map import TOOLS_MAP
from src.autobots.user.user_orm_model import UserORM


class ToolNotFound(Exception):
    pass


class ToolConfigUnavailable(Exception):
    pass


class ToolRunNotFound(Exception):
    pass


class ToolArgsInvalid(Exception):
    pass


class ToolRunInFlight(Exception):
    pass


class ToolDescription(BaseModel):
    name: str
    description: str


class ToolConfig(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class ToolService:

    @staticmethod
    def list_tool_names() -> list[str]:
        return sorted(TOOLS_MAP.keys())

    @staticmethod
    def get_tool_description(tool_name: str) -> ToolDescription:
        tool = ToolService._get_tool(tool_name)
        return ToolDescription(name=tool_name, description=ToolService._description(tool))

    @staticmethod
    def get_tool_config(tool_name: str) -> ToolConfig:
        tool = ToolService._get_tool(tool_name)
        config_model = ToolService._config_model(tool)
        if config_model is None:
            raise ToolConfigUnavailable(tool_name)

        return ToolConfig(
            name=tool_name,
            description=ToolService._description(tool),
            input_schema=config_model.model_json_schema(),
        )

    @staticmethod
    async def run_tool(
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        tool_name: str,
        tool_args: dict[str, Any],
        webhook: dict[str, Any] | None = None,
    ) -> ToolResultDoc:
        ToolService._validate_tool_args(tool_name, tool_args)

        settings = SettingsProvider.sget()
        user_tool_result = UserToolResult(user, db)

        tool_result_doc = await user_tool_result.create_tool_result(
            ToolResultCreate(
                name=tool_name,
                status=EventResultStatus.processing,
                result=ToolRun(tool_name=tool_name, tool_args=tool_args),
            )
        )
        tool_run_id = tool_result_doc.id

        payload = ToolWorkflowInput(
            tool_run_id=tool_run_id,
            tool_name=tool_name,
            tool_args=tool_args,
            user_id=str(user.id),
            space_id=tool_result_doc.space_id,
            trace_id=ctx.trace_id,
            access_token=user.access_token,
            webhook=webhook,
            start_to_close_timeout_minutes=settings.TOOL_RUN_START_TO_CLOSE_TIMEOUT_MINUTES,
            output_text_to_file_char_limit=settings.TOOL_RUN_OUTPUT_TEXT_TO_FILE_CHAR_LIMIT,
        )

        try:
            client = await get_temporal_client()
            handle = await client.start_workflow(
                ToolWorkflow.run,
                payload,
                id=tool_workflow_id(tool_run_id),
                task_queue=settings.TEMPORAL_TASK_QUEUE,
            )
        except Exception as e:
            logger.bind(ctx=ctx, tool_run_id=tool_run_id).exception(
                f"Tool workflow dispatch failed: {str(e)}"
            )
            return await user_tool_result.update_tool_result(
                tool_run_id,
                ToolResultUpdate(
                    status=EventResultStatus.error, error_message=TextObj(text=str(e))
                ),
            )

        try:
            await asyncio.wait_for(
                handle.result(), timeout=settings.TOOL_RUN_INLINE_WAIT_SECONDS
            )
        except asyncio.TimeoutError:
            return tool_result_doc
        except Exception as e:
            logger.bind(ctx=ctx, tool_run_id=tool_run_id).exception(
                f"Tool workflow failed: {str(e)}"
            )
            return await user_tool_result.update_tool_result(
                tool_run_id,
                ToolResultUpdate(
                    status=EventResultStatus.error, error_message=TextObj(text=str(e))
                ),
            )

        return await user_tool_result.get_tool_result(tool_run_id) or tool_result_doc

    @staticmethod
    async def get_tool_run(
        user: UserORM, db: AsyncIOMotorDatabase, tool_run_id: str
    ) -> ToolResultDoc:
        return await ToolService.get_tool_run_doc(UserToolResult(user, db), tool_run_id)

    @staticmethod
    async def cancel_tool_run(
        user: UserORM, db: AsyncIOMotorDatabase, tool_run_id: str
    ) -> ToolResultDoc:
        user_tool_result = UserToolResult(user, db)
        tool_result_doc = await user_tool_result.get_tool_result(tool_run_id)
        if tool_result_doc is None:
            raise ToolRunNotFound(tool_run_id)

        if tool_result_doc.status in TERMINAL_TOOL_RUN_STATUSES:
            return tool_result_doc
        if tool_result_doc.status == EventResultStatus.request_cancel:
            return tool_result_doc

        cancelling_doc = await user_tool_result.update_tool_result(
            tool_run_id, ToolResultUpdate(status=EventResultStatus.request_cancel)
        )

        if not await cancel_workflow(tool_workflow_id(tool_run_id)):
            logger.bind(tool_run_id=tool_run_id).info(
                "No running tool workflow; finalising cancellation"
            )
            return await ToolService._settle_or_read(
                user_tool_result,
                tool_run_id,
                ToolResultUpdate(status=EventResultStatus.cancelled),
            )

        return cancelling_doc

    @staticmethod
    async def delete_tool_run(
        user: UserORM, db: AsyncIOMotorDatabase, tool_run_id: str
    ) -> ToolResultDoc:
        user_tool_result = UserToolResult(user, db)
        tool_result_doc = await user_tool_result.get_tool_result(tool_run_id)
        if tool_result_doc is None:
            raise ToolRunNotFound(tool_run_id)

        # Deleting a live run would leave the runner writing to a record that no
        # longer exists, failing the activity instead of finishing it.
        if tool_result_doc.status not in TERMINAL_TOOL_RUN_STATUSES:
            raise ToolRunInFlight(tool_run_id)

        deleted_doc = await user_tool_result.delete_tool_result(tool_run_id)
        if deleted_doc is None:
            raise ToolRunNotFound(tool_run_id)
        return deleted_doc

    @staticmethod
    async def list_tool_runs(
        user: UserORM,
        db: AsyncIOMotorDatabase,
        tool_result_find: ToolResultFind,
        limit: int = 20,
        offset: int = 0,
    ) -> ToolResultDocsFound:
        return await UserToolResult(user, db).list_tool_results(
            tool_result_find=tool_result_find, limit=limit, offset=offset
        )

    @staticmethod
    async def _settle_or_read(
        user_tool_result: UserToolResult,
        tool_run_id: str,
        tool_result_update: ToolResultUpdate,
    ) -> ToolResultDoc:
        """Settle the run, or return what settled it first."""
        settled_doc = await user_tool_result.settle_tool_result(
            tool_run_id, tool_result_update
        )
        if settled_doc is not None:
            return settled_doc
        return await ToolService.get_tool_run_doc(user_tool_result, tool_run_id)

    @staticmethod
    async def get_tool_run_doc(
        user_tool_result: UserToolResult, tool_run_id: str
    ) -> ToolResultDoc:
        tool_result_doc = await user_tool_result.get_tool_result(tool_run_id)
        if tool_result_doc is None:
            raise ToolRunNotFound(tool_run_id)
        return tool_result_doc

    @staticmethod
    def _validate_tool_args(tool_name: str, tool_args: dict[str, Any]) -> None:
        """Reject a bad payload before a record and a workflow exist for it."""
        config_model = ToolService._config_model(ToolService._get_tool(tool_name))
        if config_model is None:
            return
        try:
            config_model.model_validate(tool_args)
        except ValidationError as e:
            raise ToolArgsInvalid(str(e)) from e

    @staticmethod
    def _get_tool(tool_name: str) -> Any:
        tool = TOOLS_MAP.get(tool_name)
        if tool is None:
            raise ToolNotFound(tool_name)
        return tool

    @staticmethod
    def _config_model(tool: Any) -> type[BaseModel] | None:
        if inspect.isfunction(tool):
            param = inspect.signature(tool).parameters.get("config")
            annotation = param.annotation if param is not None else None
        else:
            get_config_type = getattr(tool, "get_config_type", None)
            annotation = get_config_type() if get_config_type else None

        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return annotation
        return None

    @staticmethod
    def _description(tool: Any) -> str:
        if inspect.isfunction(tool):
            return inspect.cleandoc(tool.__doc__ or "")
        get_description = getattr(tool, "get_description", None)
        return inspect.cleandoc(get_description() or "") if get_description else ""
