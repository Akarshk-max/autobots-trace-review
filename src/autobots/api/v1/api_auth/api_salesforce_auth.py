
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
from src.autobots.conn.only_auth.salesforce.salesforce_client import (
    SalesforceAccessTokenRq,
)
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationCreate, OAuthModel
from src.autobots.integrations.integration_type.salesforce_auth import (
    SalesforceAuth,
    SalesforceCreds,
)
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(prefix=SettingsProvider.sget().API_AUTH, tags=[SettingsProvider.sget().API_TAG_EXTERNAL_APP_AUTH])

_CALLBACK_PATH = (
    f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}/salesforce/oauth-callback"
)


class UrlObj(BaseModel):
    url: HttpUrl


def _salesforce_callback_url(ctx: Context, request: Request) -> str:
    return build_backend_callback_url(ctx=ctx, request=request, callback_path=_CALLBACK_PATH)


@router.get("/salesforce/oauth-url", deprecated=True)
async def salesforce_oauth_url(
    request: Request,
    name: str,
    # Where to land once the connection is stored; never sent to Salesforce.
    redirect_uri: str = "",
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> UrlObj:
    ctx: Context = request.state.context
    ctx.user_id = str(user_res.user.id)
    post_auth_uri = await post_auth_redirect_uri(ctx=ctx, redirect_uri=redirect_uri)

    user = UserORM(id=user_res.user.id)
    app_creds_doc = await UserAppCreds.get_app_creds_by_name(
        ctx=ctx, db=db, user=user, name=name, app_name=AppTypes.salesforce, decrypt=True
    )

    # Built by the auth class, so this route and /integrations/connect agree.
    oauth_url = SalesforceAuth.build_authorize_url(
        ctx=ctx,
        name=name,
        callback_url=_salesforce_callback_url(ctx=ctx, request=request),
        post_auth_uri=post_auth_uri,
        app_creds=SalesforceCreds.model_validate(app_creds_doc.creds),
    )
    return UrlObj(url=HttpUrl(oauth_url))


@router.get("/salesforce/oauth-callback")
async def salesforce_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    # No bearer token: Salesforce redirects the browser here; identity comes from state.
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
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
            raise HTTPException(status_code=400, detail="Missing authorization code")

        app_creds_doc = await UserAppCreds.get_app_creds_by_name(
            ctx=ctx, db=db, user=user, name=name, app_name=AppTypes.salesforce, decrypt=True
        )
        settings = SettingsProvider.sget()

        token_rq = SalesforceAccessTokenRq(
            client_id=settings.SALESFORCE_CLIENT_ID,
            client_secret=settings.SALESFORCE_CLIENT_SECRET,
            # Salesforce validates this against the connected app's Callback URL exactly.
            redirect_uri=_salesforce_callback_url(ctx=ctx, request=request),
            code=code,
        )
        instance_url = app_creds_doc.creds["instance_url"]
        is_sandbox = "sandbox" in instance_url
        oauth_rs = await SalesforceAuth.create_auth(
            ctx=ctx,
            app_creds=token_rq,
            is_sandbox=is_sandbox,
            instance_url=instance_url,
        )
        if isinstance(oauth_rs, Exception):
            raise HTTPException(status_code=400, detail=str(oauth_rs))

        assert isinstance(oauth_rs, OAuthModel)
        await UserIntegration.create(
            ctx=ctx,
            db=db,
            user=user,
            integration_create=IntegrationCreate(
                app_name=AppTypes.salesforce, name=name,
                auth=oauth_rs.model_dump(exclude_none=True),
            ),
        )
        logger.bind(ctx=ctx).info("Salesforce App Auth Created")
        return RedirectResponse(landing_uri, status_code=303)

    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Salesforce OAuth callback failed: {e}")
        if not landing_uri:
            # Nowhere safe to send the browser — the state never yielded a destination.
            raise HTTPException(status_code=400, detail=str(e))
        return oauth_error_redirect(ctx=ctx, redirect_uri=landing_uri, error=e)
