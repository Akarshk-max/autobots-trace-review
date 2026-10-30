import json
from datetime import datetime, timezone

import pytest

from src.autobots.action.action_type.action_types import ActionType
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_crud import EventResultCRUD
from src.autobots.event_result.event_result_model import (
    EventResultDocCreate,
    EventResultStatus,
    EventType,
)
from src.autobots.llm.tools.run_config_tool import RunConfigToolConfig, run_config_tool
from src.autobots.user.user_orm_model import UserORM


@pytest.mark.asyncio
async def test_run_config_tool_internal_user(set_test_settings, ctx, user, db):
    """Internal (@runkai.ai) user fetches run config via AdminEventResult (MongoDB path)."""
    fake_config = {"action": "pdf_highlight", "model": "gpt-4o"}

    inserted = await EventResultCRUD(db).insert_one(
        EventResultDocCreate(
            user_id=str(user.id),
            name="test run",
            type=EventType.action_run,
            status=EventResultStatus.success,
            result={"config": fake_config, "internal_field": "should_not_be_returned"},
        )
    )
    assert not isinstance(inserted, Exception)

    out = await run_config_tool(
        config=RunConfigToolConfig(run_id=inserted.id),
        ctx=ctx,
        app_state=AppState(),
        user=user,
        db=db,
    )

    assert not isinstance(out, Exception)
    assert isinstance(out, str)

    returned = json.loads(out)
    assert returned["action"] == "pdf_highlight"
    assert returned["model"] == "gpt-4o"
    assert "internal_field" not in returned


@pytest.mark.asyncio
async def test_run_config_tool_external_user(set_test_settings, db):
    """External (non-runkai.ai) user fetches their own run config via UserActionResult."""
    external_user = UserORM(id="ext_test_user_id", email="external@example.com")
    ext_ctx = Context(user_id="ext_test_user_id")
    fake_config = {"model": "gpt-4o", "temperature": 0.7}

    # Result must be ActionDoc-compatible so UserActionResult.get_action_result() can validate it
    action_doc_result = {
        "_id": "507f1f77bcf86cd799439011",
        "name": "Test Action",
        "type": ActionType.text2text_llm_chat_openai.value,
        "user_id": "ext_test_user_id",
        "config": fake_config,
        "version": 0,
        "description": "",
        "user_manual": "",
        "is_saved": False,
        "is_published": False,
        "shared_with": [],
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }

    inserted = await EventResultCRUD(db).insert_one(
        EventResultDocCreate(
            user_id="ext_test_user_id",
            name="test run",
            type=EventType.action_run,
            status=EventResultStatus.success,
            result=action_doc_result,
        )
    )
    assert not isinstance(inserted, Exception)

    out = await run_config_tool(
        config=RunConfigToolConfig(run_id=inserted.id),
        ctx=ext_ctx,
        app_state=AppState(),
        user=external_user,
        db=db,
    )

    assert not isinstance(out, Exception)
    assert isinstance(out, str)

    returned = json.loads(out)
    assert returned["model"] == "gpt-4o"
    assert returned["temperature"] == 0.7


@pytest.mark.asyncio
async def test_run_config_tool_blocks_shared_action_config(set_test_settings, db):
    """External user cannot extract config of a run whose action was owned by someone else."""
    external_user = UserORM(id="ext_test_user_id2", email="external2@example.com")
    ext_ctx = Context(user_id="ext_test_user_id2")

    # Action is owned by a DIFFERENT user — simulates a shared/published action run
    action_doc_result = {
        "_id": "507f1f77bcf86cd799439012",
        "name": "Shared Action",
        "type": ActionType.text2text_llm_chat_openai.value,
        "user_id": "other_owner_id",  # not ext_test_user_id2
        "config": {"proprietary_prompt": "secret"},
        "version": 0,
        "description": "",
        "user_manual": "",
        "is_saved": False,
        "is_published": True,
        "shared_with": ["ext_test_user_id2"],
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }

    inserted = await EventResultCRUD(db).insert_one(
        EventResultDocCreate(
            user_id="ext_test_user_id2",  # run belongs to this user
            name="shared action run",
            type=EventType.action_run,
            status=EventResultStatus.success,
            result=action_doc_result,
        )
    )
    assert not isinstance(inserted, Exception)

    out = await run_config_tool(
        config=RunConfigToolConfig(run_id=inserted.id),
        ctx=ext_ctx,
        app_state=AppState(),
        user=external_user,
        db=db,
    )

    # Must be blocked — config belongs to the action owner, not the runner
    assert isinstance(out, Exception)
    assert "proprietary" in str(out).lower()
