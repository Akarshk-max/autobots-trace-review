from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from src.autobots.action.action.common_action_models import DataBlocks
from src.autobots.data_model.context import Context
from src.autobots.skills.skill_doc_model import (
    SkillCreate,
    SkillDoc,
    SkillLiteDocPage,
    SkillRead,
    SkillUpdate,
)
from src.autobots.skills.user_skills import UserSkills
from src.autobots.user.user_orm_model import UserORM

MAX_GET_SKILLS_BATCH_SIZE = 5


def _dedupe_preserve_first(items: list[str]) -> list[str]:
    """De-dupe while preserving first occurrence order."""

    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


class GetSkillConfig(BaseModel):
    """Get owned skills by ids (preferred) or names.

    Breaking change: replaces the prior {id,name} config shape.

    Rules:
    - Provide either `ids` or `names`.
    - If both are provided and `ids` is non-empty, `ids` wins.
    - Max batch size: 5 (after de-duping).
    """

    ids: list[str] | None = None
    names: list[str] | None = None


class GetSkillResult(BaseModel):
    """Bulk get result for owned skills."""

    docs: list[SkillDoc] = []
    missing_ids: list[str] = []
    missing_names: list[str] = []


class DeleteSkillConfig(BaseModel):
    """Delete a skill by id."""

    id: str


class UpdateSkillConfig(BaseModel):
    """Update a skill by id."""

    id: str
    name: str | None = None
    blocks: DataBlocks | None = None


class ListSkillsConfig(BaseModel):
    """List skills with optional filters."""

    id: str | None = None
    name: str | None = None
    limit: int = 100
    offset: int = 0


async def create_skill(
    config: SkillCreate, ctx: Context, user: UserORM, db: AsyncIOMotorDatabase
) -> SkillDoc:
    """
    Create a new skill.

    Input:
    - name: string
    - blocks: DataBlocks

    Output:
    - SkillDoc
    """
    return await UserSkills.create(ctx=ctx, db=db, user=user, skill_create=config)


async def get_skill(
    config: GetSkillConfig, ctx: Context, user: UserORM, db: AsyncIOMotorDatabase
) -> GetSkillResult:
    """Get owned skills by ids (preferred) or names.

    Output is always a list via GetSkillResult.
    Missing items are returned in `missing_ids` / `missing_names` (partial success).
    """

    if config.ids and len(config.ids) > 0:
        ids = _dedupe_preserve_first(config.ids)
        if len(ids) > MAX_GET_SKILLS_BATCH_SIZE:
            raise ValueError(f"Max batch size is {MAX_GET_SKILLS_BATCH_SIZE}")

        docs: list[SkillDoc] = []
        missing_ids: list[str] = []
        for skill_id in ids:
            doc = await UserSkills.get(ctx=ctx, db=db, user=user, skill_id=skill_id)
            if doc:
                docs.append(doc)
            else:
                missing_ids.append(skill_id)

        return GetSkillResult(docs=docs, missing_ids=missing_ids, missing_names=[])

    if config.names and len(config.names) > 0:
        names = _dedupe_preserve_first(config.names)
        if len(names) > MAX_GET_SKILLS_BATCH_SIZE:
            raise ValueError(f"Max batch size is {MAX_GET_SKILLS_BATCH_SIZE}")

        docs: list[SkillDoc] = []
        missing_names: list[str] = []
        for name in names:
            doc = await UserSkills.get_by_name(ctx=ctx, db=db, user=user, name=name)
            if doc:
                docs.append(doc)
            else:
                missing_names.append(name)

        return GetSkillResult(docs=docs, missing_ids=[], missing_names=missing_names)

    raise ValueError("Must provide ids or names")


async def update_skill(
    config: UpdateSkillConfig, ctx: Context, user: UserORM, db: AsyncIOMotorDatabase
) -> SkillDoc:
    """
    Update a skill by id.

    Input:
    - id: string
    - name: optional string
    - blocks: optional DataBlocks
    """
    update = SkillUpdate(name=config.name, blocks=config.blocks)
    doc = await UserSkills.update(ctx=ctx, db=db, user=user, id=config.id, skill_update=update)
    if not doc:
        raise ValueError("Skill not found")
    return doc


