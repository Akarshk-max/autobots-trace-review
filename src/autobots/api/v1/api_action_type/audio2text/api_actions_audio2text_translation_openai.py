from typing import Optional

import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from starlette.requests import Request

from src.autobots.action.action.action_doc_model import ActionCreate, ActionDoc
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.conn.openai.openai_audio.translation_model import TranslationReq
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

router = APIRouter()


class ActionCreateAudio2TextTranslationOpenai(ActionCreate):
    type: ActionType = ActionType.audio2text_translation_openai
    config: TranslationReq
    input: Optional[DataBlock] = None
    output: Optional[DataBlocks] = None


@router.post("/audio2text/translation_openai")
async def create_action_audio2text_translation_openai(
    request: Request,
    action_create: ActionCreateAudio2TextTranslationOpenai,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> ActionDoc:
    ctx = request.state.context
    app_state: AppState = request.state.app_state
    try:
        user_orm = UserORM(id=user_res.user.id)
        action_doc = await UserActions(user_orm, db).create_action(
            ctx=ctx, app_state=app_state, action_create=ActionCreate(**action_create.model_dump())
        )
        return action_doc
    except Exception as e:
        logger.bind(ctx=ctx).exception(str(e))
        raise HTTPException(500)
