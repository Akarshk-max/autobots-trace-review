from pathlib import Path

import pytest

from src.autobots.data_model.context import Context
from src.autobots.integrations.app_type_to_integration_class import AppTypeToIntegrationClass
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.nango_auth import (
    NANGO_API_BASE_URL,
    NangoAuth,
    NangoAuthModel,
)

API_KEY = "nango-test-key"


@pytest.mark.asyncio
async def test_auth_header_uses_bearer_api_key():
    header = await NangoAuth.get_auth_header(
        ctx=Context(), app_auth=NangoAuthModel(api_key=API_KEY)
    )
    assert header == {"Authorization": f"Bearer {API_KEY}"}


def test_api_key_is_required_and_domain_defaults_to_cloud():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        NangoAuthModel()
    assert NangoAuthModel(api_key=API_KEY).api_domain == NANGO_API_BASE_URL


@pytest.mark.asyncio
async def test_get_api_domain_defaults_and_strips_trailing_slash():
    assert await NangoAuth.get_api_domain(app_creds=None) == NANGO_API_BASE_URL
    assert (
        await NangoAuth.get_api_domain(app_creds=NangoAuthModel(api_key=API_KEY))
        == NANGO_API_BASE_URL
    )
    assert (
        await NangoAuth.get_api_domain(
            app_creds=NangoAuthModel(api_key=API_KEY, api_domain="http://localhost:3003/")
        )
        == "http://localhost:3003"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "api_domain, url, expected",
    [
        pytest.param(
            NANGO_API_BASE_URL,
            "https://api.nango.dev/connection",
            "https://api.nango.dev/connection",
            id="cloud_url_unchanged",
        ),
        pytest.param(
            "http://localhost:3003",
            "https://api.nango.dev/connection",
            "http://localhost:3003/connection",
            id="self_hosted_host_and_scheme",
        ),
        pytest.param(
            "http://localhost:3003",
            "https://api.nango.dev/connection?provider_config_key=slack",
            "http://localhost:3003/connection?provider_config_key=slack",
            id="query_preserved",
        ),
        pytest.param(
            "https://nango.internal/api",
            "https://api.nango.dev/connection",
            "https://nango.internal/api/connection",
            id="domain_path_prefix",
        ),
    ],
)
async def test_self_hosted_domain_rewrites_the_url(api_domain, url, expected):
    auth = NangoAuthModel(api_key=API_KEY, api_domain=api_domain)
    assert (
        await NangoAuth.get_updated_url(ctx=Context(), app_creds=None, app_auth=auth, url=url)
        == expected
    )


@pytest.mark.asyncio
async def test_api_domain_survives_create_and_refresh():
    ctx = Context()
    creds = NangoAuthModel(api_key=API_KEY, api_domain="http://localhost:3003")
    created = await NangoAuth.create_auth(ctx=ctx, app_creds=creds)
    assert created == creds

    assert await NangoAuth.is_refresh_required(ctx=ctx, app_auth=created) is False
    assert await NangoAuth.refresh_auth(ctx=ctx, app_creds=creds, app_auth=created) == created
    assert await NangoAuth.refresh_auth(ctx=ctx, app_creds=creds, app_auth=None) == creds


def test_creds_json_schema_serves_key_and_domain():
    """This schema is what `GET /app-creds/types/nango` serves to the UI."""
    schema = NangoAuth.get_creds_model_type().model_json_schema()
    assert list(schema["properties"]) == ["api_key", "api_domain"]
    assert schema["required"] == ["api_key"]
    assert schema["properties"]["api_domain"]["default"] == NANGO_API_BASE_URL


def test_nango_is_added_to_app_types_enum():
    assert AppTypes.nango.value == "nango"


def test_nango_is_registered_in_app_type_to_auth_registry():
    assert AppTypeToIntegrationClass.get(AppTypes.nango) is NangoAuth


def test_nango_is_registered_in_app_type_to_auth_registry_file():
    repo_root = Path(__file__).resolve().parents[2]
    registry_path = repo_root / "src" / "autobots" / "integrations" / "app_type_to_integration_class.py"
    text = registry_path.read_text(encoding="utf-8")
    assert "from src.autobots.integrations.integration_type.nango_auth import NangoAuth" in text
    assert "AppTypes.nango" in text
