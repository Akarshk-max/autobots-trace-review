from typing import Optional

import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from starlette.requests import Request

from src.autobots.action.action.action_doc_model import ActionCreate, ActionDoc
from src.autobots.action.action.common_action_models import TextObj, TextObjs
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action.action_type.action_multimodal.action_multimodal_assistant_openai.assistant_openai_model import (
    AssistantOpenaiConfigCreate,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

router = APIRouter()


class ActionCreateMultimodalAssistantOpenai(ActionCreate):
    type: ActionType = ActionType.multimodal_assistant_openai
    config: AssistantOpenaiConfigCreate
    input: Optional[TextObj] = None
    output: Optional[TextObjs] = None


@router.post("/multimodal/assistant_openai")
async def create_action_multimodal_assistant_openai(
    request: Request,
    action_create: ActionCreateMultimodalAssistantOpenai,
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
        logger.exception(str(e))
        raise HTTPException(500, detail=str(e))
