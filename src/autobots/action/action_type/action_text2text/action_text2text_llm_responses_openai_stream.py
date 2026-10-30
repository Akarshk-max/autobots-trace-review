# import json
# import mimetypes
# from typing import AsyncIterator, Dict, List, Type

# from fastapi import HTTPException
# from loguru import logger
# from motor.motor_asyncio import AsyncIOMotorDatabase
# from openai import AsyncAzureOpenAI, AsyncOpenAI
# from openai.types import FileObject, responses
# from openai.types.responses import (
#     FileSearchToolParam,
#     Response,
#     ResponseInputContentParam,
#     ResponseInputImageParam,
#     ResponseInputMessageContentListParam,
#     ResponseInputTextParam,
#     WebSearchToolParam,
# )
# from openai.types.responses.file_search_tool_param import RankingOptions
# from openai.types.responses.response_input_item_param import Message
# from openai.types.responses.response_input_param import ResponseInputParam
# from openai.types.responses.tool_param import (
#     CodeInterpreter,
#     CodeInterpreterContainerCodeInterpreterToolAuto,
#     Mcp,
# )
# from tenacity import retry, stop_after_attempt, wait_fixed

# from src.autobots.action.action.action_doc_model import ActionResult
# from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
# from src.autobots.action.action_type.abc.ActionABC import (
#     ActionABC,
#     ActionConfigCreateType,
#     ActionConfigType,
#     ActionConfigUpdateType,
#     ActionInputType,
#     ActionOutputType,
# )
# from src.autobots.action.action_type.action_types import ActionType
# from src.autobots.integrations.app_conn.app_conn import AppConn
# from src.autobots.conn.openai.openai_client import OpenAI
# from src.autobots.conn.openai.openai_common_models import (
#     OPENAI_FILE_FORMATS,
#     OPENAI_FILE_FORMATS_CODE_INTERPRETER,
# )
# from src.autobots.conn.openai.openai_responses.openai_responses import (
#     LlmResponsesOpenaiConfig,
#     VectorStoreLite,
# )
# from src.autobots.conn.openai.openai_responses.openai_responses_stream import OpenaiResponsesStream
# from src.autobots.conn.openai.openai_vector_stores.openai_vector_stores.openai_vector_stores import (
#     OpenaiVectorStores,
# )
# from src.autobots.conn.openai.openai_vector_stores.openai_vector_stores.openai_vector_stores_model import (
#     CreateVectorStore,
# )
# from src.autobots.data_model.context import Context
# from src.autobots.files.file_uploader import FileUploader
# from src.autobots.llm.tools.tool_factory import ToolFactory
# from src.autobots.mcp.mcp_doc_model import MCP_START_CONSTANT
# from src.autobots.mcp.user_mcp import UserMCP
# from src.autobots.server_sent_events.server_sent_events import Notify
# from src.autobots.user.user_orm_model import UserORM


# class ActionText2TextLlmResponsesOpenaiStream(
#     ActionABC[
#         LlmResponsesOpenaiConfig,
#         LlmResponsesOpenaiConfig,
#         LlmResponsesOpenaiConfig,
#         DataBlock,
#         DataBlocks,
#     ]
# ):
#     async def run_tool(
#         self, action_config: ActionConfigType, ctx: Context
#     ) -> ActionOutputType | Exception:
#         pass

#     type = ActionType.text2text_llm_responses_openai

#     # Supported image formats for multimodal inputs
#     IMAGE_FORMATS = ["jpeg", "jpg", "png", "gif", "webp"]

#     @staticmethod
#     def get_config_create_type() -> Type[ActionConfigCreateType]:
#         return LlmResponsesOpenaiConfig

#     @staticmethod
#     def get_config_update_type() -> Type[ActionConfigUpdateType]:
#         return LlmResponsesOpenaiConfig

#     @staticmethod
#     def get_config_type() -> Type[ActionConfigType]:
#         return LlmResponsesOpenaiConfig

#     @staticmethod
#     def get_input_type() -> Type[ActionInputType]:
#         return DataBlock

