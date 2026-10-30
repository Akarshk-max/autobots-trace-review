# import asyncio
# import io
# import json
# from enum import Enum, auto
# from typing import IO, AsyncIterator, Iterable, List, Literal, Type

# from fastapi import HTTPException
# from loguru import logger
# from motor.motor_asyncio import AsyncIOMotorDatabase
# from openai.types.beta import Assistant, AssistantToolParam
# from openai.types.beta.threads import ImageFileContentBlock, TextContentBlock
# from openai.types.beta.threads.message import Attachment
# from tenacity import retry, stop_after_attempt, wait_fixed

# from src.autobots import SettingsProvider
# from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
# from src.autobots.action.action_type.abc.ActionABC import (
#     ActionABC,
#     ActionConfigCreateType,
#     ActionConfigType,
#     ActionConfigUpdateType,
#     ActionInputType,
#     ActionOutputType,
# )
# from src.autobots.action.action_type.action_multimodal.action_multimodal_assistant_openai.assistant_openai_model import (
#     AssistantOpenaiConfig,
#     AssistantOpenaiConfigCreate,
#     AssistantOpenaiConfigUpdate,
# )
# from src.autobots.action.action_type.action_types import ActionType
# from src.autobots.integrations.app_conn.app_conn import AppConn
# from src.autobots.conn.aws.aws_s3 import get_public_s3
# from src.autobots.conn.openai.openai_assistants.assistant_model import (
#     AssistantCreate,
#     AssistantDelete,
#     AssistantUpdate,
# )
# from src.autobots.conn.openai.openai_assistants.openai_thread_messages.openai_thread_messages_model import (
#     ThreadMessageList,
#     ThreadMessagesCreate,
#     ThreadMessagesRetrieve,
# )
# from src.autobots.conn.openai.openai_assistants.openai_thread_runs.openai_thread_runs_model import (
#     ThreadRunCreate,
#     ThreadRunSubmitToolOutputs,
# )
# from src.autobots.conn.openai.openai_assistants.openai_threads.openai_threads_model import (
#     ThreadCreate,
#     ThreadRetrieve,
# )
# from src.autobots.conn.openai.openai_chat.chat_model import Message
# from src.autobots.core.clock.clock import Clock
# from src.autobots.core.utils import gen_uuid
# from src.autobots.data_model.context import Context
# from src.autobots.files.file_uploader import FileUploader
# from src.autobots.files.user_files import UserFiles
# from src.autobots.files.user_files_model import FileMetaPlus
# from src.autobots.llm.tools.tool_factory import ToolFactory
# from src.autobots.llm.tools.tools_map import TOOLS_MAP
# from src.autobots.server_sent_events.server_sent_events import Notify
# from src.autobots.user.user_orm_model import UserORM


# class ActionMultimodalAssistantOpenaiStream(
#     ActionABC[
#         AssistantOpenaiConfigCreate,
#         AssistantOpenaiConfigUpdate,
#         AssistantOpenaiConfig,
#         DataBlock,
#         DataBlocks,
#     ]
# ):
#     type = ActionType.multimodal_assistant_openai_stream

#     IMAGE_FORMATS = ["jpeg", "jpg", "png", "gif", "webp"]

#     # https://learn.microsoft.com/en-us/azure/ai-services/openai/how-to/file-search?tabs=python#file-search-support
#     FILE_FORMATS_FILE_SEARCH = [
#         "c",
#         "cs",
#         "cpp",
#         "doc",
#         "docx",
#         "html",
#         "java",
#         "json",
#         "md",
#         "pdf",
#         "php",
#         "pptx",
#         "py",
#         "rb",
#         "sh",
#         "tex",
#         "txt",
#         "css",
#         "js",
#         "sh",
#         "ts",
#     ]
#     # https://learn.microsoft.com/en-us/azure/ai-services/openai/how-to/code-interpreter?tabs=python#supported-file-types
#     FILE_FORMATS_CODE_INTERPRETER = [
#         "c",
#         "cpp",
#         "csv",
#         "docx",
#         "html",
#         "java",
#         "json",
#         "md",
#         "pdf",
#         "php",
#         "pptx",
#         "py",
#         "rb",
#         "tex",
#         "txt",
#         "css",
#         "jpeg",
#         "jpg",
#         "js",
#         "gif",
#         "png",
#         "tar",
#         "ts",
#         "xlsx",
#         "xml",
#         "zip",
#     ]

#     class ModelFamily(Enum):
#         REASONING = auto()
#         GENERAL = auto()

#     @staticmethod
#     async def get_model_family(model_name: str) -> ModelFamily:
#         """
#         Categorizes an OpenAI model into a family based on its supported parameter set.
#         This is the basis for the incompatibility check. Reasoning models (o-series, base gpt-5)
#         forbid sampling parameters like `temperature`, while General models forbid `reasoning_effort`.
#         """
#         name = model_name.lower()
#         if name.startswith("o") or (
#             name.startswith("gpt-5") and "pro" not in name and "chat" not in name
#         ):
#             return ActionMultimodalAssistantOpenaiStream.ModelFamily.REASONING
#         return ActionMultimodalAssistantOpenaiStream.ModelFamily.GENERAL

#     @staticmethod
#     def get_config_create_type() -> Type[ActionConfigCreateType]:
#         return AssistantOpenaiConfigCreate

#     @staticmethod
#     def get_config_update_type() -> Type[ActionConfigUpdateType]:
#         return AssistantOpenaiConfigUpdate

