import pytest
from fastapi import HTTPException
from tenacity import RetryError, retry, stop_after_attempt, wait_fixed


@pytest.mark.asyncio
async def test_action_retry_happy_path(set_test_settings):
    try:
        await a_func()
    except RetryError:
        raise AssertionError
    except HTTPException:
        assert True
    except Exception:
        raise AssertionError


async def a_func():
    await a_retry_func()


@retry(stop=stop_after_attempt(3), wait=wait_fixed(3), reraise=True)
async def a_retry_func():
    raise HTTPException(status_code=500)
