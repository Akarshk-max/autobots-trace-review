
import slack_sdk
import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, HttpUrl
from slack_sdk.web import SlackResponse
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
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationCreate
from src.autobots.integrations.integration_type.slack_auth import SlackAuth
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.user.user_orm_model import UserORM

SLACK_USER_NAME_SUFFIX = "_user"

router = APIRouter(
    prefix=f"{SettingsProvider.sget().API_AUTH}{SettingsProvider.sget().API_PATH_SLACK}",
    tags=[SettingsProvider.sget().API_TAG_EXTERNAL_APP_AUTH],
)

_CALLBACK_PATH = SlackAuth.oauth_callback_path()


class UrlObj(BaseModel):
    url: HttpUrl


def _slack_callback_url(ctx: Context, request: Request) -> str:
    return build_backend_callback_url(ctx=ctx, request=request, callback_path=_CALLBACK_PATH)


@router.get("/oauth-url", deprecated=True)
async def slack_oauth_url(
    request: Request,
    name: str,
    # Where to land once the connection is stored; never sent to Slack.
    redirect_uri: str = "",
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
) -> UrlObj:
    ctx: Context = request.state.context
    ctx.user_id = str(user_res.user.id)
    post_auth_uri = await post_auth_redirect_uri(ctx=ctx, redirect_uri=redirect_uri)
    # Built by the auth class, so this route and /integrations/connect agree.
    oauth_url = SlackAuth.build_authorize_url(
        ctx=ctx,
        name=name,
        callback_url=_slack_callback_url(ctx=ctx, request=request),
        post_auth_uri=post_auth_uri,
    )
    return UrlObj(url=HttpUrl(oauth_url))


# https://api.slack.com/methods/oauth.v2.access
@router.get("/oauth-callback")
async def slack_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    # No bearer token: Slack redirects the browser here; identity comes from state.
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
            raise HTTPException(status_code=400, detail="Missing 'code'")

        client = slack_sdk.WebClient(token=SettingsProvider.sget().SLACK_USER_OAUTH_TOKEN)
        oauth_rs: SlackResponse = client.oauth_v2_access(
            client_id=SettingsProvider.sget().SLACK_CLIENT_ID,
            client_secret=SettingsProvider.sget().SLACK_CLIENT_SECRET,
            code=code,
            redirect_uri=_slack_callback_url(ctx=ctx, request=request),
        )

        assert isinstance(oauth_rs, SlackResponse)
        integration_create = IntegrationCreate(app_name=AppTypes.slack, name=name, auth=oauth_rs.data)
        app_auth_create_user = IntegrationCreate(app_name=AppTypes.slack_user, name=f"{name}{SLACK_USER_NAME_SUFFIX}", auth=oauth_rs.data)
        slack_auth = await UserIntegration.create(
            ctx=ctx, db=db, user=user, integration_create=integration_create, decrypt=True
        )
        slack_auth_user = await UserIntegration.create(
            ctx=ctx, db=db, user=user, integration_create=app_auth_create_user, decrypt=True
        )
        logger.bind(ctx=ctx, app_name=slack_auth.app_name, id=slack_auth.id).info(
            "Slack App Auth Created"
        )
        logger.bind(ctx=ctx, app_name=slack_auth_user.app_name, id=slack_auth_user.id).info(
            "Slack User App Auth Created"
        )
        return RedirectResponse(landing_uri, status_code=303)

    except Exception as e:
        logger.bind(ctx=ctx, app_name="slack").exception(f"Error in slack oauth callback: {str(e)}")
        if not landing_uri:
            # Nowhere safe to send the browser — the state never yielded a destination.
            raise HTTPException(status_code=400, detail=str(e))
        return oauth_error_redirect(ctx=ctx, redirect_uri=landing_uri, error=e)