#     @staticmethod
#     def get_config_type() -> Type[ActionConfigType]:
#         return AssistantOpenaiConfig

#     @staticmethod
#     def get_input_type() -> Type[ActionInputType]:
#         return DataBlock

#     @staticmethod
#     def get_output_type() -> Type[ActionOutputType]:
#         return DataBlocks

#     @staticmethod
#     async def create_config(
#         ctx: Context,
#         config_create: AssistantOpenaiConfigCreate,
#         user: UserORM,
#         db: AsyncIOMotorDatabase,
#     ) -> AssistantOpenaiConfig:
#         assistant_tools = (
#             await ActionMultimodalAssistantOpenaiStream.replace_action_tools_with_tools_defs(
#                 ctx=ctx, tools=config_create.tools
#             )
#         )
#         config_create.tools = assistant_tools

#         # db = next(get_mongo_db())
#         # user = UserORM(id=ctx.user_id)
#         # user = await User.get(ctx=ctx, db=db, user=user, id=user.id)

#         client = await AppConn.get_openai(
#             ctx=ctx, db=db, user=user, provider=config_create.provider
#         )
#         assistant_client = client.openai_assistants
#         # create assistant
#         assistant_create = AssistantCreate(**config_create.model_dump(exclude_none=True))
#         assistant: Assistant = await assistant_client.create(assistant_create)
#         config = AssistantOpenaiConfig(provider=config_create.provider, **assistant.model_dump())
#         return config

#     @staticmethod
#     async def update_config(
#         ctx: Context,
#         config: AssistantOpenaiConfig,
#         config_update: AssistantOpenaiConfigUpdate,
#         user: UserORM,
#         db: AsyncIOMotorDatabase,
#     ) -> AssistantOpenaiConfig:
#         assistant_tools = (
#             await ActionMultimodalAssistantOpenaiStream.replace_action_tools_with_tools_defs(
#                 ctx=ctx, tools=config_update.tools
#             )
#         )
#         config_update.tools = assistant_tools

#         # --- START: UNIFIED "RE-CREATE" STRATEGY ---
#         # Functional Rationale: The OpenAI API forbids updating an assistant to a new model
#         # if the assistant's *current server-side state* contains parameters incompatible
#         # with the new model (e.g., an assistant with `temperature=1.0` cannot be updated to
#         # use `o3-mini`). Our local config may not reflect this server-side state (e.g.,
#         # `temperature` might be `None` locally).
#         #
#         # Our Strategy: Instead of fetching the live state (which adds latency to every update),
#         # we use a robust heuristic: if the update crosses a "model family" boundary
#         # (General Purpose ↔ Reasoning) or a provider boundary, we assume an incompatibility
#         # exists and force a re-creation. This provides a clean slate, ensuring no invalid
#         # parameters are carried over.

#         target_model = config_update.model or config.model
#         target_provider = config_update.provider or config.provider

#         provider_switched = target_provider != config.provider
#         family_switched = await ActionMultimodalAssistantOpenaiStream.get_model_family(
#             target_model
#         ) != await ActionMultimodalAssistantOpenaiStream.get_model_family(config.model)

#         if provider_switched or family_switched:
#             reason = "provider switch" if provider_switched else "model family switch"
#             logger.bind(ctx=ctx).warning(
#                 f"Triggering assistant re-creation due to {reason} ({config.model} -> {target_model})."
#             )
#             # Construct a clean payload: merge old and new configs, then strip all potentially incompatible fields.
#             recreate_dict = config.model_dump(exclude_none=True)
#             recreate_dict.update(config_update.model_dump(exclude_unset=True))

#             # Explicitly remove keys that are invalid for a 'create' payload or are part of the family incompatibility.
#             fields_to_remove = {"id", "created_at", "updated_at", "object", "thread_id", "provider"}
#             target_family = await ActionMultimodalAssistantOpenaiStream.get_model_family(
#                 target_model
#             )
#             if target_family == ActionMultimodalAssistantOpenaiStream.ModelFamily.REASONING:
#                 fields_to_remove.update({"temperature", "top_p"})
#             elif target_family == ActionMultimodalAssistantOpenaiStream.ModelFamily.GENERAL:
#                 fields_to_remove.update({"reasoning_effort"})

#             for key in fields_to_remove:
#                 recreate_dict.pop(key, None)

#             recreate_payload = AssistantOpenaiConfigCreate(
#                 provider=target_provider, **recreate_dict
#             )
#             return await ActionMultimodalAssistantOpenaiStream.create_config(
#                 ctx, recreate_payload, user, db
#             )

#         # If the update is compatible, proceed with a normal in-place PATCH update.
#         client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=config.provider)
#         assistant_client = client.openai_assistants
#         assistant_update = AssistantUpdate(
#             assistant_id=config.id, **config_update.model_dump(exclude_unset=True)
#         )
#         assistant: Assistant = await assistant_client.update(assistant_update)
#         config = config.model_copy(update=assistant.model_dump())
#         return config

#     @staticmethod
#     async def delete_config(
#         ctx: Context,
#         config: AssistantOpenaiConfig,
#         user: UserORM,
#         db: AsyncIOMotorDatabase,
#     ) -> AssistantOpenaiConfig:
#         # db = next(get_mongo_db())
#         # user = UserORM(id=ctx.user_id)
#         client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=config.provider)
#         assistant_client = client.openai_assistants
#         # delete assistant
#         assistant_delete = AssistantDelete(assistant_id=config.id)
#         await assistant_client.delete(assistant_delete)
#         return config

