import io
from typing import Annotated, Any, Dict, List

import httpx
import requests
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Response
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, ConfigDict, Field
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse
from starlette.requests import Request

from src.autobots import SettingsProvider
from src.autobots.action.action.action_doc_model import ActionDoc, ActionLiteDoc
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action.action_result.user_action_result import UserActionResult
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    ActionText2TextLlmChatOpenai,
    LlmChatOpenaiConfig,
)
from src.autobots.action_graph.action_graph.action_graph_doc_model import ActionGraphLiteDoc
from src.autobots.action_graph.action_graph.user_action_graph import UserActionGraphs
from src.autobots.action_graph.action_graph_result.user_action_graph_result import (
    UserActionGraphResult,
)
from src.autobots.integrations.integration_model import IntegrationDoc
from src.autobots.integrations.integration_type.slack_auth import SlackAuth, SlackAuthModel
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.integrations.slack.slack_runnable_doc_model import (
    SlackInfo,
    SlackRunnableCreate,
    SlackRunnableDoc,
)
from src.autobots.integrations.slack.user_slack_runnable import UserSlackRunnable
from src.autobots.user.user import User
from src.autobots.user.user_orm_model import UserDoc, UserORM
from src.autobots.data_model.app_state import AppState

router = APIRouter(
    prefix=f"{SettingsProvider.sget().API_WEBHOOKS}{SettingsProvider.sget().API_PATH_SLACK}",
    tags=[SettingsProvider.sget().API_WEBHOOKS],
)


class SlackAuthorization(BaseModel):
    user_id: str
    team_id: str | None = None
    enterprise_id: str | None = None
    is_bot: bool | None = None
    is_enterprise_install: bool | None = None


class SlackEvent(BaseModel):
    type: str
    text: str = ""
    user: str
    channel: str
    event_ts: str
    ts: str
    thread_ts: str | None = None
    blocks: List[
        Dict[str, Any]
    ]  # Later do Block.parse_all(slack_event.blocks) if event.get("blocks") else []
    files: List[Dict[str, Any]] | None = None
    client_msg_id: str | None = None
    team: str | None = None
    parent_user_id: str | None = None
    channel_type: str | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)


class SlackData(BaseModel):
    token: str
    team_id: str
    api_app_id: str
    event: SlackEvent
    type: str
    event_id: str
    event_time: int
    authorizations: List[SlackAuthorization] = Field(default_factory=list)
    is_ext_shared_channel: bool = False
    event_context: str


