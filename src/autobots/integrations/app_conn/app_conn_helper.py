from typing import List

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.data_model.context import Context
from src.autobots.data_model.ownership import Ownership
from src.autobots.integrations.app_conn_rule.user_app_conn_rule import UserAppConnRule
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationDoc
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.user.user_orm_model import UserORM


class AppConnHelper:
    @staticmethod
    async def get_ownership(
        ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, app_type: AppTypes
    ) -> List[Ownership]:
        ownership: List[Ownership] = await UserAppConnRule.get_ownership(
            ctx=ctx, db=db, user=user, app_type=app_type
        )
        logger.bind(ctx=ctx, app_type=app_type, ownership=ownership).info(
            f"Get app conn ownership for {app_type}"
        )
        return ownership

    @staticmethod
    async def get_app_auth(
        ctx: Context, db: AsyncIOMotorDatabase, user: UserORM, app_type: AppTypes
    ) -> IntegrationDoc | None:
        try:
            integration_doc: IntegrationDoc | None = await UserIntegration.get_by_app_name(
                ctx=ctx, db=db, user=user, app_name=app_type, decrypt=True
            )
            if integration_doc is None:
                logger.bind(ctx=ctx, app_type=app_type).debug(
                    f"App auth doc not found for {app_type}"
                )
                raise HTTPException(
                    status_code=404, detail=f"App auth doc not found for {app_type}"
                )
            return integration_doc
        except HTTPException:
            raise
        except Exception as e:
            logger.bind(ctx=ctx, app_type=app_type, error=str(e)).error(
                f"Cannot get app auth for {app_type}: {str(e)}"
            )
            raise
