# from typing import List, Optional, Type
#
# from loguru import logger
# from motor.motor_asyncio import AsyncIOMotorDatabase
# from openai.types.chat import ChatCompletionUserMessageParam
# from pydantic import BaseModel
#
# from src.autobots.action.action.action_doc_model import ActionCreate, ActionResult
# from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
# from src.autobots.action.action_type.abc.ActionABC import (
#     ActionABC,
#     ActionConfigCreateType,
#     ActionConfigType,
#     ActionConfigUpdateType,
#     ActionInputType,
#     ActionOutputType,
# )
# from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
#     LlmChatOpenaiConfig,
# )
# from src.autobots.action.action_type.action_types import ActionType
# from src.autobots.integrations.app_conn.app_conn import AppConn
# from src.autobots.conn.aws.s3 import get_s3
# from src.autobots.conn.openai.openai_chat.chat_model import Role
# from src.autobots.conn.pinecone.pinecone import get_pinecone
# from src.autobots.data_model.context import Context
# from src.autobots.datastore.datastore import Datastore
# from src.autobots.server_sent_events.server_sent_events import Notify
# from src.autobots.user.user_orm_model import UserORM
#
#
# class ActionCreateText2TextLlmChatWithVectorSearchOpenaiConfig(BaseModel):
#     datastore_id: str
#     chat_req: LlmChatOpenaiConfig
#     input: Optional[DataBlock] = None
#     output: Optional[DataBlocks] = None
#
#
# class ActionCreateText2TextLlmChatWithVectorSearchOpenai(ActionCreate):
#     type: ActionType = ActionType.text2text_llm_chat_with_vector_search_openai
#     config: ActionCreateText2TextLlmChatWithVectorSearchOpenaiConfig
#
#
# class ActionText2TextLlmChatWithVectorSearchOpenai(
#     ActionABC[
#         ActionCreateText2TextLlmChatWithVectorSearchOpenaiConfig,
#         ActionCreateText2TextLlmChatWithVectorSearchOpenaiConfig,
#         ActionCreateText2TextLlmChatWithVectorSearchOpenaiConfig,
#         DataBlock,
#         DataBlocks,
#     ]
# ):
#     """
#     Vector search and add it to chat prompt as context
#     """
#
#     type = ActionType.text2text_llm_chat_with_vector_search_openai
#
#     @staticmethod
#     def get_config_create_type() -> Type[ActionConfigCreateType]:
#         return ActionCreateText2TextLlmChatWithVectorSearchOpenaiConfig
#
#     @staticmethod
#     def get_config_update_type() -> Type[ActionConfigUpdateType]:
#         return ActionCreateText2TextLlmChatWithVectorSearchOpenaiConfig
#
#     @staticmethod
#     def get_config_type() -> Type[ActionConfigType]:
#         return ActionCreateText2TextLlmChatWithVectorSearchOpenaiConfig
#
#     @staticmethod
#     def get_input_type() -> Type[ActionInputType]:
#         return DataBlock
#
#     @staticmethod
#     def get_output_type() -> Type[ActionOutputType]:
#         return DataBlocks
#
#     def __init__(
#         self,
#         action_config: ActionCreateText2TextLlmChatWithVectorSearchOpenaiConfig,
#         user: UserORM | None = None,
#         db: AsyncIOMotorDatabase | None = None,
#     ):
#         super().__init__(action_config=action_config, user=user, db=db)
#         # self.datastore = Datastore(
#         #     s3=get_s3(), pinecone=get_pinecone(), unstructured=get_unstructured_io()
#         # ).hydrate(datastore_id=action_config.datastore_id)
#         self.datastore: Datastore | None = None
#
#     async def get_datastore(self, ctx: Context) -> Datastore:
#         if self.datastore is None:
#             unstructured = await AppConn.get_unstructured_io(
#                 ctx=ctx,
#                 user=self.user,
#                 db=self.db,
#             )
#
#             self.datastore = Datastore(
#                 s3=get_s3(),
#                 pinecone=get_pinecone(),
#                 unstructured=unstructured,
#             ).hydrate(datastore_id=self.action_config.datastore_id)
#
#         return self.datastore
#
#     # @staticmethod
#     # async def update_config_with_prev_IO(
#     #         curr_config: ActionCreateText2TextLlmChatWithVectorSearchOpenaiConfig,
#     #         prev_input: TextObj | None = None,
#     #         prev_output: TextObjs | None = None,
#     # ) -> ActionCreateText2TextLlmChatWithVectorSearchOpenaiConfig:
#     #     #     ChatCompletionUserMessageParam,
#     #     #     ChatCompletionAssistantMessageParam,
#     #     if not prev_input or not prev_output or prev_input.text == "" or len(prev_output.texts) == 0 :
#     #         return curr_config
#     #     updated_messages = (
#     #             curr_config.chat_req.messages +
#     #             [ChatCompletionUserMessageParam(role="user", content=prev_input.text)] +
#     #             [ChatCompletionAssistantMessageParam(role="assistant", content=prev_output_text_obj.text) for prev_output_text_obj in prev_output.texts]
#     #     )
#     #     curr_config.chat_req.messages = updated_messages
#     #     return curr_config
#
#     @staticmethod
#     async def update_config_with_prev_results(
#         ctx: Context,
#         curr_config: ActionCreateText2TextLlmChatWithVectorSearchOpenaiConfig,
#         prev_results: List[ActionResult] | None = None,
#     ) -> ActionCreateText2TextLlmChatWithVectorSearchOpenaiConfig:
#         # if not prev_results:
#         #     return curr_config
#         #
#         # for prev_result in prev_results:
#         #     action_input: DataBlock = ActionText2TextLlmChatWithVectorSearchOpenai.get_input_type().model_validate(prev_result.input)
#         #     action_output: DataBlocks = ActionText2TextLlmChatWithVectorSearchOpenai.get_output_type().model_validate(prev_result.output)
#         #     config_message_1 = [ChatCompletionUserMessageParam(role="user", content=action_input.text)]
#         #     config_messages_2 = [
#         #         ChatCompletionAssistantMessageParam(role="assistant", content=prev_output_text_obj.text) for
#         #         prev_output_text_obj in action_output.blocks]
#         #
#         #     curr_config.chat_req.messages = curr_config.chat_req.messages + config_message_1 + config_messages_2
#         return curr_config
#
#     async def run_action(
#         self, ctx: Context, action_input: DataBlock, notify: Notify | None = None
#     ) -> DataBlocks | Exception:
#         try:
#             # vector search
#             _ = await self.get_datastore(ctx)
#             search_results = await self.datastore.search(action_input.text, top_k=3)
#             context = ""
#             if len(search_results) == 0:
#                 # return None
#                 pass
#             else:
#                 context += 'Relevant context to give/build response. If the context is unrelated say "Cannot answer from given context"\nContext: \n'
#                 for result in search_results:
#                     context += f"{result}\n"
#             # LM chat
#             input_message = ChatCompletionUserMessageParam(
#                 role=Role.user.value, content=f"{action_input.text}"
#             )
#             context_message = ChatCompletionUserMessageParam(
#                 role=Role.user.value, content=f"{context}"
#             )
#             self.action_config.chat_req.messages = self.action_config.chat_req.messages + [
#                 input_message,
#                 context_message,
#             ]
#             self.action_config.input = action_input
#             client = await AppConn.get_openai(
#                 ctx=ctx, db=self.db, user=self.user, provider=self.action_config.chat_req.provider
#             )
#             chat_res = await client.openai_chat.chat(
#                 ctx=ctx, chat_req=self.action_config.chat_req, notify=notify
#             )
#             action_results = DataBlocks()
#             for choice in chat_res.choices:
#                 action_results.blocks.append(DataBlock(text=choice.message.content))
#             return action_results
#         except Exception as e:
#             logger.bind(ctx=ctx).exception(f"Error in run_action: {str(e)}")
#             return e
#
#     async def run_tool(
#         self,
#         action_config: ActionCreateText2TextLlmChatWithVectorSearchOpenaiConfig,
#         ctx: Context,
#         notify: Notify | None = None,
#     ) -> DataBlocks | Exception:
#         pass
