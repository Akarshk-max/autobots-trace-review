# src/autobots/ingestion/ingestion_llm_connector.py
from typing import Literal

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

# Import the new application-wide connection framework
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.claude.claude_client import Claude, get_claude_conn
from src.autobots.conn.gemini.gemini import GeminiClient, get_gemini_conn

# Import the connection factory functions for the fallback/dev path
from src.autobots.conn.openai.openai_client import OpenAI, get_openai
from src.autobots.conn.openrouter.openrouter_client import OpenRouter, get_conn_openrouter
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM

INGESTION_PROVIDER_DEFAULT_MODELS = {
    "openai": "gpt-5.6-terra",
    "openrouter": "openai/gpt-5.6-terra",
    "gemini": "gemini-3.5-flash",
    "anthropic": "claude-sonnet-5",
}


class NoActiveIngestionProvider(RuntimeError):
    """No configured provider can service an ingestion/GraphRAG LLM request."""


class IngestionLLMConnector:
    """
    A dedicated connector for the ingestion pipeline to acquire LLM clients.

    This class acts as an adapter, encapsulating the logic to choose between:
    1. Forcing the use of global system API keys (controlled by a developer toggle).
    2. Using the standard, application-wide AppConn framework to get user-specific or system keys.
    """

    # --- DEVELOPER TOGGLE ---
    # Set to True to force the ingestion pipeline to use the globally configured
    # system API keys, bypassing the AppConn logic. This is useful for
    # development, testing, or ensuring system-level consistency for ingestion tasks.
    _FORCE_SYSTEM_KEYS_FOR_INGESTION: bool = False

    @staticmethod
    async def resolve_provider(
        ctx: Context, user: UserORM, db: AsyncIOMotorDatabase
    ) -> tuple[str, str]:
        try:
            connection = await IngestionLLMConnector.get_openai_client(ctx, user, db, "openai")
            if not isinstance(connection, Exception):
                return "openai", INGESTION_PROVIDER_DEFAULT_MODELS["openai"]
        except Exception as e:
            logger.bind(ctx=ctx, error=str(e)).debug("OpenAI not available for auto-select")

        try:
            connection = await IngestionLLMConnector.get_gemini_client(ctx, user, db)
            if not isinstance(connection, Exception):
                return "gemini", INGESTION_PROVIDER_DEFAULT_MODELS["gemini"]
        except Exception as e:
            logger.bind(ctx=ctx, error=str(e)).debug("Gemini not available for auto-select")

        try:
            connection = await IngestionLLMConnector.get_claude_client(ctx, user, db)
            if not isinstance(connection, Exception):
                return "anthropic", INGESTION_PROVIDER_DEFAULT_MODELS["anthropic"]
        except Exception as e:
            logger.bind(ctx=ctx, error=str(e)).debug("Claude not available for auto-select")

        try:
            connection = await IngestionLLMConnector.get_openrouter_client(ctx, user, db)
            if not isinstance(connection, Exception):
                return "openrouter", INGESTION_PROVIDER_DEFAULT_MODELS["openrouter"]
        except Exception as e:
            logger.bind(ctx=ctx, error=str(e)).debug("OpenRouter not available for auto-select")

        raise NoActiveIngestionProvider(
            "No active LLM provider found for ingestion. Configure at least one provider "
            "(OpenAI, Gemini, Claude, or OpenRouter)."
        )

    @staticmethod
    async def get_openai_client(
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        provider: Literal["azure", "openai"] = "openai",
    ) -> OpenAI:
        if IngestionLLMConnector._FORCE_SYSTEM_KEYS_FOR_INGESTION:
            logger.warning("INGESTION DEV TOGGLE: Forcing global system keys for OpenAI.")
            # The global 'get_openai' still works as a factory based on provider.
            return get_openai(provider=provider)
        else:
            logger.info("Using AppConn to acquire OpenAI client for ingestion.")
            client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=provider)
            if isinstance(client, Exception):
                raise client
            return client

    @staticmethod
    async def get_gemini_client(
        ctx: Context, user: UserORM, db: AsyncIOMotorDatabase
    ) -> GeminiClient:
        if IngestionLLMConnector._FORCE_SYSTEM_KEYS_FOR_INGESTION:
            logger.warning("INGESTION DEV TOGGLE: Forcing global system keys for Gemini.")
            # Use the new connection factory with the global key from settings.
            settings = SettingsProvider.sget()
            return get_gemini_conn(ctx=ctx, api_key=settings.GEMINI_API_KEY)
        else:
            logger.info("Using AppConn to acquire Gemini client for ingestion.")
            client = await AppConn.get_gemini(ctx=ctx, db=db, user=user)
            if isinstance(client, Exception):
                raise client
            return client

    @staticmethod
    async def get_claude_client(ctx: Context, user: UserORM, db: AsyncIOMotorDatabase) -> Claude:
        if IngestionLLMConnector._FORCE_SYSTEM_KEYS_FOR_INGESTION:
            logger.warning("INGESTION DEV TOGGLE: Forcing global system keys for Claude.")
            # Use the new connection factory with the global key from settings.
            settings = SettingsProvider.sget()
            return get_claude_conn(ctx=ctx, api_key=settings.CLAUDE_API_KEY)
        else:
            logger.info("Using AppConn to acquire Claude client for ingestion.")
            client = await AppConn.get_claude(ctx=ctx, db=db, user=user)
            if isinstance(client, Exception):
                raise client
            return client

    @staticmethod
    async def get_openrouter_client(
        ctx: Context, user: UserORM, db: AsyncIOMotorDatabase
    ) -> OpenRouter:
        if IngestionLLMConnector._FORCE_SYSTEM_KEYS_FOR_INGESTION:
            logger.warning("INGESTION DEV TOGGLE: Forcing global system keys for OpenRouter.")
            settings = SettingsProvider.sget()
            return get_conn_openrouter(
                ctx=ctx,
                api_key=settings.OPENROUTER_API_KEY,
                http_referer=settings.OPENROUTER_SYSTEM_HTTP_REFERER,
                app_title=settings.OPENROUTER_SYSTEM_X_OPENROUTER_TITLE,
                is_system_creds=True,
            )
        else:
            logger.info("Using AppConn to acquire OpenRouter client for ingestion.")
            client = await AppConn.get_openrouter(ctx=ctx, db=db, user=user)
            if isinstance(client, Exception):
                raise client
            return client
