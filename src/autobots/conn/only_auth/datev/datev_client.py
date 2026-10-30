"""
DATEV OAuth 2.0 Client.

DATEV uses OAuth 2.0 for cloud-based integrations (Unternehmen Online,
accounting documents APIs, accounting clients, accounting data exchange).

Important integration notes:
- Access tokens are short lived (~15 minutes typical)
- Refresh tokens are single-use in the standard DATEV flow
- Some DATEV resource APIs require X-DATEV-Client-Id in addition to Bearer auth
- Sandbox and production endpoints must never be mixed
"""

from typing import Literal, Optional
from urllib.parse import urlparse, urlunparse

import httpx
from fastapi import HTTPException
from loguru import logger
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

from src.autobots.data_model.context import Context

DATEV_SANDBOX_AUTHORIZE_URL = "https://login.datev.de/openidsandbox/authorize"
DATEV_SANDBOX_TOKEN_URL = "https://sandbox-api.datev.de/token"
DATEV_SANDBOX_USERINFO_URL = "https://sandbox-api.datev.de/userinfo"
DATEV_PRODUCTION_AUTHORIZE_URL = "https://login.datev.de/openid/authorize"
DATEV_PRODUCTION_TOKEN_URL = "https://api.datev.de/token"
DATEV_PRODUCTION_USERINFO_URL = "https://api.datev.de/userinfo"

DATEV_DEFAULT_SCOPES = (
    "openid profile email datev:accounting:clients datev:accounting:exchange"
)
DATEV_SCOPE_ALIASES = {
    "accounting:clients": "datev:accounting:clients",
    "accounting:exchange": "datev:accounting:exchange",
}
DATEV_SANDBOX_HOSTS = {
    "sandbox-api.datev.de",
}
DATEV_PRODUCTION_HOSTS = {
    "api.datev.de",
}
DATEV_PATH_SANDBOX_HINTS = ("/platform-sandbox/", "/platform-sandbox")
DATEV_PATH_PRODUCTION_HINTS = ("/platform/", "/platform")

DATEV_ACCOUNTING_CLIENTS_HOST = "accounting-clients.api.datev.de"
DATEV_ACCOUNTING_DOCUMENTS_HOST = "accounting-documents.api.datev.de"
DATEV_ACCOUNTING_DATAEXCHANGE_HOST = "accounting-data-exchange.api.datev.de"

DATEV_DATAEXCHANGE_HOST_ALIASES = {
    DATEV_ACCOUNTING_DATAEXCHANGE_HOST,
    "accounting-dataexchange.api.datev.de",
}


class DatevAccessTokenRq(BaseModel):
    """Exchange authorization code for access/refresh tokens."""

    client_id: str
    client_secret: str
    code: str
    redirect_uri: str
    grant_type: str = "authorization_code"
    environment: Literal["sandbox", "production"] = "sandbox"
    code_verifier: Optional[str] = None  # PKCE S256 verifier
    normalized_scope: Optional[str] = None
    scope_source: Optional[str] = None


class DatevRefreshTokenRq(BaseModel):
    """Refresh expired access token using refresh token."""

    client_id: str
    client_secret: str
    refresh_token: str
    grant_type: str = "refresh_token"
    environment: Literal["sandbox", "production"] = "sandbox"


class DatevCreds(BaseModel):
    """User-level DATEV app credentials."""

    client_id: str
    client_secret: str
    environment: Literal["sandbox", "production"] = "sandbox"
    scopes: Optional[str] = None


class DatevAccessToken(BaseModel):
    """Token response from DATEV OAuth."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = 900  # 15 minutes typical
    refresh_token: Optional[str] = None
    refresh_token_expires_in: Optional[int] = None
    scope: Optional[str] = None
    id_token: Optional[str] = None


class DatevRefreshedAccessToken(BaseModel):
    """Refreshed token response from DATEV."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = 900
    refresh_token: Optional[str] = None
    refresh_token_expires_in: Optional[int] = None
    scope: Optional[str] = None


