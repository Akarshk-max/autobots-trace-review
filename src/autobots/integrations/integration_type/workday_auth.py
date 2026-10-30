import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, Type
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from jose import jwt as jose_jwt
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field, model_validator

from src.autobots import SettingsProvider
from src.autobots.api.v1.api_auth.oauth_state import new_oauth_state
from src.autobots.conn.only_auth.workday.workday_client import (
    Workday,
    WorkdayAccessToken,
    WorkdayAccessTokenRq,
    WorkdayRefreshedAccessToken,
    WorkdayRefreshTokenRq,
)
from src.autobots.conn.only_auth.workday.workday_jwt import connect_workday_jwt
from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import (
    IntegrationConnectResolved,
    IntegrationConnectResult,
    OAuthModel,
)
from src.autobots.integrations.integration_type.integration_type import IntegrationTypeABC
from src.autobots.user.user_orm_model import UserORM


class WorkdayCreds(BaseModel):
    client_id: str
    tenant_url: str  # keep existing required base input for backward compatibility
    client_secret: str | None = None

    # Additional optional fields to support modern Workday JWT/certificate configuration
    auth_type: str = Field(default="client_secret")
    token_url: str | None = None
    authorization_url: str | None = None
    rest_base_url: str | None = None
    tenant_name: str | None = None
    scopes: str | None = None
    jwt_method: str | None = None
    key_id: str | None = None
    public_certificate: str | None = None
    private_key_path: str | None = None
    integration_user: str | None = None

    @model_validator(mode="after")
    def validate_auth_fields(self) -> "WorkdayCreds":
        auth_type = (self.auth_type or "client_secret").strip()
        if auth_type == "client_secret" and not self.client_secret:
            raise ValueError("client_secret is required when auth_type=client_secret")
        if auth_type == "jwt_certificate" and not self.private_key_path:
            raise ValueError("private_key_path is required when auth_type=jwt_certificate")
        return self


