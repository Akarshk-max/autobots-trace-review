import pytest

from src.autobots.conn.replicate.replicate import get_replicate_client
from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


@pytest.mark.asyncio
async def test_list_collections_happy_path(set_test_settings: Settings = SettingsProvider.sget()):
    ctx = Context()
    collections = await get_replicate_client(
        ctx=ctx, user=UserORM(id=str(gen_uuid())), api_key=set_test_settings.REPLICATE_API_KEY
    ).list_collections_namespace()
    assert collections


@pytest.mark.asyncio
async def test_list_models_happy_path(set_test_settings: Settings = SettingsProvider.sget()):
    ctx = Context()
    models = await get_replicate_client(
        ctx=ctx, user=UserORM(id=str(gen_uuid())), api_key=set_test_settings.REPLICATE_API_KEY
    ).list_models()
    assert models


# @pytest.mark.asyncio
# async def test_face_swap_happy_path(set_test_settings):
#     params = YanOpsFaceSwapParams(
#         target_image="https://replicate.delivery/pbxt/JkUYWp60oNwz1SF9AJvJPv7upLqucTyaeCxQ07qZGijlDKxt/face_swap_09.jpg",
#         source_image="https://media.licdn.com/dms/image/D4E03AQFsm5Y9vP66Cw/profile-displayphoto-shrink_200_200/0/1698898979044?e=2147483647&v=beta&t=_JUeBPwHfyfbcuEtp9vSfuF3r9gWARAMIKe3FQCTp6k"
#     )
#     output = await get_replicate().yan_ops_face_swap.run(params)
#     assert output
#     assert output.code == 200
