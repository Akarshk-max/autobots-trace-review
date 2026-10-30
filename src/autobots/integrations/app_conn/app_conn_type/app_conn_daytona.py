# src/autobots/integrations/app_conn/app_conn_type/app_conn_daytona.py
from typing import List

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.conn.daytona.daytona_client import DaytonaClient
from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.data_model.ownership import Ownership
from src.autobots.integrations.app_conn.app_conn_helper import AppConnHelper
from src.autobots.integrations.app_type_to_integration_class import AppTypeToIntegrationClass
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationDoc
from src.autobots.integrations.integration_type.daytona_auth import DaytonaApiKeyAuthModel
from src.autobots.user.user_orm_model import UserORM


class AppConnDaytona:
    @staticmethod
    async def get_daytona(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> DaytonaClient | None:
        app_type = AppTypes.daytona
        conn: DaytonaClient | None = None

        ownership: List[Ownership] = await AppConnHelper.get_ownership(
            ctx=ctx, db=db, user=user, app_type=app_type
        )
        # --- TEMP LOGS START (DAYTONA CONN RESOLUTION DIAG) ---
        current_settings = SettingsProvider.sget()
        logger.bind(ctx=ctx, user_id=str(user.id), app_type=app_type).warning(
            "--- TEMP DAYTONA CONN RESOLUTION DIAG START --- "
            f"AppConnDaytona.get_daytona enter "
            f"ownership={ownership} "
            f"settings_id={id(settings)} current_settings_id={id(current_settings)} "
            f"env={getattr(settings, 'ENV', None)} version={getattr(settings, 'VERSION', None)} "
            f"mongo_db={getattr(settings, 'MONGO_DATABASE', None)} "
            f"daytona_system_key_set={bool(getattr(settings, 'DAYTONA_API_KEY', '') or '')} "
            f"daytona_system_url_set={bool(getattr(settings, 'DAYTONA_API_URL', '') or '')}"
        )
        # --- TEMP LOGS END (DAYTONA CONN RESOLUTION DIAG) ---

        for owner in ownership:
            try:
                match owner:
                    case Ownership.user:
                        # --- TEMP LOGS START (DAYTONA CONN RESOLUTION DIAG) ---
                        logger.bind(ctx=ctx, user_id=str(user.id), app_type=app_type, owner=owner).warning(
                            "--- TEMP DAYTONA CONN RESOLUTION DIAG --- attempting owner=user (DB AppAuth)"
                        )
                        # --- TEMP LOGS END (DAYTONA CONN RESOLUTION DIAG) ---
                        integration_doc: IntegrationDoc = await AppConnHelper.get_app_auth(
                            ctx=ctx, db=db, user=user, app_type=app_type
                        )
                        app_auth_class = AppTypeToIntegrationClass.get(app_type)
                        app_auth: DaytonaApiKeyAuthModel = (
                            app_auth_class.get_auth_model_type().model_validate(integration_doc.auth)
                        )
                        conn = DaytonaClient(api_key=app_auth.api_key, api_url=app_auth.api_url)

                    case Ownership.system:
                        # system ownership is already gated by UserAppConnRule.get_ownership (runkai.ai)
                        # --- TEMP LOGS START (DAYTONA CONN RESOLUTION DIAG) ---
                        logger.bind(ctx=ctx, user_id=str(user.id), app_type=app_type, owner=owner).warning(
                            "--- TEMP DAYTONA CONN RESOLUTION DIAG --- attempting owner=system "
                            f"daytona_system_key_set={bool(getattr(settings, 'DAYTONA_API_KEY', '') or '')} "
                            f"daytona_system_url_set={bool(getattr(settings, 'DAYTONA_API_URL', '') or '')}"
                        )
                        # --- TEMP LOGS END (DAYTONA CONN RESOLUTION DIAG) ---
                        if not settings.DAYTONA_API_KEY:
                            # --- TEMP LOGS START (DAYTONA CONN RESOLUTION DIAG) ---
                            logger.bind(ctx=ctx, user_id=str(user.id), app_type=app_type, owner=owner).warning(
                                "--- TEMP DAYTONA CONN RESOLUTION DIAG --- skip owner=system: DAYTONA_API_KEY is empty"
                            )
                            # --- TEMP LOGS END (DAYTONA CONN RESOLUTION DIAG) ---
                            continue
                        if not getattr(settings, "DAYTONA_API_URL", ""):
                            # --- TEMP LOGS START (DAYTONA CONN RESOLUTION DIAG) ---
                            logger.bind(ctx=ctx, user_id=str(user.id), app_type=app_type, owner=owner).warning(
                                "--- TEMP DAYTONA CONN RESOLUTION DIAG --- skip owner=system: DAYTONA_API_URL is empty"
                            )
                            # --- TEMP LOGS END (DAYTONA CONN RESOLUTION DIAG) ---
                            continue
                        conn = DaytonaClient(api_key=settings.DAYTONA_API_KEY, api_url=settings.DAYTONA_API_URL)

                    case _:
                        logger.bind(ctx=ctx, app_type=app_type, owner=owner).error(
                            f"Unsupported owner mode for Daytona: {owner}"
                        )

                if conn:
                    logger.bind(ctx=ctx, app_type=app_type, owner=owner).info(
                        f"Got Daytona connection for owner={owner}"
                    )
                    break

            except Exception as e:
                # --- TEMP LOGS START (DAYTONA CONN RESOLUTION DIAG) ---
                logger.bind(ctx=ctx, user_id=str(user.id), app_type=app_type, owner=owner).warning(
                    "--- TEMP DAYTONA CONN RESOLUTION DIAG --- owner attempt failed "
                    f"owner={owner} err_type={type(e).__name__} err={str(e)[:240]}"
                )
                # --- TEMP LOGS END (DAYTONA CONN RESOLUTION DIAG) ---
                logger.bind(ctx=ctx, app_type=app_type, owner=owner, error=str(e)).exception(
                    "Cannot get Daytona conn"
                )
                continue

        # --- TEMP LOGS START (DAYTONA CONN RESOLUTION DIAG) ---
        logger.bind(ctx=ctx, user_id=str(user.id), app_type=app_type).warning(
            "--- TEMP DAYTONA CONN RESOLUTION DIAG END --- "
            f"AppConnDaytona.get_daytona result conn_set={bool(conn)} "
            f"ownership={ownership} "
            f"daytona_system_key_set={bool(getattr(settings, 'DAYTONA_API_KEY', '') or '')} "
            f"daytona_system_url_set={bool(getattr(settings, 'DAYTONA_API_URL', '') or '')}"
        )
        # --- TEMP LOGS END (DAYTONA CONN RESOLUTION DIAG) ---
        return conn