#     # def __init__(self, action_config: AssistantOpenaiConfig, user: UserORM | None = None):
#     #     super().__init__(action_config=action_config, user=user)

#     async def run_action(
#         self, ctx: Context, action_input: DataBlock, notify: Notify | None = None
#     ) -> AsyncIterator[bytes]:
#         def send(ev: str, **kw) -> bytes:
#             return (json.dumps({"event": ev, **kw}, ensure_ascii=False) + "\n").encode("utf-8")

#         try:
#             final_response: DataBlocks | None = None
#             agen = self.execute(ctx=ctx, action_input=action_input, notify=notify)
#             async for chunk in agen:
#                 try:
#                     obj = json.loads(chunk.decode("utf-8"))
#                     if obj.get("event") == "execute":
#                         err = obj.get("error", None)
#                         if err:
#                             raise Exception(err)
#                         else:
#                             res = obj.get("result")
#                             if res is None:
#                                 raise Exception("No result in execute event")
#                             print("res: ", res)
#                             final_response = DataBlocks.model_validate(res)
#                             yield send("action-run", result=final_response.model_dump())
#                     yield chunk
#                 except Exception:
#                     # logger.bind(ctx=ctx).exception("Error in run_action stream chunk")
#                     raise

#         except Exception as e:
#             logger.bind(ctx=ctx).exception(f"Error in run_action: {str(e)}")
#             yield send("action-run", error=str(e))

#     @retry(stop=stop_after_attempt(3), wait=wait_fixed(2), reraise=True)
#     async def execute(
#         self, ctx: Context, action_input: DataBlock, notify: Notify | None = None
#     ) -> AsyncIterator[bytes]:
#         client = await AppConn.get_openai(
#             ctx=ctx, db=self.db, user=self.user, provider=self.action_config.provider
#         )
#         assistant_client = client.openai_assistants
#         assistant = self.action_config

#         def send(ev: str, **kw) -> bytes:
#             return (json.dumps({"event": ev, **kw}, ensure_ascii=False) + "\n").encode("utf-8")

#         try:
#             thread = None
#             # create thread or use existing
#             if not self.action_config.thread_id:
#                 thread_create = ThreadCreate()
#                 thread = await assistant_client.threads.create(thread_create)
#                 self.action_config.thread_id = thread.id
#             else:
#                 thread_retrieve = ThreadRetrieve(thread_id=self.action_config.thread_id)
#                 thread = await assistant_client.threads.retrieve(thread_retrieve)

#             assert thread is not None

#             # add tools
#             tools = [
#                 tool.model_dump(exclude_none=True)
#                 for tool in assistant.tools + action_input.tools
#                 if hasattr(tool, "model_dump")
#             ]
#             # tool_resources = [resource.model_dump(exclude_none=True) for resource in action_input.tool_resources]

#             # add message - text content
#             content = []
#             content += [{"type": "text", "text": action_input.get_llm_prompt(ctx=ctx)}]
#             tool_names = []
#             for tool in assistant.tools + action_input.tools:
#                 if isinstance(tool, str):
#                     tool_names.append(tool)
#                 elif isinstance(tool, dict):
#                     if tool.get("function", {}).get("name"):
#                         tool_names.append(tool["function"]["name"])
#                 elif hasattr(tool, "function") and hasattr(tool.function, "name"):
#                     tool_names.append(tool.function.name)
#                 elif hasattr(tool, "type"):
#                     tool_names.append(tool.type)
#             if "ocr_tool" in tool_names:
#                 content += [
#                     {
#                         "type": "text",
#                         "text": "\nIMPORTANT: Use the exact HTTPS file URL provided in the "
#                         "file_paths parameter from the context when calling ocr_tool. Do "
#                         "NOT create a new file path or substitute an assistant ID. \nFor "
#                         "example, if the context includes: 'file_paths': ["
#                         "'https://your-s3-bucket.s3.amazonaws.com/your_file.ext'], "
#                         "then use that URL exactly\n.",
#                     }
#                 ]
#             # add message - image content
#             image_urls = []
#             file_urls_for_file_search = []
#             file_urls_for_code_interpreter = []
#             if action_input and action_input.urls:
#                 for url in action_input.urls:
#                     logger.bind(ctx=ctx, url=url).info("Processing url attachment")
#                     extension = url.split(".")[-1].lower()
#                     if "?" in url:
#                         non_redirect_url = url.split("?")[0]
#                         extension = non_redirect_url.split(".")[-1].lower()