class WorkdayAuth(IntegrationTypeABC):

    @staticmethod
    def _infer_expires_in_from_access_token(access_token: str | None) -> int | None:
        if not access_token or access_token.count('.') < 2:
            return None
        try:
            claims = jose_jwt.get_unverified_claims(access_token)
        except Exception:
            return None
        exp = claims.get('exp')
        if exp is None:
            return None
        try:
            remaining = int(exp) - int(datetime.now(timezone.utc).timestamp())
        except Exception:
            return None
        return max(remaining, 0)

    @staticmethod
    def _normalize_token_response(token_response: dict | None) -> dict:
        normalized = dict(token_response or {})
        if normalized.get('expires_in') in (None, ''):
            normalized['expires_in'] = WorkdayAuth._infer_expires_in_from_access_token(normalized.get('access_token')) or 3600
        return normalized

    @staticmethod
    def _resolve_rest_base_url(app_creds: WorkdayCreds) -> str | None:
        if app_creds.rest_base_url:
            return app_creds.rest_base_url.rstrip('/')
        host_root, tenant_name = Workday._extract_host_root_and_tenant(
            app_creds.rest_base_url,
            app_creds.tenant_url,
            app_creds.token_url,
            explicit_tenant_name=app_creds.tenant_name,
        )
        if not host_root or not tenant_name:
            return app_creds.tenant_url.rstrip('/') if app_creds.tenant_url else None
        tenant_parts = [p for p in urlparse(app_creds.tenant_url).path.split('/') if p]
        if len(tenant_parts) >= 4 and tenant_parts[:3] == ['ccx', 'api', 'v1']:
            return f"{host_root}/ccx/api/v1/{tenant_name}"
        if len(tenant_parts) >= 3 and tenant_parts[:2] == ['ccx', 'api']:
            return f"{host_root}/ccx/api/{tenant_name}"
        return f"{host_root}/ccx/api/v1/{tenant_name}"

    @staticmethod
    def _preferred_rest_base_url(app_creds: WorkdayCreds) -> str | None:
        host_root, tenant_name = Workday._extract_host_root_and_tenant(
            app_creds.rest_base_url,
            app_creds.tenant_url,
            app_creds.token_url,
            explicit_tenant_name=app_creds.tenant_name,
        )
        if host_root and tenant_name:
            return f"{host_root}/ccx/api/v1/{tenant_name}"
        return WorkdayAuth._resolve_rest_base_url(app_creds)

    @staticmethod
    def _dedupe_strings(values: list[str | None]) -> list[str]:
        seen = set()
        result: list[str] = []
        for value in values:
            if not value:
                continue
            normalized = value.rstrip('/')
            if normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
        return result

    @staticmethod
    def _build_rest_base_candidates(app_creds: WorkdayCreds) -> list[str]:
        host_root, tenant_name = Workday._extract_host_root_and_tenant(
            app_creds.rest_base_url,
            app_creds.tenant_url,
            app_creds.token_url,
            explicit_tenant_name=app_creds.tenant_name,
        )
        candidates = [
            WorkdayAuth._preferred_rest_base_url(app_creds),
            app_creds.rest_base_url,
            WorkdayAuth._resolve_rest_base_url(app_creds),
            app_creds.tenant_url,
        ]
        if host_root and tenant_name:
            candidates.extend(
                [
                    f"{host_root}/ccx/api/v1/{tenant_name}",
                    f"{host_root}/ccx/api/{tenant_name}",
                ]
            )
        return WorkdayAuth._dedupe_strings(candidates)

    @staticmethod
    def _build_rest_workers_probe_urls(app_creds: WorkdayCreds) -> list[str]:
        preferred_base = WorkdayAuth._preferred_rest_base_url(app_creds)
        if preferred_base:
            return [preferred_base.rstrip('/') + '/workers?limit=1']
        return []

    @staticmethod
    def _build_rest_workers_me_probe_urls(app_creds: WorkdayCreds) -> list[str]:
        preferred_base = WorkdayAuth._preferred_rest_base_url(app_creds)
        if preferred_base:
            return [preferred_base.rstrip('/') + '/workers/me']
        return []

    @staticmethod
    def _build_wql_probe_urls(app_creds: WorkdayCreds) -> list[str]:
        host_root, tenant_name = Workday._extract_host_root_and_tenant(
            app_creds.rest_base_url,
            app_creds.tenant_url,
            app_creds.token_url,
            explicit_tenant_name=app_creds.tenant_name,
        )
        if not host_root or not tenant_name:
            return []
        return [f"{host_root}/api/wql/v1/{tenant_name}/dataSources?limit=1"]

    @staticmethod
    def _classify_health_status(status_code: int | None, body_snippet: str | None = None) -> str:
        body_lower = (body_snippet or '').lower()
        if status_code == 200:
            return 'success'
        if status_code == 401 or 'invalid access token' in body_lower:
            return 'token_not_accepted'
        if status_code == 403 or 'permission denied' in body_lower or '"code":"s22"' in body_lower:
            return 'permission_denied'
        if status_code == 404:
            return 'endpoint_construction'
        return 'needs_investigation'

    @staticmethod
    def _classify_health_attempts(attempts: list[dict[str, Any]]) -> str:
        classifications = [a.get('classification') for a in attempts]
        if 'success' in classifications:
            return 'success'
        if 'permission_denied' in classifications:
            return 'permission_denied'
        if 'token_not_accepted' in classifications:
            return 'token_not_accepted'
        if classifications and all(c == 'endpoint_construction' for c in classifications):
            return 'endpoint_construction'
        return 'needs_investigation'

    @staticmethod
    async def _probe_urls(urls: list[str], headers: Dict[str, str], client: httpx.AsyncClient) -> list[dict[str, Any]]:
        attempts: list[dict[str, Any]] = []
        for url in urls:
            try:
                res = await client.get(url, headers=headers)
                body_snippet = (res.text or '')[:500]
                attempts.append(
                    {
                        'url': url,
                        'status_code': res.status_code,
                        'success': res.status_code == 200,
                        'classification': WorkdayAuth._classify_health_status(res.status_code, body_snippet),
                        'body_snippet': body_snippet,
                    }
                )
            except Exception as exc:
                attempts.append(
                    {
                        'url': url,
                        'status_code': None,
                        'success': False,
                        'classification': 'needs_investigation',
                        'body_snippet': str(exc),
                    }
                )
        return attempts

    @staticmethod
    def _select_surface_attempt(attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not attempts:
            return None
        for preferred in ('success', 'permission_denied', 'token_not_accepted', 'endpoint_construction'):
            for attempt in attempts:
                if attempt.get('classification') == preferred:
                    return attempt
        return attempts[0]

    @staticmethod
    def _build_surface_result(
        surface: str,
        requested_url: str | None,
        probe_urls: list[str],
        attempts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        winning = WorkdayAuth._select_surface_attempt(attempts)
        return {
            'surface': surface,
            'requested_url': requested_url,
            'effective_url': winning.get('url') if winning else (probe_urls[0] if probe_urls else None),
            'http_status': winning.get('status_code') if winning else None,
            'classification': WorkdayAuth._classify_health_attempts(attempts),
            'body_snippet': winning.get('body_snippet') if winning else None,
            'attempts': attempts,
        }

    @staticmethod
    async def get_connection_info(
        ctx: Context,
        app_creds: WorkdayCreds | None,
        app_auth: OAuthModel | None,
        api_domain: str | None = None,
    ) -> Dict[str, Any]:
        if not app_creds:
            return {
                "api_domain": api_domain,
                "has_app_auth": bool(app_auth),
                "classification": "missing_credentials",
                "supports_health_check": True,
            }
        resolved_token_url = Workday.get_token_url(
            app_creds.tenant_url,
            token_url=app_creds.token_url,
            rest_base_url=app_creds.rest_base_url,
            tenant_name=app_creds.tenant_name,
        )
        _, tenant_name = Workday._extract_host_root_and_tenant(
            app_creds.rest_base_url,
            app_creds.tenant_url,
            app_creds.token_url,
            explicit_tenant_name=app_creds.tenant_name,
        )
        rest_base_candidates = WorkdayAuth._build_rest_base_candidates(app_creds)
        rest_workers_probe_urls = WorkdayAuth._build_rest_workers_probe_urls(app_creds)
        rest_workers_me_probe_urls = WorkdayAuth._build_rest_workers_me_probe_urls(app_creds)
        wql_probe_urls = WorkdayAuth._build_wql_probe_urls(app_creds)
        return {
            "api_domain": api_domain,
            "auth_type": app_creds.auth_type,
            "jwt_method": app_creds.jwt_method,
            "integration_user": app_creds.integration_user,
            "tenant_name": tenant_name,
            "resolved_token_url": resolved_token_url,
            "rest_base_candidates": rest_base_candidates,
            "wql_candidate_urls": wql_probe_urls,
            "has_app_auth": bool(app_auth),
            "classification": "connected" if app_auth else "missing_app_auth",
            "supports_health_check": True,
            "recommended_surface": "rest_workers",
            "recommended_primary_probe": rest_workers_probe_urls[0] if rest_workers_probe_urls else None,
            "recommended_secondary_probe": rest_workers_me_probe_urls[0] if rest_workers_me_probe_urls else None,
        }

    @staticmethod
    async def health_check(
        ctx: Context,
        app_creds: WorkdayCreds | None,
        app_auth: OAuthModel | None,
        api_domain: str | None = None,
        headers: Dict[str, str] | None = None,
    ) -> Dict[str, Any]:
        if not app_creds:
            return {
                "connected": False,
                "has_app_auth": bool(app_auth),
                "business_ready": False,
                "exact_url_requested": None,
                "http_status": None,
                "body_snippet": "Workday credentials are not configured.",
                "classification": "needs_investigation",
                "attempts": [],
                "rest_base_candidates": [],
                "wql_candidate_urls": [],
                "api_domain": api_domain,
                "tenant_name": None,
                "recommended_surface": "rest_workers",
                "recommended_primary_probe": None,
                "recommended_secondary_probe": None,
                "surfaces": {},
            }

        rest_base_candidates = WorkdayAuth._build_rest_base_candidates(app_creds)
        rest_workers_probe_urls = WorkdayAuth._build_rest_workers_probe_urls(app_creds)
        rest_workers_me_probe_urls = WorkdayAuth._build_rest_workers_me_probe_urls(app_creds)
        wql_probe_urls = WorkdayAuth._build_wql_probe_urls(app_creds)
        _, tenant_name = Workday._extract_host_root_and_tenant(
            app_creds.rest_base_url,
            app_creds.tenant_url,
            app_creds.token_url,
            explicit_tenant_name=app_creds.tenant_name,
        )

        if not headers or 'Authorization' not in headers:
            classification = 'missing_auth_header'
            empty_surface = {
                'surface': 'rest_workers',
                'requested_url': 'https://api.workday.com/ccx/api/v1/workers?limit=1',
                'effective_url': rest_workers_probe_urls[0] if rest_workers_probe_urls else None,
                'http_status': None,
                'classification': classification,
                'body_snippet': 'No Workday auth header available for health check.',
                'attempts': [],
            }
            return {
                "connected": False,
                "has_app_auth": bool(app_auth),
                "business_ready": False,
                "exact_url_requested": empty_surface.get('effective_url'),
                "http_status": None,
                "body_snippet": empty_surface.get('body_snippet'),
                "classification": classification,
                "attempts": [],
                "rest_base_candidates": rest_base_candidates,
                "wql_candidate_urls": wql_probe_urls,
                "api_domain": api_domain,
                "tenant_name": tenant_name,
                "recommended_surface": "rest_workers",
                "recommended_primary_probe": rest_workers_probe_urls[0] if rest_workers_probe_urls else None,
                "recommended_secondary_probe": rest_workers_me_probe_urls[0] if rest_workers_me_probe_urls else None,
                "surfaces": {"rest_workers": empty_surface},
            }

        async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
            rest_workers_attempts = await WorkdayAuth._probe_urls(rest_workers_probe_urls, headers, client)
            rest_workers_me_attempts = await WorkdayAuth._probe_urls(rest_workers_me_probe_urls, headers, client)
            wql_attempts = await WorkdayAuth._probe_urls(wql_probe_urls, headers, client)

        surfaces = {
            'rest_workers': WorkdayAuth._build_surface_result(
                'rest_workers',
                'https://api.workday.com/ccx/api/v1/workers?limit=1',
                rest_workers_probe_urls,
                rest_workers_attempts,
            ),
            'rest_workers_me': WorkdayAuth._build_surface_result(
                'rest_workers_me',
                'https://api.workday.com/ccx/api/v1/workers/me',
                rest_workers_me_probe_urls,
                rest_workers_me_attempts,
            ),
            'wql_data_sources': WorkdayAuth._build_surface_result(
                'wql_data_sources',
                None,
                wql_probe_urls,
                wql_attempts,
            ),
        }
        summary = surfaces['rest_workers']
        business_ready = any(surface.get('classification') == 'success' for surface in surfaces.values())
        return {
            "connected": business_ready,
            "has_app_auth": bool(app_auth),
            "business_ready": business_ready,
            "exact_url_requested": summary.get('effective_url'),
            "http_status": summary.get('http_status'),
            "body_snippet": summary.get('body_snippet'),
            "classification": summary.get('classification'),
            "attempts": summary.get('attempts', []),
            "rest_base_candidates": rest_base_candidates,
            "wql_candidate_urls": wql_probe_urls,
            "api_domain": api_domain,
            "tenant_name": tenant_name,
            "recommended_surface": 'rest_workers',
            "recommended_primary_probe": rest_workers_probe_urls[0] if rest_workers_probe_urls else None,
            "recommended_secondary_probe": rest_workers_me_probe_urls[0] if rest_workers_me_probe_urls else None,
            "surfaces": surfaces,
        }
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.workday

    @staticmethod
    def oauth_callback_path() -> str:
        """Where Workday sends the browser back. Registered on the API client."""
        return f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}{SettingsProvider.sget().API_PATH_WORKDAY}/oauth-callback"

    @staticmethod
    def build_authorize_url(
        ctx: Context,
        name: str,
        callback_url: str,
        post_auth_uri: str,
        app_creds: "WorkdayCreds | None" = None,
    ) -> str:
        """The consent URL. Workday's authorize host is the customer's own tenant."""
        if not app_creds or not app_creds.tenant_url:
            raise HTTPException(400, "Workday tenant_url is required.")
        if not app_creds.client_id or not app_creds.client_secret:
            raise HTTPException(400, "Workday client_id and client_secret are required.")
        url_encoded_callback_uri = urllib.parse.quote_plus(callback_url)
        url_encoded_scope = urllib.parse.quote_plus(SettingsProvider.sget().WORKDAY_SCOPES)
        state_b64 = new_oauth_state(ctx=ctx, name=name, redirect_uri=post_auth_uri).to_base64()
        auth_url = Workday.get_authorize_url(
            app_creds.tenant_url,
            authorization_url=app_creds.authorization_url,
            token_url=app_creds.token_url,
            rest_base_url=app_creds.rest_base_url,
            tenant_name=app_creds.tenant_name,
        )
        return (
            f"{auth_url}?"
            f"response_type=code&"
            f"client_id={app_creds.client_id}&"
            f"redirect_uri={url_encoded_callback_uri}&"
            f"scope={url_encoded_scope}&"
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
        """JWT-certificate connections have no consent screen; they connect from creds."""
        from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds

        creds_doc = await UserAppCreds.get_app_creds_by_name(
            ctx=ctx, db=db, user=user, name=req.name, app_name=req.app_name, decrypt=True
        )
        app_creds = (
            WorkdayCreds.model_validate(creds_doc.creds) if creds_doc and creds_doc.creds else None
        )
        if app_creds and (app_creds.auth_type or "").strip() == "jwt_certificate":
            return await IntegrationTypeABC.connect(
                ctx=ctx, db=db, user=user, req=req, decrypt=decrypt
            )
        return IntegrationConnectResult(
            redirect_url=WorkdayAuth.build_authorize_url(
                ctx=ctx,
                name=req.name,
                callback_url=req.callback_url or "",
                post_auth_uri=req.post_auth_uri or "",
                app_creds=app_creds,
            )
        )

    @staticmethod
    async def get_api_domain(app_creds: WorkdayCreds | None) -> str:
        """Return the best available Workday REST base URL for API calls."""
        if not app_creds:
            return None
        return (WorkdayAuth._preferred_rest_base_url(app_creds) or app_creds.rest_base_url or WorkdayAuth._resolve_rest_base_url(app_creds) or app_creds.tenant_url)

    @staticmethod
    def get_creds_model_type() -> Type[WorkdayCreds]:
        return WorkdayCreds

    @staticmethod
    def get_auth_model_type() -> Type[OAuthModel]:
        return OAuthModel

    @staticmethod
    async def create_auth(
        ctx: Context, app_creds: WorkdayAccessTokenRq
    ) -> OAuthModel | Exception:
        """
        Create OAuth model from Workday authorization code.

        Exchanges authorization code for access token and refresh token.
        Workday recommends using non-expiring refresh tokens for integrations.
        """
        access_token = await Workday.access_token(ctx=ctx, rq=app_creds)
        match access_token:
            case WorkdayAccessToken():
                res = OAuthModel.model_validate(access_token.model_dump())
                res.code = app_creds.code
                return res
            case Exception():
                return access_token

    @staticmethod
    async def refresh_auth(
        ctx: Context, app_creds: WorkdayCreds | None, app_auth: OAuthModel | None
    ) -> OAuthModel | Exception:
        """
        Refresh or re-mint a Workday access token.

        - client_secret flows use the refresh token flow.
        - jwt_certificate flows mint a fresh token using the stored cert/key and
          auto-negotiate the effective Workday JWT method / subject when needed.
        """
        if not app_creds:
            return Exception("Workday credentials are required for token refresh")

        auth_type = (app_creds.auth_type or "client_secret").strip()
        if auth_type == "jwt_certificate":
            jwt_result = await connect_workday_jwt(
                ctx=ctx,
                payload=app_creds.model_dump(exclude_none=True),
                try_fallbacks=True,
            )
            if isinstance(jwt_result, Exception):
                logger.bind(ctx=ctx).error("Unable to mint JWT token for Workday during refresh")
                return jwt_result

            token_response = WorkdayAuth._normalize_token_response(jwt_result.token_response)
            access_token = token_response.get("access_token")
            if not access_token:
                logger.bind(ctx=ctx).error("Workday JWT refresh returned no access_token")
                return Exception("Workday JWT refresh returned no access_token")
            oauth_payload = {
                "access_token": access_token,
                "refresh_token": None,
                "expires_in": token_response.get("expires_in", 3600),
                "created_at": Clock.datetime_now(),
                "token_type": token_response.get("token_type"),
                "scope": token_response.get("scope"),
            }
            new_auth_model = OAuthModel.model_validate(oauth_payload)
            logger.bind(
                ctx=ctx,
                workday_effective_jwt_method=jwt_result.method,
                workday_effective_subject=jwt_result.subject,
            ).info("Minted fresh JWT token for Workday")
            return new_auth_model

        if not app_auth or not app_auth.refresh_token:
            return Exception("No refresh token available for Workday")

        token_rq = WorkdayRefreshTokenRq(
            client_id=app_creds.client_id,
            client_secret=app_creds.client_secret,
            refresh_token=app_auth.refresh_token,
            tenant_url=app_creds.tenant_url,
            token_url=app_creds.token_url,
        )

        refreshed_access_token = await Workday.refresh_access_token(ctx, token_rq)
        match refreshed_access_token:
            case WorkdayRefreshedAccessToken():
                new_auth_model = OAuthModel.model_validate(app_auth)
                new_auth_model.access_token = refreshed_access_token.access_token
                new_auth_model.expires_in = refreshed_access_token.expires_in
                new_auth_model.created_at = Clock.datetime_now()

                if refreshed_access_token.refresh_token:
                    new_auth_model.refresh_token = refreshed_access_token.refresh_token

                logger.bind(ctx=ctx).info("Refreshed token for Workday")
                return new_auth_model
            case Exception():
                logger.bind(ctx=ctx).error("Unable to refresh token for Workday")
                return refreshed_access_token

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: OAuthModel) -> bool:
        """
        Check if token refresh is required.

        Workday access tokens typically expire after 1 hour (3600 seconds).
        Refresh if we have less than 10 minutes (600 seconds) remaining.
        """
        if not app_auth.created_at:
            return False

        curr_time = Clock.datetime_now()
        auth_creation_time = app_auth.created_at
        duration_since_creation = curr_time - auth_creation_time
        duration_sec = duration_since_creation.total_seconds()

        # Workday tokens expire after 1 hour
        expires_in = app_auth.expires_in if app_auth.expires_in else 3600
        is_refresh = duration_sec > expires_in - 600  # 10-minute buffer

        logger.bind(
            ctx=ctx,
            is_refresh_required=is_refresh,
            expires_in=expires_in,
            duration_sec=duration_sec,
            curr_time=str(curr_time),
            auth_creation_time=str(auth_creation_time),
        ).info("is_refresh_required for Workday")

        return is_refresh

    @staticmethod
    async def get_auth_header(
        ctx: Context,
        app_auth: OAuthModel,
        config: Any = None,
    ) -> Dict[str, str]:
        """Create authorization header with Bearer token.

        The shared auth plumbing passes an optional request config to all
        providers. Workday does not currently need request-specific config to
        build its bearer header, but it must accept the argument so generic
        callers can invoke it consistently.
        """
        return {"Authorization": f"Bearer {app_auth.access_token}"}

    @staticmethod
    async def get_updated_url(
        ctx: Context, app_creds: WorkdayCreds, app_auth: OAuthModel, url: str
    ) -> str:
        """Rewrite URLs so Workday API calls target the tenant-specific REST base."""
        try:
            if not url.startswith("http"):
                url = f"https://{url}"
            parsed = urlparse(url)
            host_root, tenant_name = Workday._extract_host_root_and_tenant(
                app_creds.rest_base_url,
                app_creds.tenant_url,
                app_creds.token_url,
                explicit_tenant_name=app_creds.tenant_name,
            )
            if not host_root:
                return url
            resolved_rest_base = WorkdayAuth._resolve_rest_base_url(app_creds)
            path = parsed.path or ''
            new_path = path
            parts = [p for p in path.split('/') if p]
            if tenant_name and len(parts) >= 3 and parts[:3] == ['ccx', 'api', 'v1']:
                suffix = parts[4:] if len(parts) >= 4 and parts[3] == tenant_name else parts[3:]
                new_path = f"/ccx/api/v1/{tenant_name}" + ("/" + "/".join(suffix) if suffix else '')
            elif tenant_name and len(parts) >= 2 and parts[:2] == ['ccx', 'api']:
                base_path = urlparse(resolved_rest_base).path.rstrip('/') if resolved_rest_base else f"/ccx/api/v1/{tenant_name}"
                if len(parts) >= 3 and parts[2] == tenant_name:
                    suffix = parts[3:]
                elif len(parts) >= 4 and parts[2] == 'v1' and parts[3] == tenant_name:
                    suffix = parts[4:]
                else:
                    suffix = parts[2:]
                new_path = base_path + ("/" + "/".join(suffix) if suffix else '')
            elif tenant_name and path.startswith('/workers'):
                new_path = f"/ccx/api/v1/{tenant_name}{path}"
            host_parsed = urlparse(host_root)
            updated = f"{host_parsed.scheme}://{host_parsed.netloc}{new_path}"
            if parsed.query:
                updated += f"?{parsed.query}"
            if parsed.fragment:
                updated += f"#{parsed.fragment}"
            logger.bind(ctx=ctx, original_url=url, new_url=updated).info("Updated Workday URL")
            return updated
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in Workday get_updated_url: {str(e)}")
            return url