#     @staticmethod
#     def get_output_type() -> Type[ActionOutputType]:
#         return DataBlocks

#     @staticmethod
#     async def update_config_with_prev_results(
#         ctx: Context,
#         curr_config: LlmResponsesOpenaiConfig,
#         prev_results: List[ActionResult] | None = None,
#     ) -> LlmResponsesOpenaiConfig:
#         if not prev_results:
#             return curr_config
#         return curr_config

#     async def run_action(
#         self, ctx: Context, action_input: DataBlock, notify: Notify | None = None
#     ) -> AsyncIterator[bytes]:
#         if not self.action_config.store:
#             self.action_config.include = [
#                 "reasoning.encrypted_content",
#                 "code_interpreter_call.outputs",
#                 "file_search_call.results",
#             ]

#         openai_conn = await AppConn.get_openai(
#             ctx=ctx, db=self.db, user=self.user, provider=self.action_config.provider
#         )
#         openai_client: AsyncAzureOpenAI | AsyncOpenAI = await self.get_openai_provider_client(
#             ctx=ctx, openai=openai_conn
#         )

#         def send(ev: str, **kw) -> bytes:
#             return (json.dumps({"event": ev, **kw}, ensure_ascii=False) + "\n").encode("utf-8")

#         try:
#             # input message (text + images)
#             response_input_param = await self.action_input_to_response_input_param(
#                 ctx=ctx, action_input=action_input
#             )
#             self.action_config.input += response_input_param

#             # function tool
#             tool_list = (self.action_config.tools or []) + (action_input.tools or [])
#             function_tool_params: (
#                 List[responses.FunctionToolParam] | None
#             ) = await ToolFactory.get_responses_function_tool_params(ctx=ctx, tool_names=tool_list)
#             self.action_config.tools = function_tool_params

#             # upload files to openai
#             urls_to_upload = []
#             urls_from_config = [
#                 url
#                 for url in self.action_config.file_urls
#                 if url not in self.action_config.file_url_to_file_object_map
#             ]
#             urls_from_input = [
#                 url
#                 for url in action_input.urls
#                 if url not in self.action_config.file_url_to_file_object_map
#             ]
#             urls_to_upload = urls_from_config + urls_from_input
#             file_url_to_file_object_map: Dict[str, FileObject] = await self.upload_urls_to_openai(
#                 ctx=ctx, user=self.user, urls=urls_to_upload, openai_client=openai_conn
#             )
#             for file_url, file_object in file_url_to_file_object_map.items():
#                 self.action_config.file_url_to_file_object_map[file_url] = file_object
#             # file ids to use
#             file_ids = []
#             for file_url, file_object in self.action_config.file_url_to_file_object_map.items():
#                 file_ids.append(file_object.id)

#             # handle openai code_interpreter
#             await self.build_responses_code_interpreter_tool_param(
#                 ctx=ctx, action_input=action_input, file_ids=file_ids
#             )

#             await self.build_responses_mcp_tool_param(
#                 ctx=ctx, user=self.user, db=self.db, action_input=action_input
#             )

#             # handle openai file_search
#             await openai_conn.openai_responses.build_responses_file_search_tool_param(
#                 ctx=ctx, responses_req=self.action_config, openai_client=openai_client, file_ids=file_ids
#             )

#             # handle web_search
#             await self.build_responses_web_search_tool_param(ctx=ctx, action_input=action_input)

#             responses_client: OpenaiResponsesStream = openai_conn.openai_responses_stream

#             # resp = await self.execute(
#             #     ctx=ctx, action_input=action_input, responses_client=responses_client
#             # )
#             resp = await self.execute(
#                 ctx=ctx, action_input=action_input, responses_client=responses_client, notify=notify
#             )
#             logger.bind(ctx=ctx, action_input=action_input, resp=resp).info(
#                 "openai_responses.create final response"
#             )
#             # return resp
#             logger.bind(ctx=ctx, resp=resp).info(f"openai_responses_stream return value : {resp}")
#             yield send("action-run", result=resp.model_dump())
#         except Exception as e:
#             logger.bind(ctx=ctx).exception(f"Error in run_action: {str(e)}")
#             yield send("action-run", error=str(e))
#             # return e

