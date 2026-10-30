import base64
import hashlib
import os
import secrets
import urllib.parse
from typing import Dict, Optional, Type

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots import SettingsProvider
from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.api.v1.api_auth.oauth_state import new_oauth_state
from src.autobots.integrations.integration_model import (
    IntegrationConnectResolved,
    IntegrationConnectResult,
    OAuthModel,
)


class DatevOAuthModel(OAuthModel):
    client_id: str | None = None
    environment: str | None = None
    normalized_scope: str | None = None
    requested_scope: str | None = None
    granted_scope: str | None = None
    scope_source: str | None = None

from src.autobots.conn.only_auth.datev.datev_client import (
    Datev,
    DatevAccessToken,
    DatevAccessTokenRq,
    DatevCreds,
    DatevRefreshedAccessToken,
    DatevRefreshTokenRq,
)
from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC
from src.autobots.user.user_orm_model import UserORM


class DatevAuth(IntegrationTypeABC):
    """
    DATEV OAuth 2.0 authentication.

    DATEV cloud APIs (accounting documents, Unternehmen Online) use OAuth 2.0.
    Some endpoints require X-DATEV-Client-Id header in addition to Bearer token.
    """

    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.datev

    @staticmethod
    def oauth_callback_path() -> str:
        """Where DATEV sends the browser back. Whitelisted in the DATEV portal."""
        return f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}{SettingsProvider.sget().API_PATH_DATEV}/oauth-callback"

    @staticmethod
    def generate_pkce_pair() -> tuple[str, str]:
        """(verifier, challenge), S256. The verifier never leaves us except inside state."""
        code_verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
        digest = hashlib.sha256(code_verifier.encode()).digest()
        return code_verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    @staticmethod
    def build_authorize_url(
        ctx: Context,
        name: str,
        callback_url: str,
        post_auth_uri: str,
        app_creds: "DatevCreds | None" = None,
    ) -> str:
        """The consent URL. DATEV is PKCE, so the verifier is minted here and carried in
        the signed state — the callback needs it to complete the exchange, and it must
        not travel any other way."""
        if not app_creds or not app_creds.client_id or not app_creds.client_secret:
            raise HTTPException(400, "DATEV client_id and client_secret are required.")
        settings_scopes = SettingsProvider.sget().DATEV_SCOPES
        selected_scope_input, _ = Datev.choose_scope_input(settings_scopes, app_creds.scopes)
        scope, _ = Datev.normalize_scopes(selected_scope_input)

        code_verifier, code_challenge = DatevAuth.generate_pkce_pair()
        state_b64 = new_oauth_state(
            ctx=ctx, name=name, redirect_uri=post_auth_uri, code_verifier=code_verifier
        ).to_base64()
        return (
            f"{Datev.get_authorize_url(app_creds.environment)}?"
            f"response_type=code&"
            f"response_mode=query&"
            f"client_id={app_creds.client_id}&"
            f"redirect_uri={urllib.parse.quote_plus(callback_url)}&"
            f"scope={urllib.parse.quote_plus(scope)}&"
            f"code_challenge={code_challenge}&"
            f"code_challenge_method=S256&"
            f"nonce={secrets.token_urlsafe(16)}&"
            f"state={urllib.parse.quote_plus(state_b64)}"
        )

    @staticmethod
    async def connect(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        req: IntegrationConnectResolved,
        decrypt: bool = False,
    ) -> IntegrationConnectResult:
        """The consent URL needs this connection's saved credentials, so load them first."""
        from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds

        creds_doc = await UserAppCreds.get_app_creds_by_name(
            ctx=ctx, db=db, user=user, name=req.name, app_name=req.app_name, decrypt=True
        )
        app_creds = (
            DatevCreds.model_validate(creds_doc.creds) if creds_doc and creds_doc.creds else None
        )
        return IntegrationConnectResult(
            redirect_url=DatevAuth.build_authorize_url(
                ctx=ctx,
                name=req.name,
                callback_url=req.callback_url or "",
                post_auth_uri=req.post_auth_uri or "",
                app_creds=app_creds,
            )
        )

    @staticmethod
    async def get_api_domain(app_creds: DatevCreds | None) -> str | None:
        if not app_creds:
            return None
        if app_creds.environment == "sandbox":
            return "https://accounting-documents.api.datev.de"
        return "https://accounting-documents.api.datev.de"

    @staticmethod
    def get_creds_model_type() -> Type[DatevCreds]:
        return DatevCreds

    @staticmethod
    def get_auth_model_type() -> Type[DatevOAuthModel]:
        return DatevOAuthModel

    @staticmethod
    async def create_auth(
        ctx: Context, app_creds: DatevAccessTokenRq
    ) -> OAuthModel | Exception:
        """Exchange authorization code for tokens. Called from OAuth callback."""
        token_result = await Datev.access_token(ctx, app_creds)
        match token_result:
            case DatevAccessToken():
                auth_model = DatevOAuthModel.model_validate(token_result.model_dump())
                auth_model.client_id = app_creds.client_id
                auth_model.environment = app_creds.environment
                auth_model.normalized_scope = app_creds.normalized_scope
                auth_model.requested_scope = app_creds.normalized_scope
                auth_model.granted_scope = token_result.scope or app_creds.normalized_scope
                auth_model.scope_source = app_creds.scope_source
                return auth_model
            case Exception():
                return token_result

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: DatevCreds | None, app_auth: OAuthModel | None
    ) -> OAuthModel | Exception:
        logger.bind(
            ctx=ctx,
            datev_environment=app_creds.environment if app_creds else None,
            client_id=Datev.redact(app_creds.client_id) if app_creds else None,
            token_client_id=Datev.redact(getattr(app_auth, "client_id", None)) if app_auth else None,
            token_environment=getattr(app_auth, "environment", None) if app_auth else None,
            has_refresh_token=bool(app_auth and app_auth.refresh_token),
        ).info("Refreshing DATEV auth")
        if not app_creds:
            return Exception("DATEV credentials are required for token refresh")

        if not app_auth or not app_auth.refresh_token:
            return Exception("No refresh token available for DATEV")

        token_client_id = getattr(app_auth, "client_id", None)
        token_environment = getattr(app_auth, "environment", None)
        if token_client_id and token_client_id != app_creds.client_id:
            logger.bind(
                ctx=ctx,
                current_client_id=Datev.redact(app_creds.client_id),
                token_client_id=Datev.redact(token_client_id),
            ).error("DATEV refresh blocked because the stored token belongs to another client_id")
            return Exception("DATEV token belongs to another client_id. Reconnect DATEV with the current client configuration.")
        if token_environment and token_environment != app_creds.environment:
            logger.bind(
                ctx=ctx,
                current_environment=app_creds.environment,
                token_environment=token_environment,
            ).error("DATEV refresh blocked because the stored token belongs to another environment")
            return Exception("DATEV token belongs to another environment. Reconnect DATEV after switching sandbox/production.")

        token_rq = DatevRefreshTokenRq(
            client_id=app_creds.client_id,
            client_secret=app_creds.client_secret,
            refresh_token=app_auth.refresh_token,
            environment=app_creds.environment,
        )

        refreshed = await Datev.refresh_access_token(ctx, token_rq)
        match refreshed:
            case DatevRefreshedAccessToken():
                new_auth_model = DatevOAuthModel.model_validate(app_auth)
                new_auth_model.access_token = refreshed.access_token
                new_auth_model.expires_in = refreshed.expires_in
                new_auth_model.created_at = Clock.datetime_now()
                if refreshed.refresh_token:
                    new_auth_model.refresh_token = refreshed.refresh_token
                if refreshed.refresh_token_expires_in:
                    new_auth_model.refresh_token_expires_in = refreshed.refresh_token_expires_in
                if refreshed.scope:
                    new_auth_model.granted_scope = refreshed.scope
                logger.bind(
                    ctx=ctx,
                    token_client_id=Datev.redact(new_auth_model.client_id),
                    token_environment=new_auth_model.environment,
                    requested_scope=new_auth_model.requested_scope,
                    granted_scope=new_auth_model.granted_scope,
                ).info("Refreshed token for DATEV")
                return new_auth_model
            case Exception():
                logger.bind(ctx=ctx).error("Unable to refresh token for DATEV")
                return refreshed

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: OAuthModel) -> bool:
        if not app_auth.created_at:
            logger.bind(ctx=ctx).warning("DATEV auth is missing created_at; skipping proactive refresh check")
            return False

        curr_time = Clock.datetime_now()
        duration_sec = (curr_time - app_auth.created_at).total_seconds()
        expires_in = app_auth.expires_in if app_auth.expires_in else 900  # 15 min default
        is_refresh = duration_sec > expires_in - 120  # 2-minute buffer

        logger.bind(
            ctx=ctx,
            is_refresh_required=is_refresh,
            expires_in=expires_in,
            duration_sec=duration_sec,
        ).info("is_refresh_required for DATEV")

        return is_refresh

    @staticmethod

    @staticmethod
    async def get_updated_url(
        ctx: Context, app_creds: DatevCreds | None, app_auth: DatevOAuthModel | None, url: str
    ) -> str:
        effective_environment = (getattr(app_auth, "environment", None) if app_auth else None) or (app_creds.environment if app_creds else None)
        if not effective_environment:
            return url
        rewritten = Datev.canonicalize_resource_url(effective_environment, url)
        if rewritten != url:
            logger.bind(
                ctx=ctx,
                original_url=url,
                rewritten_url=rewritten,
                effective_environment=effective_environment,
            ).info("Canonicalized DATEV URL before request execution")
        return rewritten or url

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: DatevOAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        target_url = str(config.url) if config and getattr(config, "url", None) else None
        token_client_id = getattr(app_auth, "client_id", None)
        token_environment = getattr(app_auth, "environment", None)
        token_scope = getattr(app_auth, "granted_scope", None) or getattr(app_auth, "normalized_scope", None)
        request_method = (str(getattr(config, "method", "GET")) if config else "GET").upper()
        caller_headers = getattr(config, "headers", None) or {}
        caller_header_names = {str(key).lower() for key in caller_headers} if isinstance(caller_headers, dict) else set()
        expected_response_format = Datev.expected_response_format_for_url(target_url, request_method)
        header = Datev.augment_headers_for_url(
            {"Authorization": f"Bearer {app_auth.access_token}"},
            target_url,
            token_client_id,
        )
        default_accept_applied = False
        if "accept" not in caller_header_names:
            header = Datev.apply_default_headers_for_url(header, target_url, request_method)
            default_accept_applied = "accept" in {k.lower() for k in header}
        if target_url:
            compatibility = Datev.is_environment_compatible(token_environment, target_url) if token_environment else None
            scope_compatibility = Datev.scope_snapshot_matches_url(token_scope, target_url)
            logger.bind(
                ctx=ctx,
                target_url=target_url,
                request_method=request_method,
                token_client_id=Datev.redact(token_client_id),
                token_environment=token_environment,
                url_environment=Datev.get_url_environment(target_url),
                environment_compatible=compatibility,
                requested_scope=getattr(app_auth, "requested_scope", None),
                granted_scope=getattr(app_auth, "granted_scope", None),
                scope_compatible=scope_compatibility,
                requires_client_id_header=Datev.requires_client_id_header(target_url),
                has_client_id_header=("X-DATEV-Client-Id" in header or "x-datev-client-id" in {k.lower() for k in header}),
                caller_accept_present=("accept" in caller_header_names),
                default_accept_header=Datev.default_accept_header_for_url(target_url, request_method),
                default_accept_applied=default_accept_applied,
                expected_response_format=expected_response_format,
            ).info("Built DATEV auth header")
            if compatibility is False:
                logger.bind(
                    ctx=ctx,
                    target_url=target_url,
                    token_environment=token_environment,
                    url_environment=Datev.get_url_environment(target_url),
                    reconnect_required=True,
                ).warning("DATEV request environment mismatch detected")
            if scope_compatibility is False:
                logger.bind(
                    ctx=ctx,
                    target_url=target_url,
                    granted_scope=getattr(app_auth, "granted_scope", None),
                    expected_scopes=Datev.expected_scopes_for_url(target_url),
                    reconnect_required=True,
                ).warning("DATEV token scope snapshot does not appear to cover the requested API")
        return header
