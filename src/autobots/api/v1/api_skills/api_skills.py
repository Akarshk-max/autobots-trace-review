from fastapi import APIRouter, Depends, HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.auth.security import (
    get_offline_user_from_access_token,
    get_online_admin_user_from_access_token,
)
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.skills.skill_doc_model import (
    SkillCreate,
    SkillDoc,
    SkillLiteDocPage,
    SkillRead,
    SkillUpdate,
)
from src.autobots.skills.user_skills import UserSkills
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(prefix="/skills", tags=["skills"])


@router.post("/")
async def create_skill(
    request: Request,
    skill_create: SkillCreate,
    user_res=Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> SkillDoc:
    ctx = request.state.context
    user_orm = UserORM(id=user_res.user.id, space_member=space_member)

    if len(skill_create.blocks.blocks) > 50:
        raise HTTPException(413, "Too many blocks")  # 50 is enough

    return await UserSkills.create(ctx=ctx, db=db, user=user_orm, skill_create=skill_create)


@router.get("/")
async def list_skills(
    request: Request,
    id: str | None = None,
    name: str | None = None,
    is_published: bool | None = None,
    is_shared: bool | None = None,
    limit: int = 100,
    offset: int = 0,
    user_res=Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> SkillLiteDocPage:
    ctx = request.state.context
    user_orm = UserORM(id=user_res.user.id, space_member=space_member)
    skill_read = SkillRead(id=id, name=name, is_published=is_published)
    if is_shared:
        skill_read.shared_with = user_orm.email
    skills = await UserSkills.list(
        ctx=ctx,
        db=db,
        user=user_orm,
        skill_read=skill_read,
        limit=limit,
        offset=offset,
    )
    return skills


@router.get("/system_skills/")
async def list_system_skills(
    request: Request,
    id: str | None = None,
    name: str | None = None,
    limit: int = 100,
    offset: int = 0,
    user_res=Depends(get_online_admin_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> SkillLiteDocPage:
    ctx = request.state.context
    skill_read = SkillRead(id=id, name=name)
    system_skills = await UserSkills.list_system_skills(
        ctx=ctx,
        db=db,
        skill_read=skill_read,
        limit=limit,
        offset=offset,
    )
    return system_skills


@router.get("/system_skills/by_name/")
async def get_system_skills_by_name(
    request: Request,
    name: str,
    limit: int = 100,
    user_res=Depends(get_online_admin_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> list[SkillDoc]:
    ctx = request.state.context
    return await UserSkills.get_system_skills_by_name(
        ctx=ctx, db=db, skill_name=name, limit=limit
    )


@router.get("/system_skills/{id}")
async def get_system_skill(
    request: Request,
    id: str,
    user_res=Depends(get_online_admin_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> SkillDoc:
    ctx = request.state.context
    skill = await UserSkills.get_system_skill(ctx=ctx, db=db, skill_id=id)
    if not skill:
        raise HTTPException(404, "System skill not found")
    return skill


@router.get("/{id}")
async def get_skill(
    request: Request,
    id: str,
    user_res=Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> SkillDoc:
    ctx = request.state.context
    user_orm = UserORM(id=user_res.user.id, space_member=space_member)

    skill = await UserSkills.get(ctx=ctx, db=db, user=user_orm, skill_id=id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    return skill


@router.put("/{id}")
async def update_skill(
    request: Request,
    id: str,
    skill_update: SkillUpdate,
    user_res=Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> SkillDoc:
    ctx = request.state.context
    user_orm = UserORM(id=user_res.user.id, space_member=space_member)

    skill = await UserSkills.update(ctx=ctx, db=db, user=user_orm, id=id, skill_update=skill_update)
    if not skill:
        raise HTTPException(404, "Skill not found")
    return skill


@router.delete("/{id}")
async def delete_skill(
    request: Request,
    id: str,
    user_res=Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> SkillDoc:
    ctx = request.state.context
    user_orm = UserORM(id=user_res.user.id, space_member=space_member)

    skill = await UserSkills.get(ctx=ctx, db=db, user=user_orm, skill_id=id)
    if not skill:
        raise HTTPException(400, "Skill not found")

    deleted = await UserSkills.delete(ctx=ctx, db=db, user=user_orm, skill_delete=SkillRead(id=id))
    if deleted != 1:
        raise HTTPException(500, "Error deleting skill")

    return skill