#     @retry(stop=stop_after_attempt(3), wait=wait_fixed(2), reraise=True)
#     async def execute(
#         self, ctx: Context, action_input: DataBlock, responses_client: OpenaiResponsesStream
#     ) -> AsyncIterator[bytes]:
#         def send(ev: str, **kw) -> bytes:
#             return (json.dumps({"event": ev, **kw}, ensure_ascii=False) + "\n").encode("utf-8")

#         try:
#             # Dont need action_input, it has been added in config in caller method
#             # responses_client = get_openai(provider=self.action_config.provider).openai_responses
#             # responses_req = OpenaiResponsesReq(**self.action_config.model_dump(exclude_none=True))
#             # response: Response = await responses_client.create(
#             #     responses_req=self.action_config, user=self.user, ctx=ctx, db=self.db
#             # )
#             agen = responses_client.create(
#                 responses_req=self.action_config,
#                 user=self.user,
#                 ctx=ctx,
#                 db=self.db,
#             )

#             response: Response = None

#             async for chunk in agen:
#                 # chunk = await agen.__anext__()   # or: await anext(agen) on 3.10+
#                 try:
#                     obj = json.loads(chunk.decode("utf-8"))
#                     if obj.get("event") == "create":
#                         response = obj.get("error")
#                         if response is None:
#                             response = Response.model_validate(obj.get("result"))
#                             if response is None:
#                                 raise Exception("No return value in create event")
#                         else:
#                             raise Exception(f"Error in create event: {response}")
#                 except Exception as e:
#                     raise e
#                 yield chunk  # forward chunks to the upstream caller

#             # self.action_config = self.action_config.model_copy(update=responses_req.model_dump())
#             data_blocks = await self.response_to_action_output(
#                 ctx=ctx, response=response, action_input=action_input
#             )
#             logger.bind(ctx=ctx).info(
#                 f"openai_responses_stream.execute data_blocks : {data_blocks}"
#             )
#             yield send("execute", result=data_blocks.model_dump())
#             # return data_blocks
#         except Exception as e:
#             logger.bind(ctx=ctx).exception(f"Error in execute: {str(e)}")
#             yield send("execute", error=str(e))

#     async def get_openai_provider_client(
#         self, ctx: Context, openai: OpenAI
#     ) -> AsyncAzureOpenAI | AsyncOpenAI:
#         client: AsyncAzureOpenAI | AsyncOpenAI | None = None
#         model = self.action_config.model
#         # openai = await AppConn.get_openai(
#         #     ctx=ctx, db=self.db, user=self.user, provider=self.action_config.provider
#         # )
#         if self.action_config.provider == "azure":
#             logger.bind(ctx=ctx).info(f"Using Azure OpenAI client with model: {model}")
#             client: AsyncAzureOpenAI = await openai.openai_responses.client_factory.create_client(
#                 model=model
#             )
#         elif self.action_config.provider == "openai":
#             logger.bind(ctx=ctx).info("Using OpenAI responses client")
#             client: AsyncOpenAI = openai.client
#         assert client, "Openai LLM responses Client not found"
#         return client

#     async def action_input_to_response_input_param(
#         self, ctx: Context, action_input: DataBlock
#     ) -> ResponseInputParam:
#         response_input_message_content_list_param: ResponseInputMessageContentListParam = []
#         if action_input.text:
#             response_input_message_content_list_param.append(
#                 ResponseInputTextParam(type="input_text", text=action_input.text)
#             )
#         for url in action_input.urls:
#             try:
#                 response_input_image_param = await self.url_to_response_input_image_param(
#                     ctx=ctx,
#                     url=url,
#                 )
#                 if response_input_image_param:
#                     response_input_message_content_list_param.append(response_input_image_param)
#             except Exception as e:
#                 logger.bind(ctx=ctx, url=url).exception(
#                     f"Error in convert_urls_to_input_params: {str(e)}"
#                 )

