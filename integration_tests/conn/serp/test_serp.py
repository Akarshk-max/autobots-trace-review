import pytest

from src.autobots.conn.serp.serp import get_serp_client
from src.autobots.conn.serp.serp_search_request import SerpGoogleSearchParams
from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


@pytest.mark.asyncio
async def test_serp_google_search_shop_happy_path(
    set_test_settings: Settings = SettingsProvider.sget(),
):
    search_params = SerpGoogleSearchParams(
        q="buy coffee",
        tbm="shop",
    )
    ctx = Context()
    user = UserORM(id=str(gen_uuid()))
    result = await get_serp_client(
        ctx=ctx, user=user, api_key=set_test_settings.SERP_API_KEY
    ).serp_google_search.search(search_params)
    assert not isinstance(result, Exception)


@pytest.mark.asyncio
async def test_serp_google_search_happy_path(set_test_settings: Settings = SettingsProvider.sget()):
    search_params = SerpGoogleSearchParams(
        q="buy coffee",
    )
    ctx = Context()
    user = UserORM(id=str(gen_uuid()))
    result = await get_serp_client(
        ctx=ctx, user=user, api_key=set_test_settings.SERP_API_KEY
    ).serp_google_search.search(search_params)
    assert not isinstance(result, Exception)
