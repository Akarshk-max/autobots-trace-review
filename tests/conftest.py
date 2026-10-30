# import asyncio
#
# import pytest
# import pytest_asyncio
# from fastapi.testclient import TestClient
# from motor.motor_asyncio import AsyncIOMotorClient
#
# from tests.setup_test_data import setup_test_db#, setup_test_wide, TEST_WIDE
# from src.autobots import SettingsProvider
# from src.autobots.action.action_type.tool_routes import register_tools
# from src.autobots.action_graph.schedule.schedule import Schedule
# from src.autobots.core.database.mongo_base import get_mongo_db, get_mongo_client
# from src.autobots.data_model.context import Context
# from src.autobots.main import app
# from src.autobots.user.user_orm_model import UserORM
#
#
# # ## TODO: Replacing the event_loop fixture with a custom implementation is deprecated and will lead to errors in the future.
# # @pytest.fixture(scope="session")
# # def event_loop():
# #     loop = get_event_loop()
# #     yield loop
# #     # loop.close()
#
#
# # @pytest.fixture(scope="session")
# # def event_loop():
# #     """Create a session-scoped event loop to avoid 'Event loop closed' errors."""
# #     loop = asyncio.new_event_loop()
# #     yield loop
# #     loop.close()
#
# # pytestmark = pytest.mark.asyncio(scope="session")  # If in root conftest.py then Session scope
#
#
# @pytest_asyncio.fixture(scope="session")
# def ctx():
#     return Context(user_id="test_user_id")
#
#
# @pytest_asyncio.fixture(scope="session")
# def user():
#     return UserORM(id="test_user_id", email="test_user@runkai.ai")
#
#
# # @pytest_asyncio.fixture(scope="function")
# # async def db(motor_client):
# #     db = motor_client[SettingsProvider.sget().MONGO_DATABASE]
# #     return db
#
#
# @pytest_asyncio.fixture(scope="session")
# def event_loop():
#     loop = asyncio.get_event_loop_policy().new_event_loop()
#     yield loop
#     loop.close()
#
#
# @pytest_asyncio.fixture(scope="function")
# async def motor_client():
#     mongo_conn = SettingsProvider.sget().MONGO_CONN
#     # moved ?retryWrites=true&w=majority out of secrets as github unable to handle it
#     # TODO: move it back to secrets
#     client = AsyncIOMotorClient(f"{mongo_conn}?retryWrites=true&w=majority")
#     yield client
#     client.close()
#
#
# @pytest_asyncio.fixture(scope="function")
# async def db(motor_client):
#     db = motor_client[SettingsProvider.sget().MONGO_DATABASE]
#     return db
#
#
# @pytest_asyncio.fixture(scope="session")
# def register_tools_fixture():
#     register_tools()
#
#
# @pytest_asyncio.fixture(scope="session")
# def set_test_settings():
#     register_tools()
#     print("Function scope fixture for test: runs for every test")

#
# @pytest_asyncio.fixture
# def test_client() -> TestClient:
#     print("Function scope fixture for test: test_client")
#     client = TestClient(app)
#     return client
#
#
# @pytest_asyncio.fixture(scope="session")
# def apscheduler_setup():
#     # print("this is setup")
#     yield
#     Schedule.stop_scheduler()
#     print("apscheduler teardown")
#
#
# @pytest_asyncio.fixture(scope="session", autouse=True)
# async def start():
#     print("🔥 pytest session started")
#     print("🔥 pytest session — setting up test data")
#     ctx = Context(user_id="test_user_id")
#     db1 = next(get_mongo_db())
#     user = UserORM(id="test_user_id", email="test_user@runkai.ai")
#     await setup_test_db(ctx, db1, user)


import asyncio

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from motor.motor_asyncio import AsyncIOMotorClient

from src.autobots import SettingsProvider
from src.autobots.action.action_type.tool_routes import register_tools
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.integrations.crypto.encoder_decoder import init_crypto_keys as _init_crypto_keys
from src.autobots.main import app
from src.autobots.user.user_orm_model import UserORM
from tests.setup_test_data import setup_test_db


# --- App-authz crypto init (production does this in the app lifespan) ---
@pytest.fixture(scope="session", autouse=True)
def _init_test_crypto():
    """Initialize integrations crypto for the test session.

    Production initializes these keys in the app lifespan (core/lifespan.py) from
    settings; tests bypass the lifespan, so any encrypt()/decrypt() call would
    raise "crypto not initialized: call init_keys first".

    We mirror the lifespan exactly: init from ENCRYPTION_PUBLIC_KEY_B64 /
    ENCRYPTION_PRIVATE_KEY_B64 when configured, and otherwise leave crypto
    uninitialized. There is deliberately no generated-keypair fallback — a key that
    is not the real one cannot decrypt app-auth records already in the shared DB, so
    it only turns a clear "crypto not initialized" into a confusing
    "ValueError: Decryption failed". Tests that need to encrypt or decrypt must
    supply the real keys.
    """
    settings = SettingsProvider.sget()
    pub, priv = settings.ENCRYPTION_PUBLIC_KEY_B64, settings.ENCRYPTION_PRIVATE_KEY_B64
    if pub and priv:
        _init_crypto_keys(pub, priv)


# --- Core session event loop for pytest-asyncio ---
@pytest_asyncio.fixture(scope="session")
def event_loop():
    """Create a session-scoped event loop for pytest-asyncio (safe for xdist)."""
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


# --- Basic context and user for tests ---
@pytest_asyncio.fixture(scope="function")
def set_test_settings():
    register_tools()
    print("set_test_settings: register_tools")


@pytest_asyncio.fixture(scope="session")
def ctx():
    return Context(user_id="test_user_id")


@pytest_asyncio.fixture(scope="session")
def user():
    return UserORM(id="test_user_id", email="test_user@runkai.ai")


@pytest_asyncio.fixture(scope="function")
def app_state():
    return AppState()


# --- MongoDB client and db fixtures ---
@pytest_asyncio.fixture(scope="function")
async def motor_client():
    mongo_conn = SettingsProvider.sget().MONGO_CONN
    client = AsyncIOMotorClient(f"{mongo_conn}?retryWrites=true&w=majority")
    print("🔥 DB client Connection established")
    yield client
    client.close()
    print("🔥 DB client Connection closed")


@pytest_asyncio.fixture(scope="function")
async def db(motor_client):
    db = motor_client[SettingsProvider.sget().MONGO_DATABASE]
    # print("🔥 DB Connection established")
    return db


# --- Register tools once per test session ---
@pytest_asyncio.fixture
def register_tools_fixture():
    print("register_tools_fixture")
    register_tools()


# --- TestClient for FastAPI (sync fixture is fine) ---
@pytest.fixture
def test_client() -> TestClient:
    client = TestClient(app)
    return client


# --- Schedule/apscheduler setup ---
# @pytest_asyncio.fixture(scope="session", autouse=True)
# def apscheduler_setup():
#     yield
#     Schedule.stop_scheduler()
#     print("apscheduler teardown")


# --- Set up test data once per session ---
@pytest_asyncio.fixture(scope="session", autouse=True)
async def start(_init_test_crypto, ctx, user):
    print("🔥 pytest session started")
    register_tools()
    print("🔥 register_tools")
    db1 = next(get_mongo_db())
    await setup_test_db(ctx, db1, user)