#         message = Message(
#             type="message",
#             content=response_input_message_content_list_param,
#             role="user",
#         )
#         response_input_param: ResponseInputParam = [message]
#         return response_input_param

#     async def url_to_response_input_image_param(
#         self, ctx: Context, url: str
#     ) -> ResponseInputContentParam | None:
#         type, encoding = mimetypes.guess_type(url)
#         if type is None:
#             return None
#         extension = mimetypes.guess_extension(type)
#         if extension in ActionText2TextLlmResponsesOpenaiStream.IMAGE_FORMATS:
#             content = ResponseInputImageParam(type="input_image", image_url=url, detail="auto")
#             return content

#     # async def build_responses_file_search_tool_param(
#     #     self,
#     #     ctx: Context,
#     #     action_input: DataBlock,
#     #     openai_client: AsyncAzureOpenAI | AsyncOpenAI,
#     #     file_ids: List[str] = list,
#     # ) -> None:
#     #     # DEPRECATED: This method is unused by run_action. Logic moved to OpenaiResponses.
#     #     new_vector_store_ids = []
#     #     tool_list = [] if self.action_config.tools is None else self.action_config.tools
#     #     tool_list += action_input.tools
#     #     for i, tool in enumerate(tool_list):
#     #         if (
#     #             isinstance(tool, dict)
#     #             and "type" in tool
#     #             and tool["type"] == "file_search"
#     #             and "vector_store_ids" in tool
#     #         ):
#     #             new_vector_store_ids += tool["vector_store_ids"]

#     #     new_file_ids = []
#     #     for file_id in file_ids:
#     #         for file_url, file_object in self.action_config.file_url_to_file_object_map.items():
#     #             if file_id == file_object.id:
#     #                 ext = "." + file_object.filename.split(".")[-1]
#     #                 ext = ext.lower()
#     #                 if ext in OPENAI_FILE_FORMATS:
#     #                     if file_id not in self.action_config.file_id_to_vector_store_map:
#     #                         new_file_ids.append(file_id)
#     #                 else:
#     #                     logger.bind(
#     #                         ctx=ctx, filename=file_object.filename, file_object=file_object
#     #                     ).info("openai_responses.file_search file_ext not in FILE_FORMATS")
#     #                     continue

#     #     logger.bind(ctx=ctx, file_ids=new_file_ids).info(
#     #         "openai_responses.file_search new file_ids"
#     #     )
#     #     if new_file_ids:
#     #         vector_store_client = OpenaiVectorStores(openai_client)
#     #         create_vector_store = CreateVectorStore(file_ids=new_file_ids)
#     #         new_vector_store = await vector_store_client.create(
#     #             create_vector_store=create_vector_store,
#     #         )
#     #         for file_id in new_file_ids:
#     #             self.action_config.file_id_to_vector_store_map[file_id] = VectorStoreLite(
#     #                 **new_vector_store.model_dump()
#     #             )
#     #         logger.bind(ctx=ctx, vector_store=new_vector_store).info(
#     #             "openai_responses.create vector_store"
#     #         )
#     #         new_vector_store_ids.append(new_vector_store.id)

#         new_vector_store_ids = list(set(new_vector_store_ids))

#         # create new file_search_tool_param
#         file_search_tool_param = FileSearchToolParam(
#             type="file_search",
#             vector_store_ids=new_vector_store_ids,
#             max_num_results=10,
#             ranking_options=RankingOptions(ranker="auto", score_threshold=0.5),
#         )

#         sanitized_tool_list: list | None = []
#         file_search_index = -1
#         for i, tool in enumerate(tool_list):
#             if (isinstance(tool, str) and tool == "file_search") or (
#                 isinstance(tool, dict) and "type" in tool and tool["type"] == "file_search"
#             ):
#                 if (
#                     file_search_index == -1
#                     and new_vector_store_ids
#                     and len(new_vector_store_ids) > 0
#                 ):
#                     file_search_index = i
#                     sanitized_tool_list.append(file_search_tool_param)
#             else:
#                 sanitized_tool_list.append(tool)

#         self.action_config.tools = sanitized_tool_list if sanitized_tool_list else None