class Datev:
    """DATEV OAuth 2.0 Client for accounting/document APIs."""

    @staticmethod
    def get_authorize_url(environment: Literal["sandbox", "production"]) -> str:
        if environment == "sandbox":
            return DATEV_SANDBOX_AUTHORIZE_URL
        return DATEV_PRODUCTION_AUTHORIZE_URL

    @staticmethod
    def get_token_url(environment: Literal["sandbox", "production"]) -> str:
        if environment == "sandbox":
            return DATEV_SANDBOX_TOKEN_URL
        return DATEV_PRODUCTION_TOKEN_URL

    @staticmethod
    def get_userinfo_url(environment: Literal["sandbox", "production"]) -> str:
        if environment == "sandbox":
            return DATEV_SANDBOX_USERINFO_URL
        return DATEV_PRODUCTION_USERINFO_URL

    @staticmethod
    def get_platform_prefix(environment: Literal["sandbox", "production"]) -> str:
        return "/platform-sandbox" if environment == "sandbox" else "/platform"

    @staticmethod
    def get_default_scopes() -> str:
        return DATEV_DEFAULT_SCOPES

    @staticmethod
    def choose_scope_input(settings_scopes: Optional[str], app_creds_scopes: Optional[str]) -> tuple[str, str]:
        if settings_scopes and settings_scopes.strip():
            return settings_scopes.strip(), "settings"
        if app_creds_scopes and app_creds_scopes.strip():
            return app_creds_scopes.strip(), "app_creds"
        return DATEV_DEFAULT_SCOPES, "default"

    @staticmethod
    def redact(value: Optional[str], prefix: int = 4, suffix: int = 2) -> str:
        if not value:
            return "<empty>"
        if len(value) <= prefix + suffix:
            return "*" * len(value)
        return f"{value[:prefix]}***{value[-suffix:]}"

    @staticmethod
    def normalize_scopes(raw_scopes: Optional[str]) -> tuple[str, dict[str, object]]:
        configured_scopes = (raw_scopes or "").strip()
        used_default_scopes = not bool(configured_scopes)
        source = configured_scopes if configured_scopes else DATEV_DEFAULT_SCOPES

        aliases_applied: dict[str, str] = {}
        auto_added_scopes: list[str] = []
        warnings: list[str] = []
        scopes: list[str] = []
        seen: set[str] = set()

        for raw_token in source.replace("+", " ").split():
            token = raw_token.strip()
            if not token:
                continue
            normalized = DATEV_SCOPE_ALIASES.get(token, token)
            if normalized != token:
                aliases_applied[token] = normalized
            if normalized not in seen:
                scopes.append(normalized)
                seen.add(normalized)

        if "openid" not in seen:
            scopes.insert(0, "openid")
            seen.add("openid")
            auto_added_scopes.append("openid")

        if used_default_scopes:
            warnings.append(
                "No explicit DATEV scopes configured; using default bundle. Configure DATEV_SCOPES or app_creds.scopes to match subscribed DATEV products."
            )

        if "offline_access" in seen and not any(scope.startswith("datev:iam:client:") for scope in scopes):
            warnings.append(
                "offline_access requires a matching datev:iam:client:<consultant>-<client> scope for DATEV long-lived refresh tokens."
            )
        if "offline_access" in seen and "datev:accounting:exchange" in seen:
            warnings.append(
                "offline_access is currently not available with DATEV accounting:dataexchange API; expect re-auth or remove offline_access for this product."
            )
        if "accounting:documents" in seen and "datev:accounting:exchange" in seen:
            warnings.append(
                "Mixed accounting:documents and datev:accounting:exchange scopes detected. Ensure the DATEV app is subscribed to both products before re-authentication."
            )

        if any(scope.startswith("datev:hr:") for scope in scopes) and any("accounting" in scope for scope in scopes):
            warnings.append(
                "Mixed HR and accounting scopes detected. Verify that the DATEV products are intentionally combined."
            )

        normalized_scope = " ".join(scopes)
        return normalized_scope, {
            "configured_scopes": configured_scopes,
            "used_default_scopes": used_default_scopes,
            "aliases_applied": aliases_applied,
            "auto_added_scopes": auto_added_scopes,
            "warnings": warnings,
            "scope_list": scopes,
        }

    @staticmethod
    def get_url_environment(url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        parsed = urlparse(str(url))
        host = parsed.netloc.lower()
        path = parsed.path.lower()

        if host in DATEV_SANDBOX_HOSTS:
            return "sandbox"
        if host in DATEV_PRODUCTION_HOSTS:
            return "production"
        if any(hint in path for hint in DATEV_PATH_SANDBOX_HINTS):
            return "sandbox"
        if any(hint in path for hint in DATEV_PATH_PRODUCTION_HINTS):
            return "production"
        return None

    @staticmethod
    def is_environment_compatible(expected_environment: Literal["sandbox", "production"], url: Optional[str]) -> Optional[bool]:
        inferred_environment = Datev.get_url_environment(url)
        if inferred_environment is None:
            return None
        return inferred_environment == expected_environment

    @staticmethod
    def requires_client_id_header(url: Optional[str]) -> bool:
        if not url:
            return False
        parsed = urlparse(str(url))
        host = parsed.netloc.lower()
        path = parsed.path.lower()

        if host in {"api.datev.de", "sandbox-api.datev.de"} and path in {"/token", "/userinfo"}:
            return False
        if host.endswith(".api.datev.de"):
            return True
        return False

    @staticmethod
    def augment_headers_for_url(
        headers: Optional[dict[str, str]], url: Optional[str], client_id: Optional[str]
    ) -> dict[str, str]:
        merged = dict(headers or {})
        if not client_id or not Datev.requires_client_id_header(url):
            return merged

        lower_keys = {key.lower() for key in merged}
        if "x-datev-client-id" not in lower_keys:
            merged["X-DATEV-Client-Id"] = client_id
        return merged

    @staticmethod
    def is_dataexchange_url(url: Optional[str]) -> bool:
        if not url:
            return False
        parsed = urlparse(str(url))
        return parsed.netloc.lower() in DATEV_DATAEXCHANGE_HOST_ALIASES

    @staticmethod
    def expected_response_format_for_url(url: Optional[str], method: Optional[str] = None) -> Optional[str]:
        method_upper = (method or "GET").upper()
        if Datev.is_dataexchange_url(url) and method_upper == "GET":
            return "application/x-ndjson"
        return None

    @staticmethod
    def default_accept_header_for_url(url: Optional[str], method: Optional[str] = None) -> Optional[str]:
        return Datev.expected_response_format_for_url(url, method)

    @staticmethod
    def apply_default_headers_for_url(
        headers: Optional[dict[str, str]], url: Optional[str], method: Optional[str] = None
    ) -> dict[str, str]:
        merged = dict(headers or {})
        lower_keys = {key.lower() for key in merged}
        if "accept" in lower_keys:
            return merged

        default_accept = Datev.default_accept_header_for_url(url, method)
        if default_accept:
            merged["Accept"] = default_accept
        return merged

    @staticmethod
    def canonicalize_resource_url(environment: Literal["sandbox", "production"], url: Optional[str]) -> Optional[str]:
        if not url:
            return url
        parsed = urlparse(str(url))
        host = parsed.netloc.lower()
        path = parsed.path
        prefix = Datev.get_platform_prefix(environment)

        if path == "/userinfo":
            query = f"?{parsed.query}" if parsed.query else ""
            return f"{Datev.get_userinfo_url(environment)}{query}"

        if host in {"api.datev.de", "sandbox-api.datev.de"}:
            if path.rstrip("/") == "/accounting/v1/clients":
                new_path = f"{prefix}/v2/clients"
                return urlunparse(("https", DATEV_ACCOUNTING_CLIENTS_HOST, new_path, "", parsed.query, ""))
            if path.startswith("/accounting/v1/clients/"):
                suffix = path[len("/accounting/v1/clients/"):].strip("/")
                if "/" not in suffix:
                    new_path = f"{prefix}/v2/clients/{suffix}"
                    return urlunparse(("https", DATEV_ACCOUNTING_CLIENTS_HOST, new_path, "", parsed.query, ""))
                new_path = f"{prefix}/v1/clients/{suffix}"
                return urlunparse(("https", DATEV_ACCOUNTING_DATAEXCHANGE_HOST, new_path, "", parsed.query, ""))

        if host in {
            DATEV_ACCOUNTING_CLIENTS_HOST,
            DATEV_ACCOUNTING_DOCUMENTS_HOST,
            *DATEV_DATAEXCHANGE_HOST_ALIASES,
        }:
            canonical_host = DATEV_ACCOUNTING_DATAEXCHANGE_HOST if host in DATEV_DATAEXCHANGE_HOST_ALIASES else host
            new_path = path
            if path.startswith("/platform-sandbox/"):
                new_path = f"{prefix}{path[len('/platform-sandbox'):] }"
            elif path == "/platform-sandbox":
                new_path = prefix
            elif path.startswith("/platform/"):
                new_path = f"{prefix}{path[len('/platform'):] }"
            elif path == "/platform":
                new_path = prefix
            return urlunparse(("https", canonical_host, new_path, "", parsed.query, ""))

        return str(url)

    @staticmethod
    def build_debug_context(
        environment: Literal["sandbox", "production"],
        url: Optional[str] = None,
        scopes: Optional[str] = None,
    ) -> dict[str, object]:
        normalized_scopes, scope_debug = Datev.normalize_scopes(scopes)
        return {
            "environment": environment,
            "token_url": Datev.get_token_url(environment),
            "userinfo_url": Datev.get_userinfo_url(environment),
            "normalized_scopes": normalized_scopes,
            "scope_debug": scope_debug,
            "url": str(url) if url else None,
            "url_environment": Datev.get_url_environment(url),
            "environment_compatible": Datev.is_environment_compatible(environment, url),
            "requires_client_id_header": Datev.requires_client_id_header(url),
            "expected_response_format": Datev.expected_response_format_for_url(url),
        }

    @staticmethod
    def expected_scopes_for_url(url: Optional[str]) -> list[str]:
        if not url:
            return []
        parsed = urlparse(str(url))
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        if host == "accounting-clients.api.datev.de":
            return ["datev:accounting:clients"]
        if host == "accounting-documents.api.datev.de":
            return ["accounting:documents"]
        if host in DATEV_DATAEXCHANGE_HOST_ALIASES:
            return ["datev:accounting:exchange"]
        if host in {"sandbox-api.datev.de", "api.datev.de"} and path == "/userinfo":
            return ["openid"]
        return []

    @staticmethod
    def scope_snapshot_matches_url(scope_snapshot: Optional[str], url: Optional[str]) -> Optional[bool]:
        expected = Datev.expected_scopes_for_url(url)
        if not expected or not scope_snapshot:
            return None
        normalized_snapshot, _ = Datev.normalize_scopes(scope_snapshot)
        snapshot_set = set(normalized_snapshot.split())
        return all(scope in snapshot_set for scope in expected)

    @staticmethod
    def classify_api_error(url: Optional[str], status_code: int, response_text: str) -> str:
        lower_body = (response_text or "").lower()
        if status_code == 406 and "acceptable representations" in lower_body and "application/x-ndjson" in lower_body:
            return (
                "DATEV exchange endpoint rejected the requested media type. Retry with Accept: application/x-ndjson. This is not an auth or scope failure."
            )
        if status_code == 404 and "no fiscal year data found for" in lower_body:
            return (
                "DATEV could not find data for the requested fiscal year. First call the fiscal-years endpoint and use one of the returned fiscalYearIds instead of guessing."
            )
        if status_code == 401 and "token issued to another client" in lower_body:
            return (
                "DATEV says the token was issued to another client. This usually means the stored token was minted for a different DATEV client_id, or the request is using a mismatched X-DATEV-Client-Id. Reconnect with the current client configuration."
            )
        if status_code == 401 and "invalid client id or secret" in lower_body:
            return (
                "DATEV rejected the client identity for this resource request. Check X-DATEV-Client-Id, verify the app subscription for this API product, and ensure the bearer token belongs to the same DATEV client."
            )
        if status_code == 401 and '"error": "invalid_token"' in lower_body:
            return (
                "DATEV rejected the access token. Possible causes: sandbox/production mismatch, expired/revoked token, or a token that was minted without the required DATEV product scope."
            )
        if status_code == 401 and "cannot find valid subscription" in lower_body:
            return (
                "DATEV could not match this request to a valid subscribed API product. Verify the DATEV app is subscribed to the requested product in the current stage/environment."
            )
        if Datev.expected_scopes_for_url(url):
            return (
                f"Expected DATEV scopes for this URL include: {', '.join(Datev.expected_scopes_for_url(url))}. Verify the consented token contains them and reconnect if scopes changed."
            )
        return "Inspect DATEV environment, product subscription, X-DATEV-Client-Id, and token scopes."

    @staticmethod
    def _classify_token_error(status_code: int, err_code: str, err_desc: str) -> str:
        err_code = (err_code or "").strip().lower()
        err_desc = (err_desc or "").strip().lower()

        if err_code in {"unauthorized_client", "invalid_scope"}:
            return (
                "Check that the DATEV app is approved in the developer portal, that the requested scopes are enabled, "
                "and that the redirect_uri is whitelisted exactly as sent."
            )
        if err_code == "invalid_client" or "client id or secret" in err_desc:
            return (
                "DATEV rejected the client credentials. Verify client_id/client_secret and ensure sandbox vs production is consistent."
            )
        if err_code == "invalid_grant":
            return (
                "DATEV rejected the authorization code or refresh token. It may be expired, already used, bound to a different redirect_uri, or mismatched with the PKCE verifier."
            )
        if status_code == 401:
            return "DATEV returned 401. Verify the token endpoint/environment and client authentication mode."
        return "Review the DATEV response body, environment, scopes, redirect_uri, and PKCE verifier."

    @staticmethod
    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True)
    async def access_token(ctx: Context, rq: DatevAccessTokenRq) -> DatevAccessToken | Exception:
        """Exchange authorization code for access token."""
        token_url = Datev.get_token_url(rq.environment)
        normalized_scopes, _ = Datev.normalize_scopes(None)
        logger.bind(
            ctx=ctx,
            environment=rq.environment,
            token_url=token_url,
            redirect_uri=rq.redirect_uri,
            client_id=Datev.redact(rq.client_id),
            pkce_present=bool(rq.code_verifier),
            default_scope_debug=normalized_scopes,
        ).info("DATEV token exchange prepared")

        payload = {
            "grant_type": rq.grant_type,
            "client_id": rq.client_id,
            "client_secret": rq.client_secret,
            "code": rq.code,
            "redirect_uri": rq.redirect_uri,
        }
        if rq.code_verifier:
            payload["code_verifier"] = rq.code_verifier

        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        basic_only_payload = {
            "grant_type": rq.grant_type,
            "code": rq.code,
            "redirect_uri": rq.redirect_uri,
        }
        if rq.code_verifier:
            basic_only_payload["code_verifier"] = rq.code_verifier

        public_payload = {
            "grant_type": rq.grant_type,
            "client_id": rq.client_id,
            "code": rq.code,
            "redirect_uri": rq.redirect_uri,
        }
        if rq.code_verifier:
            public_payload["code_verifier"] = rq.code_verifier

        attempts = [
            ("basic_auth_only", lambda c: c.post(token_url, data=basic_only_payload, headers=headers, auth=(rq.client_id, rq.client_secret))),
            ("body_creds", lambda c: c.post(token_url, data=payload, headers=headers)),
            ("basic_auth+body", lambda c: c.post(token_url, data=payload, headers=headers, auth=(rq.client_id, rq.client_secret))),
            ("public_client", lambda c: c.post(token_url, data=public_payload, headers=headers)),
        ]

        logger.bind(
            ctx=ctx,
            environment=rq.environment,
            client_id=Datev.redact(rq.client_id),
            code=Datev.redact(rq.code),
            code_verifier=Datev.redact(rq.code_verifier),
            redirect_uri=rq.redirect_uri,
            attempt_names=[name for name, _ in attempts],
        ).debug("DATEV token exchange request context")

        last_response: httpx.Response | None = None
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=15.0)) as client:
            for name, make_request in attempts:
                logger.bind(
                    ctx=ctx,
                    attempt=name,
                    token_url=token_url,
                    environment=rq.environment,
                    client_id=Datev.redact(rq.client_id),
                    redirect_uri=rq.redirect_uri,
                    pkce_present=bool(rq.code_verifier),
                ).info("DATEV token exchange attempt")
                res = await make_request(client)
                last_response = res
                logger.bind(
                    ctx=ctx,
                    attempt=name,
                    status_code=res.status_code,
                    response_preview=res.text[:1000] if res.text else "",
                ).info("DATEV token exchange response")
                if res.status_code == 200:
                    token_data = res.json()
                    logger.bind(
                        ctx=ctx,
                        attempt=name,
                        access_token=Datev.redact(token_data.get("access_token")),
                        refresh_token=Datev.redact(token_data.get("refresh_token")),
                        expires_in=token_data.get("expires_in"),
                        scope=token_data.get("scope"),
                    ).info("DATEV token exchange succeeded")
                    return DatevAccessToken(**token_data)

        assert last_response is not None
        res = last_response
        try:
            err_body = res.json()
            err_code = err_body.get("error", "")
            err_desc = err_body.get("error_description", "")
        except Exception:
            err_code, err_desc = "", res.text

        logger.bind(
            ctx=ctx,
            status_code=res.status_code,
            error_code=err_code,
            error_description=err_desc,
            hint=Datev._classify_token_error(res.status_code, err_code, err_desc),
            redirect_uri=rq.redirect_uri,
            pkce_present=bool(rq.code_verifier),
            environment=rq.environment,
        ).error("DATEV token exchange failed all attempts")
        return HTTPException(status_code=res.status_code, detail=res.text)

    @staticmethod
    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True)
    async def refresh_access_token(ctx: Context, rq: DatevRefreshTokenRq) -> DatevRefreshedAccessToken | Exception:
        """Refresh an expired access token."""
        token_url = Datev.get_token_url(rq.environment)
        logger.bind(
            ctx=ctx,
            environment=rq.environment,
            token_url=token_url,
            client_id=Datev.redact(rq.client_id),
            refresh_token=Datev.redact(rq.refresh_token),
        ).info("DATEV refresh prepared")

        payload = {
            "grant_type": rq.grant_type,
            "client_id": rq.client_id,
            "client_secret": rq.client_secret,
            "refresh_token": rq.refresh_token,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        basic_only_payload = {
            "grant_type": rq.grant_type,
            "refresh_token": rq.refresh_token,
        }

        public_payload = {
            "grant_type": rq.grant_type,
            "client_id": rq.client_id,
            "refresh_token": rq.refresh_token,
        }

        attempts = [
            ("basic_auth_only", lambda c: c.post(token_url, data=basic_only_payload, headers=headers, auth=(rq.client_id, rq.client_secret))),
            ("body_creds", lambda c: c.post(token_url, data=payload, headers=headers)),
            ("basic_auth+body", lambda c: c.post(token_url, data=payload, headers=headers, auth=(rq.client_id, rq.client_secret))),
            ("public_client", lambda c: c.post(token_url, data=public_payload, headers=headers)),
        ]

        last_response: httpx.Response | None = None
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=15.0)) as client:
            for name, make_request in attempts:
                logger.bind(
                    ctx=ctx,
                    attempt=name,
                    environment=rq.environment,
                    token_url=token_url,
                    client_id=Datev.redact(rq.client_id),
                ).info("DATEV refresh attempt")
                res = await make_request(client)
                last_response = res
                logger.bind(
                    ctx=ctx,
                    attempt=name,
                    status_code=res.status_code,
                    response_preview=res.text[:1000] if res.text else "",
                ).info("DATEV refresh response")
                if res.status_code == 200:
                    refreshed = res.json()
                    logger.bind(
                        ctx=ctx,
                        attempt=name,
                        access_token=Datev.redact(refreshed.get("access_token")),
                        refresh_token=Datev.redact(refreshed.get("refresh_token")),
                        expires_in=refreshed.get("expires_in"),
                    ).info("DATEV refresh succeeded")
                    return DatevRefreshedAccessToken(**refreshed)

        assert last_response is not None
        res = last_response
        try:
            err_body = res.json()
            err_code = err_body.get("error", "")
            err_desc = err_body.get("error_description", "")
        except Exception:
            err_code, err_desc = "", res.text

        logger.bind(
            ctx=ctx,
            status_code=res.status_code,
            error_code=err_code,
            error_description=err_desc,
            hint=Datev._classify_token_error(res.status_code, err_code, err_desc),
            environment=rq.environment,
        ).error("DATEV refresh failed all attempts")
        return HTTPException(status_code=res.status_code, detail=res.text)
