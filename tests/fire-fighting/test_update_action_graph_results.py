import pytest

from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultUpdate,
)
from src.autobots.action_graph.action_graph_result.user_action_graph_result import (
    UserActionGraphResult,
)
from src.autobots.user.user import User
from src.autobots.user.user_orm_model import UserORM


@pytest.mark.skip("used only for adhoc work")
@pytest.mark.asyncio
async def manual_update_of_action_graph_result(ctx, db, set_test_settings):
    user_id = ""
    run_id = ""

    user = await User.get(ctx=ctx, user=UserORM(id=user_id), id=user_id, db=db)

    user_action_graph_result = UserActionGraphResult(user=user, db=db)

    action_graph_result_doc = await user_action_graph_result.get_action_graph_result(
        ctx=ctx, action_graph_result_id=run_id
    )

    update = action_graph_result_doc.model_copy(deep=True)

    result_update = ActionGraphResultUpdate(**update.model_dump())

    updated_result_doc = await user_action_graph_result.update_action_graph_result(
        ctx, action_graph_result_id=run_id, action_graph_update=result_update
    )

    assert updated_result_doc.id == run_id
