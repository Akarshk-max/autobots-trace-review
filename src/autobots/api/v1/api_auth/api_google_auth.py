
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
from src.autobots.conn.only_auth.google.google import GoogleAccessTokenRq
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationCreate, OAuthModel
from src.autobots.integrations.integration_type.google_auth import GoogleAuth
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(prefix=SettingsProvider.sget().API_AUTH, tags=[SettingsProvider.sget().API_TAG_EXTERNAL_APP_AUTH])

_CALLBACK_PATH = GoogleAuth.oauth_callback_path()


# https://developers.google.com/identity/protocols/oauth2/web-server#httprest


class UrlObj(BaseModel):
    url: HttpUrl


def _google_callback_url(ctx: Context, request: Request) -> str:
    return build_backend_callback_url(ctx=ctx, request=request, callback_path=_CALLBACK_PATH)


@router.get("/google/oauth-url", deprecated=True)
async def google_oauth_url(
    request: Request,
    name: str,
    # Where to land once the connection is stored; never sent to Google.
    redirect_uri: str = "",
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
) -> UrlObj:
    ctx: Context = request.state.context
    ctx.user_id = str(user_res.user.id)
    post_auth_uri = await post_auth_redirect_uri(ctx=ctx, redirect_uri=redirect_uri)
    # Built by the auth class, so this route and /integrations/connect agree.
    oauth_url = GoogleAuth.build_authorize_url(
        ctx=ctx,
        name=name,
        callback_url=_google_callback_url(ctx=ctx, request=request),
        post_auth_uri=post_auth_uri,
    )
    return UrlObj(url=HttpUrl(oauth_url))


@router.get("/google/oauth-callback")
async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    # No bearer token: Google redirects the browser here; identity comes from state.
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    ctx: Context = request.state.context
    landing_uri = ""
    try:
        oauth_state = resolve_oauth_state(ctx=ctx, state=state)
        landing_uri = await post_auth_redirect_uri(ctx=ctx, redirect_uri=oauth_state.redirect_uri)
        user = UserORM(id=oauth_state.user_id)

        if error:
            raise HTTPException(status_code=400, detail=error)
        if not code:
            raise HTTPException(status_code=400, detail="Missing 'code'")

        client_id = SettingsProvider.sget().GOOGLE_CLIENT_ID
        client_secret = SettingsProvider.sget().GOOGLE_CLIENT_SECRET
        scopes = SettingsProvider.sget().GOOGLE_SCOPES
        callback_uri = _google_callback_url(ctx=ctx, request=request)

        token_rq = GoogleAccessTokenRq(
            client_id=client_id,
            client_secret=client_secret,
            scope=scopes,
            redirect_uri=callback_uri,
            code=code,
        )
        logger.bind(ctx=ctx).info(
            f"Google token exchange: redirect_uri={callback_uri}, "
            f"scope={scopes}, "
            f"code_length={len(code)}, "
            f"name={oauth_state.name}"
        )
        oauth_rs = await GoogleAuth.create_auth(ctx=ctx, app_creds=token_rq)
        if isinstance(oauth_rs, Exception):
            logger.bind(ctx=ctx).error(
                f"Google token exchange failed: {oauth_rs}, redirect_uri_used={callback_uri}"
            )
            raise HTTPException(status_code=400, detail=str(oauth_rs))

        assert isinstance(oauth_rs, OAuthModel)
        integration_create = IntegrationCreate(
            app_name=AppTypes.google,
            name=oauth_state.name,
            auth=oauth_rs.model_dump(exclude_none=True),
        )
        _ = await UserIntegration.create(ctx=ctx, db=db, user=user, integration_create=integration_create)
        logger.bind(ctx=ctx).info("Google App Auth Created")
        return RedirectResponse(landing_uri, status_code=303)

    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Google OAuth callback failed: {e}")
        if not landing_uri:
            # Nowhere safe to send the browser — the state never yielded a destination.
            raise HTTPException(status_code=400, detail=str(e))
        return oauth_error_redirect(ctx=ctx, redirect_uri=landing_uri, error=e)
