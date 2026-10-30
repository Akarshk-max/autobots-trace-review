"""Three defects found while migrating autobots-surface onto /v1/integrations.

Two of them combined to make a Google connection permanently undeletable: a preview
deployment's origin is not allowlisted, so consent is refused; `connect` has already
stored the credentials by then; and the resulting record — credentials, no auth — cannot
be deleted, because the revoke step refuses to parse an empty auth.
"""

import pytest
from fastapi import HTTPException
from loguru import logger as loguru_logger

from src.autobots.auth.security import raise_if_redirect_uri_not_allowed
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.integrations.user_integration import UserIntegration


class _Collection:
    name = "AppAuth"


class _Db:
    """Enough of a database for IntegrationCRUD's constructor."""

    def __getitem__(self, name):
        return _Collection()

USER_ID = "11111111-2222-3333-4444-555555555555"



def _captured(level: str = "DEBUG"):
    """loguru does not feed pytest's caplog. These paths all end the same way — the delete
    proceeds — so the log is the only evidence of which one ran."""
    written: list[str] = []
    sink = loguru_logger.add(written.append, level=level)
    return written, sink


# --------------------------------------------------------- revoking, without blocking


@pytest.mark.asyncio
async def test_a_connection_whose_consent_never_completed_is_not_revoked(monkeypatch):
    """`connect` stores creds before the consent screen, so a user who never comes back
    leaves a record with no auth. Nothing was granted; there is nothing to revoke."""
    from src.autobots.integrations.integration_type.custom_auth import CustomAuth

    revoked = []

    async def fake_revoke(ctx, app_creds, app_auth):
        revoked.append(app_auth)
        return True

    monkeypatch.setattr(CustomAuth, "revoke_external_auth", fake_revoke)

    written, sink = _captured("INFO")
    try:
        await UserIntegration.revoke_external_access_for_delete(
            ctx=Context(user_id=USER_ID),
            integration_docs=[{"app_name": "custom", "name": "half-done", "auth": {}}],
        )
    finally:
        loguru_logger.remove(sink)
    said = "".join(written)

    assert revoked == [], "nothing was granted, so nothing should be revoked"
    assert "nothing to revoke" in said, "it should skip knowingly, not fail to parse"


@pytest.mark.asyncio
async def test_a_connection_with_auth_is_revoked(monkeypatch):
    """The ordinary case: disconnect should still drop the grant at the provider."""
    from src.autobots.integrations.integration_type.custom_auth import CustomAuth

    revoked = []

    async def fake_revoke(ctx, app_creds, app_auth):
        revoked.append(app_auth)
        return True

    monkeypatch.setattr(CustomAuth, "revoke_external_auth", fake_revoke)

    await UserIntegration.revoke_external_access_for_delete(
        ctx=Context(user_id=USER_ID),
        integration_docs=[{"app_name": "custom", "name": "live", "auth": {"api_key": "sk-1"}}],
    )

    assert len(revoked) == 1


@pytest.mark.asyncio
async def test_a_provider_that_refuses_does_not_block_the_delete(monkeypatch):
    """It used to raise 502 and abort, which is how a connection became impossible to
    remove."""
    from src.autobots.integrations.integration_type.custom_auth import CustomAuth

    async def fake_revoke(ctx, app_creds, app_auth):
        return Exception("provider said no")

    monkeypatch.setattr(CustomAuth, "revoke_external_auth", fake_revoke)

    written, sink = _captured("WARNING")
    try:
        # must return, not raise
        await UserIntegration.revoke_external_access_for_delete(
            ctx=Context(user_id=USER_ID),
            integration_docs=[{"app_name": "custom", "name": "live", "auth": {"api_key": "sk-1"}}],
        )
    finally:
        loguru_logger.remove(sink)
    assert "still being deleted" in "".join(written)


@pytest.mark.asyncio
async def test_auth_that_cannot_be_parsed_is_skipped_not_fatal(monkeypatch):
    """Same reasoning: unreadable auth is worth a warning, not a record nobody can delete."""
    written, sink = _captured("WARNING")
    try:
        await UserIntegration.revoke_external_access_for_delete(
            ctx=Context(user_id=USER_ID),
            integration_docs=[{"app_name": "custom", "name": "corrupt", "auth": {"junk": 1}}],
        )
    finally:
        loguru_logger.remove(sink)
    assert "skipping revoke" in "".join(written)


@pytest.mark.asyncio
async def test_a_provider_that_raises_does_not_block_the_delete(monkeypatch):
    from src.autobots.integrations.integration_type.custom_auth import CustomAuth

    async def fake_revoke(ctx, app_creds, app_auth):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(CustomAuth, "revoke_external_auth", fake_revoke)

    await UserIntegration.revoke_external_access_for_delete(
        ctx=Context(user_id=USER_ID),
        integration_docs=[{"app_name": "custom", "name": "live", "auth": {"api_key": "sk-1"}}],
    )


# ------------------------------------------------------------------ the redirect allowlist


@pytest.mark.asyncio
async def test_an_origin_with_a_port_can_be_allowlisted(monkeypatch):
    """`HttpUrl.host` drops the port and `str(HttpUrl(...))` keeps it, so every origin
    carrying a port was rejected — including the shipped default, http://localhost:3000."""
    settings = SettingsProvider.sget()
    monkeypatch.setattr(settings, "ALLOWED_ORIGINS", ["http://localhost:3000/"])

    assert await raise_if_redirect_uri_not_allowed(ctx=Context(user_id=USER_ID), redirect_uri="http://localhost:3000/connections")


@pytest.mark.asyncio
async def test_a_default_port_and_no_port_are_the_same_origin(monkeypatch):
    settings = SettingsProvider.sget()
    monkeypatch.setattr(settings, "ALLOWED_ORIGINS", ["https://app.runkai.ai/"])

    assert await raise_if_redirect_uri_not_allowed(ctx=Context(user_id=USER_ID), redirect_uri="https://app.runkai.ai:443/connections")


@pytest.mark.asyncio
async def test_a_wrong_origin_says_it_is_not_allowed_not_that_it_is_invalid(monkeypatch):
    """The rejection was raised inside a try that caught it, so every refusal reported as
    "Invalid redirect URI" — pointing at the URI when the cause is server configuration."""
    settings = SettingsProvider.sget()
    monkeypatch.setattr(settings, "ALLOWED_ORIGINS", ["https://app.runkai.ai/"])

    with pytest.raises(HTTPException) as raised:
        await raise_if_redirect_uri_not_allowed(ctx=Context(user_id=USER_ID), redirect_uri="https://preview.vercel.app/connections")

    assert raised.value.status_code == 403
    assert raised.value.detail == "Redirect URI not allowed"


@pytest.mark.asyncio
async def test_a_genuinely_malformed_uri_still_reports_as_invalid(monkeypatch):
    settings = SettingsProvider.sget()
    monkeypatch.setattr(settings, "ALLOWED_ORIGINS", ["https://app.runkai.ai/"])

    with pytest.raises(HTTPException) as raised:
        await raise_if_redirect_uri_not_allowed(ctx=Context(user_id=USER_ID), redirect_uri="not a url at all")

    assert raised.value.detail == "Invalid redirect URI"
