"""No-op the repo-wide integration bootstrap for these pure-logic tests."""
import pytest_asyncio


@pytest_asyncio.fixture(scope="session", autouse=True)
async def start():
    yield
