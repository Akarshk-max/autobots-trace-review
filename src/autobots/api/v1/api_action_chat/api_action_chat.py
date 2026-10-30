# from typing import List, Literal

# import supabase_auth
# from fastapi import APIRouter, Depends, HTTPException
# from motor.motor_asyncio import AsyncIOMotorDatabase
# from starlette.requests import Request

# from src.autobots import SettingsProvider
# from src.autobots.action.action.common_action_models import DataBlock
# from src.autobots.action.action.user_actions import UserActions
# from src.autobots.action.action_chat.chat_doc_model import ChatCreate, ChatDoc, ChatFind, ChatUpdate
# from src.autobots.action.action_chat.user_chat import UserChat
# from src.autobots.auth.security import get_online_user_from_access_token
# from src.autobots.core.database.mongo_base import get_mongo_db
# from src.autobots.space.space_auth import get_optional_space_member
# from src.autobots.space.space_doc_model import SpaceMemberDoc
# from src.autobots.user.user_orm_model import UserORM

# router = APIRouter(
#     prefix=SettingsProvider.sget().API_ACTION_CHATS, tags=[SettingsProvider.sget().API_ACTION_CHATS]
# )


# @router.post("/")
# async def create_chat(
#     action_id: str,
#     openai_provider: Literal["openai", "azure"],
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
#     space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
# ) -> ChatDoc:
#     user_orm = UserORM(id=user_res.user.id)
#     action_doc = await UserActions(user=user_orm, db=db).get_action(action_id, space_member=space_member)
#     chat_create = ChatCreate(action=action_doc, messages=[], openai_provider=openai_provider)
#     chat_doc = await UserChat(user=user_orm, db=db).create_chat(chat_create)
#     return chat_doc


# @router.get("/")
# async def list_chat(
#     id: str = None,
#     title: str = None,
#     limit: int = 100,
#     offset: int = 0,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
#     space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
# ) -> List[ChatDoc]:
#     user_orm = UserORM(id=user_res.user.id)
#     chat_find = ChatFind(id=id, title=title)
#     chat_docs = await UserChat(user=user_orm, db=db).list_chat(chat_find, limit, offset)
#     return chat_docs


# @router.get("/{id}")
# async def get_chat(
#     id: str,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
#     space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
# ) -> ChatDoc:
#     user_orm = UserORM(id=user_res.user.id)
#     chat_doc = await UserChat(user=user_orm, db=db).get_chat(id)
#     return chat_doc


# @router.put("/{id}")
# async def update_chat(
#     id: str,
#     chat_update: ChatUpdate,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
#     space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
# ) -> ChatDoc:
#     user_orm = UserORM(id=user_res.user.id)
#     chat_doc = await UserChat(user=user_orm, db=db).update_chat(id, chat_update)
#     return chat_doc


# @router.delete("/{id}")
# async def delete_chat(
#     id: str,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
#     space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
# ) -> ChatDoc:
#     user_orm = UserORM(id=user_res.user.id)
#     user_chat = UserChat(user=user_orm, db=db)
#     chat_doc = await user_chat.get_chat(id)
#     if chat_doc is None:
#         raise HTTPException(400, "Action not found")
#     deleted_count = await user_chat.delete_chat(id)
#     if deleted_count != 1:
#         raise HTTPException(500, "Error in deleting action")
#     return chat_doc


# @router.post("/{id}")
# async def chat(
#     request: Request,
#     id: str,
#     input: DataBlock,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
#     space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
# ) -> str:
#     ctx = request.state.context
#     user_orm = UserORM(id=user_res.user.id)
#     resp = await UserChat(user=user_orm, db=db).chat(
#         ctx=ctx, chat_id=id, input=input, user=user_orm, space_member=space_member
#     )
#     return resp.messages[-1].content
