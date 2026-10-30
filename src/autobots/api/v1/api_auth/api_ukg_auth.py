
import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, HttpUrl
from starlette.requests import Request
from starlette.responses import RedirectResponse

from src.autobots import SettingsProvider
from src.autobots.api.v1.api_auth.oauth_state import (
    build_backend_callback_url,
    oauth_error_redirect,
    post_auth_redirect_uri,
    resolve_oauth_state,
)
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.conn.only_auth.ukg.ukg_client import UKGAccessTokenRq
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationCreate, OAuthModel
from src.autobots.integrations.integration_type.ukg_auth import UKGAuth, UKGCreds
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.user.user_orm_model import UserORM

"""
UKG OAuth API Endpoints

NOTE: These OAuth endpoints are OPTIONAL and only needed for Authorization Code Flow.

For most use cases, you should use Client Credentials Flow instead, which is simpler:
1. Create app_creds with client_id, client_secret, and base_url
2. Create app_auth directly (POST /v1/app-auth) - system automatically gets token
3. No need for oauth-url or oauth-callback

These OAuth endpoints are only needed if:
- UKG requires user authorization for specific scopes
- You need user-specific permissions beyond what client credentials provide
"""

router = APIRouter(
    prefix=f"{SettingsProvider.sget().API_AUTH}{SettingsProvider.sget().API_PATH_UKG}",
    tags=[SettingsProvider.sget().API_TAG_EXTERNAL_APP_AUTH],
)

_CALLBACK_PATH = (
    f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}"
    f"{SettingsProvider.sget().API_PATH_UKG}/oauth-callback"
)


class UrlObj(BaseModel):
    url: HttpUrl


def _ukg_callback_url(ctx: Context, request: Request) -> str:
    return build_backend_callback_url(ctx=ctx, request=request, callback_path=_CALLBACK_PATH)


@router.get("/oauth-url", deprecated=True)
async def ukg_oauth_url(
    request: Request,
    name: str,
    # Where to land once the connection is stored; never sent to UKG.
    redirect_uri: str = "",
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> UrlObj:
    """
    Generate UKG OAuth authorization URL.
    """
    ctx: Context = request.state.context
    ctx.user_id = str(user_res.user.id)
    post_auth_uri = await post_auth_redirect_uri(ctx=ctx, redirect_uri=redirect_uri)
    user = UserORM(id=user_res.user.id)

    # Get user's UKG credentials (which includes the customer-specific base_url)
    app_creds_doc = await UserAppCreds.get_app_creds_by_name(
        ctx=ctx, db=db, user=user, name=name, app_name=AppTypes.ukg, decrypt=True
    )

    if not app_creds_doc or not app_creds_doc.creds:
        raise HTTPException(
            status_code=400,
            detail="UKG credentials not found. Please configure your UKG base_url in app credentials first.",
        )

    app_creds = UKGCreds.model_validate(app_creds_doc.creds)

    if not app_creds.base_url:
        raise HTTPException(
            status_code=400,
            detail="UKG base_url is required. Please configure your customer-specific UKG URL.",
        )

    if not app_creds.client_id or not app_creds.client_secret:
        raise HTTPException(
            status_code=400,
            detail="UKG client_id and client_secret are required. Please configure your UKG credentials.",
        )
    # Built by the auth class, so this route and /integrations/connect agree.
    oauth_url = UKGAuth.build_authorize_url(
        ctx=ctx,
        name=name,
        callback_url=_ukg_callback_url(ctx=ctx, request=request),
        post_auth_uri=post_auth_uri,
        app_creds=UKGCreds.model_validate(app_creds_doc.creds),
    )

    return UrlObj(url=HttpUrl(oauth_url))


@router.get("/oauth-callback")
async def ukg_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    # No bearer token: UKG redirects the browser here; identity comes from state.
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    """
    Handle UKG OAuth callback after user authorization.

    This endpoint receives the authorization code from UKG and exchanges it
    for an access token, then stores it in the user's app_auth.
    """
    ctx: Context = request.state.context
    landing_uri = ""
    try:
        oauth_state = resolve_oauth_state(ctx=ctx, state=state)
        landing_uri = await post_auth_redirect_uri(ctx=ctx, redirect_uri=oauth_state.redirect_uri)
        user = UserORM(id=oauth_state.user_id)
        name = oauth_state.name

        if error:
            raise HTTPException(status_code=400, detail=error)

        if not code:
            raise HTTPException(status_code=400, detail="Authorization code not provided")

        # Get user's UKG credentials to retrieve base_url
        app_creds_doc = await UserAppCreds.get_app_creds_by_name(
            ctx=ctx, db=db, user=user, name=name, app_name=AppTypes.ukg, decrypt=True
        )

        if not app_creds_doc or not app_creds_doc.creds:
            raise HTTPException(
                status_code=400,
                detail="UKG credentials not found. Please configure your UKG base_url first.",
            )

        app_creds = UKGCreds.model_validate(app_creds_doc.creds)

        if not app_creds.base_url:
            raise HTTPException(status_code=400, detail="UKG base_url is required in credentials.")

        if not app_creds.client_id or not app_creds.client_secret:
            raise HTTPException(
                status_code=400,
                detail="UKG client_id and client_secret are required in credentials.",
            )

        # Exchange authorization code for access token

        token_rq = UKGAccessTokenRq(
            client_id=app_creds.client_id,
            client_secret=app_creds.client_secret,
            code=code,
            redirect_uri=_ukg_callback_url(ctx=ctx, request=request),
            base_url=app_creds.base_url,
        )

        oauth_rs = await UKGAuth.create_auth(ctx=ctx, app_creds=token_rq)

        if isinstance(oauth_rs, Exception):
            raise HTTPException(status_code=400, detail=str(oauth_rs))

        assert isinstance(oauth_rs, OAuthModel)

        # Store the OAuth tokens
        integration_create = IntegrationCreate(
            app_name=AppTypes.ukg, name=name, auth=oauth_rs.model_dump(exclude_none=True)
        )

        created_auth = await UserIntegration.create(
            ctx=ctx, db=db, user=user, integration_create=integration_create, decrypt=True
        )

        logger.bind(ctx=ctx, app_name=AppTypes.ukg, id=created_auth.id).info(
            "UKG App Auth Created"
        )
        return RedirectResponse(landing_uri, status_code=303)

    except Exception as e:
        logger.bind(ctx=ctx, app_name="ukg").exception(f"Error in UKG oauth callback: {str(e)}")
        if not landing_uri:
            # Nowhere safe to send the browser — the state never yielded a destination.
            raise HTTPException(status_code=400, detail=str(e))
        return oauth_error_redirect(ctx=ctx, redirect_uri=landing_uri, error=e)
