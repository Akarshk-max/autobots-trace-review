from typing import Optional

import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from starlette.requests import Request

from src.autobots.action.action.action_doc_model import ActionCreate, ActionDoc
from src.autobots.action.action.common_action_models import DataBlock, TextObj, TextObjs
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
    LlmChatOpenaiConfig,
)

# from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_with_vector_search_openai import (
#     ActionCreateText2TextLlmChatWithVectorSearchOpenai,
# )
from src.autobots.action.action_type.action_text2text.action_text2text_read_urls import (
    ReadUrlConfig,
)

# from src.autobots.action.action_type.action_text2text.action_text2text_search_map import (
#     SearchMapsConfig,
# )
from src.autobots.action.action_type.action_text2text.action_text2text_search_web import (
    SearchWebConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

router = APIRouter()


class ActionCreateText2TextLlmChatOpenai(ActionCreate):
    type: ActionType = ActionType.text2text_llm_chat_openai
    config: LlmChatOpenaiConfig
    input: Optional[TextObj] = None
    output: Optional[TextObjs] = None


@router.post("/text2text/llm_chat_openai")
async def create_action_text2text_llm_chat_openai(
    request: Request,
    action_create: ActionCreateText2TextLlmChatOpenai,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> ActionDoc:
    ctx = request.state.context
    app_state: AppState = request.state.app_state
    try:
        user_orm = UserORM(id=user_res.user.id)
        action_doc = await UserActions(user_orm, db).create_action(
            ctx, app_state, ActionCreate(**action_create.model_dump())
        )
        return action_doc
    except Exception as e:
        logger.error(str(e))
        raise HTTPException(500)


# @router.post("/text2text/llm_chat_with_vector_search_openai")
# async def create_action_text2text_llm_chat_with_vector_search_openai(
#     request: Request,
#     action_create: ActionCreateText2TextLlmChatWithVectorSearchOpenai,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> ActionDoc:
#     ctx = request.state.context
#     try:
#         user_orm = UserORM(id=user_res.user.id)
#         action_doc = await UserActions(user_orm, db).create_action(
#             ctx, ActionCreate(**action_create.model_dump())
#         )
#         return action_doc
#     except Exception as e:
#         logger.error(str(e))
#         raise HTTPException(500)


class ActionCreateText2TextReadUrl(ActionCreate):
    type: ActionType = ActionType.text2text_read_url
    config: ReadUrlConfig


@router.post("/text2text/read_url")
async def create_action_text2text_read_url(
    request: Request,
    action_create: ActionCreateText2TextReadUrl,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> ActionDoc:
    ctx = request.state.context
    app_state = request.state.app_state
    try:
        user_orm = UserORM(id=user_res.user.id)
        action_doc = await UserActions(user_orm, db).create_action(
            ctx, app_state, ActionCreate(**action_create.model_dump())
        )
        return action_doc
    except Exception as e:
        logger.error(str(e))
        raise HTTPException(500)


class ActionCreateText2TextSearchWeb(ActionCreate):
    type: ActionType = ActionType.text2text_search_web
    config: SearchWebConfig


@router.post("/text2text/search_web")
async def create_action_text2text_search_web(
    request: Request,
    action_create: ActionCreateText2TextSearchWeb,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> ActionDoc:
    ctx = request.state.context
    app_state = request.state.app_state
    try:
        user_orm = UserORM(id=user_res.user.id)
        action_doc = await UserActions(user_orm, db).create_action(
            ctx, app_state, ActionCreate(**action_create.model_dump(exclude_none=True))
        )
        return action_doc
    except HTTPException:
        raise
    except Exception as e:
        logger.error(str(e))
        raise HTTPException(500)


# class ActionCreateText2TextSearchMaps(ActionCreate):
#     type: ActionType = ActionType.text2text_search_maps
#     config: SearchMapsConfig


# @router.post("/text2text/search_maps")
# async def create_action_text2text_search_maps(
#     request: Request,
#     action_create: ActionCreateText2TextSearchMaps,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> ActionDoc:
#     ctx = request.state.context
#     try:
#         user_orm = UserORM(id=user_res.user.id)
#         action_doc = await UserActions(user_orm, db).create_action(
#             ctx, ActionCreate(**action_create.model_dump(exclude_none=True))
#         )
#         return action_doc
#     except HTTPException:
#         raise
#     except Exception as e:
#         logger.error(str(e))
#         raise HTTPException(500)


class ActionCreateText2TextUserInput(ActionCreate):
    type: ActionType = ActionType.text2text_user_input
    config: DataBlock


@router.post("/text2text/user_input")
async def create_action_text2text_user_input(
    request: Request,
    action_create: ActionCreateText2TextUserInput,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> ActionDoc:
    ctx = request.state.context
    app_state: AppState = request.state.app_state
    try:
        user_orm = UserORM(id=user_res.user.id)
        action_doc = await UserActions(user_orm, db).create_action(
            ctx, app_state, ActionCreate(**action_create.model_dump(exclude_none=True))
        )
        return action_doc
    except HTTPException:
        raise
    except Exception as e:
        logger.error(str(e))
        raise HTTPException(500)
