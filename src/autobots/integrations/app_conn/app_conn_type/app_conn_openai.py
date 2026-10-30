from typing import List

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.conn.openai.openai_client import OpenAI, get_conn_openai, get_openai
from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.data_model.ownership import Ownership
from src.autobots.integrations.app_conn.app_conn_helper import AppConnHelper
from src.autobots.integrations.app_type_to_integration_class import AppTypeToIntegrationClass
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationDoc
from src.autobots.integrations.integration_type.openai_auth import OpenaiAuthModel
from src.autobots.user.user_orm_model import UserORM


class AppConnOpenAI:
    @staticmethod
    async def get_openai_provider_openai(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> OpenAI:
        app_type = AppTypes.openai
        conn: OpenAI | None = None
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
                        if not integration_doc:
                            raise HTTPException(status_code=404, detail="App auth doc not found")
                        app_auth_class = AppTypeToIntegrationClass.get(app_type)
                        app_auth: OpenaiAuthModel = (
                            app_auth_class.get_auth_model_type().model_validate(integration_doc.auth)
                        )
                        org_id = app_auth.org_id
                        api_key = app_auth.api_key
                        conn: OpenAI = get_conn_openai(ctx=ctx, api_key=api_key, org_id=org_id)
                    case Ownership.system:
                        org_id = settings.OPENAI_ORG_ID
                        api_key = settings.OPENAI_API_KEY
                        conn: OpenAI = get_conn_openai(ctx=ctx, api_key=api_key, org_id=org_id)
                    case _:
                        logger.bind(ctx=ctx, app_type=app_type, owner=owner).error(
                            f"Cannot get app conn openai for apptype: {app_type} and owner: {owner}"
                        )
                if conn:
                    logger.bind(ctx=ctx, app_type=app_type, owner=owner).info(
                        f"Got app conn openai for apptype: {app_type} and owner: {owner}"
                    )
                    break
            except HTTPException as e:
                if e.status_code == 404:
                    logger.bind(ctx=ctx, app_type=app_type, owner=owner).debug(
                        "App conn openai not configured for this owner, trying next"
                    )
                else:
                    logger.bind(ctx=ctx, app_type=app_type, owner=owner, error=str(e)).error(
                        "Cannot get app conn openai"
                    )
                continue
            except Exception as e:
                logger.bind(ctx=ctx, app_type=app_type, owner=owner, error=str(e)).error(
                    "Cannot get app conn openai"
                )
                continue
        return conn

    @staticmethod
    async def get_openai_provider_azure(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> OpenAI:
        conn: OpenAI | None = None
        if SettingsProvider.is_system_email(user.email):
            conn = get_openai(provider="azure")
        else:
            logger.bind(ctx=ctx).error(
                "Cant get openai provider azure for email outside configured system domain"
            )
        return conn
