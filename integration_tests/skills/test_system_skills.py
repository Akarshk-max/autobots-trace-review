import pytest

from src.autobots import SettingsProvider
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.data_model.context import Context
from src.autobots.skills.skill_crud import SkillCRUD
from src.autobots.skills.skill_doc_model import SkillCreate, SkillDocRead, SkillRead, SkillUpdate
from src.autobots.skills.user_skills import UserSkills
from src.autobots.user.user_orm_model import UserORM

SKILL_NAME = "test_system_skill_published"


@pytest.mark.asyncio
async def test_create_publish_then_list_and_get_system_skill(db):
    """
    1. Create a skill as the SYSTEM_USER
    2. Update it to is_published=True
    3. List system skills — should find it
    4. Get system skill by id — should return it
    5. Cleanup
    """
    system_user_id = SettingsProvider.sget().SYSTEM_USER
    system_user = UserORM(id=system_user_id)
    ctx = Context(user_id=system_user_id)
    crud = SkillCRUD(db=db)

    # --- cleanup any leftover from previous runs ---
    leftover = SkillDocRead(user_id=system_user_id, name=SKILL_NAME)
    await crud.delete_many(doc_find=leftover)

    try:
        # 1. Create skill as system user (not published yet)
        skill_create = SkillCreate(
            name=SKILL_NAME,
            blocks=DataBlocks(blocks=[DataBlock(text="system skill content")]),
            is_published=False,
        )
        created = await UserSkills.create(ctx=ctx, db=db, user=system_user, skill_create=skill_create)
        assert created is not None
        assert created.name == SKILL_NAME
        assert created.is_published is False
        skill_id = str(created.id)

        # 2. Update to is_published=True
        skill_update = SkillUpdate(is_published=True)
        updated = await UserSkills.update(
            ctx=ctx, db=db, user=system_user, id=skill_id, skill_update=skill_update,
        )
        assert updated is not None
        assert updated.is_published is True

        # 3. List system skills — should include our skill
        skill_read = SkillRead()
        page = await UserSkills.list_system_skills(
            ctx=ctx, db=db, skill_read=skill_read, limit=100, offset=0,
        )
        assert page.total_count >= 1
        found_ids = [str(doc.id) for doc in page.docs]
        assert skill_id in found_ids

        # 4. List system skills filtered by name
        page_by_name = await UserSkills.list_system_skills(
            ctx=ctx, db=db, skill_read=SkillRead(name=SKILL_NAME), limit=100, offset=0,
        )
        assert page_by_name.total_count == 1
        assert str(page_by_name.docs[0].id) == skill_id

        # 5. Get system skill by id
        fetched = await UserSkills.get_system_skill(ctx=ctx, db=db, skill_id=skill_id)
        assert fetched is not None
        assert str(fetched.id) == skill_id
        assert fetched.name == SKILL_NAME
        assert fetched.is_published is True
        assert fetched.blocks.blocks[0].text == "system skill content"

        # 6. A different user should NOT see this via regular get, but SHOULD via system get
        other_user = UserORM(id="other_test_user", email="other@test.com")
        other_ctx = Context(user_id="other_test_user")

        owned = await UserSkills.get(ctx=other_ctx, db=db, user=other_user, skill_id=skill_id)
        assert owned is None  # not owned by other user

        via_system = await UserSkills.get_system_skill(ctx=other_ctx, db=db, skill_id=skill_id)
        assert via_system is not None
        assert str(via_system.id) == skill_id

        # 7. get_owned_or_shared_or_system_skill should fall through to system
        cascaded = await UserSkills.get_owned_or_shared_or_system_skill(
            ctx=other_ctx, db=db, user=other_user, skill_id=skill_id,
        )
        assert cascaded is not None
        assert str(cascaded.id) == skill_id

    finally:
        # Cleanup
        cleanup_find = SkillDocRead(user_id=system_user_id, name=SKILL_NAME)
        await crud.delete_many(doc_find=cleanup_find)
