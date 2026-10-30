from enum import Enum
from functools import lru_cache

from src.autobots.auth.providers.auth_abc import AuthABC
from src.autobots.auth.providers.supabase_auth import SupabaseAuth


class AuthProvider(str, Enum):
    """Identity providers the app can authenticate against."""

    supabase = "supabase"


# Enum -> concrete provider class (mirrors integrations' app_type_to_integration_class registry).
AUTH_PROVIDER_MAP: dict[AuthProvider, type[AuthABC]] = {
    AuthProvider.supabase: SupabaseAuth,
}

# The active-provider default lives here in the auth package (NOT in core/settings.py) so that
# settings stays free of any dependency on the provider layer — no settings<->providers cycle.
# When a second IdP is added, layer an optional settings/env override on top of this default.
DEFAULT_AUTH_PROVIDER: AuthProvider = AuthProvider.supabase


@lru_cache
def get_auth_provider(provider: AuthProvider | None = None) -> AuthABC:
    """Return the auth-provider singleton (defaults to ``DEFAULT_AUTH_PROVIDER``).

    Cached per provider (one instance per worker), matching the ``@lru_cache get_auth()`` style.
    """
    provider = provider or DEFAULT_AUTH_PROVIDER
    provider_cls = AUTH_PROVIDER_MAP.get(provider)
    if provider_cls is None:
        raise ValueError(f"Unsupported auth provider: {provider}")
    return provider_cls()
