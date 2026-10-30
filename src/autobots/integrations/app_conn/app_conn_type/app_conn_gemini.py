from typing import List

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.conn.gemini.gemini import GeminiClient, get_gemini_conn
from src.autobots.conn.gemini.gen_image.gemini_image import (
    NanoBananaImageClient,
    get_gemini_nano_conn,
)
from src.autobots.conn.gemini.gen_video.gemini_video import VeoVideoClient, get_gemini_veo_conn
from src.autobots.conn.gemini.gen_video_omni.gemini_omni import (
    OmniVideoClient,
    get_gemini_omni_conn,
)
from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.data_model.ownership import Ownership
from src.autobots.integrations.app_conn.app_conn_helper import AppConnHelper
from src.autobots.integrations.app_type_to_integration_class import AppTypeToIntegrationClass
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationDoc
from src.autobots.integrations.integration_type.gemini_auth import GeminiAuthModel
from src.autobots.user.user_orm_model import UserORM


class AppConnGemini:
    @staticmethod
    async def get_gemini(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> GeminiClient | None:
        app_type = AppTypes.gemini
        conn: GeminiClient | None = None
        ownership: List[Ownership] = await AppConnHelper.get_ownership(
            ctx=ctx, db=db, user=user, app_type=app_type
        )
        for owner in ownership:
            try:
                match owner:
                    case Ownership.user:
                        integration_doc: IntegrationDoc = await AppConnHelper.get_app_auth(
                            ctx=ctx, db=db, user=user, app_type=app_type
                        )
                        app_auth_class = AppTypeToIntegrationClass.get(app_type)
                        app_auth: GeminiAuthModel = (
                            app_auth_class.get_auth_model_type().model_validate(integration_doc.auth)
                        )
                        api_key = app_auth.api_key
                        conn: GeminiClient = get_gemini_conn(ctx=ctx, api_key=api_key)
                    case Ownership.system:
                        api_key = settings.GEMINI_API_KEY
                        conn: GeminiClient = get_gemini_conn(ctx=ctx, api_key=api_key)
                    case _:
                        logger.bind(ctx=ctx, app_type=app_type, owner=owner).error(
                            f"Cannot get app conn gemini for apptype: {app_type} and owner: {owner}"
                        )
                if conn:
                    logger.bind(ctx=ctx, app_type=app_type, owner=owner).info(
                        f"Got app conn gemini for apptype: {app_type} and owner: {owner}"
                    )
                    break
            except Exception as e:
                logger.bind(ctx=ctx, app_type=app_type, owner=owner, error=str(e)).exception(
                    "Cannot get app conn gemini"
                )
                continue
        return conn

    @staticmethod
    async def get_gemini_image(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> NanoBananaImageClient | None:
        app_type = AppTypes.gemini
        conn: NanoBananaImageClient | None = None
        ownership: List[Ownership] = await AppConnHelper.get_ownership(
            ctx=ctx, db=db, user=user, app_type=app_type
        )
        for owner in ownership:
            try:
                match owner:
                    case Ownership.user:
                        integration_doc: IntegrationDoc = await AppConnHelper.get_app_auth(
                            ctx=ctx, db=db, user=user, app_type=app_type
                        )
                        app_auth_class = AppTypeToIntegrationClass.get(app_type)
                        app_auth: GeminiAuthModel = (
                            app_auth_class.get_auth_model_type().model_validate(integration_doc.auth)
                        )
                        api_key = app_auth.api_key
                        conn: NanoBananaImageClient = get_gemini_nano_conn(ctx=ctx, api_key=api_key)
                    case Ownership.system:
                        api_key = settings.GEMINI_API_KEY
                        conn: NanoBananaImageClient = get_gemini_nano_conn(ctx=ctx, api_key=api_key)
                    case _:
                        logger.bind(ctx=ctx, app_type=app_type, owner=owner).error(
                            f"Cannot get app conn gemini for apptype: {app_type} and owner: {owner}"
                        )
                if conn:
                    logger.bind(ctx=ctx, app_type=app_type, owner=owner).info(
                        f"Got app conn gemini for apptype: {app_type} and owner: {owner}"
                    )
                    break
            except Exception as e:
                logger.bind(ctx=ctx, app_type=app_type, owner=owner, error=str(e)).exception(
                    "Cannot get app conn gemini"
                )
                continue
        return conn
    
    @staticmethod
    async def get_gemini_veo_video(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> VeoVideoClient | None:
        app_type = AppTypes.gemini
        conn: VeoVideoClient | None = None
        ownership: List[Ownership] = await AppConnHelper.get_ownership(ctx=ctx, db=db, user=user, app_type=app_type)
        for owner in ownership:
            try:
                match owner:
                    case Ownership.user:
                        integration_doc: IntegrationDoc = await AppConnHelper.get_app_auth(ctx=ctx, db=db, user=user, app_type=app_type)
                        app_auth_class = AppTypeToIntegrationClass.get(app_type)
                        app_auth: GeminiAuthModel = app_auth_class.get_auth_model_type().model_validate(integration_doc.auth)
                        api_key = app_auth.api_key
                        conn = get_gemini_veo_conn(ctx=ctx, api_key=api_key)
                    case Ownership.system:
                        api_key = settings.GEMINI_API_KEY
                        conn = get_gemini_veo_conn(ctx=ctx, api_key=api_key)
                    case _:
                        logger.bind(ctx=ctx, app_type=app_type, owner=owner).error(
                            f"Cannot get app conn gemini for apptype: {app_type} and owner: {owner}"
                        )
                if conn:
                    logger.bind(ctx=ctx, app_type=app_type, owner=owner).info(
                        f"Got app conn gemini for apptype: {app_type} and owner: {owner}"
                    )
                    break
            except Exception as e:
                logger.bind(ctx=ctx, app_type=app_type, owner=owner, error=str(e)).exception("Cannot get app conn gemini")
                continue
        return conn

    @staticmethod
    async def get_gemini_omni_video(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> OmniVideoClient | None:
        app_type = AppTypes.gemini
        conn: OmniVideoClient | None = None
        ownership: List[Ownership] = await AppConnHelper.get_ownership(ctx=ctx, db=db, user=user, app_type=app_type)
        for owner in ownership:
            try:
                match owner:
                    case Ownership.user:
                        integration_doc: IntegrationDoc = await AppConnHelper.get_app_auth(ctx=ctx, db=db, user=user, app_type=app_type)
                        app_auth_class = AppTypeToIntegrationClass.get(app_type)
                        app_auth: GeminiAuthModel = app_auth_class.get_auth_model_type().model_validate(integration_doc.auth)
                        conn = get_gemini_omni_conn(ctx=ctx, api_key=app_auth.api_key)
                    case Ownership.system:
                        conn = get_gemini_omni_conn(ctx=ctx, api_key=settings.GEMINI_API_KEY)
                    case _:
                        logger.bind(ctx=ctx, app_type=app_type, owner=owner).error(
                            f"Cannot get app conn gemini for apptype: {app_type} and owner: {owner}"
                        )
                if conn:
                    logger.bind(ctx=ctx, app_type=app_type, owner=owner).info(
                        f"Got app conn gemini for apptype: {app_type} and owner: {owner}"
                    )
                    break
            except Exception as e:
                logger.bind(ctx=ctx, app_type=app_type, owner=owner, error=str(e)).exception("Cannot get app conn gemini")
                continue
        return conn
