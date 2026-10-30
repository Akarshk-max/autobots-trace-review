from datetime import datetime, timezone

import supabase_auth
from fastapi import HTTPException
from jose import jwt
from loguru import logger
from starlette.status import HTTP_403_FORBIDDEN

from src.autobots.auth.auth import get_auth
from src.autobots.auth.data_models import JwtPayload
from src.autobots.auth.providers.auth_abc import AuthABC
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.user.user import User
from src.autobots.user.user_orm_model import UserCreate, UserDoc


class SupabaseAuth(AuthABC):
    """Supabase (GoTrue) implementation of the access-token verification contract.

    Offline verification decodes the Supabase JWT locally (ES256 public key / HS secret from
    settings, ``aud="authenticated"``); online verification calls GoTrue ``GET /user`` via the
    stateless client in ``auth/auth.py``. Both provision the user in Mongo on first sight.
    """

    # ---- AuthABC: offline claims + user build -------------------------------

    def decode_claims_offline(
        self, token: str, audience: str = "authenticated", *, verify_exp: bool = True
    ) -> dict | None:
        """Single source of truth for local Supabase-JWT decoding — no GoTrue call.

        Validates signature and audience; expiry is validated unless ``verify_exp`` is False.
        Returns the raw claims dict, or ``None`` if the token is missing/invalid (or expired).
        """
        settings = SettingsProvider.sget()
        jwt_algo = settings.JWT_ALGORITHM
        key = settings.JWT_PUBLIC_KEY if jwt_algo in ["ES256"] else settings.JWT_SECRET_KEY
        try:
            return jwt.decode(
                token=token,
                key=key,
                algorithms=jwt_algo,
                audience=audience,
                options={"verify_exp": verify_exp},
            )
        except Exception as e:
            logger.warning(f"Offline access token decode failed: {e}")
            return None

    def user_from_claims(self, claims: dict) -> supabase_auth.UserResponse:
        """Build a ``UserResponse`` from verified claims.

        ``sub``/``email`` become the user id and email (both required downstream by ``UserORM``);
        ``created_at`` comes from the token ``iat`` (falling back to now), since the authoritative
        value lives in GoTrue and this path is intentionally network-free. Uses raw claims (not the
        strict ``JwtPayload``) so a validly-signed token that omits optional claims is not rejected.
        """
        iat = claims.get("iat")
        created_at = (
            datetime.fromtimestamp(iat, tz=timezone.utc) if iat else datetime.now(timezone.utc)
        )
        user = supabase_auth.User(
            id=claims["sub"],
            aud=claims.get("aud") or "authenticated",
            role=claims.get("role"),
            email=claims.get("email"),
            phone=claims.get("phone"),
            app_metadata=claims.get("app_metadata") or {},
            user_metadata=claims.get("user_metadata") or {},
            created_at=created_at,
        )
        return supabase_auth.UserResponse(user=user)

    # ---- AuthABC: verify + provision ----------------------------------------

    async def verify_token_offline(
        self, token: str, audience: str = "authenticated"
    ) -> supabase_auth.UserResponse:
        claims = self.decode_claims_offline(token, audience)
        if not claims or not claims.get("sub"):
            raise HTTPException(
                status_code=HTTP_403_FORBIDDEN, detail="Could not validate credentials"
            )
        user_res = self.user_from_claims(claims)
        if not user_res.user.email:
            raise HTTPException(status_code=401, detail="Could not validate credentials")
        await self._provision(user_res)
        return user_res

    async def verify_token_online(
        self, token: str, audience: str = "authenticated"
    ) -> supabase_auth.UserResponse:
        try:
            # Validate the token at GoTrue over httpx (no supabase client / session state).
            user_res: supabase_auth.UserResponse = await get_auth().get_user(token)
            if not user_res.user.email:
                raise HTTPException(status_code=401, detail="Could not validate credentials")
            await self._provision(user_res)
            return user_res
        except HTTPException:
            raise
        except Exception:
            logger.exception("Unexpected error while validating access token")
            raise

    @staticmethod
    async def _provision(user_res: supabase_auth.UserResponse) -> None:
        """Create the Mongo user on first sight (idempotent)."""
        ctx = Context(user_id=user_res.user.id)
        db = next(get_mongo_db())
        user_create = UserCreate(**user_res.user.model_dump(exclude_none=True))
        _: UserDoc = await User.create_if_not_exist(
            ctx=ctx, supabase_user=user_res, user_create=user_create, db=db
        )

    # ---- Supabase-specific JwtPayload helpers (not part of AuthABC) ----------
    # JwtPayload is the strict Supabase claim schema; these back the usage-tracking
    # middleware and the non-blocking forensic decode.

    def decode_payload_offline(
        self, token: str, audience: str = "authenticated"
    ) -> JwtPayload | None:
        """Offline-decode into the strict ``JwtPayload`` (signature + audience + expiry)."""
        claims = self.decode_claims_offline(token, audience)
        if claims is None:
            return None
        try:
            return JwtPayload(**claims)
        except Exception as e:
            logger.warning(f"Access token payload parse failed: {e}")
            return None

    def decode_payload_allow_expired(
        self, token: str, audience: str = "authenticated"
    ) -> JwtPayload | None:
        """Offline-decode into ``JwtPayload`` allowing expired tokens (non-blocking forensics)."""
        claims = self.decode_claims_offline(token, audience, verify_exp=False)
        if claims is None:
            return None
        try:
            return JwtPayload(**claims)
        except Exception as e:
            # Warning (not exception) because this is expected to fail sometimes and
            # must never block the caller.
            logger.warning(f"Non-blocking access token decode failed: {e}")
            return None
