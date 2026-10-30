import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, HttpUrl
from starlette.requests import Request

from src.autobots import SettingsProvider
from src.autobots.integrations.integration_model import IntegrationCreate, OAuthModel
from src.autobots.integrations.integration_type.looker_auth import LookerAuth
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.integrations.app_types import AppTypes
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(prefix=SettingsProvider.sget().API_AUTH, tags=[SettingsProvider.sget().API_TAG_EXTERNAL_APP_AUTH])


class UrlObj(BaseModel):
    url: HttpUrl


class LookerClientCredentialsRequest(BaseModel):
    base_url: str
    client_id: str
    client_secret: str


# Client Credentials Flow Endpoint
@router.post("/looker/client-credentials")
async def looker_client_credentials(
    request: Request,
    looker_request: LookerClientCredentialsRequest,
    name: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    """
    Authenticate using Looker client credentials flow.
    This is for server-to-server authentication using the /api/4.0/login endpoint.
    """
    ctx = request.state.context
    try:
        user = UserORM(id=user_res.user.id)

        # Create credentials object for LookerAuth
        from src.autobots.integrations.integration_type.looker_auth import LookerCreds

        looker_creds = LookerCreds(
            base_url=looker_request.base_url,
            client_id=looker_request.client_id,
            client_secret=looker_request.client_secret,
        )

        # Create auth using client credentials
        oauth_rs = await LookerAuth.create_auth(ctx=ctx, app_creds=looker_creds)

        if isinstance(oauth_rs, Exception):
            logger.bind(ctx=ctx).error(f"Failed to create Looker auth: {str(oauth_rs)}")
            raise HTTPException(status_code=400, detail=str(oauth_rs))

        assert isinstance(oauth_rs, OAuthModel)
        integration_create = IntegrationCreate(
            app_name=AppTypes.looker, name=name, auth=oauth_rs.model_dump(exclude_none=True)
        )

        created_auth = await UserIntegration.create(
            ctx=ctx, db=db, user=user, integration_create=integration_create, decrypt=True
        )
        logger.bind(ctx=ctx).info("Looker Client Credentials Auth Created")

        return {"message": "Looker authentication successful", "auth_id": str(created_auth.id)}

    except HTTPException:
        raise
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in Looker client credentials auth: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
