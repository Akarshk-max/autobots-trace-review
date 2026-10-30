import uuid

import pytest

from src.autobots.conn.unstructured_io.unstructured_io import get_unstructured_io
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM

# datastore_factory imports the retired pinecone connector at module load, so it is
# currently un-importable (see note in the report). The test below is already skipped;
# the guarded import keeps collection green until datastore_factory is fixed or removed.
try:
    from src.autobots.datastore.datastore_factory import get_datastore_factory
except ImportError:
    get_datastore_factory = None


@pytest.mark.skip(reason="Skipping test_datastore_happy_path to keep within rate limit")
@pytest.mark.asyncio
async def test_datastore_factory_happy_path(set_test_settings):
    str1 = "The truth is one, the wise call it by many names. ― Rig Veda"
    str2 = "One who talks sweet, spend all their days in happiness. – Rig Veda"
    str3 = "One has to be humble if he desires to acquire knowledge. – Rig Veda"
    ctx = Context()
    user_id = uuid.UUID("4d5d5063-36fb-422e-a811-cac8c2003d37")
    user = UserORM(id=str(user_id))
    settings = SettingsProvider.sget()
    unstructured_client = get_unstructured_io(
        ctx=ctx,
        user=user,
        api_key=settings.UNSTRUCTURED_API_KEY,
        api_uri=settings.UNSTRUCTURED_URL,
    )
    datastore = await get_datastore_factory(unstructured=unstructured_client).create_datastore(
        "test_datastore_factory_happy_path"
    )
    try:
        await datastore.put_data(str1)
        await datastore.put_data(str2)
        await datastore.put_data(str3)

        search_res = await datastore.search("How to be happy?", top_k=1)

        assert len(search_res) == 1
        assert search_res[0] == str2

    finally:
        await datastore.empty_and_close()
