"""The rest of `/v1/integrations`: reading, refreshing and disconnecting.

These call the endpoint functions directly. What is being checked is the part a route
test can get wrong and a unit test cannot see: that `/types` is reachable at all, and
that nothing hands a stored secret back out.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.autobots.api.v1.api_integrations import api_integrations
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationDoc

USER_ID = "11111111-2222-3333-4444-555555555555"


def _request():
    return SimpleNamespace(state=SimpleNamespace(context=Context(user_id=USER_ID)))


def _user_res():
    return SimpleNamespace(user=SimpleNamespace(id=USER_ID))


def _doc(**over):
    fields = dict(
        id="000000000000000000000000",
        user_id=USER_ID,
        app_name=AppTypes.custom,
        name="my-api",
        auth={"api_key": "sk-secret"},
        creds={"api_key": "sk-secret"},
    )
    fields.update(over)
    return IntegrationDoc.model_validate(fields)


def test_types_is_matched_before_the_id_route():
    """`/types` must be declared first or `GET /types` reads as `GET /{id}`.

    FastAPI matches in declaration order, so this is a property of the file's layout —
    the kind that survives every unit test and 404s in production.
    """
    paths = [r.path for r in api_integrations.router.routes]
    assert paths.index("/integrations/types") < paths.index("/integrations/{id}")
    assert paths.index("/integrations/types/{app}") < paths.index("/integrations/{id}")


@pytest.mark.asyncio
async def test_reading_a_connection_does_not_return_its_secrets(monkeypatch):
    async def fake_get(ctx, db, user, integration_id, decrypt=False):
        return _doc()

    monkeypatch.setattr(api_integrations.UserIntegration, "get", fake_get)

    doc = await api_integrations.get_integration(
        request=_request(), id="000000000000000000000000", user_res=_user_res(), db=None,
        space_member=None,
    )

    assert doc.auth == {} and doc.creds == {}
    assert "sk-secret" not in doc.model_dump_json()


@pytest.mark.asyncio
async def test_a_missing_connection_is_a_404_not_an_empty_body(monkeypatch):
    async def fake_get(ctx, db, user, integration_id, decrypt=False):
        return None

    monkeypatch.setattr(api_integrations.UserIntegration, "get", fake_get)

    with pytest.raises(HTTPException) as raised:
        await api_integrations.get_integration(
            request=_request(), id="nope", user_res=_user_res(), db=None, space_member=None,
        )
    assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_refresh_writes_and_returns_the_connection_without_its_new_token(monkeypatch):
    calls = {}

    async def fake_refresh(ctx, db, user, name, app_name, decrypt=False):
        calls["name"], calls["app_name"] = name, app_name
        return _doc(auth={"access_token": "fresh-token"})

    monkeypatch.setattr(api_integrations.UserIntegration, "refresh", fake_refresh)

    doc = await api_integrations.refresh_integration(
        request=_request(), name="my-api", app=AppTypes.custom, user_res=_user_res(), db=None,
        space_member=None,
    )

    assert calls == {"name": "my-api", "app_name": AppTypes.custom}
    assert "fresh-token" not in doc.model_dump_json()


@pytest.mark.asyncio
async def test_disconnect_deletes_the_id_in_the_path(monkeypatch):
    """The id comes from the path alone — the old route also read one from the body."""
    seen = {}

    async def fake_delete(ctx, db, user, app_auth_delete, decrypt=False):
        seen["filter"] = app_auth_delete
        return 1

    monkeypatch.setattr(api_integrations.UserIntegration, "delete", fake_delete)

    result = await api_integrations.disconnect_integration(
        request=_request(), id="000000000000000000000000", user_res=_user_res(), db=None,
        space_member=None,
    )

    assert result.deleted_count == 1
    assert seen["filter"].id == "000000000000000000000000"
    assert seen["filter"].name is None and seen["filter"].app_name is None


@pytest.mark.asyncio
async def test_a_connection_that_will_not_delete_is_a_404(monkeypatch):
    async def fake_delete(ctx, db, user, app_auth_delete, decrypt=False):
        return 0

    monkeypatch.setattr(api_integrations.UserIntegration, "delete", fake_delete)

    with pytest.raises(HTTPException) as raised:
        await api_integrations.disconnect_integration(
            request=_request(), id="nope", user_res=_user_res(), db=None, space_member=None,
        )
    assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_the_connect_form_schema_comes_from_the_app_itself():
    """The UI builds its form from this, so it has to be the app's own creds model."""
    schema = await api_integrations.get_connect_schema(
        request=_request(), app=AppTypes.custom, _user=None,
    )
    assert "properties" in schema

    oauth_schema = await api_integrations.get_connect_schema(
        request=_request(), app=AppTypes.github, _user=None,
    )
    assert not oauth_schema.get("properties")  # nothing to type: connect and follow the URL
