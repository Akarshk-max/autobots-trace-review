from typing import Literal

import httpx
from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.conn.assembly.assemblyai import AssemblyAIClient
from src.autobots.conn.cartesia.cartesia_client import CartesiaConnection
from src.autobots.conn.claude.claude_client import Claude
from src.autobots.conn.claude.claude_vertex_client import VertexClaude
from src.autobots.conn.claude.model_registry import ClaudeProvider
from src.autobots.conn.daytona.daytona_client import DaytonaClient
from src.autobots.conn.deepgram.deepgram import DeepgramConnection
from src.autobots.conn.deepseek.deepseek import DeepSeekConnection
from src.autobots.conn.exa.exa import ExaAPI
from src.autobots.conn.flux.flux_client import FluxClient
from src.autobots.conn.gemini.gemini import GeminiClient
from src.autobots.conn.gemini.gen_image.gemini_image import NanoBananaImageClient
from src.autobots.conn.gemini.gen_video.gemini_video import VeoVideoClient
from src.autobots.conn.gemini.gen_video_omni.gemini_omni import OmniVideoClient
from src.autobots.conn.grok.grok import GrokConnection
from src.autobots.conn.openai.openai_client import AzureOpenAIBYOK, OpenAI
from src.autobots.conn.openrouter.openrouter_client import OpenRouter
from src.autobots.conn.opus.opus import OpusClient
from src.autobots.conn.perplexity.perplexity_client import PerplexitySDKClient
from src.autobots.conn.replicate.replicate import Replicate
from src.autobots.conn.serp.serp import Serp
from src.autobots.conn.unstructured_io.unstructured_io import UnstructuredIO
from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_conn.app_conn_type.app_conn_assemblyai import AppConnAssemblyAi
from src.autobots.integrations.app_conn.app_conn_type.app_conn_autobots import AppConnAutobots
from src.autobots.integrations.app_conn.app_conn_type.app_conn_azure_openai import (
    AppConnAzureOpenAI,
)
from src.autobots.integrations.app_conn.app_conn_type.app_conn_cartesia import AppConnCartesia
from src.autobots.integrations.app_conn.app_conn_type.app_conn_claude import AppConnClaude
from src.autobots.integrations.app_conn.app_conn_type.app_conn_daytona import AppConnDaytona
from src.autobots.integrations.app_conn.app_conn_type.app_conn_deepgram import AppConnDeepGram
from src.autobots.integrations.app_conn.app_conn_type.app_conn_deepseek import AppConnDeepseek
from src.autobots.integrations.app_conn.app_conn_type.app_conn_exa import AppConnExa
from src.autobots.integrations.app_conn.app_conn_type.app_conn_gemini import AppConnGemini
from src.autobots.integrations.app_conn.app_conn_type.app_conn_google_serp import AppConnGoogleSerp
from src.autobots.integrations.app_conn.app_conn_type.app_conn_grok import AppConnGrok
from src.autobots.integrations.app_conn.app_conn_type.app_conn_openai import AppConnOpenAI
from src.autobots.integrations.app_conn.app_conn_type.app_conn_openrouter import AppConnOpenRouter
from src.autobots.integrations.app_conn.app_conn_type.app_conn_opus import AppConnOpus
from src.autobots.integrations.app_conn.app_conn_type.app_conn_perplexity import AppConnPerplexity
from src.autobots.integrations.app_conn.app_conn_type.app_conn_replicate import AppConnReplicate
from src.autobots.integrations.app_conn.app_conn_type.app_conn_replicate_flux import (
    AppConnReplicateFlux,
)
from src.autobots.integrations.app_conn.app_conn_type.app_conn_unstructured_io import (
    AppConnUnstructuredIO,
)
from src.autobots.integrations.app_conn.app_conn_type.app_conn_vertex_ai import AppConnVertexAI
from src.autobots.user.user_orm_model import UserORM


