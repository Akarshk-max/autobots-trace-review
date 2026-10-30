# import json
# from typing import AsyncIterator, List, Literal, Type

# from fastapi import HTTPException
# from loguru import logger
# from openai.types.chat import (
#     ChatCompletion,
#     ChatCompletionContentPartImageParam,
#     ChatCompletionToolParam,
#     ChatCompletionUserMessageParam,
# )
# from openai.types.chat.chat_completion_content_part_image_param import ImageURL

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
# from src.autobots.conn.openai.openai_chat.chat_model import ChatReq
# from src.autobots.data_model.context import Context
# from src.autobots.exception.app_exception import AppException
# from src.autobots.llm.tools.tool_factory import ToolFactory
# from src.autobots.llm.tools.tools_map import TOOLS_MAP


# class LlmChatOpenaiConfig(ChatReq):
#     provider: Literal["azure", "openai"]


# class ActionText2TextLlmChatOpenaiStream(
#     ActionABC[LlmChatOpenaiConfig, LlmChatOpenaiConfig, LlmChatOpenaiConfig, DataBlock, DataBlocks]
# ):
#     type = ActionType.text2text_llm_chat_openai

#     # https://platform.openai.com/docs/guides/vision/what-type-of-files-can-i-upload#what-type-of-files-can-i-upload
#     IMAGE_FORMATS = ["jpeg", "jpg", "png", "gif", "webp"]
#     FILE_FORMATS = ["pdf", "docx", "txt", "md", "html", "json", "yaml", "yml"]

#     @staticmethod
#     def get_config_create_type() -> Type[ActionConfigCreateType]:
#         return LlmChatOpenaiConfig

#     @staticmethod
#     def get_config_update_type() -> Type[ActionConfigUpdateType]:
#         return LlmChatOpenaiConfig

#     @staticmethod
#     def get_config_type() -> Type[ActionConfigType]:
#         return LlmChatOpenaiConfig

#     @staticmethod
#     def get_input_type() -> Type[ActionInputType]:
#         return DataBlock

#     @staticmethod
#     def get_output_type() -> Type[ActionOutputType]:
#         return DataBlocks

#     # def __init__(self, action_config: LlmChatOpenaiConfig, user: UserORM | None = None, db: AsyncIOMotorDatabase | None = None):
#     #     super().__init__(action_config=action_config, user=user, db=db)

#     @staticmethod
#     async def update_config_with_prev_results(
#         ctx: Context,
#         curr_config: LlmChatOpenaiConfig,
#         prev_results: List[ActionResult] | None = None,
#     ) -> LlmChatOpenaiConfig:
#         return curr_config

#     async def run_action(self, ctx: Context, action_input: DataBlock) -> AsyncIterator[bytes]:
#         def send(ev: str, **kw) -> bytes:
#             logger.bind(ctx=ctx).info(f"Sending event: {ev} with data: {kw}")
#             return (json.dumps({"event": ev, **kw}, ensure_ascii=False) + "\n").encode("utf-8")

#         data_blocks = DataBlocks(blocks=[])
#         try:
#             openai_client = await AppConn.get_openai(
#                 ctx=ctx, db=self.db, user=self.user, provider=self.action_config.provider
#             )
#             tool_defs = (
#                 await ActionText2TextLlmChatOpenaiStream.replace_action_tools_with_tools_defs(
#                     ctx=ctx, action_config=self.action_config
#                 )
#             )
#             self.action_config.tools = tool_defs
#             messages = []
#             if action_input and action_input.urls:
#                 file_urls = []
#                 for url in action_input.urls:
#                     if url.split(".")[-1].lower() in self.IMAGE_FORMATS:
#                         image_message = ChatCompletionUserMessageParam(
#                             role="user",
#                             content=[
#                                 ChatCompletionContentPartImageParam(
#                                     image_url=ImageURL(url=url, detail="auto"), type="image_url"
#                                 )
#                             ],
#                         )
#                         messages.append(image_message)
#                     else:
#                         file_urls.append(url)

#                 # TODO: Debug getting error  Error: Error code: 500 - {'error': {'message': 'The server had an error processing your request. Sorry about that! You can retry your request, or contact us through our help center at help.openai.com if you keep seeing this error. (Please include the request ID req_761d92d7600143518e5e93aba7131c88 in your email.)', 'type': 'server_error', 'param': None, 'code': None}}
#                 # if file_urls:
#                 #     match self.action_config.provider:
#                 #         case "azure":
#                 #             pass
#                 #         case "openai":
#                 #             uploaded_files: Dict[
#                 #                 str, FileObject
#                 #             ] = await FileUploader.upload_urls_to_openai(
#                 #                 ctx=ctx,
#                 #                 user=self.user,
#                 #                 db=self.db,
#                 #                 urls=file_urls,
#                 #                 openai_client=openai_client,
#                 #                 supported_file_formats=OPENAI_FILE_FORMATS,
#                 #             )
#                 #             content = []
#                 #             for url, file_obj in uploaded_files.items():
#                 #                 file = File(type="file", file=FileFile(file_id=file_obj.id))
#                 #                 content.append(file)
#                 #             if content:
#                 #                 message = ChatCompletionUserMessageParam(
#                 #                     role="user", content=content
#                 #                 )
#                 #                 messages.append(message)

