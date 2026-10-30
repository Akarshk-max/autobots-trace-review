"""MCP Server authentication — email/password → Supabase JWT with auto-refresh.

Reads Supabase config directly from SettingsProvider (same as every other module).
The user only needs email + password.
"""

from __future__ import annotations

import time

import httpx
from loguru import logger

from src.autobots.core.settings import SettingsProvider

# GoTrue REST API version — pinned to match supabase-py client
_GOTRUE_API_VERSION = "2024-01-01"
_GOTRUE_TIMEOUT = httpx.Timeout(15.0)


class MCPAuth:
    """Manages a Supabase JWT session for the MCP server.

    Reads Supabase URL and anon key from SettingsProvider (no extra config needed).
    Signs in with email + password to obtain a JWT. Auto-refreshes before expiry.
    """

    def __init__(self, email: str, password: str) -> None:
        settings = SettingsProvider.sget()
        self._email = email
        self._password = password
        self._supabase_url = settings.SUPABASE_URL.rstrip("/")
        self._anon_key = settings.SUPABASE_ANON_KEY
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0

    @property
    def access_token(self) -> str:
        """Return the current access token, refreshing if needed."""
        if self._access_token is None:
            raise RuntimeError("Not authenticated — call authenticate() first")
        return self._access_token

    @property
    def expires_in(self) -> int:
        """Seconds until the current token should be treated as expired (never negative)."""
        return max(0, int(self._expires_at - time.time()))

    async def _gotrue(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        """Make a request to Supabase GoTrue."""
        url = f"{self._supabase_url}/auth/v1/{path}"
        headers = {
            "apikey": self._anon_key,
            "Authorization": f"Bearer {self._anon_key}",
            "Content-Type": "application/json",
            "X-Supabase-Api-Version": _GOTRUE_API_VERSION,
        }
        async with httpx.AsyncClient(timeout=_GOTRUE_TIMEOUT) as client:
            return await client.request(method, url, json=json, params=params, headers=headers)

    async def authenticate(self) -> None:
        """Sign in with email + password to obtain a JWT session."""
        logger.info("Authenticating with Supabase ...")
        res = await self._gotrue(
            "POST",
            "token",
            params={"grant_type": "password"},
            json={"email": self._email, "password": self._password},
        )
        if res.status_code >= 400:
            detail = ""
            try:
                detail = res.json().get("error_description", res.text[:300])
            except Exception:
                detail = res.text[:300]
            raise RuntimeError(f"Authentication failed ({res.status_code}): {detail}")

        data = res.json()
        self._access_token = data.get("access_token")
        self._refresh_token = data.get("refresh_token")
        if not self._access_token:
            raise RuntimeError("Authentication succeeded but no access_token returned")

        expires_in = data.get("expires_in", 3600) or 3600
        self._expires_at = time.time() + expires_in - 60
        logger.info("Authenticated successfully")

    async def ensure_fresh_token(self) -> str:
        """Return a valid access token, refreshing if near expiry."""
        if self._access_token is None:
            await self.authenticate()
            return self._access_token  # type: ignore[return-value]

        if time.time() >= self._expires_at:
            logger.info("Access token near expiry, refreshing ...")
            if not self._refresh_token:
                await self.authenticate()
                return self._access_token  # type: ignore[return-value]
            try:
                res = await self._gotrue(
                    "POST",
                    "token",
                    params={"grant_type": "refresh_token"},
                    json={"refresh_token": self._refresh_token},
                )
                if res.status_code >= 400:
                    raise RuntimeError(f"Token refresh failed ({res.status_code})")
                data = res.json()
                self._access_token = data.get("access_token")
                self._refresh_token = data.get("refresh_token")
                expires_in = data.get("expires_in", 3600) or 3600
                self._expires_at = time.time() + expires_in - 60
                logger.info("Token refreshed successfully")
            except Exception:
                logger.warning("Token refresh failed, re-authenticating ...")
                await self.authenticate()

        return self._access_token  # type: ignore[return-value]