#                     if extension in ActionMultimodalAssistantOpenaiStream.IMAGE_FORMATS:
#                         if url.startswith("https://"):
#                             image_urls.append(url)
#                             logger.bind(ctx=ctx, url=url).info(f"Added to image url: {url}")
#                         elif url.startswith(
#                             f"s3://{SettingsProvider.sget().AWS_S3_PUBLIC_BUCKET_NAME}"
#                         ):
#                             s3 = get_public_s3()
#                             https_url = await s3.get_https_url_from_s3_uri(url)
#                             image_urls.append(str(https_url))
#                             logger.bind(ctx=ctx, url=url).info(f"Added to image url: {https_url}")
#                         else:
#                             logger.bind(ctx=ctx, url=url).warning(f"Cannot handle image url: {url}")
#                     elif (
#                         extension in ActionMultimodalAssistantOpenaiStream.FILE_FORMATS_FILE_SEARCH
#                         and extension
#                         in ActionMultimodalAssistantOpenaiStream.FILE_FORMATS_CODE_INTERPRETER
#                     ):
#                         file_urls_for_file_search.append(url)
#                         file_urls_for_code_interpreter.append(url)
#                         logger.bind(ctx=ctx, url=url).info(
#                             f"Url added to file_search and code_interpreter: {url}"
#                         )
#                     elif (
#                         extension in ActionMultimodalAssistantOpenaiStream.FILE_FORMATS_FILE_SEARCH
#                     ):
#                         file_urls_for_file_search.append(url)
#                         logger.bind(ctx=ctx, url=url).info(f"Added to file_search url: {url}")
#                     elif (
#                         extension
#                         in ActionMultimodalAssistantOpenaiStream.FILE_FORMATS_CODE_INTERPRETER
#                     ):
#                         file_urls_for_code_interpreter.append(url)
#                         logger.bind(ctx=ctx, url=url).info(f"Added to code_interpreter url: {url}")
#                     else:
#                         file_urls_for_code_interpreter.append(url)
#                         logger.bind(ctx=ctx, url=url).info(
#                             f"Extension not supported, will try to handle file url in code_interpreter: {url}"
#                         )

#             for image_url in image_urls:
#                 content.append({"type": "image_url", "image_url": {"url": image_url}})

#             thread_message_create_1 = ThreadMessagesCreate(
#                 thread_id=thread.id, content=content, attachments=[]
#             )
#             # add tool related attachments to message
#             combined_file_urls = file_urls_for_file_search + [
#                 url
#                 for url in file_urls_for_code_interpreter
#                 if url not in file_urls_for_file_search
#             ]
#             logger.bind(ctx=ctx, urls=combined_file_urls).info("File urls to be uploaded to OpenAI")
#             if combined_file_urls:
#                 attachments = []
#                 # file_objs = await self.upload_s3_file_to_openai(
#                 #     ctx=ctx,
#                 #     db=self.db,
#                 #     user=self.user,
#                 #     provider=self.action_config.provider,
#                 #     urls=combined_file_urls,
#                 # )
#                 supported_file_formats = (
#                     ActionMultimodalAssistantOpenaiStream.FILE_FORMATS_FILE_SEARCH
#                     + ActionMultimodalAssistantOpenaiStream.FILE_FORMATS_CODE_INTERPRETER
#                 )
#                 uploaded_files = await FileUploader.upload_urls_to_openai(
#                     ctx=ctx,
#                     user=self.user,
#                     db=self.db,
#                     urls=combined_file_urls,
#                     openai_client=client,
#                     supported_file_formats=supported_file_formats,
#                 )
#                 file_objs = list(uploaded_files.values())
#                 for file_obj in file_objs:
#                     file_obj_lite = {
#                         "openai_file_id": file_obj.id,
#                         "filename": file_obj.filename,
#                         "status": file_obj.status,
#                         "status_details": file_obj.status_details,
#                         "created_at": file_obj.created_at,
#                         "purpose": file_obj.purpose,
#                     }
#                     logger.bind(ctx=ctx, file_id=file_obj.id, file_name=file_obj.filename).info(
#                         "Processing attachment"
#                     )
#                     tools_for_attachments = []
#                     file_obj_extension = file_obj.filename.split(".")[-1].lower()
#                     if (
#                         file_obj_extension
#                         in ActionMultimodalAssistantOpenaiStream.FILE_FORMATS_FILE_SEARCH
#                     ):
#                         tools_for_attachments += [{"type": "file_search"}]
#                     if (
#                         file_obj_extension
#                         in ActionMultimodalAssistantOpenaiStream.FILE_FORMATS_CODE_INTERPRETER
#                     ):
#                         tools_for_attachments += [{"type": "code_interpreter"}]
#                     if len(tools_for_attachments) > 0:
#                         logger.bind(ctx=ctx, file_obj=file_obj_lite, attachments=attachments).info(
#                             "Adding attachment to message attachments"
#                         )
#                         attachment = Attachment(file_id=file_obj.id, tools=tools_for_attachments)
#                         attachments.append(attachment.model_dump())  # requires a dict
#                     else:
#                         logger.bind(ctx=ctx, file_obj=file_obj_lite).info("No tools for attachment")

#                 thread_message_create_1.attachments += attachments
#                 logger.bind(ctx=ctx, attachments=attachments).info("Added attachments to message")

#             thread_message = await assistant_client.threads.messages.create(thread_message_create_1)  # noqa F841
#             remaining_attempts = 5
#             while (
#                 thread_message.status is not None
#                 and thread_message.status not in ["completed"]
#                 and remaining_attempts > 0
#             ):
#                 logger.bind(ctx=ctx, status=thread_message.status).info(
#                     "Waiting for thread message to complete"
#                 )
#                 remaining_attempts -= 1
#                 await asyncio.sleep(10)
#                 thread_messages_retrieve = ThreadMessagesRetrieve(
#                     message_id=thread_message.id, thread_id=thread_message.thread_id
#                 )
#                 thread_message = await assistant_client.threads.messages.retrieve(
#                     thread_messages_retrieve=thread_messages_retrieve
#                 )
#             logger.bind(ctx=ctx, thread_message=thread_message.model_dump()).info(
#                 f"thread message status: {thread_message.status}"
#             )