#             if action_input and (action_input.text != "" or action_input.urls):
#                 llm_prompt = action_input.get_llm_prompt(ctx=ctx)
#                 logger.bind(ctx=ctx, llm_prompt=llm_prompt).info("llm prompt")
#                 message = ChatCompletionUserMessageParam(role="user", content=llm_prompt)
#                 messages.append(message)

#             self.action_config.messages += messages
#             chat_req = ChatReq(**self.action_config.model_dump(exclude_none=True))
#             chat_res: ChatCompletion | None = None

#             agen = openai_client.openai_chat_stream.chat_stream(
#                 ctx=ctx, chat_req=chat_req, user=self.user, db=self.db
#             )
#             async for chunk in agen:
#                 obj = json.loads(chunk.decode("utf-8"))
#                 if obj.get("event") == "chat-stream":
#                     err = obj.get("error")
#                     if err:
#                         raise Exception(f"Error from the chat_stream function : {str(err)}")
#                     res = obj.get("result")
#                     if not res:
#                         raise Exception("No result from the chat_stream function")
#                     chat_res = ChatCompletion.model_validate(res)
#                 yield chunk

#             self.action_config = self.action_config.model_copy(
#                 update=chat_req.model_dump()
#             )  # creates a new model and assigns it to self.action_config, breaking the reference to config passed in __init__
#             # self.action_config.messages = chat_req.messages # This wont break the reference, but what about fields other thhan message, will have to keep track of all fields that might change
#             if not chat_res:
#                 # return data_blocks
#                 yield send("action-run", result=data_blocks.model_dump())
#             for choice in chat_res.choices:
#                 if choice.message.content:
#                     data_blocks.blocks.append(DataBlock(text=choice.message.content))
#                 else:
#                     logger.bind(
#                         ctx=ctx, chat_req_messages=self.action_config.messages, choice=choice
#                     ).error("openai_chat.chat response choice.message.content is None")
#             if len(data_blocks.blocks) == 0:
#                 raise HTTPException(
#                     status_code=503, detail="No response from OpenAI, please try again."
#                 )
#             # return data_blocks
#             yield send("action-run", result=data_blocks.model_dump())
#         except Exception as e:
#             logger.bind(ctx=ctx).exception(f"Error in run action of LLMChat: {str(e)}")
#             yield send("error", error=str(e))

#     async def run_tool(
#         self, action_config: LlmChatOpenaiConfig, ctx: Context
#     ) -> DataBlocks | Exception:
#         data_blocks = DataBlocks(blocks=[])
#         try:
#             tool_defs = (
#                 await ActionText2TextLlmChatOpenaiStream.replace_action_tools_with_tools_defs(
#                     ctx=ctx, action_config=action_config
#                 )
#             )
#             action_config.tools = tool_defs
#             openai_client = await AppConn.get_openai(
#                 ctx=ctx, db=self.db, user=self.user, provider=action_config.provider
#             )
#             chat_req = ChatReq(**action_config.model_dump(exclude_none=True))
#             chat_res = await openai_client.openai_chat.chat(
#                 ctx=ctx, chat_req=chat_req, user=self.user
#             )
#             if not chat_res:
#                 raise AppException(detail="Cannot run LLM", http_status=500)
#             for choice in chat_res.choices:
#                 data_blocks.blocks.append(DataBlock(text=choice.message.content))
#             return data_blocks
#         except Exception as e:
#             logger.bind(ctx=ctx).exception(f"Error in run tool of LLMChat: {str(e)}")
#             return e

#     @staticmethod
#     async def replace_action_tools_with_tools_defs(
#         ctx: Context, action_config: LlmChatOpenaiConfig
#     ) -> List[ChatCompletionToolParam] | None:
#         tool_defs = []
#         if action_config.tools:
#             action_tools = []
#             for action_tool in action_config.tools:
#                 if isinstance(action_tool, str):
#                     if action_tool in TOOLS_MAP:
#                         action_tools.append(action_tool)
#                 if isinstance(action_tool, dict):
#                     tool_defs.append(action_tool)
#             logger.bind(ctx=ctx).info(f"Getting tool definitions for {action_tools}")
#             new_tool_defs = await ToolFactory.get_chat_completion_tool_param(
#                 ctx=ctx, tool_names=action_tools
#             )
#             if new_tool_defs:
#                 tool_defs += new_tool_defs
#             if len(tool_defs) == 0:
#                 return None
#             return tool_defs
