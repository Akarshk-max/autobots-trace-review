# api_meta_auth.py

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
from src.autobots.conn.only_auth.meta.meta import MetaAccessTokenRq
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationCreate
from src.autobots.integrations.integration_type.meta_auth import MetaAuth
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(prefix=SettingsProvider.sget().API_AUTH, tags=[SettingsProvider.sget().API_TAG_EXTERNAL_APP_AUTH])

_CALLBACK_PATH = MetaAuth.oauth_callback_path()


class UrlObj(BaseModel):
    url: HttpUrl


def _meta_callback_url(ctx: Context, request: Request) -> str:
    return build_backend_callback_url(ctx=ctx, request=request, callback_path=_CALLBACK_PATH)


@router.get("/meta/oauth-url", deprecated=True)
async def meta_oauth_url(
    request: Request,
    name: str,
    # Where to land once the connection is stored; never sent to Meta.
    redirect_uri: str = "",
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
) -> UrlObj:
    ctx: Context = request.state.context
    ctx.user_id = str(user_res.user.id)
    post_auth_uri = await post_auth_redirect_uri(ctx=ctx, redirect_uri=redirect_uri)
    logger.info(f"Meta OAuth URL requested for user: {user_res.user.id}")
    # Built by the auth class, so this route and /integrations/connect agree.
    oauth_url = MetaAuth.build_authorize_url(
        ctx=ctx,
        name=name,
        callback_url=_meta_callback_url(ctx=ctx, request=request),
        post_auth_uri=post_auth_uri,
    )
    return UrlObj(url=HttpUrl(oauth_url))


@router.get("/meta/oauth-callback")
async def meta_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    # No bearer token: Meta redirects the browser here; identity comes from state.
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    ctx: Context = request.state.context
    landing_uri = ""
    try:
        oauth_state = resolve_oauth_state(ctx=ctx, state=state)
        landing_uri = await post_auth_redirect_uri(ctx=ctx, redirect_uri=oauth_state.redirect_uri)
        user = UserORM(id=oauth_state.user_id)
        logger.bind(ctx=ctx).info(f"Meta OAuth callback received: error={error}")

        if error:
            raise HTTPException(status_code=400, detail=error)
        if not code:
            raise HTTPException(status_code=400, detail="Missing 'code'")

        token_rq = MetaAccessTokenRq(
            client_id=SettingsProvider.sget().META_CLIENT_ID,
            client_secret=SettingsProvider.sget().META_CLIENT_SECRET,
            redirect_uri=_meta_callback_url(ctx=ctx, request=request),
            code=code,
        )

        oauth_rs = await MetaAuth.create_auth(ctx=ctx, app_creds=token_rq)
        if isinstance(oauth_rs, Exception):
            # bubble up Meta's error
            raise HTTPException(status_code=400, detail=str(oauth_rs))

        integration_create = IntegrationCreate(
            app_name=AppTypes.meta,
            name=oauth_state.name,
            auth=oauth_rs.model_dump(exclude_none=True),
        )
        await UserIntegration.create(ctx=ctx, db=db, user=user, integration_create=integration_create)
        logger.bind(ctx=ctx).info("Meta App Auth Created")
        # 303 so browsers don't re-POST if any
        return RedirectResponse(landing_uri, status_code=303)

    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Meta OAuth callback failed: {e}")
        if not landing_uri:
            # Nowhere safe to send the browser — the state never yielded a destination.
            raise HTTPException(status_code=400, detail=str(e))
        return oauth_error_redirect(ctx=ctx, redirect_uri=landing_uri, error=e)
