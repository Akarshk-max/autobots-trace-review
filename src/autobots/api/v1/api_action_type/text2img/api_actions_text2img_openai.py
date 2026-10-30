import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from starlette.requests import Request

from src.autobots.action.action.action_doc_model import ActionCreate, ActionDoc
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action.action_type.action_text2img.action_text2img_dalle_openai_v2 import (
    ActionGenImageDalleOpenAiV2,
    DalleOpenaiConfig,
)
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

router = APIRouter()


@router.post("/text2img/dalle_openai")
async def create_text2img_dalle_openai(
    request: Request,
    action_create: DalleOpenaiConfig,
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


@router.post("/text2img/dalle_openai/{action_id}/run")
async def run_text2img_dalle_openai(
    request: Request,
    action_id: str,
    action_input: DataBlock,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> DataBlocks:
    ctx = request.state.context
    app_state: AppState = request.state.app_state
    try:
        user_orm = UserORM(**user_res.user.model_dump())
        action = await UserActions(user=user_orm, db=db).get_action(action_id)
        text2img = ActionGenImageDalleOpenAiV2(
            DalleOpenaiConfig.model_validate(action.config),
            db=db,
            user=UserORM(**user_res.model_dump()),
        )
        resp = await text2img.run_action(ctx, app_state, action_input)
        if isinstance(resp, Exception):
            raise resp
        return resp
    except Exception as e:
        logger.error(str(e))
        raise HTTPException(500)