#             # run thread
#             is_thread_run_success = False
#             thread_run_count_remaining = 3
#             while not is_thread_run_success and thread_run_count_remaining >= 0:
#                 thread_run_count_remaining -= 1
#                 start_time = Clock.get_date_time()
#                 thread_run_create = ThreadRunCreate(
#                     thread_id=thread.id,
#                     assistant_id=assistant.id,
#                     instructions=assistant.instructions,
#                     tools=tools,
#                 )
#                 logger.bind(ctx=ctx, thread_run_create=thread_run_create.model_dump()).info(
#                     "Creating thread run"
#                 )
#                 # run = await assistant_client.threads.runs.create(thread_run_create)
#                 # TODO: store thread_id and run_id for re-run in Output
#                 # get run status - loop until status completed
#                 # The status of the run, which can be either:
#                 #   exit criteria - completed, cancelled, failed, expired
#                 #   wait criteria - queued, in_progress, cancelling, incomplete
#                 #   action criteria - requires_action
#                 max_loop = 30
#                 curr_loop = 0
#                 run = None
#                 tool_call = False
#                 thread_run_submit_tool_outputs_req = None
#                 while True:
#                     loop_to_break = True
#                     notify(ctx, "OpenAI thinking...") if notify else None
#                     if curr_loop > max_loop:
#                         logger.bind(ctx=ctx).error(f"Exceeded the max loop threshold of {max_loop}")
#                         break
#                     logger.bind(ctx=ctx, curr_loop=curr_loop).info(
#                         f"Starting thread run loop {curr_loop}"
#                     )
#                     logger.bind(ctx=ctx, curr_loop=curr_loop, tool_call=tool_call).info(
#                         f"tool call value : {tool_call}"
#                     )
#                     logger.bind(ctx=ctx, curr_loop=curr_loop).info(
#                         f"thread_run_submit_tool_outputs_req value : {thread_run_submit_tool_outputs_req}"
#                     )
#                     if tool_call:
#                         logger.bind(ctx=ctx, curr_loop=curr_loop).info("Submitting tool outputs...")
#                         if thread_run_submit_tool_outputs_req is None:
#                             raise Exception("thread_run_submit_tool_outputs_req is None")

#                         run_ctx = (
#                             assistant_client.client.beta.threads.runs.submit_tool_outputs_stream(
#                                 **thread_run_submit_tool_outputs_req.model_dump(exclude_none=True)
#                             )
#                         )
#                         # print("Submitted tool outputs.")
#                         logger.bind(ctx=ctx, curr_loop=curr_loop).info("Submitted tool outputs.")
#                         tool_call = False
#                         thread_run_submit_tool_outputs_req = None
#                     else:
#                         run_ctx = assistant_client.client.beta.threads.runs.stream(
#                             **thread_run_create.model_dump(exclude_none=True)
#                         )
#                     async with run_ctx as stream:
#                         async for event in stream:
#                             # logger.bind(ctx=ctx, event=event.event, data=event.data).info(
#                             #     "Received event"
#                             # )
#                             if event.event == "thread.run.completed":
#                                 curr_loop += 1
#                                 logger.bind(
#                                     ctx=ctx,
#                                     start_time=start_time,
#                                     curr_loop=curr_loop,
#                                     status=run.status,
#                                 ).info("Assistant run end")
#                                 is_thread_run_success = True
#                                 yield send("token_2", text=f"The run with {run.id} has completed.")
#                                 run = event.data
#                                 # break
#                             if (
#                                 event.event == "thread.run.cancelled"
#                                 or event.event == "thread.run.failed"
#                                 or event.event == "thread.run.expired"
#                             ):
#                                 curr_loop += 1
#                                 logger.bind(ctx=ctx, status=run.status).error(
#                                     f"Error: Assistant run {event.event}"
#                                 )
#                                 # await asyncio.sleep(20)
#                                 run = event.data
#                                 yield send(
#                                     "token_2", error=f"The run with {run.id} has {event.event}."
#                                 )
#                                 # break
#                             if (
#                                 event.event == "thread.run.in_progress"
#                                 or event.event == "thread.run.queued"
#                                 or event.event == "thread.run.cancelling"
#                                 or event.event == "thread.run.incomplete"
#                             ):
#                                 msg = "Task run in progress..."
#                                 if event.event == "thread.run.queued":
#                                     msg = "Task run queued..."
#                                 if event.event == "thread.run.cancelling":
#                                     msg = "Task run cancelling..."
#                                 if event.event == "thread.run.incomplete":
#                                     msg = "Task run incomplete...Moving ahead."
#                                 run = event.data
#                                 yield send("token_2", text=msg)

