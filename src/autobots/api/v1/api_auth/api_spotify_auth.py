import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from starlette.requests import Request

from src.autobots import SettingsProvider
from src.autobots.integrations.integration_model import IntegrationCreate, OAuthModel
from src.autobots.integrations.integration_type.spotify_auth import SpotifyAuth, SpotifyCreds
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.integrations.app_creds.app_creds_model import AppCredsCreate, AppCredsRead
from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds
from src.autobots.integrations.app_types import AppTypes
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(prefix=SettingsProvider.sget().API_AUTH, tags=[SettingsProvider.sget().API_TAG_EXTERNAL_APP_AUTH])


class SpotifyClientCredentialsRequest(BaseModel):
    client_id: str
    client_secret: str


@router.post("/spotify/client-credentials")
async def spotify_client_credentials(
    request: Request,
    spotify_request: SpotifyClientCredentialsRequest,
    name: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    """
    Authenticate using Spotify client credentials flow.
    This is for server-to-server authentication using the Client Credentials Flow.
    """
    ctx = request.state.context
    user = UserORM(**user_res.user.model_dump())

    try:
        spotify_creds = SpotifyCreds(
            client_id=spotify_request.client_id,
            client_secret=spotify_request.client_secret,
        )

        # Use `name` as the unique identifier for stored credentials, rather than `app_name`.
        # This enables multiple Spotify credential sets to coexist (distinguished by `name`).
        app_creds_delete = AppCredsRead(name=name)
        deleted_count = await UserAppCreds.delete_app_creds(
            ctx=ctx, db=db, user=user, app_creds_delete=app_creds_delete
        )
        logger.bind(ctx=ctx).info(
            f"Deleted {deleted_count} old Spotify credentials for name='{name}'"
        )

        app_creds_create = AppCredsCreate(
            app_name=AppTypes.spotify, name=name, creds=spotify_creds.model_dump()
        )
        await UserAppCreds.create_app_creds(
            ctx=ctx, db=db, user=user, app_creds_create=app_creds_create
        )
        logger.bind(ctx=ctx).info("Spotify credentials stored in AppCreds")

        oauth_rs = await SpotifyAuth.create_auth(ctx=ctx, app_creds=spotify_creds)

        if isinstance(oauth_rs, Exception):
            logger.bind(ctx=ctx).error(f"Failed to create Spotify auth: {str(oauth_rs)}")
            raise HTTPException(status_code=400, detail=str(oauth_rs))

        assert isinstance(oauth_rs, OAuthModel)

        integration_create = IntegrationCreate(
            app_name=AppTypes.spotify, name=name, auth=oauth_rs.model_dump(exclude_none=True)
        )
        created_auth = await UserIntegration.create(
            ctx=ctx, db=db, user=user, integration_create=integration_create, decrypt=True
        )
        logger.bind(ctx=ctx).info("Spotify Client Credentials Auth Created")

        return {"message": "Spotify authentication successful", "auth_id": str(created_auth.id)}

    except HTTPException:
        raise
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in Spotify client credentials auth: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
