import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
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
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationCreate
from src.autobots.integrations.integration_type.intuit_auth import (
    IntuitAuth,
    IntuitAuthModel,
    IntuitCreds,
)
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=f"{SettingsProvider.sget().API_AUTH}{SettingsProvider.sget().API_PATH_INTUIT}",
    tags=[SettingsProvider.sget().API_TAG_EXTERNAL_APP_AUTH],
)

_CALLBACK_PATH = (
    f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}"
    f"{SettingsProvider.sget().API_PATH_INTUIT}/oauth-callback"
)


# https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0


def _intuit_callback_url(ctx: Context, request: Request) -> str:
    return build_backend_callback_url(ctx=ctx, request=request, callback_path=_CALLBACK_PATH)


@router.get("/oauth-url", deprecated=True)
async def oauth_url(
    request: Request,
    name: str,
    # Where to land once the connection is stored; never sent to Intuit.
    redirect_uri: str = "",
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    ctx: Context = request.state.context
    ctx.user_id = str(user_res.user.id)
    post_auth_uri = await post_auth_redirect_uri(ctx=ctx, redirect_uri=redirect_uri)
    user = UserORM(id=user_res.user.id)
    creds_doc = await UserAppCreds.get_app_creds_by_name(ctx, db, user, name=name, app_name=AppTypes.intuit, decrypt=True)
    if not creds_doc:
        raise HTTPException(400, "Intuit credentials not configured")

    creds = IntuitCreds.model_validate(creds_doc.creds)
    # Built by the auth class, so this route and /integrations/connect agree.
    auth_url = IntuitAuth.build_authorize_url(
        ctx=ctx,
        name=name,
        callback_url=_intuit_callback_url(ctx=ctx, request=request),
        post_auth_uri=post_auth_uri,
        app_creds=creds,
    )
    return auth_url

    # Using standard redirect
    # return RedirectResponse(auth_url)


@router.get("/oauth-callback")
async def oauth_redirect(
    request: Request,
    code: str,
    state: str,
    realm_id: str,
    # No bearer token: Intuit redirects the browser here; identity comes from state.
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    ctx: Context = request.state.context
    landing_uri = ""
    try:
        oauth_state = resolve_oauth_state(ctx=ctx, state=state)
        landing_uri = await post_auth_redirect_uri(ctx=ctx, redirect_uri=oauth_state.redirect_uri)
        user = UserORM(id=oauth_state.user_id)
        name = oauth_state.name

        creds_doc = await UserAppCreds.get_app_creds_by_name(ctx, db, user, name=name, app_name=AppTypes.intuit, decrypt=True)
        if not creds_doc:
            raise HTTPException(400, "Intuit credentials not configured")

        creds = IntuitCreds.model_validate(creds_doc.creds)
        auth_client = IntuitAuth.auth_client(environment=creds.environment, callback_url=_intuit_callback_url(ctx=ctx, request=request))

        # Get OAuth2 Bearer token
        auth_client.get_bearer_token(auth_code=code, realm_id=realm_id)

        # retrieve access_token and refresh_token
        oauth_rs = IntuitAuthModel(
            access_token=auth_client.access_token,
            refresh_token=auth_client.refresh_token,
            expires_in=auth_client.expires_in,
            refresh_token_expires_in=auth_client.x_refresh_token_expires_in,
            scope=SettingsProvider.sget().INTUIT_SCOPES,
            token_type="bearer",
            realm_id=realm_id,
        )

        integration_create = IntegrationCreate(
            app_name=AppTypes.intuit, name=name, auth=oauth_rs.model_dump(exclude_none=True)
        )
        _ = await UserIntegration.create(ctx=ctx, db=db, user=user, integration_create=integration_create)
        logger.bind(ctx=ctx).info("Intuit App Auth Created")
        return RedirectResponse(landing_uri, status_code=303)

    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Intuit OAuth callback failed: {e}")
        if not landing_uri:
            # Nowhere safe to send the browser — the state never yielded a destination.
            raise HTTPException(status_code=400, detail=str(e))
        return oauth_error_redirect(ctx=ctx, redirect_uri=landing_uri, error=e)
