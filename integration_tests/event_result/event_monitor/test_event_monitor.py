import pytest

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_crud import EventResultAdminCRUD


@pytest.mark.skip("Test not ready yet")
@pytest.mark.asyncio
async def test_crud_mongo_db_happy_path(set_test_settings):
    ctx = Context()
    # settings = SettingsProvider.sget()
    db = next(get_mongo_db())

    docs = await EventResultAdminCRUD(db=db).get_stuck_graphs(ctx=ctx, minutes=60)
    assert docs
