from typing import List

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.conn.openrouter.openrouter_client import OpenRouter, get_conn_openrouter
from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.data_model.ownership import Ownership
from src.autobots.integrations.app_conn.app_conn_helper import AppConnHelper
from src.autobots.integrations.app_type_to_integration_class import AppTypeToIntegrationClass
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationDoc
from src.autobots.integrations.integration_type.openrouter_auth import OpenRouterAuthModel
from src.autobots.user.user_orm_model import UserORM


class AppConnOpenRouter:
    @staticmethod
    async def get_openrouter(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> OpenRouter | None:
        app_type = AppTypes.openrouter
        conn: OpenRouter | None = None

        ownership: List[Ownership] = await AppConnHelper.get_ownership(
            ctx=ctx,
            db=db,
            user=user,
            app_type=app_type,
        )

        for owner in ownership:
            try:
                match owner:
                    case Ownership.user:
                        integration_doc: IntegrationDoc = await AppConnHelper.get_app_auth(
                            ctx=ctx,
                            db=db,
                            user=user,
                            app_type=app_type,
                        )

                        if not integration_doc:
                            raise HTTPException(
                                status_code=404,
                                detail="OpenRouter app auth doc not found",
                            )

                        app_auth_class = AppTypeToIntegrationClass.get(app_type)

                        app_auth: OpenRouterAuthModel = (
                            app_auth_class.get_auth_model_type().model_validate(
                                integration_doc.auth
                            )
                        )

                        conn = get_conn_openrouter(
                            ctx=ctx,
                            api_key=app_auth.api_key,
                            http_referer=app_auth.http_referer,
                            app_title=app_auth.app_title,
                            settings=settings,
                        )

                    case Ownership.system:
                        api_key = settings.OPENROUTER_API_KEY
                        # api_key = getattr(settings, "OPENROUTER_API_KEY", None)

                        if not api_key:
                            raise HTTPException(
                                status_code=404,
                                detail="System OpenRouter API key not configured",
                            )

                        conn = get_conn_openrouter(
                            ctx=ctx,
                            api_key=api_key,
                            http_referer=settings.OPENROUTER_SYSTEM_HTTP_REFERER,
                            app_title=settings.OPENROUTER_SYSTEM_X_OPENROUTER_TITLE,
                            settings=settings,
                            is_system_creds=True,
                        )

                    case _:
                        logger.bind(
                            ctx=ctx,
                            app_type=app_type,
                            owner=owner,
                        ).error(
                            f"Cannot get app conn openrouter for app_type={app_type}, owner={owner}"
                        )

                if conn:
                    logger.bind(
                        ctx=ctx,
                        app_type=app_type,
                        owner=owner,
                    ).info(
                        f"Got app conn openrouter for app_type={app_type}, owner={owner}"
                    )
                    break

            except Exception as e:
                logger.bind(
                    ctx=ctx,
                    app_type=app_type,
                    owner=owner,
                    error=str(e),
                ).error("Cannot get app conn openrouter")
                continue

        return conn