class AppConn:
    @staticmethod
    async def get_openai(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        provider: Literal["openai", "azure", "azure_byok"],
        settings: Settings = SettingsProvider.sget(),
    ) -> OpenAI | AzureOpenAIBYOK | Exception:
        try:
            conn: OpenAI | AzureOpenAIBYOK | None = None
            match provider:
                case "openai":
                    conn = await AppConnOpenAI.get_openai_provider_openai(
                        ctx=ctx, db=db, user=user, settings=settings
                    )
                case "azure":
                    conn = await AppConnOpenAI.get_openai_provider_azure(
                        ctx=ctx, db=db, user=user, settings=settings
                    )
                case "azure_byok":
                    conn = await AppConnAzureOpenAI.get(
                        ctx=ctx, db=db, user=user, settings=settings
                    )
                case _:
                    logger.bind(ctx=ctx, provider=provider).error(
                        f"Cannot get app conn openai for provider: {provider}"
                    )

            if conn is None:
                logger.bind(ctx=ctx, provider=provider).error(
                    f"Cannot get app conn openai for provider: {provider}"
                )
                raise HTTPException(
                    status_code=404, detail=f"Cannot get app conn openai for provider: {provider}"
                )
            return conn
        except Exception as e:
            logger.bind(ctx=ctx, provider=provider).exception(str(e))
            return e

    @staticmethod
    async def get_daytona(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> DaytonaClient | Exception:
        try:
            # --- TEMP LOGS START (DAYTONA CONN RESOLUTION DIAG) ---
            current_settings = SettingsProvider.sget()
            logger.bind(ctx=ctx, user_id=str(user.id)).warning(
                "--- TEMP DAYTONA CONN RESOLUTION DIAG START --- "
                f"AppConn.get_daytona enter "
                f"settings_id={id(settings)} current_settings_id={id(current_settings)} "
                f"env={getattr(settings, 'ENV', None)} version={getattr(settings, 'VERSION', None)} "
                f"mongo_db={getattr(settings, 'MONGO_DATABASE', None)} "
                f"daytona_system_key_set={bool(getattr(settings, 'DAYTONA_API_KEY', '') or '')} "
                f"daytona_system_url_set={bool(getattr(settings, 'DAYTONA_API_URL', '') or '')}"
            )
            # --- TEMP LOGS END (DAYTONA CONN RESOLUTION DIAG) ---

            conn = await AppConnDaytona.get_daytona(ctx=ctx, db=db, user=user, settings=settings)
            if conn is None:
                # --- TEMP LOGS START (DAYTONA CONN RESOLUTION DIAG) ---
                logger.bind(ctx=ctx, user_id=str(user.id)).warning(
                    "--- TEMP DAYTONA CONN RESOLUTION DIAG END --- "
                    "AppConnDaytona.get_daytona returned None "
                    f"env={getattr(settings, 'ENV', None)} version={getattr(settings, 'VERSION', None)} "
                    f"mongo_db={getattr(settings, 'MONGO_DATABASE', None)} "
                    f"daytona_system_key_set={bool(getattr(settings, 'DAYTONA_API_KEY', '') or '')} "
                    f"daytona_system_url_set={bool(getattr(settings, 'DAYTONA_API_URL', '') or '')}"
                )
                # --- TEMP LOGS END (DAYTONA CONN RESOLUTION DIAG) ---
                raise HTTPException(
                    status_code=404,
                    detail="Daytona is not configured for this user. Please add Daytona API key in App Creds and sync App Auth.",
                )
            # --- TEMP LOGS START (DAYTONA CONN RESOLUTION DIAG) ---
            logger.bind(ctx=ctx, user_id=str(user.id)).warning(
                "--- TEMP DAYTONA CONN RESOLUTION DIAG END --- "
                "AppConn.get_daytona success"
            )
            # --- TEMP LOGS END (DAYTONA CONN RESOLUTION DIAG) ---
            return conn
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    @staticmethod
    async def get_gemini(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        gemini_provider: str = "api",
        settings: Settings = SettingsProvider.sget(),
    ) -> GeminiClient | Exception:
        try:
            if gemini_provider == "vertex":
                return await AppConnVertexAI.get_gemini(ctx=ctx, db=db, user=user)
            conn: GeminiClient | None = await AppConnGemini.get_gemini(
                ctx=ctx, db=db, user=user, settings=settings
            )
            if conn is None:
                logger.bind(ctx=ctx).error("Cannot get app conn gemini")
                raise HTTPException(status_code=404, detail="Cannot get app conn gemini")
            return conn
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    @staticmethod
    async def get_gemini_image(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> NanoBananaImageClient | Exception:
        try:
            conn: NanoBananaImageClient | None = await AppConnGemini.get_gemini_image(
                ctx=ctx, db=db, user=user, settings=settings
            )
            if conn is None:
                logger.bind(
                    ctx=ctx,
                ).error("Cannot get app conn gemini")
                raise HTTPException(status_code=404, detail="Cannot get app conn gemini")
            return conn
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e
        
    @staticmethod
    async def get_gemini_veo_video(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> VeoVideoClient | Exception:
        try:
            conn: VeoVideoClient | None = await AppConnGemini.get_gemini_veo_video(
                ctx=ctx, db=db, user=user, settings=settings
            )
            if conn is None:
                logger.bind(
                    ctx=ctx,
                ).error("Cannot get app conn gemini")
                raise HTTPException(status_code=404, detail="Cannot get app conn gemini")
            return conn
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    @staticmethod
    async def get_gemini_omni_video(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> OmniVideoClient | Exception:
        try:
            conn: OmniVideoClient | None = await AppConnGemini.get_gemini_omni_video(
                ctx=ctx, db=db, user=user, settings=settings
            )
            if conn is None:
                logger.bind(ctx=ctx).error("Cannot get app conn gemini")
                raise HTTPException(status_code=404, detail="Cannot get app conn gemini")
            return conn
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    @staticmethod
    async def get_claude(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        provider: ClaudeProvider = ClaudeProvider.ANTHROPIC,
        settings: Settings = SettingsProvider.sget(),
    ) -> Claude | VertexClaude | Exception:
        try:
            if provider == ClaudeProvider.VERTEX:
                return await AppConnVertexAI.get_claude(ctx=ctx, db=db, user=user)
            conn: Claude | None = await AppConnClaude.get_claude(
                ctx=ctx, db=db, user=user, settings=settings
            )
            if conn is None:
                logger.bind(ctx=ctx).error("Cannot get app conn claude")
                raise HTTPException(status_code=404, detail="Cannot get app conn claude")
            return conn
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    @staticmethod
    async def get_grok(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> GrokConnection | Exception:
        try:
            conn: GrokConnection | None = await AppConnGrok.get_grok(
                ctx=ctx, db=db, user=user, settings=settings
            )
            if conn is None:
                logger.bind(
                    ctx=ctx,
                ).error("Cannot get app conn grok")
                raise HTTPException(status_code=404, detail="Cannot get app conn grok")
            return conn
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    @staticmethod
    async def get_first_active_llm(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> str | Exception:
        try:
            try:
                # OpenRouter uses system-level API key; no user connection needed
                if settings.OPENROUTER_API_KEY:
                    logger.bind(ctx=ctx).info("Found active OpenRouter system credentials")
                    return "openrouter"

                openai_conn = await AppConn.get_openai(
                    ctx=ctx, db=db, user=user, provider="openai", settings=settings
                )
                if not isinstance(openai_conn, Exception):
                    logger.bind(ctx=ctx).info("Found active OpenAI connection")
                    return "openai"
            except Exception as e:
                logger.bind(ctx=ctx).debug(f"OpenAI not available: {str(e)}")

            try:
                gemini_conn = await AppConn.get_gemini(ctx=ctx, db=db, user=user, settings=settings)
                if not isinstance(gemini_conn, Exception):
                    logger.bind(ctx=ctx).info("Found active Gemini connection")
                    return "gemini"
            except Exception as e:
                logger.bind(ctx=ctx).debug(f"Gemini not available: {str(e)}")

            try:
                claude_conn = await AppConn.get_claude(ctx=ctx, db=db, user=user, settings=settings)
                if not isinstance(claude_conn, Exception):
                    logger.bind(ctx=ctx).info("Found active Claude connection")
                    return "claude"
            except Exception as e:
                logger.bind(ctx=ctx).debug(f"Claude not available: {str(e)}")

            try:
                grok_conn = await AppConn.get_grok(ctx=ctx, db=db, user=user, settings=settings)
                if not isinstance(grok_conn, Exception):
                    logger.bind(ctx=ctx).info("Found active Grok connection")
                    return "grok"
            except Exception as e:
                logger.bind(ctx=ctx).debug(f"Grok not available: {str(e)}")

            logger.bind(ctx=ctx).error("No active LLM provider found")
            raise HTTPException(
                status_code=404,
                detail="No active LLM provider found. Please configure at least one LLM provider (OpenAI, Gemini, Claude, Grok, or OpenRouter).",
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    @staticmethod
    async def get_deepgram(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> DeepgramConnection | Exception:
        try:
            conn: DeepgramConnection | None = await AppConnDeepGram.get_deepgram(
                ctx=ctx, db=db, user=user, settings=settings
            )
            if conn is None:
                logger.bind(
                    ctx=ctx,
                ).error("Cannot get app conn deepgram")
                raise HTTPException(status_code=404, detail="Cannot get app conn deepgram")
            return conn
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    @staticmethod
    async def get_cartesia(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> CartesiaConnection | Exception:
        try:
            conn: CartesiaConnection | None = await AppConnCartesia.get_cartesia_conn(
                ctx=ctx, db=db, user=user, settings=settings
            )
            if conn is None:
                logger.bind(
                    ctx=ctx,
                ).error("Cannot get app conn cartesia")
                raise HTTPException(status_code=404, detail="Cannot get app conn cartesia")
            return conn
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    @staticmethod
    async def get_assemblyai(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> AssemblyAIClient | Exception:
        try:
            conn: AssemblyAIClient | None = await AppConnAssemblyAi.get_assemblyai_conn(
                ctx=ctx, db=db, user=user, settings=settings
            )
            if conn is None:
                logger.bind(
                    ctx=ctx,
                ).error("Cannot get app conn assemblyai")
                raise HTTPException(status_code=404, detail="Cannot get app conn assemblyai")
            return conn
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    @staticmethod
    async def get_replicate_flux(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> FluxClient | Exception:
        try:
            conn: FluxClient | None = await AppConnReplicateFlux.get_replicate_flux(
                ctx=ctx, db=db, user=user, settings=settings
            )
            if conn is None:
                logger.bind(
                    ctx=ctx,
                ).error("Cannot get app conn replicate flux")
                raise HTTPException(status_code=404, detail="Cannot get app conn replicate flux")
            return conn
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    @staticmethod
    async def get_exa(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> ExaAPI | Exception:
        try:
            conn: ExaAPI | None = await AppConnExa.get_exa(
                ctx=ctx, db=db, user=user, settings=settings
            )
            if conn is None:
                logger.bind(
                    ctx=ctx,
                ).error("Cannot get app conn exa")
                raise HTTPException(status_code=404, detail="Cannot get app conn exa")
            return conn
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    @staticmethod
    async def get_opus(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> OpusClient | Exception:
        try:
            conn: OpusClient | None = await AppConnOpus.get_opus(
                ctx=ctx, db=db, user=user, settings=settings
            )
            if conn is None:
                logger.bind(
                    ctx=ctx,
                ).error("Cannot get app conn opus")
                raise HTTPException(status_code=404, detail="Cannot get app conn opus")
            return conn
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    @staticmethod
    async def get_google_serp(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> Serp | Exception:
        try:
            conn: Serp | None = await AppConnGoogleSerp.get_serp(
                ctx=ctx, db=db, user=user, settings=settings
            )
            if conn is None:
                logger.bind(
                    ctx=ctx,
                ).error("Cannot get app conn google serp")
                raise HTTPException(status_code=404, detail="Cannot get app conn google serp")
            return conn
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    @staticmethod
    async def get_replicate(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> Replicate | Exception:
        try:
            conn: Replicate | None = await AppConnReplicate.get_replicate(
                ctx=ctx, db=db, user=user, settings=settings
            )
            if conn is None:
                logger.bind(
                    ctx=ctx,
                ).error("Cannot get app conn replicate")
                raise HTTPException(status_code=404, detail="Cannot get app conn replicate")
            return conn
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    @staticmethod
    async def get_unstructured_io(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> UnstructuredIO | Exception:
        try:
            conn: UnstructuredIO | None = await AppConnUnstructuredIO.get_unstructured_io_conn(
                ctx=ctx, db=db, user=user, settings=settings
            )
            if conn is None:
                logger.bind(
                    ctx=ctx,
                ).error("Cannot get app conn unstructured io")
                raise HTTPException(status_code=404, detail="Cannot get app conn unstructured io")
            return conn
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e
        
    @staticmethod
    async def get_deepseek(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> DeepSeekConnection | Exception:
        try:
            conn: DeepSeekConnection | None = await AppConnDeepseek.get_deepseek(
                ctx=ctx, db=db, user=user, settings=settings
            )
            if conn is None:
                logger.bind(
                    ctx=ctx,
                ).error("Cannot get app conn deepseek")
                raise HTTPException(status_code=404, detail="Cannot get app conn deepseek")
            return conn
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e
        

    @staticmethod
    async def get_perplexity(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> PerplexitySDKClient | Exception:
        try:
            conn: PerplexitySDKClient | None = await AppConnPerplexity.get_perplexity(
                ctx=ctx, db=db, user=user, settings=settings
            )
            if conn is None:
                logger.bind(
                    ctx=ctx,
                ).error("Cannot get app conn perplexity")
                raise HTTPException(status_code=404, detail="Cannot get app conn perplexity")
            return conn
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    @staticmethod
    async def get_vertex_claude(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
    ) -> VertexClaude | Exception:
        try:
            return await AppConnVertexAI.get_claude(ctx=ctx, db=db, user=user)
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    @staticmethod
    async def get_vertex_gemini(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
    ) -> GeminiClient | Exception:
        try:
            return await AppConnVertexAI.get_gemini(ctx=ctx, db=db, user=user)
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    @staticmethod
    async def get_openrouter(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> OpenRouter | Exception:
        try:
            conn: OpenRouter | None = await AppConnOpenRouter.get_openrouter(
                ctx=ctx,
                db=db,
                user=user,
                settings=settings,
            )

            if conn is None:
                logger.bind(ctx=ctx).error("Cannot get app conn openrouter")
                raise HTTPException(
                    status_code=404,
                    detail="Cannot get app conn openrouter",
                )

            return conn

        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    @staticmethod
    async def get_autobots(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> httpx.AsyncClient | Exception:
        try:
            conn = await AppConnAutobots.get_autobots(
                ctx=ctx, db=db, user=user, settings=settings
            )
            if conn is None:
                raise HTTPException(
                    status_code=404,
                    detail="Autobots is not configured. Please add your autobots credentials in App Creds and sync App Auth.",
                )
            return conn
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e