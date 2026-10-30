# from typing import Any, List, Dict
# from uuid import UUID
#
# import supabase_auth
# from fastapi import APIRouter, Depends, HTTPException
# from motor.motor_asyncio import AsyncIOMotorDatabase
# from starlette import status
# from starlette.requests import Request
#
# from src.autobots import SettingsProvider
# from src.autobots.auth.security import get_online_user_from_access_token
# from src.autobots.core.database.mongo_base import get_mongo_db
# from src.autobots.core.database.mongo_base_crud import DocFindPage
# from src.autobots.secret.app_creds.app_creds_model import AppCredsCreate, AppCredsDoc, AppCredsRead, AppCredsUpdate
# from src.autobots.secret.app_creds.user_app_creds import UserAppCreds
# from src.autobots.secret.app_type_to_app_auth_class import AppTypeToIntegrationClass
# from src.autobots.secret.app_types import AppTypes
# from src.autobots.user.user_orm_model import UserORM
#
# router = APIRouter(prefix=SettingsProvider.sget().API_SECRETS, tags=[SettingsProvider.sget().API_SECRETS])
#
#
# @router.get("/apps")  # TODO: list of obj with name and icon url
# async def list_apps(
#         user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
# ) -> List[str]:
#     apps = AppTypes.list()
#     return apps
#
#
# @router.get("/apps/{app}/creds_type")
# async def get_app_data_types(
#         request: Request,
#         app_name: AppTypes,
#         user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
# ) -> Dict[str, Any]:
#     # ctx = request.state.context
#     app = AppTypeToIntegrationClass.get(app_name)
#     cred_type = app.get_creds_model_type()
#     return cred_type.model_json_schema()
#
#
# @router.post("/")
# async def create_app_creds(
#         request: Request,
#         app_creds_create: AppCredsCreate,
#         user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#         db: AsyncIOMotorDatabase = Depends(get_mongo_db)
# ) -> AppCredsDoc:
#     ctx = request.state.context
#     user_orm = UserORM(id=UUID(user_res.user.id))
#     doc = await UserAppCreds.create_app_creds(ctx=ctx, db=db, user=user_orm, app_creds_create=app_creds_create)
#     match doc:
#         case AppCredsDoc():
#             return doc
#         case _:
#             raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="App Creds not Created")
#
#
# @router.get("/")
# async def list_app_creds(
#         request: Request,
#         id: str | None = None, app_name: AppTypes | None = None,
#         limit: int = 100, offset: int = 0,
#         user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#         db: AsyncIOMotorDatabase = Depends(get_mongo_db)
# ) -> DocFindPage:
#     ctx = request.state.context
#     user_orm = UserORM(id=UUID(user_res.user.id))
#     app_creds_list = AppCredsRead(id=id, app_name=app_name)
#     doc_page = await UserAppCreds.list_app_creds(
#         ctx=ctx, db=db, user=user_orm, app_creds_list=app_creds_list, limit=limit, offset=offset
#     )
#     return doc_page
#
#
# @router.get("/{id}")
# async def get_app_creds(
#         request: Request,
#         id: str,
#         user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#         db: AsyncIOMotorDatabase = Depends(get_mongo_db)
# ) -> AppCredsDoc:
#     ctx = request.state.context
#     user_orm = UserORM(id=UUID(user_res.user.id))
#     doc = await UserAppCreds.get_app_creds(
#         ctx=ctx,
#         db=db,
#         user=user_orm,
#         app_creds_id=id,
#     )
#     match doc:
#         case AppCredsDoc():
#             return doc
#         case _:
#             raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App Creds Not Found")
#
#
# @router.put("/{id}")
# async def update_app_creds(
#         request: Request,
#         id: str,
#         app_creds_update: AppCredsUpdate,
#         user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#         db: AsyncIOMotorDatabase = Depends(get_mongo_db)
# ) -> AppCredsDoc:
#     ctx = request.state.context
#     user_orm = UserORM(id=UUID(user_res.user.id))
#     doc = await UserAppCreds.update_app_creds(
#         ctx=ctx,
#         db=db,
#         user=user_orm,
#         id=id,
#         app_creds_update=app_creds_update,
#     )
#     match doc:
#         case AppCredsDoc():
#             return doc
#         case None:
#             raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App creds Not Updated")
#
#
# @router.delete("/{id}")
# async def delete_app_creds(
#         request: Request,
#         id: str | None = None, app_name: AppTypes | None = None,
#         user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#         db: AsyncIOMotorDatabase = Depends(get_mongo_db)
# ) -> int:
#     ctx = request.state.context
#     user_orm = UserORM(id=UUID(user_res.user.id))
#     app_creds_delete = AppCredsRead(id=id, app_name=app_name)
#     deleted_count = await UserAppCreds.delete_app_creds(ctx=ctx, db=db, user=user_orm, app_creds_delete=app_creds_delete)
#     return deleted_count
