from typing import List

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.conn.unstructured_io.unstructured_io import UnstructuredIO, get_unstructured_io
from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.data_model.ownership import Ownership
from src.autobots.integrations.app_conn.app_conn_helper import AppConnHelper
from src.autobots.integrations.app_type_to_integration_class import AppTypeToIntegrationClass
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationDoc
from src.autobots.integrations.integration_type.unstructured_io_auth import (
    UnstructuredIOAuthModel,
)
from src.autobots.user.user_orm_model import UserORM


class AppConnUnstructuredIO:
    @staticmethod
    async def get_unstructured_io_conn(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> UnstructuredIO | None:
        app_type = AppTypes.unstructuredio
        conn: UnstructuredIO | None = None
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
                        app_auth: UnstructuredIOAuthModel = (
                            app_auth_class.get_auth_model_type().model_validate(integration_doc.auth)
                        )
                        api_key = app_auth.api_key
                        api_uri = app_auth.api_uri

                        conn: UnstructuredIO = get_unstructured_io(
                            ctx=ctx, user=user, api_key=api_key, api_uri=api_uri
                        )
                    case Ownership.system:
                        api_key = settings.UNSTRUCTURED_API_KEY
                        api_uri = settings.UNSTRUCTURED_URL
                        conn: UnstructuredIO = get_unstructured_io(
                            ctx=ctx, user=user, api_key=api_key, api_uri=api_uri
                        )
                    case _:
                        logger.bind(ctx=ctx, app_type=app_type, owner=owner).error(
                            f"Cannot get app conn unstructuredio for apptype: {app_type} and owner: {owner}"
                        )
                if conn:
                    logger.bind(ctx=ctx, app_type=app_type, owner=owner).info(
                        f"Got app conn unstructuredio for apptype: {app_type} and owner: {owner}"
                    )
                    break
            except Exception as e:
                logger.bind(ctx=ctx, app_type=app_type, owner=owner, error=str(e)).exception(
                    "Cannot get app conn unstructuredio"
                )
                continue

        if conn is None and settings.ENV in ["local", "test"]:
            # TODO: We should remove this logic in future and instead create test users with system key access
            logger.bind(ctx=ctx).warning(
                f"AppConn: No ownership found for {user.id}. "
                f"Falling back to SYSTEM keys for UnstructuredIO (ENV={settings.ENV})."
            )
            conn = get_unstructured_io(
                ctx=ctx,
                user=user,
                api_key=settings.UNSTRUCTURED_API_KEY,
                api_uri=settings.UNSTRUCTURED_URL,
            )
        return conn
