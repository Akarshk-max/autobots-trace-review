"""Auth-cache tests for MCPAuthMiddleware.

Two properties are guarded here:

* The cache key must include the password. Keying on email alone means that once
  any caller authenticates an address, every later request for that address reuses
  the cached session and the password is never checked again — a wrong password
  would be accepted.
* Only *successful* authentications may be cached. Caching on the way in lets bad
  credentials claim entries that can never hold a token (so every retry hits
  Supabase regardless) while evicting sessions that do work.
"""

import pytest

from src.autobots.mcp_server import middleware


class _StubAuth:
    """Stands in for MCPAuth: records calls, optionally fails to authenticate."""

    def __init__(self, email: str, password: str) -> None:
        self.email = email
        self.password = password
        self.token_calls = 0

    async def ensure_fresh_token(self) -> str:
        self.token_calls += 1
        if self.password != "correct-horse":
            raise RuntimeError("Authentication failed (400): invalid login credentials")
        return "token-for-" + self.email


@pytest.fixture(autouse=True)
def _stub_mcp_auth(monkeypatch):
    middleware._auth_cache.clear()
    monkeypatch.setattr(middleware, "MCPAuth", _StubAuth)
    yield
    middleware._auth_cache.clear()


@pytest.mark.asyncio
async def test_same_credentials_reuse_one_auth_object():
    a = await middleware._authenticate("user@example.com", "correct-horse")
    b = await middleware._authenticate("user@example.com", "correct-horse")
    assert a is b, "identical credentials should hit the cache"
    assert len(middleware._auth_cache) == 1


@pytest.mark.asyncio
async def test_different_password_does_not_reuse_cached_auth():
    """The bug this guards: a wrong password must not ride on a cached session."""
    good = await middleware._authenticate("user@example.com", "correct-horse")
    with pytest.raises(RuntimeError):
        await middleware._authenticate("user@example.com", "wrong-password")
    assert len(middleware._auth_cache) == 1
    assert (
        middleware._auth_cache[middleware._cache_key("user@example.com", "correct-horse")] is good
    )


@pytest.mark.asyncio
async def test_password_is_not_stored_in_the_cache_key():
    await middleware._authenticate("user@example.com", "correct-horse")
    (key,) = middleware._auth_cache.keys()
    email, digest = key
    assert email == "user@example.com"
    assert "correct-horse" not in digest
    assert len(digest) == 64, "expected a sha256 hex digest"


@pytest.mark.asyncio
async def test_failed_auth_is_never_cached():
    """A wrong password must leave no trace — otherwise it is a free cache slot."""
    for _ in range(10):
        with pytest.raises(RuntimeError):
            await middleware._authenticate("attacker@example.com", "guess")
    assert len(middleware._auth_cache) == 0


@pytest.mark.asyncio
async def test_bad_credential_spam_cannot_evict_a_live_session():
    """RISK-7: credential spam used to push working sessions out of a bounded cache."""
    live = await middleware._authenticate("real@example.com", "correct-horse")

    for i in range(middleware._AUTH_CACHE_MAX * 2):
        with pytest.raises(RuntimeError):
            await middleware._authenticate(f"attacker{i}@example.com", "guess")

    assert len(middleware._auth_cache) == 1
    assert await middleware._authenticate("real@example.com", "correct-horse") is live
    assert live.token_calls == 2, "the live session must not have been re-authenticated"


@pytest.mark.asyncio
async def test_cache_is_bounded_and_evicts_least_recently_used():
    first = await middleware._authenticate("user0@example.com", "correct-horse")
    for i in range(1, middleware._AUTH_CACHE_MAX):
        await middleware._authenticate(f"user{i}@example.com", "correct-horse")
    assert len(middleware._auth_cache) == middleware._AUTH_CACHE_MAX

    # Touch the oldest entry so it is no longer the eviction candidate.
    assert await middleware._authenticate("user0@example.com", "correct-horse") is first

    await middleware._authenticate("overflow@example.com", "correct-horse")
    assert len(middleware._auth_cache) == middleware._AUTH_CACHE_MAX
    assert middleware._cache_key("user0@example.com", "correct-horse") in middleware._auth_cache
    assert middleware._cache_key("user1@example.com", "correct-horse") not in middleware._auth_cache
