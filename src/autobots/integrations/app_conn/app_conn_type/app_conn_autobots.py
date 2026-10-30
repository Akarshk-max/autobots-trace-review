import httpx
from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.data_model.ownership import Ownership
from src.autobots.integrations.app_conn.app_conn_helper import AppConnHelper
from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds
from src.autobots.integrations.app_type_to_integration_class import AppTypeToIntegrationClass
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationDoc, IntegrationUpdate
from src.autobots.integrations.integration_type.autobots_auth import AutobotsAuthModel
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.user.user_orm_model import UserORM


class AppConnAutobots:

    @staticmethod
    async def get_autobots(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> httpx.AsyncClient:
        app_type = AppTypes.autobots
        ownership = await AppConnHelper.get_ownership(
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
                        app_auth: AutobotsAuthModel = (
                            app_auth_class.get_auth_model_type().model_validate(integration_doc.auth)
                        )

                        is_refresh = await app_auth_class.is_refresh_required(ctx=ctx, app_auth=app_auth)
                        if is_refresh:
                            app_creds_doc = await UserAppCreds.get_app_creds_by_app_name(
                                ctx=ctx, db=db, user=user, app_name=app_type, decrypt=True
                            )
                            app_creds = (
                                app_auth_class.get_creds_model_type().model_validate(app_creds_doc.creds)
                                if app_creds_doc
                                else None
                            )
                            refreshed_auth = await app_auth_class.refresh_auth(
                                ctx=ctx, app_creds=app_creds, app_auth=app_auth
                            )
                            if not isinstance(refreshed_auth, Exception):
                                integration_update = IntegrationUpdate(
                                    name=integration_doc.name,
                                    app_name=integration_doc.app_name,
                                    auth=refreshed_auth.model_dump(exclude_none=True),
                                )
                                new_app_auth_doc = await UserIntegration.update(
                                    ctx=ctx, db=db, user=user,
                                    id=integration_doc.id,
                                    integration_update=integration_update,
                                    decrypt=True,
                                )
                                app_auth = app_auth_class.get_auth_model_type().model_validate(
                                    new_app_auth_doc.auth
                                )
                                logger.bind(ctx=ctx).info("Autobots token refreshed")

                        base_url = await app_auth_class.get_api_domain(None)
                        client = httpx.AsyncClient(
                            base_url=base_url,
                            headers={"Authorization": f"Bearer {app_auth.access_token}"},
                            timeout=30.0,
                        )
                        logger.bind(ctx=ctx, owner=owner).info("Got autobots conn (user)")
                        return client

                    case Ownership.system:
                        logger.bind(ctx=ctx, owner=owner).warning(
                            "Skipping system Autobots connection: Autobots must use user-owned auth"
                        )
                        continue

                    case _:
                        logger.bind(ctx=ctx, owner=owner).error(
                            f"Cannot get autobots conn for owner: {owner}"
                        )
            except Exception as e:
                logger.bind(ctx=ctx, owner=owner, error=str(e)).error(
                    "Cannot get autobots conn"
                )
                continue

        raise HTTPException(
            status_code=404,
            detail="Autobots is not configured. Please add your autobots credentials in App Creds and sync App Auth.",
        )