#                             if event.event == "thread.run.requires_action":
#                                 run = event.data
#                                 curr_loop += 1
#                                 tool_outputs = []
#                                 for tool_call in run.required_action.submit_tool_outputs.tool_calls:
#                                     name = tool_call.function.name
#                                     args = tool_call.function.arguments
#                                     yield send(
#                                         "token_2", text=f"Running tool {name} with args {args}"
#                                     )
#                                     # run tool
#                                     tool_output_str: str = await ToolFactory(
#                                         user=self.user, db=self.db
#                                     ).run_tool(ctx, name, args, notify=notify)
#                                     tool_output = {
#                                         "output": tool_output_str,
#                                         "tool_call_id": tool_call.id,
#                                     }
#                                     yield send(
#                                         "token_2",
#                                         text=f"Tool {name} completed with output {tool_output_str}.",
#                                     )
#                                     logger.bind(
#                                         ctx=ctx,
#                                         tool_call_id=tool_call.id,
#                                         tool_name=name,
#                                         tool_args=args,
#                                         tool_output_str=tool_output_str,
#                                     ).info("Submitting tool outputs")
#                                     tool_outputs.append(tool_output)
#                                 thread_run_submit_tool_outputs = ThreadRunSubmitToolOutputs(
#                                     thread_id=run.thread_id,
#                                     run_id=run.id,
#                                     tool_outputs=tool_outputs,
#                                 )
#                                 # print("Prepared to submit tool outputs: ", thread_run_submit_tool_outputs)
#                                 logger.bind(
#                                     ctx=ctx, tool_outputs=thread_run_submit_tool_outputs
#                                 ).info("Prepared to submit tool outputs")
#                                 tool_call = True
#                                 thread_run_submit_tool_outputs_req = thread_run_submit_tool_outputs
#                                 loop_to_break = False
#                                 break

#                             # coming to deltas functions now -->

#                             if event.event == "thread.message.delta":
#                                 content = event.data.delta.content
#                                 for content_type in content:
#                                     ctype = content_type.type
#                                     if ctype == "text":
#                                         text = content_type.text
#                                         curr_text = None
#                                         if text.annotations:
#                                             for annot in text.annotations:
#                                                 if curr_text is None:
#                                                     curr_text = annot.text
#                                                 else:
#                                                     if annot.text:
#                                                         curr_text += annot.text
#                                         if curr_text is None:
#                                             curr_text = text.value
#                                         yield send("token", text=curr_text)

#                                     if ctype == "refusal":
#                                         refusal = content_type.refusal
#                                         yield send(
#                                             "token_2",
#                                             text="Recieved a refusal message from the assistant.",
#                                         )
#                                         yield send("token", text=refusal)

#                             if event.event == "thread.message.incomplete":
#                                 yield (
#                                     send(
#                                         "token_2",
#                                         error="The message has been marked as incomplete by the assistant.",
#                                     )
#                                 )
#                                 yield send("token", text=event.data.incomplete_details.reason)

#                             if event.event == "thread.run.step.delta":
#                                 tool_calls = event.data.delta.step_details.tool_calls
#                                 if tool_calls and len(tool_calls) > 0:
#                                     for tool_call in tool_calls:
#                                         if tool_call.type == "code_interpreter":
#                                             yield send(
#                                                 "tool_call",
#                                                 text="The assistant is using code interpreter tool.",
#                                             )
#                                         if tool_call.type == "file_search":
#                                             yield send(
#                                                 "tool_call",
#                                                 text="The assistant is using file search tool.",
#                                             )
#                                         if tool_call.type == "function":
#                                             args = tool_call.function.arguments
#                                             name = tool_call.function.name
#                                             if not args or args == "":
#                                                 yield send(
#                                                     "tool_call",
#                                                     text=f"The assistant is calling the tool {name}.",
#                                                 )
#                                             else:
#                                                 yield send(f"tool_{name}", args_added=args)

#                     if loop_to_break:
#                         break

#                     logger.bind(ctx=ctx, run=run.model_dump(), status=run.status).info(
#                         "Assistant run retrieved"
#                     )

#             thread_message_list = ThreadMessageList(thread_id=run.thread_id)
#             messages = await assistant_client.threads.messages.list(thread_message_list)
#             # last generated message is on the top
#             message: Message = messages.data[0]
#             logger.bind(ctx=ctx, message=message.model_dump()).info("Last message retrieved")
#             # At this point the thread will have a new run. So latest message should be of role "assistant.
#             # If role is user or system that means we have got no new message. Retry for that condition"
#             if message.role != "assistant":
#                 logger.bind(ctx=ctx).error("Error: No new message, trying again.")
#                 raise HTTPException(status_code=500, detail="Error: No new message, trying again.")
#             # create return object
#             data_block = DataBlock()
#             file_metas: List[FileMetaPlus] = []
#             if message.role == "assistant" and message.attachments and len(message.attachments) > 0:
#                 if len(message.attachments) < action_input.metadata.attachment_requested:
#                     logger.bind(
#                         ctx=ctx,
#                         attachments=message.attachments,
#                         attachment_requested=action_input.metadata.attachment_requested,
#                     ).info("Attachments")
#                     raise HTTPException(400, "Attachments not matched")
#                 for attachment in message.attachments:
#                     logger.bind(ctx=ctx, file_id=attachment.file_id).debug("Reading OpenAI file")
#                     file_meta = (
#                         await ActionMultimodalAssistantOpenaiStream.upload_openai_file_to_public_s3(
#                             ctx=ctx,
#                             db=self.db,
#                             user=self.user,
#                             provider=self.action_config.provider,
#                             content=attachment,
#                         )
#                     )
#                     file_metas.extend(file_meta)  # collect file_meta from attachments
#                     # if action_input.metadata.process_attachment_url:
#                     #     data_block.urls += [str(filemeta.url) for filemeta in file_metas]
#             if message.content:
#                 for content in message.content:
#                     if isinstance(content, ImageFileContentBlock):
#                         logger.bind(ctx=ctx, file_id=content.image_file.file_id).debug(
#                             "Reading OpenAI image file"
#                         )
#                         file_meta = await ActionMultimodalAssistantOpenaiStream.upload_openai_file_to_public_s3(
#                             ctx=ctx,
#                             db=self.db,
#                             user=self.user,
#                             provider=self.action_config.provider,
#                             content=content,
#                         )
#                         if not file_meta:
#                             raise HTTPException(
#                                 status_code=500,
#                                 detail="Cannot download image uploaded to assistant",
#                             )
#                         file_metas.extend(file_meta)  # collect file_meta from images
#                         # data_block.urls += [str(filemeta.url) for filemeta in file_metas]
#                 logger.bind(ctx=ctx, file_metas=file_metas).info("File metas collected")
#                 for content in message.content:
#                     if isinstance(content, TextContentBlock):
#                         text = content.text.value
#                         # Check for sandbox file paths
#                         if "sandbox:/mnt/data/" in text and len(file_metas) > 0:
#                             # Replace sandbox paths with S3 URLs in text in all file_meta
#                             for (
#                                 file_meta
#                             ) in file_metas:  # use file_metas built from attachments and images
#                                 old_url = f"sandbox:{file_meta.original_filename}"
#                                 new_url = str(file_meta.url)
#                                 logger.bind(ctx=ctx, old_url=old_url, new_url=new_url).info(
#                                     "Replacing sandbox url with s3 url"
#                                 )
#                                 text = text.replace(old_url, new_url)
#                                 # s3_url = str(file_meta.url)
#                                 # s3_filename = s3_url.split("/")[-1][9:].split("-")[0]
#                                 # assert (s3_url.split("/")[-1][:9] == "mnt_data_")
#                                 # file_ext = s3_url.split(".")[-1]
#                                 # # sandbox_path = f"sandbox:/mnt/data/{filename}.{file_ext}"
#                                 # # text = text.replace(sandbox_path, s3_url)
#                                 # base_path = f"sandbox:/mnt/data/{s3_filename}.{file_ext}"
#                                 # encoded_path = unquote(base_path)
#                                 # text = text.replace(base_path, s3_url).replace(encoded_path, s3_url)

