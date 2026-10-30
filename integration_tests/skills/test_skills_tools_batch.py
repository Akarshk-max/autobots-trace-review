import uuid

import pytest

from src.autobots import SettingsProvider
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.data_model.context import Context
from src.autobots.skills.skill_crud import SkillCRUD
from src.autobots.skills.skill_doc_model import SkillCreate, SkillDocRead, SkillUpdate
from src.autobots.skills.skills_tools import (
    GetSkillConfig,
    GetSystemSkillConfig,
    get_skill,
    get_system_skill,
)
from src.autobots.skills.user_skills import UserSkills
from src.autobots.user.user_orm_model import UserORM


@pytest.mark.asyncio
async def test_get_skill_batch_ids_names_precedence_dedupe_and_missing(db):
    test_user_id = "test_user_id"
    ctx = Context(user_id=test_user_id)
    user = UserORM(id=test_user_id, email="test_user@meetkiwi.co")
    crud = SkillCRUD(db=db)

    name_a = f"test_get_skill_batch_a_{uuid.uuid4().hex}"
    name_b = f"test_get_skill_batch_b_{uuid.uuid4().hex}"

    # cleanup from any earlier flake
    await crud.delete_many(doc_find=SkillDocRead(user_id=test_user_id, name=name_a))
    await crud.delete_many(doc_find=SkillDocRead(user_id=test_user_id, name=name_b))

    try:
        created_a = await UserSkills.create(
            ctx=ctx,
            db=db,
            user=user,
            skill_create=SkillCreate(
                name=name_a,
                blocks=DataBlocks(blocks=[DataBlock(text="a")]),
                is_published=False,
            ),
        )
        created_b = await UserSkills.create(
            ctx=ctx,
            db=db,
            user=user,
            skill_create=SkillCreate(
                name=name_b,
                blocks=DataBlocks(blocks=[DataBlock(text="b")]),
                is_published=False,
            ),
        )

        id_a = str(created_a.id)
        id_b = str(created_b.id)

        # --- ids win over names ---
        res = await get_skill(
            config=GetSkillConfig(ids=[id_a], names=[name_b]),
            ctx=ctx,
            user=user,
            db=db,
        )
        assert [str(d.id) for d in res.docs] == [id_a]
        assert res.missing_ids == []
        assert res.missing_names == []  # names ignored entirely

        # --- missing is partial, preserves order ---
        missing_id = "000000000000000000000000"
        res2 = await get_skill(
            config=GetSkillConfig(ids=[id_a, missing_id]),
            ctx=ctx,
            user=user,
            db=db,
        )
        assert [str(d.id) for d in res2.docs] == [id_a]
        assert res2.missing_ids == [missing_id]
        assert res2.missing_names == []

        # --- de-dupe ids while preserving first ---
        res3 = await get_skill(
            config=GetSkillConfig(ids=[id_a, id_a, missing_id, missing_id]),
            ctx=ctx,
            user=user,
            db=db,
        )
        assert [str(d.id) for d in res3.docs] == [id_a]
        assert res3.missing_ids == [missing_id]

        # --- names path: order preserved, partial missing ---
        res4 = await get_skill(
            config=GetSkillConfig(names=[name_b, "missing-name", name_a, name_a]),
            ctx=ctx,
            user=user,
            db=db,
        )
        assert [str(d.id) for d in res4.docs] == [id_b, id_a]
        assert res4.missing_names == ["missing-name"]
        assert res4.missing_ids == []

        # --- enforce max batch size (after dedupe) ---
        with pytest.raises(ValueError):
            await get_skill(
                config=GetSkillConfig(ids=["1", "2", "3", "4", "5", "6"]),
                ctx=ctx,
                user=user,
                db=db,
            )

    finally:
        await crud.delete_many(doc_find=SkillDocRead(user_id=test_user_id, name=name_a))
        await crud.delete_many(doc_find=SkillDocRead(user_id=test_user_id, name=name_b))


@pytest.mark.asyncio
async def test_get_system_skill_batch_ids_dedupe_missing_and_limit(db):
    system_user_id = SettingsProvider.sget().SYSTEM_USER
    ctx = Context(user_id=system_user_id)
    system_user = UserORM(id=system_user_id)
    crud = SkillCRUD(db=db)

    name = f"test_get_system_skill_batch_{uuid.uuid4().hex}"

    await crud.delete_many(doc_find=SkillDocRead(user_id=system_user_id, name=name))

    try:
        created = await UserSkills.create(
            ctx=ctx,
            db=db,
            user=system_user,
            skill_create=SkillCreate(
                name=name,
                blocks=DataBlocks(blocks=[DataBlock(text="system")]),
                is_published=False,
            ),
        )
        skill_id = str(created.id)

        # publish it
        updated = await UserSkills.update(
            ctx=ctx,
            db=db,
            user=system_user,
            id=skill_id,
            skill_update=SkillUpdate(is_published=True),
        )
        assert updated is not None
        assert updated.is_published is True

        missing_id = "000000000000000000000000"

        # --- partial + dedupe ---
        res = await get_system_skill(
            config=GetSystemSkillConfig(ids=[skill_id, skill_id, missing_id, missing_id]),
            ctx=ctx,
            user=system_user,
            db=db,
        )
        assert [str(d.id) for d in res.docs] == [skill_id]
        assert res.missing_ids == [missing_id]

        # --- enforce max batch size ---
        with pytest.raises(ValueError):
            await get_system_skill(
                config=GetSystemSkillConfig(ids=["1", "2", "3", "4", "5", "6"]),
                ctx=ctx,
                user=system_user,
                db=db,
            )

    finally:
        await crud.delete_many(doc_find=SkillDocRead(user_id=system_user_id, name=name))
