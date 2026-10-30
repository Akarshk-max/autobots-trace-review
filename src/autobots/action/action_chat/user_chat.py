# from typing import List

# from fastapi import HTTPException
# from loguru import logger
# from motor.motor_asyncio import AsyncIOMotorDatabase
# from openai.types.chat import ChatCompletionUserMessageParam

# from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
# from src.autobots.action.action_chat.chat_crud import ChatCRUD
# from src.autobots.action.action_chat.chat_doc_model import (
#     ChatCreate,
#     ChatDoc,
#     ChatDocCreate,
#     ChatDocFind,
#     ChatDocUpdate,
#     ChatFind,
#     ChatUpdate,
# )
# from src.autobots.action.action_type.action_factory import ActionFactory, RunActionObj
# from src.autobots.action.action_type.action_types import ActionType
# from src.autobots.integrations.app_conn.app_conn import AppConn
# from src.autobots.conn.openai.openai_chat.chat_model import ChatReq, Message, Role
# from src.autobots.data_model.context import Context
# from src.autobots.space.space_doc_model import SpaceMemberDoc
# from src.autobots.user.user_orm_model import UserORM


# class UserChat:
#     """
#     LM chat uses an Action to run and stores context to enable chat functionality
#     """

#     DEFAULT_TITLE = "New Chat"

#     def __init__(self, user: UserORM, db: AsyncIOMotorDatabase):
#         self.user = user
#         self.user_id = str(user.id)
#         self.db = db
#         self.chat_crud = ChatCRUD(db)

#     async def create_chat(
#         self, chat_create: ChatCreate, title: str = DEFAULT_TITLE
#     ) -> ChatDoc | None:
#         if (
#             not chat_create.action.type == ActionType.text2text_llm_chat_openai
#             and not chat_create.action.type
#             == ActionType.text2text_llm_chat_with_vector_search_openai
#         ):
#             raise HTTPException(400, "Action is not available for chat")
#         try:
#             chat_doc_create = ChatDocCreate(
#                 user_id=self.user_id, title=title, **chat_create.model_dump(by_alias=True)
#             )
#             chat_doc = await self.chat_crud.insert_one(chat_doc_create)
#             return chat_doc
#         except Exception as e:
#             logger.error(str(e))
#         return None

#     async def list_chat(
#         self, chat_find: ChatFind, limit: int = 100, offset: int = 0
#     ) -> List[ChatDoc] | None:
#         try:
#             chat_doc_find = ChatDocFind(user_id=self.user_id, **chat_find.model_dump())
#             chat_docs = await self.chat_crud.find(chat_doc_find, limit, offset)
#             return chat_docs
#         except Exception as e:
#             logger.error(str(e))
#         return None

#     async def get_chat(self, chat_id: str) -> ChatDoc | None:
#         try:
#             chat_doc_find = ChatDocFind(id=chat_id, user_id=self.user_id)
#             chat_docs = await self.chat_crud.find(chat_doc_find)
#             if len(chat_docs) != 1:
#                 raise HTTPException(500, "Error in finding chat")
#             return chat_docs[0]
#         except Exception as e:
#             logger.error(str(e))
#         return None

#     async def update_chat(self, chat_id: str, chat_update: ChatUpdate) -> ChatDoc:
#         chat_doc_update = ChatDocUpdate(
#             id=chat_id, user_id=self.user_id, **chat_update.model_dump()
#         )
#         chat_doc = await self.chat_crud.update_one(chat_doc_update)
#         return chat_doc

#     async def delete_chat(self, chat_id: str):
#         chat_doc_find = ChatDocFind(id=chat_id, user_id=self.user_id)
#         delete_result = await self.chat_crud.delete_many(chat_doc_find)
#         return delete_result.deleted_count

#     async def chat(self, ctx: Context, chat_id: str, input: DataBlock, user: UserORM, space_member: SpaceMemberDoc | None = None) -> ChatDoc:
#         chat_doc = await self.get_chat(chat_id)
#         if not chat_doc:
#             raise HTTPException(404, "Chat not found")
#         chat_req = ChatReq.model_validate(chat_doc.action.config)
#         chat_req.messages = chat_req.messages + chat_doc.messages

#         run_obj: RunActionObj = await ActionFactory.run_action(
#             ctx=ctx,
#             action_doc=chat_doc.action,
#             action_input_dict=input.model_dump(),
#             user=self.user,
#             db=self.db,
#             space_member=space_member,
#         )
#         resp_objs: DataBlocks = DataBlocks.model_validate(run_obj.output_dict)

#         messages = []
#         input_message = Message(role=Role.user, content=input.text)
#         messages.append(input_message)
#         for resp_obj in resp_objs.texts:
#             data_block = DataBlock.model_validate(resp_obj)
#             message = Message(role=Role.assistant, content=data_block.text)
#             messages.append(message)

#         chat_doc.messages = chat_doc.messages + messages
#         if chat_doc.title == UserChat.DEFAULT_TITLE:
#             chat_doc.title = await self._gen_title(ctx, chat_doc)
#         updated_chat_doc = await self.update_chat(chat_id, ChatUpdate(**chat_doc.model_dump()))
#         return updated_chat_doc

#     async def _gen_title(self, ctx: Context, chat_doc: ChatDoc) -> str:
#         try:
#             title_gen_content = "Act as expert title generator. Generate very short text title for the following conversation:\n"

#             action_content = ""
#             for message_dict in chat_doc.action.config.get("messages"):
#                 message = Message.model_validate(message_dict)
#                 action_content = action_content + f"{message.role}: {message.content}\n"
#                 break

#             conversation_content = ""
#             i = 0
#             for message in chat_doc.messages:
#                 conversation_content = conversation_content + f"{message.role}: {message.content}\n"
#                 i = i + 1
#                 if i >= 2:
#                     break

#             title_gen_message = ChatCompletionUserMessageParam(
#                 role=Role.user.value,
#                 content=title_gen_content + action_content + conversation_content,
#             )
#             client = await AppConn.get_openai(
#                 ctx=ctx, db=self.db, user=self.user, provider=chat_doc.openai_provider
#             )
#             chat_res = await client.openai_chat.chat(
#                 chat_req=ChatReq(messages=[title_gen_message], max_tokens=25), ctx=ctx
#             )
#             title = f"{chat_doc.action.name}-{chat_res.choices[0].message.content}"
#             return title
#         except Exception as e:
#             logger.bind(ctx=ctx).error(str(e))
#         return UserChat.DEFAULT_TITLE
