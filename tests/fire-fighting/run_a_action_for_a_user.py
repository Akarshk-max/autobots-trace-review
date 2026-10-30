import pytest
from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action.action_result.action_result_doc_model import ActionResultDoc
from src.autobots.action.action_result.user_action_result import UserActionResult
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState


# @pytest.mark.skip("used only for adhoc work: migrate event results from mongo to s3")
@pytest.mark.asyncio
async def test_action_run_for_a_user(ctx: Context, db: AsyncIOMotorDatabase, set_test_settings):
    user_orm = UserORM(id="3ffcdd47", email="@")
    action_id = "69295914ccbcd104b2e7446f"
    action_result_id = None
    action_input = DataBlock(
        text="Get me list of benefits from ADP.. use api call tool with appname adp"
    )

    user_actions = UserActions(user_orm, db)
    action_doc = await user_actions.get_owned_or_shared_or_system_actions(
        ctx=ctx, action_id=action_id, empty_shared_action_config=False
    )
    user_action_result = UserActionResult(user_orm, db)
    if action_doc is None and action_result_id:
        action_result_doc: ActionResultDoc | None = await user_action_result.get_action_result(
            action_result_id=action_result_id
        )
        if action_result_doc is not None:
            action_doc = action_result_doc.result

    if action_doc is None:
        logger.bind(ctx=ctx, action_id=id).error("Action as well as Action Result doc not found")
        raise HTTPException(404, "Action not found")

    action_result_doc = await ActionFactory().run_action_in_background(
        ctx=ctx,
        app_state=AppState(),
        action_doc=action_doc,
        action_input_dict=action_input.model_dump(exclude_none=True),
        user_action_result=user_action_result,
        action_result_id=action_result_id,
        db=db,
        background_tasks=None,
        webhook=None,
    )
    assert action_result_doc
    assert action_result_doc.output
