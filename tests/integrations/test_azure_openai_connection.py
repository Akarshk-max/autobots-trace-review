from pathlib import Path

import pytest

from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.azure_openai_auth import (
    AzureOpenAIAuth,
    AzureOpenAIAuthModel,
)


@pytest.mark.asyncio
async def test_azure_openai_auth_header_uses_api_key_header():
    ctx = Context()
    auth = AzureOpenAIAuthModel(api_key="test-key", endpoint="https://example.openai.azure.com/")
    header = await AzureOpenAIAuth.get_auth_header(ctx=ctx, app_auth=auth)
    assert header == {"api-key": "test-key"}


def test_azure_openai_is_added_to_app_types_enum():
    assert AppTypes.azure_openai.value == "azure_openai"


def test_azure_openai_auth_get_app_returns_azure_openai_app_type():
    assert AzureOpenAIAuth.get_app() == AppTypes.azure_openai


def test_azure_openai_auth_model_defaults():
    auth = AzureOpenAIAuthModel(api_key="k", endpoint="https://example.openai.azure.com/")
    assert auth.api_version == "2025-03-01-preview"
    assert auth.title_gen_deployment == ""


def test_azure_openai_is_registered_in_app_type_to_auth_registry_file():
    repo_root = Path(__file__).resolve().parents[2]
    registry_path = repo_root / "src" / "autobots" / "integrations" / "app_type_to_integration_class.py"
    text = registry_path.read_text(encoding="utf-8")
    assert (
        "from src.autobots.integrations.integration_type.azure_openai_auth import "
        "AzureOpenAIAuth" in text
    )
    assert "AppTypes.azure_openai: AzureOpenAIAuth," in text


def test_azure_openai_byok_client_exposes_provider_tagged_surfaces():
    from src.autobots.conn.openai.openai_client import (
        AzureOpenAIBYOK,
        get_conn_azure_openai_byok,
    )

    conn = get_conn_azure_openai_byok(
        api_key="dummy-key",
        endpoint="https://example.openai.azure.com/",
        api_version="2025-03-01-preview",
    )
    assert isinstance(conn, AzureOpenAIBYOK)
    assert conn.provider == "azure_byok"
    assert conn.client is not None
    assert conn.openai_chat.provider == "azure_byok"
    assert conn.openai_chat.client is conn.client
    assert conn.openai_responses.provider == "azure_byok"
    assert conn.openai_responses.client is conn.client
    assert conn.openai_files.client is conn.client
    # BYOK deliberately wires no streaming surfaces (out of scope for this layer).
    assert not hasattr(conn, "openai_chat_stream")
    assert not hasattr(conn, "openai_responses_stream")


def test_app_conn_get_openai_dispatches_azure_byok():
    import inspect
    import typing

    from src.autobots.integrations.app_conn.app_conn import AppConn

    provider_annotation = inspect.signature(AppConn.get_openai).parameters["provider"].annotation
    assert set(typing.get_args(provider_annotation)) == {"openai", "azure", "azure_byok"}

    source = inspect.getsource(AppConn.get_openai)
    assert 'case "azure_byok":' in source
    assert "AppConnAzureOpenAI.get(" in source
