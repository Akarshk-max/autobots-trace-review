# netsuite_auth_api.py
import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from starlette.requests import Request

from src.autobots import SettingsProvider
from src.autobots.integrations.integration_model import IntegrationCreate
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.integrations.app_types import AppTypes
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(prefix=SettingsProvider.sget().API_AUTH, tags=[SettingsProvider.sget().API_TAG_EXTERNAL_APP_AUTH])


class NetSuiteTBARequest(BaseModel):
    """
    Token-Based Authentication (TBA) for NetSuite SuiteTalk (OAuth 1.0):
    - realm == account_id
    - signature method HMAC-SHA256 (used per-request)
    """

    account_id: str  # realm
    consumer_key: str
    consumer_secret: str
    access_token: str
    token_secret: str
    base_url: str | None = None


@router.post("/netsuite/tba")
async def netsuite_tba_auth(
    request: Request,
    netsuite_request: NetSuiteTBARequest,
    name: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    """
    Unlike OAuth2, NetSuite TBA doesn't exchange credentials for an access token.
      - consumer key/secret (integration)
      - token id/secret (TBA token)
    Auth token will be long lived so it will persists.

    """
    ctx = Context(user_id=str(user_res.user.id))
    try:
        user = UserORM(id=user_res.user.id)
        from src.autobots.integrations.integration_type.netsuite_auth import (
            NetSuiteAuth,
            NetSuiteCreds,
        )

        netsuite_creds = NetSuiteCreds(
            account_id=netsuite_request.account_id,
            consumer_key=netsuite_request.consumer_key,
            consumer_secret=netsuite_request.consumer_secret,
            access_token=netsuite_request.access_token,
            token_secret=netsuite_request.token_secret,
            base_url=netsuite_request.base_url,
        )

        auth_rs = await NetSuiteAuth.create_auth(ctx=ctx, app_creds=netsuite_creds)

        if isinstance(auth_rs, Exception):
            logger.bind(ctx=ctx).error(f"Failed to create NetSuite auth: {str(auth_rs)}")
            raise HTTPException(status_code=400, detail=str(auth_rs))

        # Persist auth as AppAuth doc
        integration_create = IntegrationCreate(
            app_name=AppTypes.netsuite, name=name,
            auth=auth_rs.model_dump(exclude_none=True),
        )

        created_auth = await UserIntegration.create(
            ctx=ctx, db=db, user=user, integration_create=integration_create, decrypt=True
        )

        logger.bind(ctx=ctx).info("NetSuite TBA Auth Created")

        return {"message": "NetSuite authentication saved", "auth_id": str(created_auth.id)}

    except HTTPException:
        raise
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in NetSuite TBA auth: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
