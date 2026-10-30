# tests/ingestion/scripts/e2e_auth_switch_runner.py

"""
End-to-End Integration Test for the IngestionLLMConnector Authentication Switch.
"""

import asyncio
import sys
import uuid
from pathlib import Path

from loguru import logger

# --- Setup Project Path ---
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ruff: noqa: E402
from src.autobots.integrations.app_types import AppTypes
from src.autobots.conn.openai.openai_chat.chat_model import ChatReq
from src.autobots.conn.openai.openai_client import OpenAI
from src.autobots.core.database.mongo_base import close_mongo_client, get_mongo_db
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.ingestion.ingestion_llm_connector import IngestionLLMConnector
from src.autobots.user.user_orm_model import UserORM

SETTINGS = SettingsProvider.sget()


async def _inject_mock_user_creds(db, user: UserORM, provider: str, api_key: str):
    """
    Inserts a temporary credential document into the database for a specific user,
    mimicking a user-saved API key.
    """
    app_name = AppTypes.openai.value

    # The 'creds' payload itself must be provider-specific and correct.
    if provider == "azure":
        # --- FIX START ---
        # The real client uses model-specific base URLs. We must mimic that here.
        # For our test call using 'gpt-4o-mini', this is the correct endpoint.
        azure_endpoint = SETTINGS.AZURE_OPENAI_GPT_FOUR_O_MINI_BASE_URL
        creds_payload = {"openai_api_key": api_key, "azure_endpoint": azure_endpoint}
        # --- FIX END ---
    else:  # openai
        creds_payload = {"openai_api_key": api_key}

    app_creds_doc = {"user_id": str(user.id), "app_name": app_name, "creds": creds_payload}

    await db["AppCreds"].insert_one(app_creds_doc)
    logger.info(f"Successfully injected mock '{app_name}' credentials for user {user.id}.")


async def _cleanup_mock_user_creds(db, user: UserORM):
    """Removes the temporary credential document from the database."""
    app_name = AppTypes.openai.value
    await db["AppCreds"].delete_one({"user_id": str(user.id), "app_name": app_name})
    logger.info(f"Successfully cleaned up mock credentials for user {user.id}.")


async def _test_llm_call(openai_client: OpenAI, ctx: Context):
    """Uses the provided client to make a simple, direct call to the LLM API."""
    logger.info("Making a direct test call to the LLM...")
    chat_req = ChatReq(
        model="gpt-4o-mini",
        messages=[
            {"role": "user", "content": "What is 2 + 2? Respond with only the word for the number."}
        ],
    )

    chat_res = await openai_client.openai_chat.chat(
        ctx=ctx, app_state=AppState(), chat_req=chat_req
    )
    answer = chat_res.choices[0].message.content if chat_res and chat_res.choices else ""
    logger.info(f"Received LLM response: '{answer}'")

    if "four" not in answer.lower():
        raise AssertionError(f"LLM call failed or returned an unexpected answer: {answer}")

    logger.success("LLM call was successful and returned the expected answer.")


async def main():
    """Main orchestrator for the integration test."""

    logger.info("--- SETUP: Simulating ingestion worker environment ---")
    user_id = str(uuid.uuid4())
    user = UserORM(id=user_id, email=f"test-user@{user_id[:8]}.test")
    db = next(get_mongo_db())
    ctx = Context(user_id=user_id)
    provider = "openai"  # "azure" if SETTINGS.OPENAI_TYPE_IS_AZURE else "openai"
    global_api_key = SETTINGS.OPENAI_API_KEY

    logger.info(f"Test User: {user.id}")
    logger.info(f"Target LLM Provider: {provider}")

    try:
        # --- RUN 1: Test with the toggle forcing system keys ---
        logger.info("\n--- RUN 1: Testing with _FORCE_SYSTEM_KEYS_FOR_INGESTION = True ---")
        IngestionLLMConnector._FORCE_SYSTEM_KEYS_FOR_INGESTION = True
        client_run1 = await IngestionLLMConnector.get_openai_client(ctx, user, db, provider)
        await _test_llm_call(client_run1, ctx)
        logger.success("--- RUN 1 PASSED ---")

        await asyncio.sleep(2)

        # --- RUN 2: Flip the switch and test the AppConn database retrieval path ---
        logger.info("\n--- RUN 2: Testing with _FORCE_SYSTEM_KEYS_FOR_INGESTION = False ---")
        IngestionLLMConnector._FORCE_SYSTEM_KEYS_FOR_INGESTION = False

        try:
            # Inject the mock credential using the global key as the value
            await _inject_mock_user_creds(db, user, provider, global_api_key)

            # Get the client. AppConn should now find and use the injected key.
            client_run2 = await IngestionLLMConnector.get_openai_client(ctx, user, db, provider)

            # Make the test call. Its success proves the DB lookup worked.
            await _test_llm_call(client_run2, ctx)
            logger.success("--- RUN 2 PASSED ---")

        finally:
            # CRITICAL: Ensure the mock credential is always cleaned up
            await _cleanup_mock_user_creds(db, user)

    finally:
        IngestionLLMConnector._FORCE_SYSTEM_KEYS_FOR_INGESTION = True
        close_mongo_client()
        logger.info("\nTest finished. Connector toggle reset and DB connection closed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logger.exception(f"E2E Auth Switch Runner failed with an unhandled exception: {e}")
