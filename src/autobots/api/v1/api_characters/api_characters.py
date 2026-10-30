import supabase_auth
from fastapi import APIRouter, Depends, HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots import SettingsProvider
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.characters.character_doc_model import (
    CharacterCreate,
    CharacterDoc,
    CharacterLiteDocPage,
    CharacterRead,
    CharacterUpdate,
)
from src.autobots.characters.user_characters import UserCharacter
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=SettingsProvider.sget().API_CHARACTERS, tags=[SettingsProvider.sget().API_CHARACTERS]
)


@router.post("/")
async def create_character(
    request: Request,
    character_create: CharacterCreate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> CharacterDoc:
    ctx = request.state.context
    user_orm = UserORM(id=user_res.user.id, space_member=space_member)
    character_doc = await UserCharacter.create(
        ctx=ctx,
        user=user_orm,
        db=db,
        character_create=character_create,
    )
    return character_doc


@router.get("/")
async def list_characters(
    request: Request,
    id: str = None,
    name: str = None,
    limit: int = 100,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> CharacterLiteDocPage:
    ctx = request.state.context
    user_orm = UserORM(id=user_res.user.id, space_member=space_member)
    character_read = CharacterRead(id=id, name=name)
    character_docs = await UserCharacter.list(
        ctx=ctx,
        db=db,
        user=user_orm,
        character_read=character_read,
        limit=limit,
        offset=offset,
    )
    return character_docs


@router.get("/{id}")
async def get_character(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> CharacterDoc:
    ctx = request.state.context
    user_orm = UserORM(id=user_res.user.id, space_member=space_member)
    character_doc = await UserCharacter.get(ctx=ctx, db=db, user=user_orm, character_id=id)
    if not character_doc:
        raise HTTPException(status_code=404, detail="Character not found")
    return character_doc


@router.put("/{id}")
async def update_character(
    request: Request,
    id: str,
    character_update: CharacterUpdate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> CharacterDoc:
    ctx = request.state.context
    user_orm = UserORM(id=user_res.user.id, space_member=space_member)
    character_doc = await UserCharacter.update(
        ctx=ctx,
        user=user_orm,
        db=db,
        id=id,
        character_update=character_update,
    )
    return character_doc


@router.delete("/{id}")
async def delete_character(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> CharacterDoc:
    ctx = request.state.context
    user_orm = UserORM(id=user_res.user.id, space_member=space_member)
    character_doc = await UserCharacter.get(ctx=ctx, db=db, user=user_orm, character_id=id)
    if character_doc is None:
        raise HTTPException(400, "Character not found")
    character_delete = CharacterRead(id=id)
    deleted_count = await UserCharacter.delete(
        ctx=ctx,
        db=db,
        user=user_orm,
        character_delete=character_delete,
    )
    if deleted_count != 1:
        raise HTTPException(500, "Error in deleting character")
    return character_doc
