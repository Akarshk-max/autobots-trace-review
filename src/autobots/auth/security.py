import hashlib
import time

import supabase_auth
from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyCookie, APIKeyHeader, APIKeyQuery, OAuth2PasswordBearer
from loguru import logger
from starlette.status import HTTP_403_FORBIDDEN

from src.autobots.auth.data_models import JwtPayload
from src.autobots.auth.providers.auth_provider_registry import get_auth_provider
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context

# ---------------------------------------------------------------------------
# FastAPI auth door-checks.
#
# These dependencies are the stable public surface consumed by all endpoints. Token
# verification itself is delegated to the configured auth provider
# (`get_auth_provider()` -> SupabaseAuth today); this module only extracts the bearer token
# from the request and shapes the 403 on failure. Swapping IdPs is a provider/registry change
# (see auth/providers/), with no endpoint edits.
#
# Offline vs online: the offline door-checks verify the JWT locally (no IdP round-trip); the
# online ones validate at the IdP and catch server-side revocation (deleted/banned users), at
# the cost of one network call — reserve them for sensitive identity ops.
# ---------------------------------------------------------------------------

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{SettingsProvider.sget().API_v1}{SettingsProvider.sget().API_AUTH}{SettingsProvider.sget().API_AUTH_TOKEN}",
    scheme_name="JWT",
)

api_key_query = APIKeyQuery(name="Authorization", auto_error=False)
api_key_header = APIKeyHeader(name="Authorization", auto_error=False)
api_key_cookie = APIKeyCookie(name="Authorization", auto_error=False)


def _bearer_value_to_token(value: str) -> str | None:
    """Extract raw token from an Authorization value.

    Accepts:
        - 'Bearer <token>'
        - '<token>' (fallback)
    """
    if not value:
        return None
    parts = value.strip().split()
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    if len(parts) >= 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


