import asyncio

import pytest

from src.autobots.data_model.updated_at import UpdatedAt


class SimpleObj(UpdatedAt):
    pass


@pytest.mark.asyncio
async def test_updated_at_happy_path(set_test_settings):
    obj1 = SimpleObj()
    await asyncio.sleep(5)
    obj2 = SimpleObj()
    difference = obj2.updated_at - obj1.updated_at
    assert difference.total_seconds() >= 5 and difference.total_seconds() <= 6
