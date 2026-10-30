from abc import ABC, abstractmethod

import supabase_auth


class AuthABC(ABC):
    """Provider-agnostic contract for access-token verification (the FastAPI door-check).

    A concrete provider (e.g. :class:`SupabaseAuth`) verifies an IdP-issued access token and
    returns the app's identity object (``supabase_auth.UserResponse``). Only the verification /
    door-check surface is abstracted here; IdP *operations* (sign in/up/out, oauth, refresh, ...)
    are not part of this contract yet and remain provider-specific (see ``auth/auth.py``).

    Adding a second IdP is: subclass ``AuthABC``, register it in the provider registry, and set
    ``AUTH_PROVIDER`` — no endpoint changes, since the FastAPI door-checks delegate here.
    """

    @abstractmethod
    def decode_claims_offline(
        self, token: str, audience: str = "authenticated", *, verify_exp: bool = True
    ) -> dict | None:
        """Locally verify the token (signature + audience + expiry) and return its raw claims.

        No network call. Returns ``None`` if the token is missing/invalid (or expired, when
        ``verify_exp`` is True).
        """
        raise NotImplementedError

    @abstractmethod
    def user_from_claims(self, claims: dict) -> supabase_auth.UserResponse:
        """Build the app identity object from already-verified claims (no network, no provisioning)."""
        raise NotImplementedError

    @abstractmethod
    async def verify_token_offline(
        self, token: str, audience: str = "authenticated"
    ) -> supabase_auth.UserResponse:
        """Offline-verify the token, build the user from its claims, and provision it in Mongo.

        No IdP network call. Raises ``HTTPException`` (401/403) when the token is invalid.
        """
        raise NotImplementedError

    @abstractmethod
    async def verify_token_online(
        self, token: str, audience: str = "authenticated"
    ) -> supabase_auth.UserResponse:
        """Verify the token at the IdP (authoritative — catches revoked/deleted users) and provision.

        Raises ``HTTPException`` (401/403) when the token is invalid.
        """
        raise NotImplementedError