#                         data_block.text += text
#             if action_input.metadata.process_attachment_url:
#                 data_block.urls += [
#                     str(filemeta.url)
#                     for filemeta in file_metas
#                     if str(filemeta.url) not in data_block.urls
#                 ]
#             print("data_block: ", data_block)
#             yield send("execute", result=DataBlocks(blocks=[data_block]).model_dump())
#             # return DataBlocks(blocks=[data_block])
#         except Exception as e:
#             logger.bind(ctx=ctx).exception(f"Error in execute: {str(e)}")
#             # raise e
#             yield send("execute", error=str(e))

#     @staticmethod
#     async def replace_action_tools_with_tools_defs(
#         ctx: Context, tools: Iterable[AssistantToolParam] | List[str] | None = None
#     ) -> Iterable[AssistantToolParam] | None:
#         function_tools = []
#         already_assistant_tools = []
#         if tools:
#             for action_tool in tools:
#                 if isinstance(action_tool, str):
#                     if action_tool in TOOLS_MAP:
#                         function_tools.append(action_tool)
#                 if isinstance(action_tool, dict):
#                     already_assistant_tools.append(action_tool)
#         assistant_function_tools = await ToolFactory.get_function_tool_param(
#             ctx=ctx, tool_names=function_tools
#         )
#         final_list_of_tools = []
#         if already_assistant_tools and len(already_assistant_tools) > 0:
#             final_list_of_tools += already_assistant_tools
#         if assistant_function_tools and len(assistant_function_tools) > 0:
#             final_list_of_tools += assistant_function_tools
#         return final_list_of_tools if len(final_list_of_tools) > 0 else []

#     @staticmethod
#     async def upload_openai_file_to_public_s3(
#         ctx: Context,
#         db: AsyncIOMotorDatabase,
#         user: UserORM,
#         provider: Literal["azure", "openai"],
#         content: ImageFileContentBlock | Attachment,
#     ) -> List[FileMetaPlus]:
#         file_id = ""
#         try:
#             match content:
#                 case ImageFileContentBlock():
#                     file_id = content.image_file.file_id
#                 case Attachment():
#                     file_id = content.file_id
#             if file_id == "":
#                 logger.bind(ctx=ctx).error("file_id is empty")
#                 return []
#             else:
#                 client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=provider)
#                 openai_assistant = client.openai_assistants
#                 file_obj = await openai_assistant.file_retrieve(file_id=file_id)
#                 file_content: bytes = await openai_assistant.file_retrieve_content(file_id=file_id)
#                 response_io: IO[bytes] = io.BytesIO(file_content)

#                 s3 = get_public_s3()
#                 logger.bind(ctx=ctx, bucket=s3.bucket.name).info(
#                     f"bucket for upload: {s3.bucket.name}"
#                 )
#                 user_files = UserFiles(user=user, s3=s3)
#                 temp_filename = (
#                     ".".join(file_obj.filename.split(".")[:-1])
#                     + str("-")
#                     + str(gen_uuid())
#                     + str(".")
#                     + file_obj.filename.split(".")[-1]
#                 )
#                 file_metas = await user_files.upload_binary(
#                     ctx=ctx, file_obj=response_io, filename=temp_filename
#                 )
#                 file_meta_pluses = [
#                     FileMetaPlus(name=fm.name, url=fm.url, original_filename=file_obj.filename)
#                     for fm in file_metas
#                 ]
#                 return file_meta_pluses

