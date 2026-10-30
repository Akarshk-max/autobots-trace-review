"""A single connect call has to save the credentials it was given.

Five providers — salesforce, intuit, workday, ukg, icims — build their consent URL out of
the customer's own client id and secret, and their callback needs them again to exchange
the code. The old flow got them from a separate `POST /app-creds` beforehand. With one
call and a frontend that no longer makes that first request, connect must store them
itself, or the URL is built from nothing.
"""

from types import SimpleNamespace

import pytest

from src.autobots.api.v1.api_integrations import api_integrations
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationConnectResult

USER_ID = "11111111-2222-3333-4444-555555555555"


def _request():
    return SimpleNamespace(
        state=SimpleNamespace(context=Context(user_id=USER_ID)),
        url=SimpleNamespace(netloc="api.runkai.ai", scheme="https"),
    )


@pytest.mark.asyncio
async def test_creds_are_stored_before_the_user_is_sent_to_the_provider(monkeypatch):
    order = []

    async def fake_store(ctx, db, user, app_name, name, creds):
        order.append(("stored", app_name, name, creds))

    async def fake_connect(ctx, db, user, req, decrypt=False):
        order.append(("connect", req.name))
        return IntegrationConnectResult(redirect_url="https://provider/authorize?x=1")

    class _Cls:
        @staticmethod
        def oauth_callback_path():
            return "/v1/auth/salesforce/oauth-callback"

        connect = staticmethod(fake_connect)

    monkeypatch.setattr(api_integrations.UserAppCreds, "store_creds", fake_store)
    # the registry is dict-backed, so its `get` cannot be patched — replace the name
    monkeypatch.setattr(
        api_integrations, "AppTypeToIntegrationClass", SimpleNamespace(get=lambda app: _Cls)
    )

    result = await api_integrations.connect_integration(
        request=_request(),
        req=api_integrations.IntegrationConnectRequest(
            app_name=AppTypes.salesforce,
            name="my-sf",
            creds={"client_id": "cid", "client_secret": "secret"},
            redirect_uri="https://app.runkai.ai/connections",
        ),
        user_res=SimpleNamespace(user=SimpleNamespace(id=USER_ID)),
        db=None,
        space_member=None,
    )

    assert result.redirect_url
    assert [step[0] for step in order] == ["stored", "connect"], (
        "the credentials must be saved before the consent URL is handed out"
    )
    assert order[0][3] == {"client_id": "cid", "client_secret": "secret"}


@pytest.mark.asyncio
async def test_an_oauth_app_with_no_creds_stores_nothing(monkeypatch):
    """Google and friends have no customer credentials — there is nothing to save."""
    calls = []

    async def fake_store(**kwargs):
        calls.append(kwargs)

    async def fake_connect(ctx, db, user, req, decrypt=False):
        return IntegrationConnectResult(redirect_url="https://accounts.google.com/o/oauth2")

    class _Cls:
        @staticmethod
        def oauth_callback_path():
            return "/v1/auth/google/oauth-callback"

        connect = staticmethod(fake_connect)

    monkeypatch.setattr(api_integrations.UserAppCreds, "store_creds", fake_store)
    # the registry is dict-backed, so its `get` cannot be patched — replace the name
    monkeypatch.setattr(
        api_integrations, "AppTypeToIntegrationClass", SimpleNamespace(get=lambda app: _Cls)
    )

    await api_integrations.connect_integration(
        request=_request(),
        req=api_integrations.IntegrationConnectRequest(
            app_name=AppTypes.google, name="my-google", redirect_uri="https://app.runkai.ai/x"
        ),
        user_res=SimpleNamespace(user=SimpleNamespace(id=USER_ID)),
        db=None,
        space_member=None,
    )

    assert calls == []