#     async def build_responses_code_interpreter_tool_param(
#         self, ctx: Context, action_input: DataBlock, file_ids: List[str]
#     ) -> None:
#         new_file_ids = []
#         tool_list = [] if self.action_config.tools is None else self.action_config.tools
#         tool_list += action_input.tools
#         for i, tool in enumerate(tool_list):
#             if (
#                 isinstance(tool, dict)
#                 and "type" in tool
#                 and tool["type"] == "code_interpreter"
#                 and "container" in tool
#                 and "file_ids" in tool["container"]
#             ):
#                 new_file_ids += tool["container"]["file_ids"]
#         new_file_ids += file_ids
#         new_file_ids = list(set(new_file_ids))
#         logger.bind(ctx=ctx, file_ids=new_file_ids).info(
#             "openai_responses.code_interpreter file_ids"
#         )

#         new_code_interpreter = CodeInterpreter(
#             type="code_interpreter",
#             container=CodeInterpreterContainerCodeInterpreterToolAuto(
#                 type="auto", file_ids=new_file_ids
#             ),
#         )

#         sanitized_tool_list: list | None = []
#         code_interpreter_index = -1
#         for i, tool in enumerate(tool_list):
#             if (isinstance(tool, str) and tool == "code_interpreter") or (
#                 isinstance(tool, dict) and "type" in tool and tool["type"] == "code_interpreter"
#             ):
#                 if code_interpreter_index == -1:
#                     code_interpreter_index = i
#                     sanitized_tool_list.append(new_code_interpreter)
#             else:
#                 sanitized_tool_list.append(tool)

#         self.action_config.tools = sanitized_tool_list if sanitized_tool_list else None

#     async def build_responses_mcp_tool_param(
#         self, ctx: Context, user: UserORM, db: AsyncIOMotorDatabase, action_input: DataBlock
#     ) -> None:
#         logger.bind(ctx=ctx, action_input=action_input).debug("Building MCP tool params")
#         sanitized_tool_list: list | None = []
#         tool_list = [] if self.action_config.tools is None else self.action_config.tools
#         tool_list += action_input.tools
#         for i, tool in enumerate(tool_list):
#             if isinstance(tool, dict) and "type" in tool and tool["type"] == "mcp":
#                 try:
#                     tool_orig = Mcp.model_validate(tool)
#                     sanitized_tool_list.append(tool_orig)
#                 except Exception as e:
#                     logger.bind(ctx=ctx, tool=tool).exception(
#                         f"Error in validate MCP tool: {str(e)}"
#                     )
#                     raise HTTPException(
#                         status_code=400, detail=f"Error in validate saved MCP tool: {str(e)}"
#                     )
#             elif isinstance(tool, str) and (len(tool) > 4 and tool.startswith(MCP_START_CONSTANT)):
#                 mcp_server_name = tool[4:]
#                 try:
#                     user_mcp = UserMCP(user=user, db=self.db)
#                     mcp_doc = await user_mcp.get_mcp_by_server_name(
#                         ctx=ctx, mcp_server_name=mcp_server_name
#                     )
#                     if not mcp_doc:
#                         raise HTTPException(
#                             status_code=400,
#                             detail=f"MCP server with name {mcp_server_name} not found",
#                         )

#                     server_name = mcp_doc.server_name
#                     server_url = mcp_doc.server_url
#                     description = (
#                         mcp_doc.description
#                         if (mcp_doc.description and mcp_doc.description != "")
#                         else None
#                     )
#                     request_headers = (
#                         mcp_doc.request_headers
#                         if (mcp_doc.request_headers and len(mcp_doc.request_headers.keys()) > 0)
#                         else None
#                     )

