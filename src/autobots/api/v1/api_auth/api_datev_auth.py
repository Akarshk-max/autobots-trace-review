import base64
import hashlib
import os

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
from src.autobots.auth.security import get_offline_user_from_access_token  # used by /oauth-url
from src.autobots.conn.only_auth.datev.datev_client import Datev, DatevAccessTokenRq, DatevCreds
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationCreate, OAuthModel
from src.autobots.integrations.integration_type.datev_auth import DatevAuth
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=f"{SettingsProvider.sget().API_AUTH}{SettingsProvider.sget().API_PATH_DATEV}",
    tags=[SettingsProvider.sget().API_TAG_EXTERNAL_APP_AUTH],
)


_CALLBACK_PATH = (
    f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}"
    f"{SettingsProvider.sget().API_PATH_DATEV}/oauth-callback"
)


class UrlObj(BaseModel):
    url: HttpUrl


def _datev_callback_url(ctx: Context, request: Request) -> str:
    return build_backend_callback_url(ctx=ctx, request=request, callback_path=_CALLBACK_PATH)


def _generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using S256 method."""
    code_verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


@router.get("/oauth-url", deprecated=True)
async def datev_oauth_url(
    request: Request,
    name: str,
    # Where to land once the connection is stored; never sent to DATEV.
    redirect_uri: str = "",
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> UrlObj:
    """
    Generate DATEV OAuth authorization URL with PKCE (S256).
    """
    ctx: Context = request.state.context
    ctx.user_id = str(user_res.user.id)
    post_auth_uri = await post_auth_redirect_uri(ctx=ctx, redirect_uri=redirect_uri)
    user = UserORM(**user_res.user.model_dump())

    app_creds_doc = await UserAppCreds.get_app_creds_by_name(
        ctx=ctx, db=db, user=user, name=name, app_name=AppTypes.datev, decrypt=True
    )

    if not app_creds_doc or not app_creds_doc.creds:
        raise HTTPException(
            status_code=400,
            detail="DATEV credentials not found. Configure client_id, client_secret, and environment in app_creds.",
        )

    app_creds = DatevCreds.model_validate(app_creds_doc.creds)

    if not app_creds.client_id or not app_creds.client_secret:
        raise HTTPException(
            status_code=400,
            detail="DATEV client_id and client_secret are required.",
        )

    # Built by the auth class: it mints the PKCE verifier and carries it in the state,
    # which the callback needs to complete the exchange.
    callback_uri = _datev_callback_url(ctx=ctx, request=request)
    oauth_url = DatevAuth.build_authorize_url(
        ctx=ctx,
        name=name,
        callback_url=callback_uri,
        post_auth_uri=post_auth_uri,
        app_creds=app_creds,
    )

    logger.bind(ctx=ctx, datev_environment=app_creds.environment).info(
        f"DATEV oauth-url generated: client_id={Datev.redact(app_creds.client_id)} "
        f"environment={app_creds.environment} redirect_uri={callback_uri}"
    )

    return UrlObj(url=HttpUrl(oauth_url))


@router.get("/debug")
async def datev_debug(
    request: Request,
    name: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    """Diagnostic endpoint — shows resolved DATEV config and validates credentials
    are present, without initiating or completing any OAuth flow."""
    ctx: Context = request.state.context
    ctx.user_id = str(user_res.user.id)
    user = UserORM(**user_res.user.model_dump())

    app_creds_doc = await UserAppCreds.get_app_creds_by_name(
        ctx=ctx, db=db, user=user, name=name, app_name=AppTypes.datev, decrypt=True
    )

    if not app_creds_doc or not app_creds_doc.creds:
        return {"status": "error", "detail": "No DATEV app_creds found for this user"}

    app_creds = DatevCreds.model_validate(app_creds_doc.creds)

    # Report the URI we would actually send DATEV — the point of this endpoint is to
    # show the resolved config.
    redirect_uri = _datev_callback_url(ctx=ctx, request=request)
    settings_scopes = SettingsProvider.sget().DATEV_SCOPES
    selected_scope_input, scope_source = Datev.choose_scope_input(settings_scopes, app_creds.scopes)
    scope, scope_debug = Datev.normalize_scopes(selected_scope_input)

    return {
        "status": "ok",
        "user_id": str(user_res.user.id),
        "environment": app_creds.environment,
        "client_id": app_creds.client_id,
        "client_secret_present": bool(app_creds.client_secret),
        "client_secret_length": len(app_creds.client_secret or ""),
        "redirect_uri": redirect_uri,
        "scope": scope,
        "authorize_url": Datev.get_authorize_url(app_creds.environment),
        "token_url": Datev.get_token_url(app_creds.environment),
    }


@router.get("/oauth-callback")
async def datev_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    # No bearer token: DATEV redirects the browser here; identity comes from state.
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    """Handle DATEV OAuth callback after user authorization.

    The user is identified by the signed state, which was minted at flow-start
    while the user was authenticated and carries the PKCE verifier with it.
    """
    ctx: Context = request.state.context
    landing_uri = ""
    try:
        oauth_state = resolve_oauth_state(ctx=ctx, state=state)
        landing_uri = await post_auth_redirect_uri(ctx=ctx, redirect_uri=oauth_state.redirect_uri)
        user = UserORM(id=oauth_state.user_id)
        name = oauth_state.name
        code_verifier = oauth_state.code_verifier

        logger.bind(ctx=ctx).info(
            f"DATEV callback received: code={'present' if code else 'missing'} "
            f"error={error} pkce={'yes' if code_verifier else 'no'}"
        )

        if error:
            _NOT_APPROVED = {"access_denied", "unauthorized_client", "invalid_scope"}
            if error in _NOT_APPROVED:
                logger.bind(ctx=ctx).error(
                    f"DATEV app not approved / not authorized: error={error} — "
                    f"check that the app is approved in the DATEV developer portal, "
                    f"the redirect_uri is whitelisted, and the requested scopes are enabled"
                )
            raise HTTPException(status_code=400, detail=error)

        if not code:
            raise HTTPException(status_code=400, detail="Authorization code not provided")

        app_creds_doc = await UserAppCreds.get_app_creds_by_name(
            ctx=ctx, db=db, user=user, name=name, app_name=AppTypes.datev, decrypt=True
        )

        if not app_creds_doc or not app_creds_doc.creds:
            raise HTTPException(
                status_code=400,
                detail="DATEV credentials not found. Configure your credentials first.",
            )

        app_creds = DatevCreds.model_validate(app_creds_doc.creds)
        settings_scopes = SettingsProvider.sget().DATEV_SCOPES
        selected_scope_input, scope_source = Datev.choose_scope_input(settings_scopes, app_creds.scopes)
        normalized_scope, scope_debug = Datev.normalize_scopes(selected_scope_input)
        logger.bind(
            ctx=ctx,
            datev_environment=app_creds.environment,
            token_url=Datev.get_token_url(app_creds.environment),
            userinfo_url=Datev.get_userinfo_url(app_creds.environment),
            client_id=Datev.redact(app_creds.client_id),
            scope_source=scope_source,
            normalized_scope=normalized_scope,
            scope_warnings=scope_debug.get("warnings"),
        ).info("DATEV callback resolved app credentials")

        token_rq = DatevAccessTokenRq(
            client_id=app_creds.client_id,
            client_secret=app_creds.client_secret,
            code=code,
            redirect_uri=_datev_callback_url(ctx=ctx, request=request),
            environment=app_creds.environment,
            code_verifier=code_verifier,
            normalized_scope=normalized_scope,
            scope_source=scope_source,
        )

        oauth_rs = await DatevAuth.create_auth(ctx=ctx, app_creds=token_rq)

        if isinstance(oauth_rs, Exception):
            detail = oauth_rs.detail if isinstance(oauth_rs, HTTPException) else str(oauth_rs)
            raise HTTPException(status_code=400, detail=detail)

        assert isinstance(oauth_rs, OAuthModel)

        integration_create = IntegrationCreate(
            app_name=AppTypes.datev, name=name, auth=oauth_rs.model_dump(exclude_none=True)
        )

        created_auth = await UserIntegration.create(
            ctx=ctx, db=db, user=user, integration_create=integration_create, decrypt=True
        )

        logger.bind(ctx=ctx, app_name=AppTypes.datev, id=created_auth.id).info(
            "DATEV App Auth Created"
        )
        return RedirectResponse(landing_uri, status_code=303)

    except Exception as e:
        logger.bind(ctx=ctx, app_name="datev").exception(f"Error in DATEV oauth callback: {str(e)}")
        if not landing_uri:
            # Nowhere safe to send the browser — the state never yielded a destination.
            raise HTTPException(status_code=400, detail=str(e))
        return oauth_error_redirect(ctx=ctx, redirect_uri=landing_uri, error=e)