async def delete_skill(
    config: DeleteSkillConfig, ctx: Context, user: UserORM, db: AsyncIOMotorDatabase
) -> SkillDoc:
    """
    Delete a skill by id. Returns the deleted skill.
    """
    doc = await UserSkills.get(ctx=ctx, db=db, user=user, skill_id=config.id)
    if not doc:
        raise ValueError("Skill not found")

    deleted = await UserSkills.delete(
        ctx=ctx, db=db, user=user, skill_delete=SkillRead(id=config.id)
    )
    if deleted != 1:
        raise RuntimeError("Error deleting skill")

    return doc


async def list_skills(
    config: ListSkillsConfig, ctx: Context, user: UserORM, db: AsyncIOMotorDatabase
) -> SkillLiteDocPage:
    """
    List skills.

    Input:
    - id: optional string
    - name: optional string
    - limit: int (default 100)
    - offset: int (default 0)

    Output:
    - SkillLiteDocPage
    """
    skill_read = SkillRead(id=config.id, name=config.name)
    return await UserSkills.list(
        ctx=ctx,
        db=db,
        user=user,
        skill_read=skill_read,
        limit=config.limit,
        offset=config.offset,
    )


class ListSystemSkillsConfig(BaseModel):
    """List system (published) skills with optional filters."""

    id: str | None = None
    name: str | None = None
    limit: int = 100
    offset: int = 0


class GetSystemSkillConfig(BaseModel):
    """Get system skills (published, owned by SYSTEM_USER) by ids or names.

    Breaking change: replaces prior single `id`.

    Rules:
    - Provide `ids` or provide `names`.
    - Max batch size: 5 (after de-duping).
    """

    ids: list[str] | None = None
    names : list[str] | None = None 


class GetSystemSkillResult(BaseModel):
    docs: list[SkillDoc] = []
    missing_ids: list[str] = []
    missing_names: list[str] = []


async def list_system_skills(
    config: ListSystemSkillsConfig, ctx: Context, user: UserORM, db: AsyncIOMotorDatabase
) -> SkillLiteDocPage:
    """
    List system skills (published by the system user).

    Input:
    - id: optional string
    - name: optional string
    - limit: int (default 100)
    - offset: int (default 0)

    Output:
    - SkillLiteDocPage
    """
    skill_read = SkillRead(id=config.id, name=config.name)
    return await UserSkills.list_system_skills(
        ctx=ctx,
        db=db,
        skill_read=skill_read,
        limit=config.limit,
        offset=config.offset,
    )


async def get_system_skill(
    config: GetSystemSkillConfig, ctx: Context, user: UserORM, db: AsyncIOMotorDatabase
) -> GetSystemSkillResult:
    """Get system skills by ids.

    Output is always a list via GetSystemSkillResult.
    Missing items are returned in `missing_ids` (partial success).
    """
    no_ids_passed = not config.ids or len(config.ids) == 0
    no_names_passed = not config.names or len(config.names) == 0
    if (no_names_passed and no_ids_passed):
        raise ValueError("Provide one of `names` or `ids`, both are empty.")
    if (not no_ids_passed and not no_names_passed):
        raise ValueError("Provide either of `names` or `ids`, not both in a single request.")
    if not no_ids_passed:
        ids = _dedupe_preserve_first(config.ids)
        if len(ids) > MAX_GET_SKILLS_BATCH_SIZE:
            raise ValueError(f"Max batch size is {MAX_GET_SKILLS_BATCH_SIZE}")

        docs: list[SkillDoc] = []
        missing_ids: list[str] = []
        for skill_id in ids:
            doc = await UserSkills.get_system_skill(ctx=ctx, db=db, skill_id=skill_id)
            if doc:
                docs.append(doc)
            else:
                missing_ids.append(skill_id)

        return GetSystemSkillResult(docs=docs, missing_ids=missing_ids)
    else:
        names = _dedupe_preserve_first(config.names)
        if len(names) > MAX_GET_SKILLS_BATCH_SIZE:
            raise ValueError(f"Max batch size is {MAX_GET_SKILLS_BATCH_SIZE}")
        docs: list[SkillDoc] = []
        missing_names: list[str] = []
        for skill_name in names:
            ret_docs = await UserSkills.get_system_skills_by_name(
                ctx=ctx,
                db=db,
                skill_name=skill_name,
            )
            from loguru import logger
            logger.bind(ctx=ctx).info(f"RET DOCS : {ret_docs}")
            if len(ret_docs) == 0:
                missing_names.append(skill_name)
            else:
                docs += ret_docs
        
        return GetSystemSkillResult(docs=docs, missing_names=missing_names)
        