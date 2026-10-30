import asyncio
import json
from functools import lru_cache
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_fixed

from src.autobots.action.action.action_doc_model import ActionDoc, ActionResult
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks, TextObj
from src.autobots.action.action_result.action_result_doc_model import (
    ActionResultDoc,
    ActionResultUpdate,
)
from src.autobots.action.action_result.user_action_result import UserActionResult
from src.autobots.action.action_type.abc.ActionABC import ActionConfigType
from src.autobots.action.action_type.action_map import ACTION_MAP
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.action_team.action_team_result.action_team_result_model_doc import (
    ActionTeamResultUpdate,
)
from src.autobots.action_team.action_team_result.user_action_team_result import UserActionTeamResult
from src.autobots.api.webhook import Webhook
from src.autobots.core.clock.clock import Clock
from src.autobots.core.heartbeat.running_executions import HeartBeatRunRegistry
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.llm.chat_snapshot.chat_snapshot import apply_to_result
from src.autobots.run_metrics.run_metrics_writer import RunMetricsWriter
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.token_usage.token_usage_service import TokenUsageService
from src.autobots.user.user_orm_model import UserORM


class ActionDataTypes(BaseModel):
    type: ActionType
    config_create: Dict[str, Any] | None = None
    config_update: Dict[str, Any] | None = None
    config: Dict[str, Any] | None = None
    input: Dict[str, Any] | None = None
    output: Dict[str, Any] | None = None


class RunActionObj(BaseModel):
    config_dict: Dict[str, Any]
    input_dict: Dict[str, Any]
    output_dict: Dict[str, Any]
    status: EventResultStatus = EventResultStatus.processing


