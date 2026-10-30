import copy
import mimetypes
from typing import Any, Dict, List, Literal, Type

import httpx
from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from openai import AsyncAzureOpenAI, AsyncOpenAI
from openai.types import FileObject, responses
from openai.types.responses import (
    EasyInputMessageParam,
    Response,
    ResponseInputContentParam,
    ResponseInputImageParam,
    ResponseInputMessageContentListParam,
    ResponseInputTextParam,
    ResponseOutputMessage,
    WebSearchToolParam,
)
from openai.types.responses.response_input_item_param import Message
from openai.types.responses.response_input_param import ResponseInputParam
from openai.types.responses.tool_param import (
    Mcp,
)
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_fixed

from src.autobots.action.action.action_doc_model import ActionResult
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import (
    ActionABC,
    ActionConfigCreateType,
    ActionConfigType,
    ActionConfigUpdateType,
    ActionInputType,
    ActionOutputType,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.openai.azure_openai_client_factory import AzureOpenAIModelClientFactory
from src.autobots.conn.openai.openai_client import OpenAI
from src.autobots.conn.openai.openai_common_models import (
    OPENAI_FILE_FORMATS,
    OPENAI_FILE_FORMATS_CODE_INTERPRETER,
)
from src.autobots.conn.openai.openai_responses.openai_responses_stream import OpenaiResponsesStream
from src.autobots.conn.openai.openai_responses.openai_responses_v2 import (
    LlmResponsesOpenaiV2Config,
    OpenaiResponsesV2,
    TurnArchiveEntry,
    _is_retryable_openai_responses_exception,
)
from src.autobots.data_model.context import Context
from src.autobots.files.file_uploader import FileUploader
from src.autobots.llm.tools.tool_factory import ToolFactory
from src.autobots.mcp.mcp_doc_model import MCP_START_CONSTANT
from src.autobots.mcp.user_mcp import UserMCP
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState
    

class ActionText2TextLlmResponsesOpenaiV2(
    ActionABC[
        LlmResponsesOpenaiV2Config,
        LlmResponsesOpenaiV2Config,
        LlmResponsesOpenaiV2Config,
        DataBlock,
        DataBlocks,
    ]
):
    async def run_tool(
        self, action_config: ActionConfigType, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> ActionOutputType | Exception:
        pass

    type = ActionType.openai_responses_v2

    # Supported image formats for multimodal inputs
    IMAGE_FORMATS = [".jpeg", ".jpg", ".png", ".gif", ".webp"]

    # Model-specific compaction thresholds for context_management
    COMPACTION_THRESHOLD_BY_MODEL = {
        "gpt-5.6-sol": 500000,
        "gpt-5.6-terra": 500000,
        "gpt-5.6-luna": 500000,
        "gpt-5.5": 500000,
        "gpt-5.4": 500000,
    }
    DEFAULT_COMPACTION_THRESHOLD = 200000

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return LlmResponsesOpenaiV2Config

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return LlmResponsesOpenaiV2Config

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return LlmResponsesOpenaiV2Config

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    # Instruction for LLM about file handling with code_interpreter and file_search
    FILE_UPLOAD_INSTRUCTION = """**Important: File Handling for Code Interpreter & File Search**

Files from tool outputs (e.g., google_search, read_web_url, or large tool outputs saved to files) are NOT automatically available to code_interpreter or file_search tools.

If you want to analyze, process, or search a file from a tool output URL using code_interpreter or file_search, you MUST first call the `file_uploader_openai` tool with that URL. This will upload the file to OpenAI and make it available to these tools.

Files that ARE automatically available:
- User-provided input files
- Files you generate using code_interpreter
- Files you explicitly upload via file_uploader_openai
"""

    # Instruction for LLM about rolling context window and chat history query tool
    ROLLING_WINDOW_INSTRUCTION = """**Important: Rolling Context Window & Chat History Query**

This conversation uses a rolling context window. Only the most recent turns are included in your visible context. Older turns are stored but may not be visible to you.

Before answering questions about earlier conversation content, you must check whether the relevant turns are in your visible context. If they are not, or if you are uncertain, use the `chat_history_query_openai` tool before answering. For whole-conversation summaries, retrieve the earliest available turns and sort oldest-first.

Do not guess, infer, or summarize past conversation details that may be outside your visible context — query first if you are uncertain.
"""

    @staticmethod
    def _add_file_upload_instruction(
        ctx: Context, config: LlmResponsesOpenaiV2Config, operation: Literal["create", "update"]
    ) -> LlmResponsesOpenaiV2Config:
        """Add file upload instruction to config if file_uploader_openai and code_interpreter/file_search are in tools."""
        if not config.tools:
            return config

        # Check which tools are present
        tool_types = []
        for t in config.tools:
            if isinstance(t, str):
                tool_types.append(t)
            elif isinstance(t, dict):
                tool_types.append(t.get("type"))
            elif hasattr(t, "type"):
                tool_types.append(t.type)

        has_file_uploader = "file_uploader_openai" in tool_types
        has_code_interpreter = "code_interpreter" in tool_types
        has_file_search = "file_search" in tool_types

        # Only add instruction if file_uploader_openai and at least one of code_interpreter/file_search are present
        if has_file_uploader and (has_code_interpreter or has_file_search):
            instruction_text = ActionText2TextLlmResponsesOpenaiV2.FILE_UPLOAD_INSTRUCTION

            # Check if instruction already exists
            has_instruction = False
            if config.input:
                for msg in config.input:
                    if isinstance(msg, dict) and instruction_text in str(msg.get("content", "")):
                        has_instruction = True
                        break

            if not has_instruction:
                if not config.input:
                    config.input = []
                elif isinstance(config.input, str):
                    config.input = [{"role": "user", "content": config.input}]
                config.input.insert(0, {"role": "system", "content": instruction_text})

                logger.bind(
                    ctx=ctx,
                    tools=tool_types,
                    operation=operation,
                ).info(f"Added file upload instruction to action config during {operation}")
            else:
                logger.bind(
                    ctx=ctx,
                    operation=operation,
                ).info(f"File upload instruction already exists in action config during {operation}")

        return config

    @staticmethod
    def _add_rolling_window_instruction(
        ctx: Context, config: LlmResponsesOpenaiV2Config, operation: Literal["create", "update"]
    ) -> LlmResponsesOpenaiV2Config:
        """Add rolling-window awareness instruction to config if not already present."""
        instruction_text = ActionText2TextLlmResponsesOpenaiV2.ROLLING_WINDOW_INSTRUCTION

        has_instruction = False
        if config.input:
            for msg in config.input:
                if isinstance(msg, dict) and instruction_text in str(msg.get("content", "")):
                    has_instruction = True
                    break

        if not has_instruction:
            if not config.input:
                config.input = []
            elif isinstance(config.input, str):
                config.input = [{"role": "user", "content": config.input}]
            config.input.insert(0, {"role": "system", "content": instruction_text})

            logger.bind(
                ctx=ctx,
                operation=operation,
            ).info(f"Added rolling-window instruction to action config during {operation}")
        else:
            logger.bind(
                ctx=ctx,
                operation=operation,
            ).info(f"Rolling-window instruction already exists in action config during {operation}")

        return config

    @staticmethod
    async def create_config(
        ctx: Context,
        app_state:AppState,
        config_create: LlmResponsesOpenaiV2Config,
        user: UserORM,
        db: AsyncIOMotorDatabase,
    ) -> LlmResponsesOpenaiV2Config:
        config = ActionText2TextLlmResponsesOpenaiV2.get_model_based_config(
            ctx=ctx, user=user, responses_req=config_create
        )
        config = ActionText2TextLlmResponsesOpenaiV2._add_file_upload_instruction(
            ctx=ctx, config=config, operation="create"
        )
        return ActionText2TextLlmResponsesOpenaiV2._add_rolling_window_instruction(
            ctx=ctx, config=config, operation="create"
        )

    @staticmethod
    async def update_config(
        ctx: Context,
        app_state:AppState,
        config: LlmResponsesOpenaiV2Config,
        config_update: LlmResponsesOpenaiV2Config,
        user: UserORM,
        db: AsyncIOMotorDatabase,
    ) -> LlmResponsesOpenaiV2Config:
        config = ActionText2TextLlmResponsesOpenaiV2.get_model_based_config(
            ctx=ctx, user=user, responses_req=config_update
        )
        config = ActionText2TextLlmResponsesOpenaiV2._add_file_upload_instruction(
            ctx=ctx, config=config, operation="update"
        )
        return ActionText2TextLlmResponsesOpenaiV2._add_rolling_window_instruction(
            ctx=ctx, config=config, operation="update"
        )

    @staticmethod
    async def update_config_with_prev_results(
        ctx: Context,
        app_state:AppState,
        curr_config: LlmResponsesOpenaiV2Config,
        prev_results: List[ActionResult] | None = None,
    ) -> LlmResponsesOpenaiV2Config:
        if not prev_results:
            return curr_config
        return curr_config

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        if not self.action_config.store:
            self.action_config.include = [
                "code_interpreter_call.outputs",
                "file_search_call.results",
            ]
            if not self.action_config.model.startswith("gpt-4"):
                self.action_config.include += [
                    "reasoning.encrypted_content",
                ]

        openai_conn = await AppConn.get_openai(
            ctx=ctx, db=self.db, user=self.user, provider=self.action_config.provider
        )
        openai_client: AsyncAzureOpenAI | AsyncOpenAI = await self.get_openai_provider_client(
            ctx=ctx, openai=openai_conn
        )
        # Instantiate OpenaiResponsesV2 directly for V2 action
        if self.action_config.provider == "azure":
            responses_client = OpenaiResponsesV2(provider="azure", client_factory=AzureOpenAIModelClientFactory)
        else:
            responses_client = OpenaiResponsesV2(provider="openai", openai_client=openai_conn.client)
        try:
            # input message (text + images)
            response_input_param: ResponseInputParam = (
                await self.action_input_to_response_input_param(ctx=ctx, action_input=action_input)
            )
            if not self.action_config.input:
                self.action_config.input = []
            elif isinstance(self.action_config.input, str):
                self.action_config.input = [
                    EasyInputMessageParam(role="user", content=self.action_config.input)
                ]

            # Capture initial instructions before conversation turns are appended.
            # This snapshot acts as the pinned prefix in the rolling-window rebuild below.
            if not self.action_config.initial_input_snapshot and self.action_config.input:
                self.action_config.initial_input_snapshot = copy.deepcopy(self.action_config.input)
                logger.bind(ctx=ctx, snapshot_count=len(self.action_config.initial_input_snapshot)).info(
                    "Captured initial input snapshot for pinned prefix"
                )

            # Rolling-window rebuild: replace input with pinned prefix + last N archive turns.
            # On turn 1 (empty archive) this is a no-op; input stays as the just-captured snapshot.
            self._rebuild_input_from_archive()
            logger.bind(
                ctx=ctx,
                archive_len=len(self.action_config.input_archive),
                visible_turns=self._visible_turns(),
                rebuilt_input_len=len(self.action_config.input),
            ).info("Rebuilt input from archive (rolling window)")

            self.action_config.input += response_input_param

            # Archive: Create new turn entry with the user input
            turn_entry = TurnArchiveEntry(
                turn_number=len(self.action_config.input_archive) + 1,
                input=[msg.model_dump() if hasattr(msg, 'model_dump') else msg for msg in response_input_param],
            )
            self.action_config.input_archive.append(turn_entry)
            logger.bind(ctx=ctx, turn_number=turn_entry.turn_number).info("Created input archive entry for turn")

            # Build sandbox-to-S3 URL mappings text (don't append yet - will consolidate)
            sandbox_text = ""
            sandbox_mappings = {
                sandbox_url: s3_url
                for sandbox_url, s3_url in self.action_config.sandbox_url_to_s3_url.items()
                if sandbox_url.startswith("sandbox:")
            }
            if sandbox_mappings:
                mapping_lines = "\n".join(
                    f"- {sandbox_url} -> {s3_url}"
                    for sandbox_url, s3_url in sandbox_mappings.items()
                )
                sandbox_text = (
                    "\n\n\nFor your reference, files generated by you in previous turns "
                    "are listed below in the format sandbox URL -> https S3 URL:\n"
                    f"{mapping_lines}\n"
                    "Use only these https URLs when you want to reference "
                    "the corresponding sandbox files in tool calls or output."
                )
                logger.bind(
                    ctx=ctx,
                    sandbox_mapping_count=len(sandbox_mappings),
                ).info("Built sandbox-to-S3 URL mappings text")

            # Build file URLs text for resurfacing after compaction (don't append yet - will consolidate)
            urls_text = ""
            if self.action_config.compaction_triggered and self.action_config.file_url_to_file_object_map:
                urls_text = "\n\nPreviously referenced files:\n" + "\n".join(
                    f"- {url}" for url in self.action_config.file_url_to_file_object_map.keys()
                )
                logger.bind(ctx=ctx, url_count=len(self.action_config.file_url_to_file_object_map)).info(
                    "Built file URLs text for resurfacing"
                )

            # Consolidate sandbox + file URLs text and maybe convert to file if too long
            consolidated_text = sandbox_text + urls_text
            if consolidated_text:
                consolidation_datablock = DataBlock()
                _ = await consolidation_datablock.maybe_convert_input_to_file(
                    ctx=ctx, user=self.user, prompt=consolidated_text
                )

                last_msg = self.action_config.input[-1]
                if isinstance(last_msg, dict) and isinstance(last_msg.get("content"), list):
                    for content_item in last_msg["content"]:
                        if isinstance(content_item, dict) and content_item.get("type") == "input_text":
                            if consolidation_datablock.urls:
                                # Converted to file - add custom message + URL to action_input for OpenAI upload
                                file_url = consolidation_datablock.urls[0]
                                if file_url not in action_input.urls:
                                    action_input.urls.append(file_url)
                                content_item["text"] += (
                                    f"\n\nThe sandbox URLs and file URLs have been consolidated into a file "
                                    f"because their length was too long. This file has been uploaded and you can "
                                    f"access it using native tools also. This is it's file url: {file_url}"
                                )
                                logger.bind(ctx=ctx, file_url=file_url).info(
                                    "Consolidated URLs text converted to file"
                                )
                            else:
                                # Not converted - add full consolidated text directly
                                content_item["text"] += consolidated_text
                            break

            # Compaction resurfacing + item-level pruning have been replaced by the
            # rolling-window rebuild above: the pinned prefix (initial_input_snapshot)
            # is always prepended, and the compaction floor in _rebuild_input_from_archive
            # guarantees we never trim below the turn containing the last compaction item.

            # Reset compaction flag after all resurfacing and pruning is done
            if self.action_config.compaction_triggered:
                self.action_config.compaction_triggered = False

            # function tool
            tool_list = []
            if self.action_config.tools:
                tool_list += self.action_config.tools
            if action_input.tools:
                tool_list += action_input.tools
            logger.bind(ctx=ctx, tool_list=tool_list).info("openai_responses.create tool_list")
            function_tool_params: (
                List[responses.FunctionToolParam] | None
            ) = await ToolFactory.get_responses_function_tool_params(ctx=ctx, tool_names=tool_list)
            self.action_config.tools = function_tool_params

            # upload files to openai
            urls_to_upload = []
            urls_from_config = [
                url
                for url in self.action_config.file_urls
                if url not in self.action_config.file_url_to_file_object_map
            ]
            urls_from_input = [
                url
                for url in action_input.urls
                if url not in self.action_config.file_url_to_file_object_map
            ]
            urls_to_upload = urls_from_config + urls_from_input
            file_url_to_file_object_map: Dict[str, FileObject] = await self.upload_urls_to_openai(
                ctx=ctx, user=self.user, urls=urls_to_upload, openai_client=openai_conn
            )
            for file_url, file_object in file_url_to_file_object_map.items():
                self.action_config.file_url_to_file_object_map[file_url] = file_object
            # file ids to use
            file_ids = []
            for file_url, file_object in self.action_config.file_url_to_file_object_map.items():
                file_ids.append(file_object.id)

            # handle openai code_interpreter
            await responses_client.build_responses_code_interpreter_tool_param(
                ctx=ctx, responses_req=self.action_config, file_ids=file_ids, client=openai_client,
                user=self.user, db=self.db,
            )

            # handle openai file_search
            await responses_client.build_responses_file_search_tool_param(
                ctx=ctx, responses_req=self.action_config, openai_client=openai_client, file_ids=file_ids
            )

            # handle web_search
            await self.build_responses_web_search_tool_param(ctx=ctx, action_input=action_input)

            # handle mcp
            await self.build_responses_mcp_tool_param(
                ctx=ctx, user=self.user, db=self.db, action_input=action_input
            )

            logger.bind(ctx=ctx).info("Using OpenAI ResponsesV2 client")

            resp = await self.execute(
                ctx=ctx, app_state=app_state, action_input=action_input, responses_client=responses_client, notify=notify
            )
            logger.bind(ctx=ctx, action_input=action_input, resp=resp).info(
                "openai_responses.create final response"
            )
            return resp
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in run_action: {str(e)}")
            return e

    async def execute(
        self,
        ctx: Context,
        app_state: AppState,
        action_input: DataBlock,
        responses_client: OpenaiResponsesV2 | OpenaiResponsesStream,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_fixed(self.action_config.execute_retry_wait),
            retry=retry_if_exception(_is_retryable_openai_responses_exception),
            reraise=True
        ):
            with attempt:
                try:
                    # Dont need action_input, it has been added in config in caller method
                    # responses_client = get_openai(provider=self.action_config.provider).openai_responses
                    # responses_req = OpenaiResponsesReq(**self.action_config.model_dump(exclude_none=True))
                    response: Response = await responses_client.create(
                        responses_req=self.action_config,
                        app_state=app_state,
                        user=self.user,
                        ctx=ctx,
                        db=self.db,
                        notify=notify,
                    )
                    # Archive: Add output after successful response (only ResponseOutputMessage, not reasoning/tool calls)
                    if self.action_config.input_archive and response and hasattr(response, 'output'):
                        self.action_config.input_archive[-1].output = [
                            item.model_dump() if hasattr(item, 'model_dump') else item
                            for item in response.output
                            if isinstance(item, ResponseOutputMessage)
                        ]
                        logger.bind(
                            ctx=ctx,
                            turn_number=self.action_config.input_archive[-1].turn_number,
                        ).info("Added output to input archive entry")
                    # self.action_config = self.action_config.model_copy(update=responses_req.model_dump())
                    data_blocks = await self.response_to_action_output(
                        ctx=ctx, response=response, action_input=action_input
                    )
                    return data_blocks
                except Exception as e:
                    logger.bind(ctx=ctx).exception(f"Error in execute: {str(e)}")
                    raise

    async def get_openai_provider_client(
        self, ctx: Context, openai: OpenAI
    ) -> AsyncAzureOpenAI | AsyncOpenAI:
        client: AsyncAzureOpenAI | AsyncOpenAI | None = None
        model = self.action_config.model
        # openai = await AppConn.get_openai(
        #     ctx=ctx, db=self.db, user=self.user, provider=self.action_config.provider
        # )
        if self.action_config.provider == "azure":
            logger.bind(ctx=ctx).info(f"Using Azure OpenAI client with model: {model}")
            client: AsyncAzureOpenAI = await openai.openai_responses.client_factory.create_client(
                model=model
            )
        elif self.action_config.provider == "openai":
            logger.bind(ctx=ctx).info("Using OpenAI responses client")
            client: AsyncOpenAI = openai.client
        assert client, "Openai LLM responses Client not found"
        return client

    async def action_input_to_response_input_param(
        self, ctx: Context, action_input: DataBlock
    ) -> ResponseInputParam:
        response_input_message_content_list_param: ResponseInputMessageContentListParam = []
        llm_prompt = action_input.get_llm_prompt(ctx=ctx)
        llm_prompt = await action_input.maybe_convert_input_to_file(
            ctx=ctx, user=self.user, prompt=llm_prompt
        )
        response_input_message_content_list_param.append(
            ResponseInputTextParam(type="input_text", text=llm_prompt)
        )
        if action_input.metadata.process_url_in_text:
            action_input.process_url_in_text(ctx=ctx)
        # if action_input.text:
        #     response_input_message_content_list_param.append(
        #         ResponseInputTextParam(type="input_text", text=action_input.text)
        #     )
        for url in action_input.urls:
            try:
                response_input_image_param = await self.url_to_response_input_image_param(
                    ctx=ctx,
                    url=url,
                )
                if response_input_image_param:
                    response_input_message_content_list_param.append(response_input_image_param)
            except Exception as e:
                logger.bind(ctx=ctx, url=url).exception(
                    f"Error in convert_urls_to_input_params: {str(e)}"
                )

        message = Message(
            type="message",
            content=response_input_message_content_list_param,
            role="user",
        )
        response_input_param: ResponseInputParam = [message]
        return response_input_param

    async def url_to_response_input_image_param(
        self, ctx: Context, url: str
    ) -> ResponseInputContentParam | None:
        last_name = url.split("/")[-1]
        mime_type, encoding = mimetypes.guess_type(last_name)
        if mime_type is None:
            return None
        extension = mimetypes.guess_extension(mime_type)
        if extension in ActionText2TextLlmResponsesOpenaiV2.IMAGE_FORMATS:
            # Check for empty file before sending to OpenAI (0-byte files cause API errors)
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.head(url, timeout=5.0, follow_redirects=True)
                    content_length = resp.headers.get("content-length")
                    if content_length is not None and int(content_length) == 0:
                        logger.bind(ctx=ctx, url=url, content_length=0).warning(
                            "Skipping empty image file (0 bytes) - would cause OpenAI error"
                        )
                        return None
                    logger.bind(
                        ctx=ctx, url=url, content_length=content_length
                    ).debug("Image URL validated, has content")
            except Exception as e:
                logger.bind(ctx=ctx, url=url, error=str(e)).warning(
                    "Could not verify image size via HEAD request, proceeding anyway"
                )

            content = ResponseInputImageParam(type="input_image", image_url=url, detail="auto")
            return content
        return None

    # async def build_responses_file_search_tool_param(
    #     self,
    #     ctx: Context,
    #     action_input: DataBlock,
    #     openai_client: AsyncAzureOpenAI | AsyncOpenAI,
    #     file_ids: List[str] = list,
    # ) -> None:
    #     # DEPRECATED: This method is unused by run_action. Logic moved to OpenaiResponses.
    #     new_vector_store_ids = []
    #     tool_list = [] if self.action_config.tools is None else self.action_config.tools
    #     tool_list += action_input.tools
    #     for i, tool in enumerate(tool_list):
    #         if (
    #             isinstance(tool, dict)
    #             and "type" in tool
    #             and tool["type"] == "file_search"
    #             and "vector_store_ids" in tool
    #         ):
    #             new_vector_store_ids += tool["vector_store_ids"]

    #     new_file_ids = []
    #     for file_id in file_ids:
    #         for file_url, file_object in self.action_config.file_url_to_file_object_map.items():
    #             if file_id == file_object.id:
    #                 ext = "." + file_object.filename.split(".")[-1]
    #                 ext = ext.lower()
    #                 if ext in OPENAI_FILE_FORMATS:
    #                     if file_id not in self.action_config.file_id_to_vector_store_map:
    #                         new_file_ids.append(file_id)
    #                 else:
    #                     logger.bind(
    #                         ctx=ctx, filename=file_object.filename, file_object=file_object
    #                     ).info("openai_responses.file_search file_ext not in FILE_FORMATS")
    #                     continue

    #     logger.bind(ctx=ctx, file_ids=new_file_ids).info(
    #         "openai_responses.file_search new file_ids"
    #     )
    #     if new_file_ids:
    #         vector_store_client = OpenaiVectorStores(openai_client)
    #         create_vector_store = CreateVectorStore(file_ids=new_file_ids)
    #         new_vector_store = await vector_store_client.create(
    #             create_vector_store=create_vector_store,
    #         )
    #         for file_id in new_file_ids:
    #             self.action_config.file_id_to_vector_store_map[file_id] = VectorStoreLite(
    #                 **new_vector_store.model_dump()
    #             )
    #         logger.bind(ctx=ctx, vector_store=new_vector_store).info(
    #             "openai_responses.create vector_store"
    #         )
    #         new_vector_store_ids.append(new_vector_store.id)

    #     new_vector_store_ids = list(set(new_vector_store_ids))

    #     # create new file_search_tool_param
    #     file_search_tool_param = FileSearchToolParam(
    #         type="file_search",
    #         vector_store_ids=new_vector_store_ids,
    #         max_num_results=10,
    #         ranking_options=RankingOptions(ranker="auto", score_threshold=0.5),
    #     )

    #     sanitized_tool_list: list | None = []
    #     file_search_index = -1
    #     for i, tool in enumerate(tool_list):
    #         if (isinstance(tool, str) and tool == "file_search") or (
    #             isinstance(tool, dict) and "type" in tool and tool["type"] == "file_search"
    #         ):
    #             if (
    #                 file_search_index == -1
    #                 and new_vector_store_ids
    #                 and len(new_vector_store_ids) > 0
    #             ):
    #                 file_search_index = i
    #                 sanitized_tool_list.append(file_search_tool_param)
    #         else:
    #             sanitized_tool_list.append(tool)

    #     self.action_config.tools = sanitized_tool_list if sanitized_tool_list else None

    # async def build_responses_code_interpreter_tool_param(
    #     self, ctx: Context, action_input: DataBlock, file_ids: List[str]
    # ) -> None:
    #     new_file_ids = []
    #     tool_list = [] if self.action_config.tools is None else self.action_config.tools
    #     tool_list += action_input.tools
    #     for i, tool in enumerate(tool_list):
    #         if (
    #             isinstance(tool, dict)
    #             and "type" in tool
    #             and tool["type"] == "code_interpreter"
    #             and "container" in tool
    #             and "file_ids" in tool["container"]
    #         ):
    #             new_file_ids += tool["container"]["file_ids"]
    #     new_file_ids += file_ids
    #     new_file_ids = list(set(new_file_ids))
    #     logger.bind(ctx=ctx, file_ids=new_file_ids).info(
    #         "openai_responses.code_interpreter file_ids"
    #     )
    #
    #     new_code_interpreter = CodeInterpreter(
    #         type="code_interpreter",
    #         container=CodeInterpreterContainerCodeInterpreterToolAuto(
    #             type="auto", file_ids=new_file_ids
    #         ),
    #     )
    #
    #     sanitized_tool_list: list | None = []
    #     code_interpreter_index = -1
    #     for i, tool in enumerate(tool_list):
    #         if (isinstance(tool, str) and tool == "code_interpreter") or (
    #             isinstance(tool, dict) and "type" in tool and tool["type"] == "code_interpreter"
    #         ):
    #             if code_interpreter_index == -1:
    #                 code_interpreter_index = i
    #                 sanitized_tool_list.append(new_code_interpreter)
    #         else:
    #             sanitized_tool_list.append(tool)
    #
    #     self.action_config.tools = sanitized_tool_list if sanitized_tool_list else None

    async def build_responses_mcp_tool_param(
        self, ctx: Context, user: UserORM, db: AsyncIOMotorDatabase, action_input: DataBlock
    ) -> None:
        logger.bind(ctx=ctx, action_input=action_input).debug("Building MCP tool params")
        sanitized_tool_list: list | None = []
        tool_list = [] if self.action_config.tools is None else self.action_config.tools
        tool_list += action_input.tools
        for i, tool in enumerate(tool_list):
            if isinstance(tool, dict) and "type" in tool and tool["type"] == "mcp":
                try:
                    logger.bind(ctx=ctx, tool=tool).debug(f"Validating MCP tool param: {tool}")
                    tool_orig = Mcp(**tool)
                    sanitized_tool_list.append(tool_orig)
                except Exception as e:
                    logger.bind(ctx=ctx, tool=tool).exception(
                        f"Error in validate MCP tool: {str(e)}"
                    )
                    raise HTTPException(
                        status_code=400, detail=f"Error in validate saved MCP tool: {str(e)}"
                    )
            elif isinstance(tool, str) and (len(tool) > 4 and tool.startswith(MCP_START_CONSTANT)):
                mcp_server_name = tool[4:]
                try:
                    user_mcp = UserMCP(user=user, db=self.db)
                    mcp_doc = await user_mcp.get_mcp_by_server_name(
                        ctx=ctx, mcp_server_name=mcp_server_name, decrypt=True
                    )
                    if not mcp_doc:
                        raise HTTPException(
                            status_code=400,
                            detail=f"MCP server with name {mcp_server_name} not found",
                        )

                    server_name = mcp_doc.server_name
                    server_url = mcp_doc.server_url
                    description = (
                        mcp_doc.description
                        if (mcp_doc.description and mcp_doc.description != "")
                        else None
                    )
                    request_headers = (
                        mcp_doc.request_headers
                        if (mcp_doc.request_headers and len(mcp_doc.request_headers.keys()) > 0)
                        else None
                    )
                    authorization = mcp_doc.oauth_token if (mcp_doc.oauth_token and mcp_doc.oauth_token != "") else None

                    mcp_tool_param = Mcp(
                        type="mcp",
                        server_label=server_name,
                        server_url=server_url,
                        server_description=description,
                        headers=request_headers,
                        # allowed_tools=mcp_doc.allowed_tools if mcp_doc.allowed_tools else None,
                        authorization=authorization,
                        require_approval="never",
                    )
                    logger.bind(ctx=ctx, tool=tool, mcp_tool_param=mcp_tool_param).info(
                        f"Adding MCP tool param for server: {mcp_server_name}"
                    )
                    logger.bind(ctx=ctx, tool=tool, mcp_tool_param=mcp_tool_param).debug(
                        f"MCP tool param: {mcp_tool_param}"
                    )
                    sanitized_tool_list.append(mcp_tool_param)

                except Exception as e:
                    logger.bind(ctx=ctx, tool=tool).exception(f"Error in get MCP server: {str(e)}")
                    raise HTTPException(
                        status_code=400,
                        detail=f"Error in get MCP server with name {mcp_server_name}: {str(e)}",
                    )
            else:
                sanitized_tool_list.append(tool)

        # logger.bind(ctx=ctx, sanitized_tool_list=sanitized_tool_list).debug(
        #     f"Sanitized MCP tool list: {sanitized_tool_list}"
        # )

        self.action_config.tools = sanitized_tool_list if sanitized_tool_list else None
        return

    async def build_responses_web_search_tool_param(
        self, ctx: Context, action_input: DataBlock
    ) -> None:
        sanitized_tool_list: list | None = []
        tool_list = [] if self.action_config.tools is None else self.action_config.tools
        tool_list += action_input.tools
        web_search_index = -1
        for i, tool in enumerate(tool_list):
            if isinstance(tool, str) and tool == "web_search":
                if web_search_index == -1:
                    web_search_index = i
                    new_web_search = WebSearchToolParam(
                        type="web_search_preview",
                        search_context_size="medium",
                    )
                    sanitized_tool_list.append(new_web_search)
            elif (
                isinstance(tool, dict)
                and "type" in tool
                and (
                    tool["type"] == "web_search_preview"
                    or tool["type"] == "web_search_preview_2025_03_11"
                )
            ):
                if web_search_index == -1:
                    web_search_index = i
                    sanitized_tool_list.append(tool)
                else:
                    sanitized_tool_list[web_search_index] = sanitized_tool_list[
                        web_search_index
                    ].update(tool)
            else:
                sanitized_tool_list.append(tool)

        self.action_config.tools = sanitized_tool_list if sanitized_tool_list else None

    async def response_to_action_output(
        self, ctx: Context, response: Response, action_input: DataBlock
    ) -> DataBlocks | Exception:
        data_block = DataBlock()
        if not response:
            return HTTPException(status_code=500, detail="OpenAI Responses is empty")
        try:
            text = response.output_text if response.output_text else ""
            logger.bind(
                ctx=ctx,
                response_type=type(response).__name__,
                response_output_text=response.output_text[:100] if response.output_text else None,
                text=text[:100] if text else None,
                text_bool=bool(text),
            ).info("response_to_action_output: checking output_text")
            if text:
                data_block.text += text
            else:
                logger.bind(ctx=ctx).error(
                    "openai_responses.create response output_text is None/Empty"
                )
                raise HTTPException(
                    status_code=500,
                    detail="OpenAI response output_text is empty"
                )

            if action_input.metadata.process_attachment_url:
                data_block.urls += [
                    str(s3_url)
                    for sandbox_url, s3_url in self.action_config.sandbox_url_to_s3_url.items()
                    if str(s3_url) not in data_block.urls
                ]
            
            if action_input.metadata.attachment_requested > 0:
                attachment_count = len(self.action_config.sandbox_url_to_s3_url)
                if attachment_count < action_input.metadata.attachment_requested:
                    logger.bind(
                        ctx=ctx,
                        attachment_count=attachment_count,
                        attachment_requested=action_input.metadata.attachment_requested,
                    ).info("Attachments count less than requested, retrying")
                    raise HTTPException(
                        400,
                        {"message": "Attachments not matched", "llm_output": data_block.text},
                    )

            return DataBlocks(blocks=[data_block])
        except HTTPException:
            raise
        except Exception as e:
            logger.bind(ctx=ctx).exception(
                f"Error in response_to_action_output of OpenAI Responses: {str(e)}"
            )
            return e  # in future we may think of raising this exception instead of returning it, if issues arise

    @staticmethod
    def get_model_based_config(
        ctx: Context, user: UserORM, responses_req: LlmResponsesOpenaiV2Config
    ) -> LlmResponsesOpenaiV2Config:
        model = responses_req.model
        responses_dict = responses_req.model_dump(exclude_none=True)
        # ["gpt-5.1-chat-latest", "gpt-5.2-chat-latest", "gpt-5.3-chat-latest", "gpt-5-chat-latest"]
        if (
            model.startswith("gpt-5")
            and (model not in ["gpt-5-chat-latest"])
        ) or (model.startswith("o")):
            logger.bind(ctx=ctx, model=model).info(
                "Removing temperature and top_p from the gpt-5 type or  model config"
            )
            responses_dict.pop("temperature", None)
            responses_dict.pop("top_p", None)
            responses_dict.pop("max_tokens", None)
            # responses_dict.pop("max_output_tokens", None)
            # responses_dict.pop("max_")
        if model.startswith("gpt-5.2-pro"):
            responses_dict.pop("reasoning", None)

        # Non-reasoning models: gpt-3.5* and gpt-4* do not support reasoning
        if model.startswith("gpt-3.5") or model.startswith("gpt-4"):
            logger.bind(ctx=ctx, model=model).info(
                "Removing reasoning for non-reasoning model"
            )
            responses_dict.pop("reasoning", None)

        # Dynamic context_management threshold based on model
        compaction_threshold = ActionText2TextLlmResponsesOpenaiV2.COMPACTION_THRESHOLD_BY_MODEL.get(
            model,
            ActionText2TextLlmResponsesOpenaiV2.DEFAULT_COMPACTION_THRESHOLD
        )
        responses_dict["context_management"] = [
            {"type": "compaction", "compact_threshold": compaction_threshold}
        ]
        if compaction_threshold != ActionText2TextLlmResponsesOpenaiV2.DEFAULT_COMPACTION_THRESHOLD:
            logger.bind(ctx=ctx, model=model, compaction_threshold=compaction_threshold).info(
                "Set custom compaction threshold for model"
            )

        new_config = LlmResponsesOpenaiV2Config(**responses_dict)
        return new_config

    # def _extract_text_from_input_snapshot(self, snapshot: List[Dict[str, Any]]) -> str:
    #     """Extract text content from initial input snapshot messages for resurfacing after compaction."""
    #     texts = []
    #     for msg in snapshot:
    #         if isinstance(msg, dict):
    #             content = msg.get("content")
    #             if isinstance(content, str):
    #                 texts.append(content)
    #             elif isinstance(content, list):
    #                 for item in content:
    #                     if isinstance(item, dict):
    #                         if item.get("type") == "input_text":
    #                             texts.append(item.get("text", ""))
    #                         elif item.get("type") == "text":
    #                             texts.append(item.get("text", ""))
    #                     elif isinstance(item, str):
    #                         texts.append(item)
    #     return "\n".join(texts).strip()

    # @staticmethod
    # def _prune_before_compaction(input_items: List) -> tuple[List, int]:
    #     """Keep only items from the latest compaction item onwards.
    #
    #     Iterates in reverse to find the last compaction item efficiently.
    #
    #     Returns:
    #         tuple: (pruned_list, compaction_index) or (original_list, -1) if no compaction found
    #     """
    #     for i in range(len(input_items) - 1, -1, -1):
    #         item = input_items[i]
    #         item_type = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
    #         if item_type == "compaction":
    #             return input_items[i:], i
    #     return input_items, -1

    def _visible_turns(self) -> int:
        raw = getattr(self.action_config, "visible_turns", 20)
        try:
            value = int(raw)
        except Exception:
            value = 20
        return max(1, min(200, value))

    def _last_compaction_turn_index(self, archive: List[TurnArchiveEntry]) -> int:
        """Return the archive index of the last turn whose in_turn_messages contains a compaction item.

        Returns -1 if no compaction item is found. Scans every time (archive is small).
        """
        for idx in range(len(archive) - 1, -1, -1):
            entry = archive[idx]
            for msg in entry.in_turn_messages or []:
                if isinstance(msg, dict) and msg.get("type") == "compaction":
                    return idx
                item_type = getattr(msg, "type", None)
                if item_type == "compaction":
                    return idx
        return -1

    @staticmethod
    def _flatten_archive_turns(turns: List[TurnArchiveEntry]) -> List[Any]:
        """Flatten a list of TurnArchiveEntry into a single list of input items.

        Each turn contributes input + in_turn_messages + output in order.
        Items that are pydantic models are dumped to dicts; dicts are copied.
        """
        flattened: List[Any] = []
        for entry in turns:
            for bucket in (entry.input, entry.in_turn_messages, entry.output):
                for msg in bucket or []:
                    if hasattr(msg, "model_dump"):
                        try:
                            flattened.append(msg.model_dump(exclude_none=True))
                        except Exception:
                            flattened.append(msg)
                    elif isinstance(msg, dict):
                        flattened.append(dict(msg))
                    else:
                        flattened.append(msg)
        return flattened

    def _rebuild_input_from_archive(self) -> None:
        """Rebuild action_config.input from the pinned prefix + last N archive turns.

        - Pinned prefix = initial_input_snapshot (captured on turn 1, always prepended).
        - Visible window = last `visible_turns` archive entries.
        - Compaction floor: never trim below the turn containing the last compaction item,
          because that item carries encrypted_content OpenAI requires to continue the conversation.
        """
        pinned = copy.deepcopy(self.action_config.initial_input_snapshot or [])
        archive = self.action_config.input_archive or []
        if not archive:
            return  # turn 1: nothing to rebuild from

        visible = self._visible_turns()
        compaction_turn_idx = self._last_compaction_turn_index(archive)
        min_keep = len(archive) - compaction_turn_idx if compaction_turn_idx >= 0 else 0
        # TODO: Optimization — when compaction floor inflates the window beyond visible_turns
        # (i.e. min_keep > visible), instead of sending all min_keep turns, send only:
        #   pinned + [compaction turn] + [last visible_turns turns]
        # This skips the gap turns between compaction and the visible window, saving tokens.
        # The gap turns remain queryable via chat_history_query_openai.
        # BLOCKERS to implement:
        #   1. API compatibility: untested whether OpenAI accepts non-contiguous post-compaction
        #      items. The compaction item's encrypted_content represents state up to that point;
        #      skipping turns between it and the visible window may cause issues.
        #   2. Conversation coherence: model loses gap turns from visible context. The rolling-
        #      window instruction (injected via config-time system message) partially addresses
        #      this by telling the model it can query older history, but there's no explicit
        #      signal that a specific gap exists between the compaction turn and the visible window.
        # Condition: only apply when (len(archive) - compaction_turn_idx) > visible.
        keep = max(visible, min_keep)

        window = archive[-keep:]
        flattened = self._flatten_archive_turns(window)
        self.action_config.input = pinned + flattened

    async def upload_urls_to_openai(
        self,
        ctx: Context,
        user: UserORM,
        urls: List[str],
        openai_client: OpenAI,
    ) -> Dict[str, FileObject]:
        file_url_to_file_object_map: Dict[str, FileObject] = (
            self.action_config.file_url_to_file_object_map
        )
        uploaded_urls: Dict[str, FileObject] = await FileUploader.upload_urls_to_openai(
            ctx=ctx,
            user=user,
            db=self.db,
            urls=urls,
            openai_client=openai_client,
            supported_file_formats=OPENAI_FILE_FORMATS + OPENAI_FILE_FORMATS_CODE_INTERPRETER,
            cache=file_url_to_file_object_map,
        )
        logger.bind(
            ctx=ctx,
            uploaded_urls=uploaded_urls,
            file_url_to_file_object_map=file_url_to_file_object_map,
        ).info("openai_responses.upload_urls_to_openai file_objects")
        return uploaded_urls