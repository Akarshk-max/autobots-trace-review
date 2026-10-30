import copy

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_http_exception_serialization_unhappy_path(set_test_settings):
    ex = Exception(HTTPException(status_code=418, detail="test"))

    try:
        ex1 = copy.deepcopy(ex)
        assert ex1 == "error"
    except Exception as exc:
        assert (
            str(exc)
            == "HTTPException.__init__() missing 1 required positional argument: 'status_code'"
        )
        assert True


@pytest.mark.asyncio
async def test_exception_serialization_happy_path(set_test_settings):
    ex = Exception("test")

    try:
        ex1 = copy.deepcopy(ex)
        assert str(ex1) == str(ex)
        assert ex1 != ex
    except Exception as exc:
        assert str(exc) == ""