#                     mcp_tool_param = Mcp(
#                         type="mcp",
#                         server_label=server_name,
#                         server_url=server_url,
#                         server_description=description,
#                         headers=request_headers,
#                         # allowed_tools=mcp_doc.allowed_tools if mcp_doc.allowed_tools else None,
#                         require_approval="never",
#                     )
#                     logger.bind(ctx=ctx, tool=tool, mcp_tool_param=mcp_tool_param).info(
#                         f"Adding MCP tool param for server: {mcp_server_name}"
#                     )
#                     logger.bind(ctx=ctx, tool=tool, mcp_tool_param=mcp_tool_param).debug(
#                         f"MCP tool param: {mcp_tool_param}"
#                     )
#                     sanitized_tool_list.append(mcp_tool_param)

#                 except Exception as e:
#                     logger.bind(ctx=ctx, tool=tool).exception(f"Error in get MCP server: {str(e)}")
#                     raise HTTPException(
#                         status_code=400,
#                         detail=f"Error in get MCP server with name {mcp_server_name}: {str(e)}",
#                     )
#             else:
#                 sanitized_tool_list.append(tool)

#         logger.bind(ctx=ctx, sanitized_tool_list=sanitized_tool_list).debug(
#             f"Sanitized MCP tool list: {sanitized_tool_list}"
#         )

#         self.action_config.tools = sanitized_tool_list if sanitized_tool_list else None
#         return

#     async def build_responses_web_search_tool_param(
#         self, ctx: Context, action_input: DataBlock
#     ) -> None:
#         sanitized_tool_list: list | None = []
#         tool_list = [] if self.action_config.tools is None else self.action_config.tools
#         tool_list += action_input.tools
#         web_search_index = -1
#         for i, tool in enumerate(tool_list):
#             if isinstance(tool, str) and tool == "web_search":
#                 if web_search_index == -1:
#                     web_search_index = i
#                     new_web_search = WebSearchToolParam(
#                         type="web_search_preview",
#                         search_context_size="medium",
#                     )
#                     sanitized_tool_list.append(new_web_search)
#             elif (
#                 isinstance(tool, dict)
#                 and "type" in tool
#                 and (
#                     tool["type"] == "web_search_preview"
#                     or tool["type"] == "web_search_preview_2025_03_11"
#                 )
#             ):
#                 if web_search_index == -1:
#                     web_search_index = i
#                     sanitized_tool_list.append(tool)
#                 else:
#                     sanitized_tool_list[web_search_index] = sanitized_tool_list[
#                         web_search_index
#                     ].update(tool)
#             else:
#                 sanitized_tool_list.append(tool)

#         self.action_config.tools = sanitized_tool_list if sanitized_tool_list else None

#     async def response_to_action_output(
#         self, ctx: Context, response: Response, action_input: DataBlock
#     ) -> DataBlocks | Exception:
#         data_block = DataBlock()
#         if not response:
#             return HTTPException(status_code=500, detail="OpenAI Responses is empty")
#         try:
#             text = response.output_text if response.output_text else ""
#             if text:
#                 data_block.text += text
#             else:
#                 logger.bind(ctx=ctx, messages=self.action_config.messages, response=response).error(
#                     "openai_responses.create response output_text is None/Empty"
#                 )

#             if action_input.metadata.process_attachment_url:
#                 data_block.urls += [
#                     str(s3_url)
#                     for sandbox_url, s3_url in self.action_config.sandbox_url_to_s3_url.items()
#                     if str(s3_url) not in data_block.urls
#                 ]

#             return DataBlocks(blocks=[data_block])
#         except Exception as e:
#             logger.bind(ctx=ctx).exception(
#                 f"Error in response_to_action_output of OpenAI Responses: {str(e)}"
#             )
#             return e

#     async def upload_urls_to_openai(
#         self,
#         ctx: Context,
#         user: UserORM,
#         urls: List[str],
#         openai_client: OpenAI,
#     ) -> Dict[str, FileObject]:
#         file_url_to_file_object_map: Dict[str, FileObject] = (
#             self.action_config.file_url_to_file_object_map
#         )
#         uploaded_urls: Dict[str, FileObject] = await FileUploader.upload_urls_to_openai(
#             ctx=ctx,
#             user=user,
#             db=self.db,
#             urls=urls,
#             openai_client=openai_client,
#             supported_file_formats=OPENAI_FILE_FORMATS + OPENAI_FILE_FORMATS_CODE_INTERPRETER,
#             cache=file_url_to_file_object_map,
#         )
#         logger.bind(
#             ctx=ctx,
#             uploaded_urls=uploaded_urls,
#             file_url_to_file_object_map=file_url_to_file_object_map,
#         ).info("openai_responses.upload_urls_to_openai file_objects")
#         return uploaded_urls

