"""`name` and `app_name` together identify a connection, and neither alone will do.

The same name can exist under several apps, so the pair is the key. What made this
expensive to diagnose is the shape of the failure: api_call's `app_name` defaults to
`anon`, which matches no connection, so a call that names a connection and omits the app
goes out with no Authorization header — and the provider answers 401, which reads as
expired credentials or missing scopes. It is neither.
"""

import pytest
from loguru import logger as loguru_logger

from src.autobots.data_model.context import Context
from src.autobots.integrations import user_integration as user_integration_module
from src.autobots.integrations.app_creds import user_app_creds as user_app_creds_module
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationDoc
from src.autobots.integrations.integration_type.custom_auth import CustomAuth
from src.autobots.user.user_orm_model import UserORM

USER_ID = "11111111-2222-3333-4444-555555555555"


def _doc(name: str, app: AppTypes, key: str):
    return IntegrationDoc.model_validate(
        dict(
            id="0" * 24, user_id=USER_ID, app_name=app, name=name,
            auth={"api_key": key}, creds={}, default=True,
        )
    )


@pytest.fixture
def connections(monkeypatch):
    """A store keyed the way the real one is: by (name, app_name)."""

    def _install(records: dict):
        async def fake_get_by_name(ctx, db, user, name, app_name=None, decrypt=False):
            for (n, a), key in records.items():
                if n == name and (app_name is None or app_name == a):
                    return _doc(n, a, key)
            return None

        async def fake_get_creds(ctx, db, user, name, app_name=None, decrypt=False):
            return None

        async def fake_header(ctx, app_auth, config=None):
            return {"Authorization": f"Bearer {app_auth.api_key}"}

        async def fake_is_refresh(ctx, app_auth):
            return False

        monkeypatch.setattr(
            user_integration_module.UserIntegration, "get_by_name", fake_get_by_name
        )
        monkeypatch.setattr(
            user_app_creds_module.UserAppCreds, "get_app_creds_by_name", fake_get_creds
        )
        monkeypatch.setattr(CustomAuth, "get_auth_header", fake_header)
        monkeypatch.setattr(CustomAuth, "is_refresh_required", fake_is_refresh)

    return _install


async def _header(name: str, app: AppTypes = AppTypes.anon):
    """`anon` is the default because it is APIRequest.app_name's default — this is what
    a caller that names a connection and says nothing about the app actually sends."""
    return await user_integration_module.UserIntegration.auth_header(
        ctx=Context(user_id=USER_ID), db=None, user=UserORM(id=USER_ID),
        name=name, app_name=app, decrypt=True,
    )


@pytest.mark.asyncio
async def test_the_pair_identifies_the_connection(connections):
    connections({("googleapitest", AppTypes.custom): "sk-1"})
    assert await _header("googleapitest", app=AppTypes.custom) == {
        "Authorization": "Bearer sk-1"
    }


@pytest.mark.asyncio
async def test_one_name_under_two_apps_resolves_to_the_app_asked_for(connections):
    """Why the pair is the key: a name on its own is ambiguous."""
    connections({
        ("work", AppTypes.custom): "sk-custom",
        ("work", AppTypes.openai): "sk-openai",
    })

    header = await _header("work", app=AppTypes.custom)

    assert header == {"Authorization": "Bearer sk-custom"}, "the app must decide which one"


@pytest.mark.asyncio
async def test_naming_a_connection_but_no_app_authenticates_nothing(connections):
    """The production failure, in one line: app_name left at `anon` matches no
    connection, so the request carries no Authorization header at all."""
    connections({("google", AppTypes.custom): "sk-1"})

    assert await _header("google") == {}


@pytest.mark.asyncio
async def test_the_miss_is_logged_with_both_halves(connections):
    """A silent empty header cost an afternoon of chasing Gmail scopes. The log has to
    name the pair that missed, so the next reader sees a lookup, not a credentials fault."""
    connections({("google", AppTypes.custom): "sk-1"})

    # loguru does not feed pytest's caplog; take its output directly
    written: list[str] = []
    sink_id = loguru_logger.add(written.append, level="WARNING")
    try:
        await _header("google", app=AppTypes.google_gmail)
    finally:
        loguru_logger.remove(sink_id)

    said = "".join(written)
    assert "no Authorization header" in said, "an unauthenticated request must say so"
    assert "google" in said and "google_gmail" in said, "the log must name the pair that missed"
