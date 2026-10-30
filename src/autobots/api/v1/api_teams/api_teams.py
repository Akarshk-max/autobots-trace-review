# import supabase_auth
# from fastapi import APIRouter, Depends, HTTPException, Request
# from motor.motor_asyncio import AsyncIOMotorDatabase

# from src.autobots import SettingsProvider
# from src.autobots.auth.security import get_online_user_from_access_token
# from src.autobots.core.database.mongo_base import get_mongo_db
# from src.autobots.teams.team_doc_model import (
#     TeamCreate,
#     TeamDoc,
#     TeamLiteDocPage,
#     TeamRead,
#     TeamUpdate,
# )
# from src.autobots.teams.user_teams import UserTeams
# from src.autobots.user.user_orm_model import UserORM

# router = APIRouter(
#     prefix=SettingsProvider.sget().API_TEAMS, tags=[SettingsProvider.sget().API_TEAMS]
# )


# @router.post("/")
# async def create_team(
#     request: Request,
#     team_create: TeamCreate,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> TeamDoc:
#     ctx = request.state.context
#     user_orm = UserORM(id=user_res.user.id)
#     team_doc = await UserTeams.create(ctx=ctx, user=user_orm, db=db, team_create=team_create)
#     return team_doc


# @router.get("/")
# async def list_teams(
#     request: Request,
#     id: str = None,
#     name: str = None,
#     limit: int = 100,
#     offset: int = 0,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> TeamLiteDocPage:
#     ctx = request.state.context
#     user_orm = UserORM(id=user_res.user.id)
#     team_read = TeamRead(id=id, name=name)
#     team_docs = await UserTeams.list(
#         ctx=ctx, db=db, user=user_orm, team_read=team_read, limit=limit, offset=offset
#     )
#     return team_docs


# @router.get("/{id}")
# async def get_team(
#     request: Request,
#     id: str,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> TeamDoc:
#     ctx = request.state.context
#     user_orm = UserORM(id=user_res.user.id)
#     team_doc = await UserTeams.get(ctx=ctx, db=db, user=user_orm, team_id=id)
#     return team_doc


# @router.put("/{id}")
# async def update_team(
#     request: Request,
#     id: str,
#     team_update: TeamUpdate,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> TeamDoc:
#     ctx = request.state.context
#     user_orm = UserORM(id=user_res.user.id)
#     team_doc = await UserTeams.update(ctx=ctx, user=user_orm, db=db, id=id, team_update=team_update)
#     return team_doc


# @router.delete("/{id}")
# async def delete_team(
#     request: Request,
#     id: str,
#     user_res: supabase_auth.UserResponse = Depends(get_online_user_from_access_token),
#     db: AsyncIOMotorDatabase = Depends(get_mongo_db),
# ) -> TeamDoc:
#     ctx = request.state.context
#     user_orm = UserORM(id=user_res.user.id)
#     team_doc = await UserTeams.get(ctx=ctx, db=db, user=user_orm, team_id=id)
#     if team_doc is None:
#         raise HTTPException(400, "Team not found")
#     team_delete = TeamRead(id=id)
#     deleted_count = await UserTeams.delete(ctx=ctx, db=db, user=user_orm, team_delete=team_delete)
#     if deleted_count != 1:
#         raise HTTPException(500, "Error in deleting character")
#     return team_doc