#         except Exception as e:
#             logger.bind(ctx=ctx).exception(f"Error in upload_openai_file_to_public_s3: {str(e)}")
#             return []

#     # @staticmethod
#     # async def upload_s3_file_to_openai(
#     #     ctx: Context,
#     #     db: AsyncIOMotorDatabase,
#     #     user: UserORM,
#     #     provider: Literal["azure", "openai"],
#     #     urls: List[str],
#     # ) -> List[FileObject]:
#     #     file_objs = []
#     #     client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=provider)
#     #     openai_files = client.openai_files
#     #     for url in urls:
#     #         try:
#     #             s3 = None
#     #             if url.__contains__(SettingsProvider.sget().AWS_S3_BUCKET_NAME):
#     #                 s3 = get_s3()
#     #             elif url.__contains__(SettingsProvider.sget().AWS_S3_PUBLIC_BUCKET_NAME):
#     #                 s3 = get_public_s3()
#     #             else:
#     #                 # db = next(get_mongo_db())
#     #                 app_creds_doc = await UserAppCreds.get_app_creds_by_app_name(
#     #                     ctx=Context(), db=db, user=user, app_name=AppTypes.aws
#     #                 )
#     #                 s3 = get_aws_s3(
#     #                     region_name=app_creds_doc.creds["region_name"],
#     #                     aws_access_key_id=app_creds_doc.creds["aws_access_key_id"],
#     #                     aws_secret_access_key=app_creds_doc.creds["aws_secret_access_key"],
#     #                     bucket_name=app_creds_doc.creds["bucket_name"],
#     #                     object_prefix=app_creds_doc.creds["object_prefix"],
#     #                 )
#     #             assert s3 is not None
#     #             logger.bind(ctx=ctx, bucket=s3.bucket.name, object_prefix=s3.object_prefix).info(
#     #                 f"selected bucket for download: {s3.bucket.name}"
#     #             )
#     #             file_path_and_name = await s3.get_key(url)
#     #             file_path_parts = file_path_and_name.split("/")
#     #             filename = file_path_parts[-1]
#     #             content_type = filename.split(".")[-1]
#     #             # download file to local
#     #             # object_prefix = "/".join(file_path_parts[:-1])
#     #             user_files = UserFiles(user=user, s3=s3)
#     #             # user_files._s3.set_object_prefix(object_prefix=object_prefix)
#     #             if "box" in url:
#     #                 with httpx.Client(
#     #                     follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}
#     #                 ) as client:
#     #                     if "?" in url:
#     #                         redirect = url.split("?")[-1]
#     #                         url = url.split("?")[0]
#     #                         response = client.get(url=URL(f"{url}?{redirect}"))
#     #                     else:
#     #                         response = client.get(url=URL(url))
#     #
#     #                     # Store response content to a file
#     #                     with open(f"/tmp/{filename}", "wb") as f:
#     #                         f.write(response.content)
#     #                     with open(f"/tmp/{filename}", "rb") as downloaded_file:
#     #                         file_create = FileCreate(
#     #                             file=(filename, downloaded_file.read(), content_type),
#     #                             purpose="assistants",
#     #                         )
#     #                         file_obj = await openai_files.create(ctx=ctx, file_create=file_create)
#     #                         file_objs.append(file_obj)
#     #                         logger.bind(ctx=ctx, url=url, openai_file_id=file_obj.id).info(
#     #                             "upload_s3_file_to_openai"
#     #                         )
#     #             else:
#     #                 download_iter = user_files.download_file_and_yield(
#     #                     ctx=ctx, filenames=[filename]
#     #                 )
#     #                 async for download_path_filename in download_iter:
#     #                     with open(download_path_filename, "rb") as downloaded_file:
#     #                         file_create = FileCreate(
#     #                             file=(filename, downloaded_file.read(), content_type),
#     #                             purpose="assistants",
#     #                         )
#     #                         file_obj = await openai_files.create(ctx=ctx, file_create=file_create)
#     #                         file_objs.append(file_obj)
#     #                         logger.bind(ctx=ctx, url=url, openai_file_id=file_obj.id).info(
#     #                             "upload_s3_file_to_openai"
#     #                         )
#     #         except Exception as e:
#     #             logger.bind(ctx=ctx, url=url).exception(
#     #                 f"Error in upload_s3_file_to_openai: {str(e)}"
#     #             )
#     #     return file_objs

#     async def run_tool(
#         self, action_config: AssistantOpenaiConfigCreate, ctx: Context, notify: Notify | None = None
#     ) -> DataBlocks | Exception:
#         data_blocks = DataBlocks(blocks=[])
#         try:
#             action_config.tools += [{"type": "code_interpreter"}]
#             config = await ActionMultimodalAssistantOpenaiStream.create_config(
#                 ctx=ctx, config_create=action_config, user=self.user, db=self.db
#             )
#             action = ActionMultimodalAssistantOpenaiStream(
#                 action_config=config, user=self.user, db=self.db
#             )
#             output = await action.run_action(ctx=ctx, action_input=DataBlock(text=""))
#             match output:
#                 case DataBlocks():
#                     data_blocks.blocks += output.blocks
#                 case Exception():
#                     data_block = DataBlock(text=str(output))
#                     data_blocks.blocks.append(data_block)
#             return data_blocks
#         except Exception as e:
#             logger.bind(ctx=ctx).exception(f"Error in run tool of Openai assistant: {str(e)}")
#             return e