@router.post("/events")
async def slack_events(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    try:
        ctx = request.state.context
        app_state: AppState = request.state.app_state
        logger.bind(ctx=ctx).info("Slack Event")

        data = await request.json()
        if data and data.get("type") and data.get("type") == "url_verification":
            challenge = data.get("challenge")
            # token = data.get("token")
            return Response(content=challenge, media_type="text/plain")
        # Handle incoming Slack events using the SlackRequestHandler
        background_tasks.add_task(background_process_slack_event, ctx, app_state, db, data)

    except Exception as e:
        # Return an HTTP 400 error for any exceptions
        raise HTTPException(status_code=400, detail=f"Error in processing Slack Event: {str(e)}")


async def background_process_slack_event(
    ctx: Context, app_state: AppState, db: AsyncIOMotorDatabase, data: dict[str, Any]
):
    if data and data.get("event") and data.get("event").get("type"):
        event_type = data.get("event").get("type")
        if event_type == "app_mention" or event_type == "message":
            logger.bind(ctx=ctx, event_type=event_type).info("Webhook Slack App_Mention started")
            event = data.get("event")
            response = await handle_slack_event(event=event, ctx=ctx, app_state=app_state, db=db)
            logger.bind(ctx=ctx, response_status_code=response.status_code).info(
                "Webhook Slack App_Mention completed"
            )


# Define a sample Slack event listener
async def handle_slack_event(
    event: dict,
    ctx: Context,
    app_state: AppState,
    db: AsyncIOMotorDatabase,
) -> AsyncSlackResponse:
    try:
        logger.bind(ctx=ctx, event=event).info("Slack Event")
        slack_event = SlackEvent(**event)
        thread_ts = slack_event.thread_ts if slack_event.thread_ts else slack_event.ts
        # channel_id = event.get("channel", "")
        # thread_ts = event.get("thread_ts", event.get("ts"))
        # slack_user_id = event.get("user", "")
        # slack_team_id: str | None = event.get("team", "")

        user: UserORM | Exception = await get_app_user_from_slack_user(
            ctx=ctx, db=db, slack_user_id=slack_event.user, slack_team_id=slack_event.team
        )

        ctx.user_id = str(user.id)

        integration_doc: IntegrationDoc | None = await UserIntegration.get_by_slack_user(
            ctx=ctx, db=db, slack_user_id=slack_event.user, slack_team_id=slack_event.team, decrypt=True
        )
        assert integration_doc
        slack_auth_model = SlackAuth.get_auth_model_type().model_validate(integration_doc.auth)

        slack_runnable_doc: (
            SlackRunnableDoc | None
        ) = await UserSlackRunnable.get_by_slack_thread_id(
            ctx=ctx, db=db, user=user, slack_thread_id=thread_ts
        )

        data_block = await build_datablock_from_slack_event(
            ctx=ctx, db=db, user=user, slack_event=slack_event, slack_auth_model=slack_auth_model
        )
        full_text = event.get(
            "text", ""
        ).strip()  # eg. '<@U07K7F83Y22> /graph/ Create a jingle for slack related development'
        message_text = full_text.strip()
        if full_text.startswith("<"):  # for message with @kiwi else use as is
            message_text_parts = full_text.split(">")
            message_text = "".join(message_text_parts[1:]).strip()
            data_block.text = message_text

        data_blocks = DataBlocks()
        if slack_runnable_doc:
            match slack_runnable_doc.runnable_type:
                case "action":
                    data_blocks = await rerun_action_for_slack(
                        ctx=ctx,
                        app_state=app_state,
                        db=db,
                        user=user,
                        slack_runnable_doc=slack_runnable_doc,
                        input=data_block,
                    )

        else:  # not slack_runnable_doc (i.e new run, and rely on runnable name)
            runnable_name: str = ""
            text_without_runnable_name = ""
            if message_text.startswith("/"):
                text_parts = message_text.split("/")
                if len(text_parts) >= 3:
                    runnable_name = text_parts[1].strip()
                    text_without_runnable_name = "".join(text_parts[2:]).strip()
                    data_block.text = text_without_runnable_name

            slack_info = SlackInfo(
                slack_user_id=slack_event.user,
                slack_thread_id=thread_ts,
                slack_channel_id=slack_event.channel,
            )

            for i in range(1):
                if runnable_name:
                    data_blocks = await run_action_graph_for_slack(
                        ctx=ctx, app_state=app_state, db=db, user=user, runnable_name=runnable_name, input=data_block
                    )
                    if len(data_blocks.blocks) > 0:
                        break
                    data_blocks = await run_action_for_slack(
                        ctx=ctx,
                        app_state=app_state,
                        db=db,
                        user=user,
                        runnable_name=runnable_name,
                        input=data_block,
                        slack_info=slack_info,
                    )
                    if len(data_blocks.blocks) > 0:
                        break
                elif data_block.text or len(data_block.urls) > 0:
                    data_blocks = await run_action_id_for_slack(
                        ctx=ctx, app_state=app_state, db=db, user=user, input=data_block, slack_info=slack_info
                    )
                    if len(data_blocks.blocks) > 0:
                        break

        if len(data_blocks.blocks) == 0:
            raise HTTPException(
                status_code=500, detail="Error with Kiwi AI Bot, DataBlocks is empty"
            )
        # Always reply in thread
        response = await post_to_slack(
            ctx=ctx,
            integration_doc=integration_doc,
            channel_id=slack_event.channel,
            thread_ts=thread_ts,
            slack_user_id=slack_event.user,
            data_blocks=data_blocks,
        )
        return response
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Slack webhook error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


async def rerun_action_for_slack(
    ctx: Context,
    app_state: AppState,
    db: AsyncIOMotorDatabase,
    user: UserORM,
    slack_runnable_doc: SlackRunnableDoc,
    input: DataBlock,
) -> DataBlocks:
    data_blocks = DataBlocks()
    user_actions = UserActions(user, db)
    action_doc: ActionDoc | None = await user_actions.get_owned_or_shared_or_system_actions(
        ctx=ctx, action_id=slack_runnable_doc.runnable_id, empty_shared_action_config=False
    )
    if action_doc:
        user_action_result = UserActionResult(user, db)
        action_result_doc = await ActionFactory().run_action_in_background(
            ctx=ctx,
            app_state=app_state,
            action_doc=action_doc,
            action_input_dict=input.model_dump(),
            user_action_result=user_action_result,
            action_result_id=slack_runnable_doc.run_id,
            db=db,
            background_tasks=None,
            webhook=None,
        )
        if action_result_doc:
            data_blocks = DataBlocks.model_validate(action_result_doc.result.output)
        else:
            pass
    return data_blocks


async def run_action_graph_for_slack(
    ctx: Context, app_state: AppState, db: AsyncIOMotorDatabase, user: UserORM, runnable_name: str, input: DataBlock
) -> DataBlocks:
    data_blocks = DataBlocks()
    user_action_graph = UserActionGraphs(user=user, db=db)
    action_graph_lite_doc = await user_action_graph.get_owned_or_shared_by_name(
        ctx=ctx, action_graph_name=runnable_name
    )
    if action_graph_lite_doc:
        user_actions = UserActions(user, db)
        user_action_graph_result = UserActionGraphResult(user, db)
        user_action_graph_result_doc = await UserActionGraphs(user=user, db=db).run_in_background(
            ctx=ctx,
            app_state=app_state,
            user_actions=user_actions,
            user_action_graph_result=user_action_graph_result,
            action_graph_id=action_graph_lite_doc.id,
            input=input,
        )
        if user_action_graph_result_doc:
            data_blocks.blocks += [
                DataBlock(text=f"{runnable_name} is {user_action_graph_result_doc.status}")
            ]
            for node_id, action_doc in user_action_graph_result_doc.result.output.items():
                try:
                    action_output = DataBlocks.model_validate(action_doc.output)
                    action_name = action_doc.name if action_doc.name else "User"
                    action_name_block = DataBlock(text=f"\n{action_name}:")
                    action_output.blocks.insert(0, action_name_block)
                    data_blocks.blocks += action_output.blocks
                except Exception as e:
                    logger.bind(ctx=ctx).exception(f"Error in model validation: {str(e)}")
        else:
            pass
    return data_blocks


async def run_action_for_slack(
    ctx: Context,
    app_state: AppState,
    db: AsyncIOMotorDatabase,
    user: UserORM,
    runnable_name: str,
    input: DataBlock,
    slack_info: SlackInfo,
) -> DataBlocks:
    data_blocks = DataBlocks()
    user_actions = UserActions(user, db)
    action_lite_doc = await user_actions.get_lite_action_by_name(ctx=ctx, action_name=runnable_name)
    if action_lite_doc:
        action_doc = await user_actions.get_owned_or_shared_or_system_actions(
            ctx=ctx, action_id=action_lite_doc.id, empty_shared_action_config=False
        )
        user_action_result = UserActionResult(user, db)
        action_result_doc = await ActionFactory().run_action_in_background(
            ctx=ctx,
            app_state=app_state,
            action_doc=action_doc,
            action_input_dict=input.model_dump(),
            user_action_result=user_action_result,
            db=db,
            action_result_id=None,
            background_tasks=None,
            webhook=None,
        )
        if action_result_doc:
            slack_runnable_create = SlackRunnableCreate(
                slack_user_id=slack_info.slack_user_id,
                slack_thread_id=slack_info.slack_thread_id,
                slack_channel_id=slack_info.slack_channel_id,
                runnable_type="action",
                runnable_id=action_result_doc.result.id,
                run_id=action_result_doc.id,
            )
            slack_runnable_doc = await UserSlackRunnable.create(
                ctx=ctx, db=db, user=user, create=slack_runnable_create
            )
            logger.bind(ctx=ctx, **slack_runnable_doc.model_dump()).info(
                "Created slack_runnable_doc"
            )
            data_blocks = DataBlocks.model_validate(action_result_doc.result.output)
        else:
            pass
    return data_blocks


async def run_action_id_for_slack(
    ctx: Context,
    app_state: AppState,
    db: AsyncIOMotorDatabase,
    user: UserORM,
    input: DataBlock,
    slack_info: SlackInfo,
    action_id: str = "69295914ccbcd104b2e7446f",  # AutoBot action ID by system user
) -> DataBlocks:
    """Run action by ID in Slack, with AutoBots as default"""
    data_blocks = DataBlocks()
    user_actions = UserActions(user, db)

    action_doc = await user_actions.get_owned_or_shared_or_system_actions(
        ctx=ctx, action_id=action_id, empty_shared_action_config=False
    )

    if action_doc:
        logger.bind(
            ctx=ctx, action_id=action_id, action_name=action_doc.name, action_type=action_doc.type
        ).info(f"Running action by ID: {action_doc.name} ({action_doc.type})")
        user_action_result = UserActionResult(user, db)
        action_result_doc = await ActionFactory().run_action_in_background(
            ctx=ctx,
            app_state=app_state,
            action_doc=action_doc,
            action_input_dict=input.model_dump(),
            user_action_result=user_action_result,
            db=db,
            action_result_id=None,
            background_tasks=None,
            webhook=None,
        )
        if action_result_doc:
            slack_runnable_create = SlackRunnableCreate(
                slack_user_id=slack_info.slack_user_id,
                slack_thread_id=slack_info.slack_thread_id,
                slack_channel_id=slack_info.slack_channel_id,
                runnable_type="action",
                runnable_id=action_result_doc.result.id,
                run_id=action_result_doc.id,
            )
            slack_runnable_doc = await UserSlackRunnable.create(
                ctx=ctx, db=db, user=user, create=slack_runnable_create
            )
            logger.bind(ctx=ctx, **slack_runnable_doc.model_dump()).info(
                "Created slack_runnable_doc"
            )
            data_blocks = DataBlocks.model_validate(action_result_doc.result.output)
        else:
            pass
    else:
        logger.bind(ctx=ctx, action_id=action_id).error(f"Action not found with ID: {action_id}")

    return data_blocks


async def run_llm_for_slack(
    ctx: Context, app_state: AppState, db: AsyncIOMotorDatabase, user: UserORM, runnable_name: str, input: DataBlock
) -> DataBlocks:
    data_blocks = DataBlocks()
    action_config = LlmChatOpenaiConfig(
        provider="azure",
        model="model-router",
        messages=[
            {"role": "system", "content": "You are an Autonomous AI Slack Bot called Kiwi AI"}
        ],
        tools=[
            "search_web",
            "read_url",
            "api_call",
            "pdf_parser",
            "file_search_tool",
            "get_date_time",
            "sleep",
            "audio_transcription",
            "text_to_speech",
            "audio_translate",
            "coding_assistant",
            "image_generator_openai",
        ],
    )
    action = ActionText2TextLlmChatOpenai(action_config=action_config, user=user, db=db)
    action_output: DataBlocks | Exception = await action.run_action(ctx=ctx, app_state=app_state, action_input=input)
    if isinstance(action_output, DataBlocks):
        data_blocks = action_output
    return data_blocks


async def post_to_slack(
    ctx: Context,
    integration_doc: IntegrationDoc,
    channel_id: str,
    thread_ts: str,
    slack_user_id: str,
    data_blocks: DataBlocks,
) -> AsyncSlackResponse:
    response_text = await build_slack_response_from_datablocks(
        ctx=ctx, slack_user_id=slack_user_id, data_blocks=data_blocks
    )
    bot_access_token = integration_doc.auth.get("access_token")
    client = AsyncWebClient(token=bot_access_token)
    response: AsyncSlackResponse = await client.chat_postMessage(
        text=response_text, channel=channel_id, thread_ts=thread_ts
    )
    return response


async def build_datablock_from_slack_event(
    ctx: Context,
    db: AsyncIOMotorDatabase,
    user: UserORM,
    slack_auth_model: SlackAuthModel,
    slack_event: SlackEvent,
) -> DataBlock:
    try:
        data_block = DataBlock()
        # slack_event = SlackEvent(**event)
        # blocks = Block.parse_all(slack_event.blocks) if slack_event.blocks else []
        logger.bind(ctx=ctx, slack_event=slack_event.model_dump()).info("slack event")
        # Always reply in thread
        message_text = slack_event.text
        data_block.text = message_text
        files = slack_event.files if slack_event.files else []
        public_s3 = get_public_s3()
        user_files = UserFiles(user=user, s3=public_s3)
        # pub_url = None
        for file in files:
            try:
                filename = file.get("name", "")
                assert filename != ""
                file_url = file.get("url_private", "")
                assert file_url != ""
                if filename and file_url:
                    async with httpx.AsyncClient(follow_redirects=True) as client:
                        res = await client.get(
                            file_url,
                            headers={"Authorization": f"Bearer {slack_auth_model.access_token}"},
                        )
                        content_bytes = res.content
                        if res.status_code != 200:
                            logger.bind(
                                ctx=ctx,
                                status_coded=res.status_code,
                                filename=filename,
                                file_url=file_url,
                                content=content_bytes.decode("utf-8"),
                            ).error("Error in downloading file from Slack Event")
                            continue
                        content = io.BytesIO(content_bytes)
                        file_metas = await user_files.upload_binary(
                            ctx=ctx, file_obj=content, filename=filename
                        )
                        file_meta = file_metas[0]
                        logger.bind(
                            ctx=ctx,
                            filename=filename,
                        ).info("Building datablock for file in slack event")
                        data_block.urls.append(str(file_meta.url))
            except Exception as e:
                logger.bind(ctx=ctx).exception(
                    f"Error in building datablock for file in slack event: {str(e)}"
                )
        return data_block
    except Exception as e:
        logger.bind(ctx=ctx).info(f"Error in building Datablock from Slack Event: {str(e)}")
        raise


async def build_slack_response_from_datablocks(
    ctx: Context,
    slack_user_id: str,
    data_blocks: DataBlocks,
):
    response_text = ""
    if len(data_blocks.blocks) > 0:
        response_text += f"Hi <@{slack_user_id}>,\n"
    for block in data_blocks.blocks:
        try:
            response_text += f"{block.text}\n"
        except Exception as e:
            logger.bind(ctx=ctx).info(f"Error in building Slack Response: {str(e)}")
    return response_text


@router.post("/command")
async def run_slack_cmd(
    request: Request,
    token: Annotated[str, Form()],
    command: Annotated[str, Form()],
    text: Annotated[str, Form()],
    response_url: Annotated[str, Form()],
    trigger_id: Annotated[str, Form()],
    user_id: Annotated[str, Form()],
    team_id: Annotated[str, Form()],
    team_domain: Annotated[str, Form()],
    channel_id: Annotated[str, Form()],
    channel_name: Annotated[str, Form()],
    api_app_id: Annotated[str, Form()],
    background_tasks: BackgroundTasks,
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    """
    commands:
    /kiwi "user msg"
    /kiwi "/name/ usr msg"
    <name> is name of Action or Action Graph
    """
    ctx = request.state.context
    app_state = request.state.app_state
    logger.bind(ctx=ctx).info("Slack webhook for slash command started")
    user: UserORM = await get_app_user_from_slack_user(
        ctx=ctx, db=db, slack_user_id=user_id, slack_team_id=team_id
    )
    # user = UserORM(id=UUID(SettingsProvider.sget().SYSTEM_USER))

    subcommand = None
    text_without_subcommand = None
    if text.startswith("/"):
        text_parts = text.split("/")
        subcommand = text_parts[1]
        text_without_subcommand = text[len(subcommand) + 3 :]
    else:
        text_without_subcommand = text

    data_block = DataBlock(text=text_without_subcommand)
    try:
        if text.startswith("/"):
            action_graph_lite_doc = await UserActionGraphs(
                user=user, db=db
            ).get_owned_or_shared_by_name(ctx=ctx, action_graph_name=subcommand)
            if action_graph_lite_doc:
                background_tasks.add_task(
                    process_graph,
                    ctx=ctx,
                    app_state=app_state,
                    request=request,
                    action_graph_lite_doc=action_graph_lite_doc,
                    subcommand=subcommand,
                    input=data_block,
                    response_url=response_url,
                    db=db,
                    user=user,
                )
            else:
                lite_action_doc = await UserActions(user=user, db=db).get_lite_action_by_name(
                    ctx=ctx, action_name=subcommand
                )
                if lite_action_doc:
                    background_tasks.add_task(
                        process_action,
                        ctx=ctx,
                        app_state=app_state,
                        request=request,
                        action_lite_doc=lite_action_doc,
                        subcommand=subcommand,
                        input=data_block,
                        response_url=response_url,
                        db=db,
                        user=user,
                    )
        else:
            background_tasks.add_task(
                process_base_llm,
                ctx=ctx,
                app_state=app_state,
                request=request,
                input=data_block,
                response_url=response_url,
                db=db,
                user=user,
            )
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in Slack slash command webhook: {str(e)}")
        return {"response_type": "ephemeral", "text": "Sorry, that didn't work. Please try again."}

    return {"response_type": "ephemeral", "text": f"You: {text}"}


async def process_graph(
    ctx: Context,
    app_state: AppState,
    request: Request,
    action_graph_lite_doc: ActionGraphLiteDoc,
    subcommand: str,
    input: DataBlock,
    response_url: str,
    db: AsyncIOMotorDatabase,
    user: UserORM,
):
    logger.bind(ctx=ctx).info("Slack command now Processing graph")
    # user = UserORM(id=UUID(SettingsProvider.sget().SYSTEM_USER))
    user_actions = UserActions(user, db)
    user_action_graph_result = UserActionGraphResult(user, db)
    resp = await UserActionGraphs(user=user, db=db).run_in_background(
        ctx=ctx,
        app_state=app_state,
        user_actions=user_actions,
        user_action_graph_result=user_action_graph_result,
        action_graph_id=action_graph_lite_doc.id,
        input=input,
    )
    response_data = {
        "text": f"{action_graph_lite_doc.name}:\n{resp.status.upper()} for {input.text} ",
    }
    requests.post(response_url, json=response_data)


async def process_action(
    ctx: Context,
    app_state: AppState,
    request: Request,
    action_lite_doc: ActionLiteDoc,
    subcommand: str,
    input: DataBlock,
    response_url: str,
    db: AsyncIOMotorDatabase,
    user: UserORM,
):
    logger.bind(ctx=ctx).info("Slack command now Processing action")
    data_block_dict = input.model_dump()

    # ctx = request.state.context
    # user = UserORM(id=UUID(SettingsProvider.sget().SYSTEM_USER))
    user_actions = UserActions(user=user, db=db)
    action_doc = await user_actions.get_owned_or_shared_or_system_actions(
        ctx=ctx,
        action_id=action_lite_doc.id,
        empty_shared_action_config=False,
    )
    user_action_result = UserActionResult(user=user, db=db)
    action_result_doc = await ActionFactory().run_action_in_background(
        ctx=ctx,
        app_state=app_state,
        action_doc=action_doc,
        action_input_dict=data_block_dict,
        user_action_result=user_action_result,
        action_result_id=None,
        db=db,
        background_tasks=None,
        webhook=None,
    )

    result_text = ""
    if action_result_doc.result.type == ActionText2TextLlmChatOpenai.type:
        data_blocks: DataBlocks = ActionText2TextLlmChatOpenai.get_output_type().model_validate(
            action_result_doc.result.output
        )
        for block in data_blocks.blocks:
            result_text += f"{block.text}\n"
    else:
        result_text = action_result_doc.result.output

    response_data = {
        # "type": "section",
        "text": f"{action_doc.name}:\n{result_text}"
    }
    requests.post(response_url, json=response_data)


async def process_base_llm(
    ctx: Context,
    app_state: AppState,
    request: Request,
    input: DataBlock,
    response_url: str,
    db: AsyncIOMotorDatabase,
    user: UserORM,
):
    logger.bind(ctx=ctx).info("Slack command now Processing llm")
    # data_block = input.model_dump()
    # user = UserORM(id=UUID(SettingsProvider.sget().SYSTEM_USER))
    action_config = LlmChatOpenaiConfig(
        provider="azure",
        messages=[{"role": "user", "content": input.text}],
        tools=["search_web", "google_search_main", "read_url", "api_call"],
    )
    data_blocks = await ActionText2TextLlmChatOpenai(
        action_config=action_config, user=user, db=db
    ).run_tool(action_config=action_config, ctx=ctx , app_state=app_state)
    result_text = ""
    for block in data_blocks.blocks:
        result_text += f"{block.text}\n"
    response_data = {"text": f"{result_text}"}
    requests.post(response_url, json=response_data)


async def get_app_user_from_slack_user(
    ctx: Context, db: AsyncIOMotorDatabase, slack_user_id: str, slack_team_id: str | None = None
) -> UserORM | Exception:
    integration_doc = await get_app_auth_from_slack_user(
        ctx=ctx, db=db, slack_user_id=slack_user_id, slack_team_id=slack_team_id
    )
    assert integration_doc, "Unable to match slack user to app user"
    user: UserORM = await get_user(ctx=ctx, db=db, user_id=integration_doc.user_id)
    return user


async def get_app_auth_from_slack_user(
    ctx: Context, db: AsyncIOMotorDatabase, slack_user_id: str, slack_team_id: str | None = None
) -> IntegrationDoc | Exception:
    integration_doc = await UserIntegration.get_by_slack_user(
        ctx=ctx, db=db, slack_user_id=slack_user_id, slack_team_id=slack_team_id, decrypt=True
    )
    if not integration_doc:
        logger.bind(ctx=ctx, slack_user_id=slack_user_id, slack_team_id=slack_team_id).error(
            f"Unable to find user app auth for slack user {slack_user_id}"
        )
        raise HTTPException(status_code=403, detail="Unable to find user app auth")
    return integration_doc


async def get_user(
    ctx: Context,
    db: AsyncIOMotorDatabase,
    user_id: str,
) -> UserORM | Exception:
    try:
        user_doc: UserDoc = await User.get(ctx=ctx, db=db, id=user_id, user=UserORM(id=user_id))
        user = UserORM(**user_doc.model_dump())
        return user
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in get user: {str(e)}")
        return e