def _token_fingerprint(token: str) -> str:
    """Short stable token fingerprint for logs (avoid logging raw secrets)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


async def get_offline_user_from_access_token(
    api_key_query: str | None = Security(api_key_query),
    api_key_header: str | None = Security(api_key_header),
    api_key_cookie: str | None = Security(api_key_cookie),
) -> supabase_auth.UserResponse:
    """Offline door-check (default): verify the caller's JWT locally, no IdP round-trip.

    Returns a ``UserResponse`` built from the token claims; the user is provisioned on first
    sight. This is the drop-in default across endpoints.
    """
    raw = api_key_query or api_key_header or api_key_cookie
    token = _bearer_value_to_token(raw) if raw else None
    if token:
        try:
            return await get_auth_provider().verify_token_offline(token, audience="authenticated")
        except HTTPException:
            raise
        except Exception as e:
            logger.exception(str(e))

    raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="Could not validate credentials")


async def get_online_user_from_access_token(
    api_key_query: str | None = Security(api_key_query),
    api_key_header: str | None = Security(api_key_header),
    api_key_cookie: str | None = Security(api_key_cookie),
) -> supabase_auth.UserResponse:
    """Online door-check: validate the token at the IdP (authoritative) and provision the user.

    One extra network round-trip vs the offline door-check; reserve for sensitive identity ops
    that must catch server-side revocation (deleted/banned users), not just JWT validity.
    """
    raw = api_key_query or api_key_header or api_key_cookie
    token = _bearer_value_to_token(raw) if raw else None
    if token:
        try:
            return await get_auth_provider().verify_token_online(token, audience="authenticated")
        except HTTPException:
            raise
        except Exception as e:
            logger.exception(str(e))

    raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="Could not validate credentials")


async def get_online_admin_user_from_access_token(
    api_key_query: str | None = Security(api_key_query),
    api_key_header: str | None = Security(api_key_header),
    api_key_cookie: str | None = Security(api_key_cookie),
) -> supabase_auth.UserResponse:
    user_res: supabase_auth.UserResponse = await get_online_user_from_access_token(
        api_key_query=api_key_query, api_key_header=api_key_header, api_key_cookie=api_key_cookie
    )

    is_admin = SettingsProvider.is_system_email(user_res.user.email)
    if not is_admin:
        logger.error(f"User not admin: {user_res.user.email}")
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="User not found")

    return user_res


async def get_online_user_from_creds(
    token: str = Depends(oauth2_scheme),
) -> supabase_auth.UserResponse:
    """OAuth2 password-bearer variant of the online door-check."""
    try:
        user_res: supabase_auth.UserResponse = await get_auth_provider().verify_token_online(
            token, audience="authenticated"
        )
        if not user_res.user.email:
            raise HTTPException(403, "Could not validate credentials")
        return user_res
    except Exception as e:
        logger.error(str(e))


async def get_offline_verified_access_token_str(
    api_key_query: str | None = Security(api_key_query),
    api_key_header: str | None = Security(api_key_header),
    api_key_cookie: str | None = Security(api_key_cookie),
) -> str:
    """Return the caller's bearer token after **verifying** it offline (signature, audience,
    expiry). Unlike a bare extractor, this authenticates the request at the door — an
    unauthenticated/forged/expired token is rejected before the endpoint body runs. The verified
    raw token is then handed back for endpoints that must forward it to the IdP (e.g. sign out);
    the IdP re-checks it on the actual operation, so this is defense in depth with no extra
    network round-trip.
    """
    raw = api_key_query or api_key_header or api_key_cookie
    token = _bearer_value_to_token(raw) if raw else None
    if not token or get_auth_provider().decode_payload_offline(token) is None:
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="Could not validate credentials")
    return token


async def get_online_verified_access_token_str(
    api_key_query: str | None = Security(api_key_query),
    api_key_header: str | None = Security(api_key_header),
    api_key_cookie: str | None = Security(api_key_cookie),
) -> str:
    """Online counterpart of :func:`get_offline_verified_access_token_str`: authenticate the
    caller at the IdP via :func:`get_online_user_from_access_token` (which also provisions the
    user), then return the raw token for forwarding. Use where the authoritative, server-side
    check (catching deleted/banned users, not just JWT validity) is wanted at the door — e.g.
    updating the user — at the cost of one extra IdP call.
    """
    # Raises 403 if the token is missing/invalid or the user can't be validated.
    await get_online_user_from_access_token(
        api_key_query=api_key_query, api_key_header=api_key_header, api_key_cookie=api_key_cookie
    )
    raw = api_key_query or api_key_header or api_key_cookie
    token = _bearer_value_to_token(raw) if raw else None
    if not token:
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="Could not validate credentials")
    return token


async def try_get_jwt_payload_from_access_token(
    api_key_query: str | None = Security(api_key_query),
    api_key_header: str | None = Security(api_key_header),
    api_key_cookie: str | None = Security(api_key_cookie),
) -> JwtPayload | None:
    """Best-effort validation of an access token (non-blocking).

    Used for endpoints like session refresh, where the access token may be
    missing or expired but we still want to detect anomalies in logs.

    Returns:
        JwtPayload when the token is a validly signed JWT (even if expired), otherwise None.
    """
    raw = None
    src = None
    if api_key_query:
        raw = api_key_query
        src = "query"
    elif api_key_header:
        raw = api_key_header
        src = "header"
    elif api_key_cookie:
        raw = api_key_cookie
        src = "cookie"
    else:
        return None

    token = _bearer_value_to_token(raw)
    if not token:
        logger.warning(f"Malformed Authorization value in {src} (non-blocking)")
        return None

    payload = get_auth_provider().decode_payload_allow_expired(token, audience="authenticated")
    fp = _token_fingerprint(token)
    if not payload:
        logger.warning(f"Invalid access token presented (non-blocking) src={src} fp={fp}")
        return None

    user_id = getattr(payload, "sub", None)
    email = getattr(payload, "email", None)
    identity_parts: list[str] = []
    if user_id:
        identity_parts.append(f"user_id={user_id}")
    if email:
        identity_parts.append(f"email={email}")
    identity = (" " + " ".join(identity_parts)) if identity_parts else ""

    now = int(time.time())
    if getattr(payload, "exp", 0) and int(payload.exp) < now:
        logger.warning(f"Expired access token presented (non-blocking) src={src} fp={fp}{identity}")
    else:
        logger.debug(f"Access token presented (non-blocking) src={src} fp={fp}{identity}")
    return payload


def get_access_token(
    api_key_query: str | None = Security(api_key_query),
    api_key_header: str | None = Security(api_key_header),
    api_key_cookie: str | None = Security(api_key_cookie),
) -> str:
    try:
        if api_key_query:
            return api_key_query.split(" ")[1]
        elif api_key_header:
            return api_key_header.split(" ")[1]
        elif api_key_cookie:
            return api_key_cookie.split(" ")[1]

    except Exception as e:
        logger.exception(str(e))

    raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="No credentials found")


async def raise_if_redirect_uri_not_allowed(ctx: Context, redirect_uri: str) -> bool:
    try:
        # Normalised at the comparison rather than in the configured values, which CORS
        # also reads.
        redirect_url_origin = SettingsProvider.origin_of(redirect_uri)
        allowed_domains = {
            SettingsProvider.origin_of(origin)
            for origin in SettingsProvider.sget().ALLOWED_ORIGINS
        }
        if redirect_url_origin in allowed_domains:
            return True
        logger.bind(
            ctx=ctx, origin=redirect_url_origin, allowed=sorted(allowed_domains)
        ).warning("Redirect URI origin is not allowlisted")
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="Redirect URI not allowed")
    except HTTPException:
        # Not caught by the handler below, which would report every rejection as malformed.
        raise
    except Exception as e:
        logger.bind(ctx=ctx).exception(str(e))
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="Invalid redirect URI")
