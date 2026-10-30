import pytest

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.context import Context
from src.autobots.user.user import User
from src.autobots.user.user_orm_model import UserORM


@pytest.mark.skip("Fill user_id")
@pytest.mark.asyncio
async def test_user_happy_path(set_test_settings):
    ctx = Context()
    user_id = ""  # fill this
    db = next(get_mongo_db())

    user = await User.get(ctx=ctx, user=UserORM(id=user_id), id=user_id, db=db)
    user_orm = UserORM.model_validate(user.model_dump())
    assert isinstance(user_orm, UserORM)