class ActionFactory:
    _EXCLUDED_FIELDS_FROM_UPDATE_DURING_RUN = {'queued_inputs'}

    @staticmethod
    @lru_cache
    def get_action_types() -> List[str]:
        action_types = [
            action_type for action_type in ActionType if not action_type.lower().startswith("mock")
        ]
        return action_types

    @staticmethod
    async def get_data_types(
        action_type: ActionType,
        is_get_config_create: bool = True,
        is_get_config_update: bool = True,
        is_get_config: bool = True,
        is_get_input: bool = True,
        is_get_output: bool = True,
    ) -> ActionDataTypes:
        try:
            action_data_types = ActionDataTypes(type=action_type)
            action = ACTION_MAP.get(action_type)
            if action:
                if is_get_config_create:
                    action_data_types.config_create = (
                        action.get_config_create_type().model_json_schema()
                    )
                if is_get_config_update:
                    action_data_types.config_update = (
                        action.get_config_update_type().model_json_schema()
                    )
                if is_get_config:
                    action_data_types.config = action.get_config_type().model_json_schema()
                if is_get_input:
                    action_data_types.input = action.get_input_type().model_json_schema()
                if is_get_output:
                    action_data_types.output = action.get_output_type().model_json_schema()
            return action_data_types
        except Exception as e:
            logger.error(f"ActionType does not exist {action_type}, error: {str(e)}")
            raise

    @staticmethod
    async def create_action_config(
        ctx: Context,
        app_state: AppState,
        action_type: ActionType,
        action_config_create: Dict[str, Any],
        user: UserORM,
        db: AsyncIOMotorDatabase,
    ) -> ActionConfigType:
        try:
            action_class = ACTION_MAP.get(action_type)
            if not action_class:
                raise Exception(f"Action {action_type} not found in ACTION_MAP")
            config_create = action_class.get_config_create_type().model_validate(
                action_config_create
            )
            config = await action_class.create_config(ctx, app_state, config_create, user=user, db=db)
            return config
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            raise

    @staticmethod
    async def update_action_config(
        ctx: Context,
        app_state: AppState,
        action_type: ActionType,
        action_config: Dict[str, Any],
        action_config_update: Dict[str, Any],
        user: UserORM,
        db: AsyncIOMotorDatabase,
    ) -> ActionConfigType:
        try:
            action_class = ACTION_MAP.get(action_type)
            if not action_class:
                raise Exception(f"Action {action_type} not found in ACTION_MAP")
            config = action_class.get_config_type().model_validate(action_config)
            config_update = action_class.get_config_update_type().model_validate(
                action_config_update
            )
            updated_config = await action_class.update_config(
                ctx, app_state, config, config_update, user=user, db=db
            )
            return updated_config
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            raise

    @staticmethod
    async def delete_action_config(
        ctx: Context,
        app_state: AppState,
        action_type: ActionType,
        action_config: Dict[str, Any],
        user: UserORM,
        db: AsyncIOMotorDatabase,
    ) -> ActionConfigType:
        try:
            action_class = ACTION_MAP.get(action_type)
            if not action_class:
                raise Exception(f"Action {action_type} not found in ACTION_MAP")
            config = action_class.get_config_type().model_validate(action_config)
            deleted_config = await action_class.delete_config(ctx, app_state, config, user=user, db=db)
            return deleted_config
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            raise

    @staticmethod
    async def run_action(
        ctx: Context,
        app_state: AppState,
        action_doc: ActionDoc,
        action_input_dict: Dict[str, Any],
        user: UserORM,
        db: AsyncIOMotorDatabase,
        notify: Notify | None = None,
    ) -> RunActionObj | Exception:
        logger.bind(
            ctx=ctx,
            action_id=action_doc.id,
            action_name=action_doc.name,
            # action_input=action_input_dict,
        ).info("Action run started")
        TokenUsageService.start_collection(app_state)
        run_status = EventResultStatus.error
        action_to_run = None
        try:
            action = ACTION_MAP.get(action_doc.type)
            if not action:
                logger.bind(ctx=ctx).error(f"Action {action_doc.type} not found in ACTION_MAP")
                raise Exception(f"Action {action_doc.type} not found in ACTION_MAP")
            config = action.get_config_type().model_validate(action_doc.config)
            prev_results: List[ActionResult] | None = action_doc.results
            updated_config = await action.update_config_with_prev_results(ctx, app_state, config, prev_results)
            logger.bind(ctx=ctx).info(f"Action Input : {action_input_dict}")
            input = action.get_input_type().model_validate(action_input_dict)
            logger.bind(ctx=ctx).info(f"Action validated input : {input}")
            action_to_run = action(action_config=updated_config, user=user, db=db)

            output = await action_to_run.run_action(ctx=ctx, app_state=app_state, action_input=input, notify=notify)

            status = EventResultStatus.success
            if isinstance(output, Exception):
                logger.bind(ctx=ctx, action_name=action_doc.name).exception(
                    f"Error in running action: {action_doc.name}"
                )
                # raise HTTPException(detail=f"Action: {action_doc.name}, error: {str(output)}", status_code=500)
                output = DataBlocks(blocks=[DataBlock(text=f"Error: {str(output)}")])
                status = EventResultStatus.error
            elif not output:
                logger.bind(ctx=ctx, action_name=action_doc.name).exception(
                    f"Error in running action: {action_doc.name}, error: Output is empty"
                )
                # raise HTTPException(detail=f"Action: {action_doc.name}, error: Output is empty",
                #                     status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
                output = DataBlocks(blocks=[DataBlock(text="Error: Output is empty")])
                status = EventResultStatus.error

            run_status = status
            run_action_obj = RunActionObj(
                # config_dict=updated_config.model_dump(exclude_none=True), # In ActionText2TextLlmChatOpenai reference between updated_config passed in __init__ and action_to_run.action_config is broken
                config_dict=action_to_run.action_config.model_dump(
                    exclude_none=True,
                    mode="json",
                    # Avoid returning/persisting raw multipart file bytes (can be huge and not JSON-safe).
                    exclude={"files"},
                ),  # action_to_run.action_config will have the latest config, eg. all messages in action_config of ActionText2TextLlmChatOpenai
                input_dict=input.model_dump(exclude_none=True, mode="json"),
                output_dict=output.model_dump(exclude_none=True, mode="json"),
                status=status,
            )
            logger.bind(
                ctx=ctx,
                action_name=action_doc.name,
                action_id=action_doc.id,
                # action_run_obj=run_action_obj,
            ).info("Action run completed")
            return run_action_obj
        except asyncio.CancelledError:
            run_status = EventResultStatus.cancelled
            raise
        except Exception as e:
            logger.bind(ctx=ctx, action_name=action_doc.name, action_id=action_doc.id).exception(
                f"Error in running action: {str(e)}"
            )
            return e
        finally:
            await TokenUsageService.flush(
                ctx=ctx,
                app_state=app_state,
                user=user,
                db=db,
                status=run_status,
                turn=getattr(getattr(action_to_run, "action_config", None), "chat_turn", None),
            )

    @staticmethod
    async def run_action_stream(
        ctx: Context,
        app_state: AppState,
        action_doc: ActionDoc,
        action_input_dict: Dict[str, Any],
        user: UserORM,
        db: AsyncIOMotorDatabase,
    ) -> RunActionObj | Exception:
        logger.bind(ctx=ctx, action_id=action_doc.id, action_name=action_doc.name).info(
            "Action run started"
        )

        def send(ev: str, **kw) -> bytes:
            return (json.dumps({"event": ev, **kw}, ensure_ascii=False) + "\n").encode("utf-8")

        try:
            action = ACTION_MAP.get(action_doc.type)
            if not action:
                logger.bind(ctx=ctx).error(f"Action {action_doc.type} not found in ACTION_MAP")
                raise Exception(f"Action {action_doc.type} not found in ACTION_MAP")
            config = action.get_config_type().model_validate(action_doc.config)
            prev_results: List[ActionResult] | None = action_doc.results
            updated_config = await action.update_config_with_prev_results(ctx, app_state, config, prev_results)
            input = action.get_input_type().model_validate(action_input_dict)
            action_to_run = action(action_config=updated_config, user=user, db=db)
            output = None
            agen = action_to_run.run_action(ctx=ctx, app_state=app_state, action_input=input)

            async for chunk in agen:
                # chunk = await agen.__anext__()
                try:
                    obj = json.loads(chunk.decode("utf-8"))
                    if obj.get("event") == "action-run":
                        # capture for server-side use (log/persist/webhook)
                        output = obj.get("error")
                        if not output:
                            output = DataBlocks.model_validate(obj.get("result"))
                        else:
                            raise Exception(str(output))
                except Exception as e:
                    raise e
                yield chunk

            # await action_to_run.run_action(ctx=ctx, action_input=input)

            status = EventResultStatus.success
            if isinstance(output, Exception):
                logger.bind(ctx=ctx, action_name=action_doc.name).exception(
                    f"Error in running action: {action_doc.name}, error: {str(output)}"
                )
                # raise HTTPException(detail=f"Action: {action_doc.name}, error: {str(output)}", status_code=500)
                output = DataBlocks(blocks=[DataBlock(text=f"Error: {str(output)}")])
                status = EventResultStatus.error
            elif not output:
                logger.bind(ctx=ctx, action_name=action_doc.name).exception(
                    f"Error in running action: {action_doc.name}, error: Output is empty"
                )
                # raise HTTPException(detail=f"Action: {action_doc.name}, error: Output is empty",
                #                     status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
                output = DataBlocks(blocks=[DataBlock(text="Error: Output is empty")])
                status = EventResultStatus.error

            run_action_obj = RunActionObj(
                # config_dict=updated_config.model_dump(exclude_none=True), # In ActionText2TextLlmChatOpenai reference between updated_config passed in __init__ and action_to_run.action_config is broken
                config_dict=action_to_run.action_config.model_dump(
                    exclude_none=True,
                    mode="json",
                    # Avoid returning/persisting raw multipart file bytes (can be huge and not JSON-safe).
                    exclude={"files"},
                ),  # action_to_run.action_config will have the latest config, eg. all messages in action_config of ActionText2TextLlmChatOpenai
                input_dict=input.model_dump(exclude_none=True, mode="json"),
                output_dict=output.model_dump(exclude_none=True, mode="json"),
                status=status,
            )
            logger.bind(
                ctx=ctx,
                action_name=action_doc.name,
                action_id=action_doc.id,
                # action_run_obj=run_action_obj,
            ).info("Action run completed")
            # return run_action_obj
            logger.bind(
                ctx=ctx,
                action_name=action_doc.name,
                action_id=action_doc.id,
                # action_run_obj=run_action_obj,
            ).info("Action factory streaming done, final result")
            yield send(
                "action-factory-action-run",
                result=run_action_obj.model_dump(exclude_none=True, mode="json"),
            )
        except Exception as e:
            logger.bind(ctx=ctx, action_name=action_doc.name, action_id=action_doc.id).exception(
                f"Error in running action: {str(e)}"
            )
            # return e
            yield send("action-factory-action-run", error=str(e))


    @staticmethod
    async def update_action_team_result_active_members(
        ctx: Context, 
        user: UserORM, 
        db: AsyncIOMotorDatabase,
        app_state: AppState, 
        action_result_id: str):
        logger.bind(ctx=ctx).info("Updating Action Team Results Active members")
        if not app_state.action_team_member_id:
            logger.bind(ctx=ctx).info("No need to update the active members dict list!! Already Updated or it is not a teams' action run!")
            return
        if not app_state.ancestors:
            logger.bind(ctx=ctx).info("Can't find ancestors -- so can't update current active members list")
            return 
        
        action_team_result_id = app_state.ancestors[1].get("result_id") ## hardcoded 
        if not action_team_result_id:
            logger.bind(ctx=ctx).info(f"Ancestors : {app_state.ancestors}")
        user_action_team_result = UserActionTeamResult(user, db)
        action_team_result_doc = await user_action_team_result.get_action_team_result(
            ctx,
            action_team_result_id
        )
        if not action_team_result_doc:
            logger.bind(ctx=ctx).exception(f"No Action Team Result Doc found for id : {action_result_id}")
            return
        
        action_team_result_doc.result.current_active_members = [{app_state.action_team_member_id : action_result_id}]
        action_team_result_doc = await user_action_team_result.update_action_team_result(
                    ctx=ctx,
                    action_team_result_id=action_team_result_doc.id,
                    action_team_update=ActionTeamResultUpdate(
                        result=action_team_result_doc.result
                    ),
                )
        logger.bind(ctx=ctx).info(f"Updated Current Active Team Members list to {action_team_result_doc.result.current_active_members}")
    
    @staticmethod
    async def run_action_in_background(
        ctx: Context,
        app_state: AppState,
        action_doc: ActionDoc,
        action_input_dict: Dict[str, Any],
        user_action_result: UserActionResult,
        db: AsyncIOMotorDatabase,
        action_result_id: str = None,
        is_stream: Optional[bool] = False,
        background_tasks: BackgroundTasks = None,
        webhook: Webhook | None = None,
        notify: Notify | None = None,
        fire_and_forget: bool = False,
    ) -> ActionResultDoc | None:
        logger.bind(
            ctx=ctx,
            action_id=action_doc.id,
            action_name=action_doc.name,
            action_result_id=action_result_id,
            # action_input=action_input_dict,
        ).info("Action run setup")
        action_result_doc = await user_action_result.init(
            app_state=app_state,
            action_result_id=action_result_id,
            action_doc=action_doc,
            action_input_dict=action_input_dict,
            db=db,
            ctx=ctx,
        )
        if app_state:
            await ActionFactory.update_action_team_result_active_members(ctx=ctx, user=user_action_result.user, db=db, app_state=app_state, action_result_id=action_result_doc.id)
        if not ctx.consumer_id and action_result_doc is not None:
            ctx.consumer_id = action_result_doc.id

        if action_result_doc is not None and not ctx.action_run_name:
            ctx.action_run_name = getattr(action_result_doc, "name", None)

        if not notify:
            notify = Notify(topic=action_result_doc.id)

        app_state.action_id = action_doc.id
        app_state.action_name = action_doc.name
        app_state.action_run_id = action_result_doc.id
        app_state.action_run_name = action_result_doc.name

        HeartBeatRunRegistry.get_instance().register(ctx=ctx , app_state=app_state, runnable_id=action_result_doc.id, runnable_type=AppCode.ACTION.value)

        # Run Action in background and update the Action Result with Output
        if background_tasks:
            # Run in background
            logger.bind(ctx=ctx).info("Starting background task with FastAPI BackgroundTasks")
            background_tasks.add_task(
                ActionFactory._run_action_as_background_task,
                ctx=ctx,
                app_state = app_state,
                action_input_dict=action_input_dict,
                action_result_doc=action_result_doc,
                user_action_result=user_action_result,
                db=db,
                notify=notify,
                webhook=webhook,
            )
        elif fire_and_forget:
            # Fire and forget using asyncio.create_task
            logger.bind(ctx=ctx).info("Starting fire-and-forget task with asyncio.create_task")
            asyncio.create_task(
                ActionFactory._run_action_as_background_task(
                    ctx=ctx,
                    app_state = app_state,
                    action_input_dict=action_input_dict,
                    action_result_doc=action_result_doc,
                    user_action_result=user_action_result,
                    db=db,
                    notify=notify,
                    webhook=webhook,
                )
            )
        else:
            # For testing
            logger.bind(ctx=ctx).info("Running action in foreground (no background tasks)")
            await ActionFactory._run_action_as_background_task(
                ctx=ctx,
                app_state = app_state,
                action_input_dict=action_input_dict,
                action_result_doc=action_result_doc,
                user_action_result=user_action_result,
                db=db,
                notify=notify,
                webhook=webhook,
            )
        return action_result_doc

    @staticmethod
    async def _run_action_as_background_task(
        ctx: Context,
        app_state: AppState,
        action_input_dict: Dict[str, Any],
        action_result_doc: ActionResultDoc,
        user_action_result: UserActionResult,
        db: AsyncIOMotorDatabase,
        notify: Notify,
        webhook: Webhook | None = None,
    ) -> None:
        """
        We are using indirection. This is because Background task doesn't work if the called function has a decorator.
        In our case `retry` decorator.
        """
        start_time = Clock.get_date_time()
        try:
            logger.bind(
                ctx=ctx,
                app_code=AppCode.ACTION_RUN.value,
                user_id=action_result_doc.user_id,
                action_id=action_result_doc.result.id,
                action_name=action_result_doc.result.name,
                action_version=action_result_doc.result.version,
                action_run_id=action_result_doc.id,
            ).info("Action Run Start")
            await ActionFactory._run_retry_action_as_background_task(
                ctx=ctx,
                app_state = app_state,
                action_input_dict=action_input_dict,
                action_result_doc=action_result_doc,
                user_action_result=user_action_result,
                db=db,
                notify=notify,
                webhook=webhook,
            )
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in running action several times: {str(e)}")
        finally:
            HeartBeatRunRegistry.get_instance().deregister(ctx=ctx, app_state=app_state, runnable_id=action_result_doc.id)

            end_time = Clock.get_date_time()
            logger.bind(
                ctx=ctx,
                app_code=AppCode.ACTION_RUN.value,
                user_id=action_result_doc.user_id,
                action_id=action_result_doc.result.id,
                action_name=action_result_doc.result.name,
                action_version=action_result_doc.result.version,
                action_run_id=action_result_doc.id,
                action_run_status=action_result_doc.status.value,
                duration=end_time.diff(start_time).in_seconds(),
            ).info("Action Run Complete")
            await RunMetricsWriter(db).record_action_run(
                ctx,
                action_result_doc,
                duration_seconds=end_time.diff(start_time).in_seconds(),
                is_root_run=not getattr(app_state, "action_team_result_id", None),
            )

    @staticmethod
    async def _continue_run(
        ctx: Context,
        app_state: AppState,
        action_result_doc: ActionResultDoc,
        user_action_result: UserActionResult,
        db: AsyncIOMotorDatabase,
        notify: Notify,
        webhook: Webhook | None = None,
    ) -> None:
        """Process next queued input if exists and current succeeded."""
        if not (
            action_result_doc.status == EventResultStatus.success
            and action_result_doc.queued_inputs
            and action_result_doc.queued_inputs.inputs
        ):
            return
        
        next_input = action_result_doc.queued_inputs.inputs.pop(0).model_dump(exclude_none=True)
        action_result_doc.queued_inputs.processed_count += 1
        action_result_doc.queued_inputs.version_number += 1
        
        # Set status to processing (prevents concurrent runs)
        action_result_doc.status = EventResultStatus.processing
        
        # Update top-level input/output (same as normal flow)
        action_result_doc.result.input = next_input
        action_result_doc.result.output = {}
        
        # Append new result entry for queued input (prevents overwriting previous turn)
        action_result_doc.result.results.append(ActionResult(input=next_input))
        
        # Update DB with queue, result, and status before processing next
        queue_update = ActionResultUpdate(
            queued_inputs=action_result_doc.queued_inputs,
            result=action_result_doc.result,
            status=action_result_doc.status
        )
        await user_action_result.update_action_result(action_result_doc.id, queue_update)
        
        logger.bind(
            ctx=ctx,
            action_run_id=action_result_doc.id,
            queue_remaining=len(action_result_doc.queued_inputs.inputs),
            processed_count=action_result_doc.queued_inputs.processed_count,
        ).info("Processing next queued input")
        
        # Continue with next input (sequential execution)
        await ActionFactory._run_action_as_background_task(
            ctx=ctx,
            app_state=app_state,
            action_input_dict=next_input,
            action_result_doc=action_result_doc,
            user_action_result=user_action_result,
            db=db,
            notify=notify,
            webhook=webhook,
        )

    @staticmethod
    @retry(stop=stop_after_attempt(3), wait=wait_fixed(1), reraise=True)
    async def _run_retry_action_as_background_task(
        ctx: Context,
        app_state: AppState,
        action_input_dict: Dict[str, Any],
        action_result_doc: ActionResultDoc,
        user_action_result: UserActionResult,
        db: AsyncIOMotorDatabase,
        notify: Notify,
        webhook: Webhook | None = None,
    ) -> None:
        if app_state is not None and app_state.cancellation is not None:
            app_state.cancellation.set_snapshot(None)
        try:
            logger.bind(
                ctx=ctx, action_run_id=action_result_doc.id, action_id=action_result_doc.result.id
            ).info("Starting _run_action_as_background_task")
            # Run the action
            run_action_obj: RunActionObj = await ActionFactory.run_action(
                ctx=ctx,
                app_state = app_state,
                action_doc=action_result_doc.result,
                action_input_dict=action_input_dict,
                user=user_action_result.user,
                db=db,
                notify=notify,
            )
            # if Action is error
            if isinstance(run_action_obj, Exception):
                raise run_action_obj
            # Action is a success
            # Update ActionDoc model config, input and output
            action_result_doc.status = run_action_obj.status
            action_result_doc.error_message = TextObj()
            action_result_doc.result.config = run_action_obj.config_dict
            action_result_doc.result.input = run_action_obj.input_dict
            action_result_doc.result.output = run_action_obj.output_dict
            if action_result_doc.result.results is None:
                action_result_doc.result.results = []
            # Update the last result with output instead of appending new item
            if action_result_doc.result.results:
                action_result_doc.result.results[-1].input = run_action_obj.input_dict
                action_result_doc.result.results[-1].output = run_action_obj.output_dict
            else:
                # Fallback: if results list is empty, append new item
                action_result_doc.result.results.append(
                    ActionResult(input=run_action_obj.input_dict, output=run_action_obj.output_dict)
                )
            logger.bind(
                ctx=ctx,
                action_run_id=action_result_doc.id,
                action_id=action_result_doc.result.id,
                # action_result_doc=action_result_doc,
            ).info("Action run success")
        except asyncio.CancelledError:
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
            # Action resulted in an error
            action_result_doc.status = EventResultStatus.error
            action_result_doc.error_message = TextObj(text=f"{type(e).__name__}: {e}")
            logger.bind(
                ctx=ctx,
                action_run_id=action_result_doc.id,
                action_id=action_result_doc.result.id,
                # action_result_doc=action_result_doc,
                error=str(e),
            ).exception("Action run error")
            raise e
        finally:
            # Finally persist the Action Result
            # Exclude queued_inputs to avoid overwriting concurrent queue additions
            excluded_fields = set(ActionFactory._EXCLUDED_FIELDS_FROM_UPDATE_DURING_RUN)
            if action_result_doc.status == EventResultStatus.cancelled:
                excluded_fields.discard("queued_inputs")
            action_result_update = ActionResultUpdate(
                **action_result_doc.model_dump(exclude=excluded_fields)
            )
            action_result_doc = await user_action_result.update_action_result(
                action_result_doc.id, action_result_update
            )
            logger.bind(
                ctx=ctx,
                action_run_id=action_result_doc.id,
                action_id=action_result_doc.result.id,
                # action_result_doc=action_result_doc,
            ).info("Action Result updated")
            # Send webhook
            if webhook:
                await webhook.send(action_result_doc.model_dump())

            # Continue with next queued input if exists
            try:
                await ActionFactory._continue_run(
                    ctx=ctx,
                    app_state = app_state,
                    action_result_doc=action_result_doc,
                    user_action_result=user_action_result,
                    db=db,
                    notify=notify,
                    webhook=webhook,
                )
            except Exception as e:
                logger.bind(
                    ctx=ctx,
                    action_run_id=action_result_doc.id,
                    error=str(e),
                ).exception("Error in continue_run")