#     # async def upload_urls_to_openai(
#     #     self,
#     #     ctx: Context,
#     #     user: UserORM,
#     #     urls: List[str],
#     #     openai_client: AsyncAzureOpenAI | AsyncOpenAI,
#     # ) -> Dict[str, FileObject]:
#     #     file_url_to_file_object_map: Dict[str, FileObject] = {}
#     #     for url in urls:
#     #         if url in self.action_config.file_url_to_file_object_map:
#     #             continue
#     #         try:
#     #             file_bytes: bytes | None = None
#     #             file_name: str = await UserFiles.extract_file_name(ctx=ctx, file_url=url)
#     #             if url.__contains__(".s3.") or url.startswith("s3://"):
#     #                 s3_key = ""
#     #                 s3: AwsS3 | None = None
#     #                 if url.__contains__(SettingsProvider.sget().AWS_S3_BUCKET_NAME):
#     #                     s3 = get_s3()
#     #                 elif url.__contains__(SettingsProvider.sget().AWS_S3_PUBLIC_BUCKET_NAME):
#     #                     s3 = get_public_s3()
#     #                 else:
#     #                     db = self.db
#     #                     app_creds_doc = await UserAppCreds.get_app_creds_by_app_name(
#     #                         ctx=Context(), db=db, user=user, app_name=AppTypes.aws
#     #                     )
#     #                     user_bucket_name = app_creds_doc.creds["bucket_name"]
#     #                     assert user_bucket_name in url
#     #                     s3 = get_aws_s3(
#     #                         region_name=app_creds_doc.creds["region_name"],
#     #                         aws_access_key_id=app_creds_doc.creds["aws_access_key_id"],
#     #                         aws_secret_access_key=app_creds_doc.creds["aws_secret_access_key"],
#     #                         bucket_name=app_creds_doc.creds["bucket_name"],
#     #                         object_prefix=app_creds_doc.creds["object_prefix"],
#     #                     )
#     #                 assert s3 is not None
#     #                 if url.__contains__(".s3."):
#     #                     s3_key = await s3.get_key_from_https_url(url=url)
#     #                 if url.startswith("s3://"):
#     #                     s3_key = await s3.get_key_from_s3_uri(url=url)
#     #                 assert s3_key
#     #                 file_bytes = await s3.get_bytes(key=s3_key)
#     #             elif url.startswith("http://") or url.startswith("https://"):
#     #                 async with httpx.AsyncClient(follow_redirects=True) as http_client:
#     #                     response = await http_client.get(url)
#     #                     if response.status_code != 200:
#     #                         logger.bind(ctx=ctx, url=url).error(
#     #                             f"openai_responses.upload_urls_to_openai: Error in getting file from url: {url}"
#     #                         )
#     #                         assert response.status_code == 200
#     #                     file_bytes = response.content
#     #             assert file_bytes
#     #             file_tuple = (file_name, file_bytes)
#     #             file_create = FileCreate(
#     #                 file=file_tuple,
#     #                 purpose="assistants",
#     #             )
#     #             # openai_files = get_openai().openai_files
#     #             openai_files = OpenaiFiles(openai_client)
#     #             file_object: FileObject = await openai_files.create(
#     #                 ctx=ctx, file_create=file_create
#     #             )
#     #             file_url_to_file_object_map[url] = file_object
#     #         except Exception as e:
#     #             logger.bind(ctx=ctx, url=url).exception(f"Error in upload_urls_to_openai: {str(e)}")
#     #     logger.bind(ctx=ctx, file_url_to_file_object_map=file_url_to_file_object_map).info(
#     #         "openai_responses.upload_urls_to_openai file_objects"
#     #     )
#     #     return file_url_to_file_object_map
