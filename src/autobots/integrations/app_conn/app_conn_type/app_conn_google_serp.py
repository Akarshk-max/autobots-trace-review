from typing import List

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.conn.serp.serp import Serp, get_serp_client
from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.data_model.ownership import Ownership
from src.autobots.integrations.app_conn.app_conn_helper import AppConnHelper
from src.autobots.integrations.app_type_to_integration_class import AppTypeToIntegrationClass
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationDoc
from src.autobots.integrations.integration_type.google_serp_auth import GoogleSerpAuthModel
from src.autobots.user.user_orm_model import UserORM


class AppConnGoogleSerp:
    @staticmethod
    async def get_serp(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> Serp | None:
        app_type = AppTypes.google_serp
        conn: Serp | None = None
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
                        app_auth: GoogleSerpAuthModel = (
                            app_auth_class.get_auth_model_type().model_validate(integration_doc.auth)
                        )
                        api_key = app_auth.api_key
                        conn: Serp = get_serp_client(ctx=ctx, user=user, api_key=api_key)
                    case Ownership.system:
                        api_key = settings.SERP_API_KEY
                        conn: Serp = get_serp_client(ctx=ctx, user=user, api_key=api_key)
                    case _:
                        logger.bind(ctx=ctx, app_type=app_type, owner=owner).error(
                            f"Cannot get app conn serp for apptype: {app_type} and owner: {owner}"
                        )
                if conn:
                    logger.bind(ctx=ctx, app_type=app_type, owner=owner).info(
                        f"Got app conn serp for apptype: {app_type} and owner: {owner}"
                    )
                    break
            except Exception as e:
                logger.bind(ctx=ctx, app_type=app_type, owner=owner, error=str(e)).exception(
                    "Cannot get app conn serp"
                )
                continue
        return conn
