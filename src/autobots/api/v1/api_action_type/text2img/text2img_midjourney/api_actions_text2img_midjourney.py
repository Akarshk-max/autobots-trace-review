# import supabase_auth
# from fastapi import APIRouter, Depends, HTTPException
# from loguru import logger
# from motor.motor_asyncio import AsyncIOMotorDatabase
# from starlette.requests import Request
#
# from src.autobots.action.action.action_doc_model import ActionCreate, ActionDoc
# from src.autobots.action.action.user_actions import UserActions
# from src.autobots.action.action_type.action_text2img.action_text2img_imagine_midjourney import (
#     ActionCreateText2ImgImagineMidJourney, ActionText2ImgMidjourney,
#     Text2ImgRunModel)
# from src.autobots.action.action_type.action_text2img.action_text2img_midjourney_button import (
#     ActionCreateText2ImgButtonMidJourney, ActionText2ImgMidjourneyButton,
#     Text2ImgRunModelButton)
# from src.autobots.api.v1.api_action_type.text2img.text2img_midjourney.api_actions_text2img_midjourney_model import \
#     ActionCreateAPIModelText2ImgMidjourney
# from src.autobots.auth.security import get_online_user_from_access_token
# from src.autobots.conn.useapi.text2img.text2img_model import (
#     DiscordErrorResponse, DiscordJobsApiResponse, DiscordReqModel)
# from src.autobots.core.database.mongo_base import get_mongo_db
# from src.autobots.user.user_orm_model import UserORM
#
# router = APIRouter()
#
#
# @router.post("/text2img_imagine/mid_journey")
# async def create_action_text2img_imagine_midjourney(
#     request: Request,
#     action: ActionCreateAPIModelText2ImgMidjourney,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> ActionDoc:
#     ctx = request.state.context
#     try:
#         user_orm = UserORM(id=user_res.user.id)
#         action_create = ActionCreateText2ImgImagineMidJourney(**action.model_dump())
#         action_doc = await UserActions(user_orm, db).create_action(
#             ctx, ActionCreate(**action_create.model_dump())
#         )
#         return action_doc
#     except HTTPException as http_exception:
#         logger.error(str(http_exception))
#         raise http_exception
#     except Exception as e:
#         logger.error(str(e))
#         raise HTTPException(status_code=500)
#
#
# @router.post("/text2img_button/mid_journey")
# async def create_action_text2img_button_midjourney(
#     request: Request,
#     action: ActionCreateAPIModelText2ImgMidjourney,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> ActionDoc:
#     ctx = request.state.context
#     try:
#         user_orm = UserORM(id=user_res.user.id)
#         action_create = ActionCreateText2ImgButtonMidJourney(**action.model_dump())
#         action_doc = await UserActions(user_orm, db).create_action(
#             ctx, ActionCreate(**action_create.model_dump())
#         )
#         return action_doc
#     except HTTPException as http_exception:
#         logger.error(str(http_exception))
#         raise http_exception
#     except Exception as e:
#         logger.error(str(e))
#         raise HTTPException(status_code=500)
#
#
# @router.post("/text2img/mid_journey/{action_id}/run")
# async def run_action_text2img_midjourney(
#     request: Request,
#     action_id: str,
#     action_input: Text2ImgRunModel,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> DiscordJobsApiResponse | DiscordErrorResponse:
#     ctx = request.state.context
#     try:
#         user_orm = UserORM(**user_res.model_dump())
#         action = await UserActions(user=user_orm, db=db).get_action(action_id)
#         text2img = ActionText2ImgMidjourney(
#             DiscordReqModel.model_validate(action.config), user=user_orm, db=db
#         )
#         resp = await text2img.run_action(ctx, action_input)
#         return resp
#     except HTTPException as http_exception:
#         logger.error(str(http_exception))
#         raise http_exception
#     except Exception as e:
#         logger.error(str(e))
#         raise HTTPException(status_code=500)
#
#
# @router.post("/text2img/mid_journey/button/{action_id}/run")
# async def run_action_text2img_midjourney_button(
#     request: Request,
#     action_id: str,
#     action_input: Text2ImgRunModelButton,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> DiscordJobsApiResponse | DiscordErrorResponse:
#     ctx = request.state.context
#     try:
#         user_orm = UserORM(**user_res.model_dump())
#         action = await UserActions(user=user_orm, db=db).get_action(action_id)
#         text2img = ActionText2ImgMidjourneyButton(
#             DiscordReqModel.model_validate(action.config), user=user_orm, db=db
#         )
#         resp = await text2img.run_action(ctx, action_input)
#         return resp
#     except HTTPException as http_exception:
#         logger.error(str(http_exception))
#         raise http_exception
#     except Exception as e:
#         logger.error(str(e))
#         raise HTTPException(status_code=500)
