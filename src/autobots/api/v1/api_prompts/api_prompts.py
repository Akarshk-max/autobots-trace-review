# import supabase_auth
# from fastapi import APIRouter, Depends, HTTPException, Request
# from motor.motor_asyncio import AsyncIOMotorDatabase

# from src.autobots import SettingsProvider
# from src.autobots.auth.security import get_online_user_from_access_token
# from src.autobots.core.database.mongo_base import get_mongo_db
# from src.autobots.prompt.prompt_doc_model import PromptCreate, PromptDoc, PromptLiteDocPage, PromptRead, PromptUpdate
# from src.autobots.prompt.user_prompts import UserPrompt
# from src.autobots.user.user_orm_model import UserORM

# router = APIRouter(
#     prefix=SettingsProvider.sget().API_PROMPTS,
#     tags=[SettingsProvider.sget().API_PROMPTS],
# )


# @router.post("/")
# async def create_prompt(request: Request, prompt_create: PromptCreate, user_res=Depends(get_online_user_from_access_token), db: AsyncIOMotorDatabase = Depends(get_mongo_db)) -> PromptDoc:
#     ctx = request.state.context
#     user_orm = UserORM(id=user_res.user.id)
#     if len(prompt_create.blocks.blocks) > 50:
#         raise HTTPException(413, "Too many blocks") # i think 50 is enough
#     return await UserPrompt.create(ctx=ctx, db=db, user=user_orm, prompt_create=prompt_create)


# @router.get("/")
# async def list_prompts(request: Request, id: str = None, name: str = None, limit: int = 100, offset: int = 0, user_res=Depends(get_online_user_from_access_token), db: AsyncIOMotorDatabase = Depends(get_mongo_db)) -> PromptLiteDocPage:
#     ctx = request.state.context
#     user_orm = UserORM(id=user_res.user.id)
#     prompt_read = PromptRead(id=id, name=name)
#     return await UserPrompt.list(ctx=ctx, db=db, user=user_orm, prompt_read=prompt_read, limit=limit, offset=offset)


# @router.get("/{id}")
# async def get_prompt(request: Request, id: str, user_res=Depends(get_online_user_from_access_token), db: AsyncIOMotorDatabase = Depends(get_mongo_db)) -> PromptDoc:
#     ctx = request.state.context
#     user_orm = UserORM(id=user_res.user.id)
#     prompt = await UserPrompt.get(ctx=ctx, db=db, user=user_orm, prompt_id=id)
#     if not prompt:
#         raise HTTPException(404, "Prompt not found")
#     return prompt


# @router.put("/{id}")
# async def update_prompt(request: Request, id: str, prompt_update: PromptUpdate, user_res=Depends(get_online_user_from_access_token), db: AsyncIOMotorDatabase = Depends(get_mongo_db)) -> PromptDoc:
#     ctx = request.state.context
#     user_orm = UserORM(id=user_res.user.id)
#     return await UserPrompt.update(ctx=ctx, db=db, user=user_orm, id=id, prompt_update=prompt_update)


# @router.delete("/{id}")
# async def delete_prompt(request: Request, id: str, user_res=Depends(get_online_user_from_access_token), db: AsyncIOMotorDatabase = Depends(get_mongo_db)) -> PromptDoc:
#     ctx = request.state.context
#     user_orm = UserORM(id=user_res.user.id)

#     prompt = await UserPrompt.get(ctx=ctx, db=db, user=user_orm, prompt_id=id)
#     if not prompt:
#         raise HTTPException(400, "Prompt not found")

#     deleted = await UserPrompt.delete(ctx=ctx, db=db, user=user_orm, prompt_delete=PromptRead(id=id))
#     if deleted != 1:
#         raise HTTPException(500, "Error deleting prompt")

#     return prompt