from pathlib import Path

import pytest

from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.retell_auth import RetellAuth, RetellAuthModel


@pytest.mark.asyncio
async def test_retell_auth_header_uses_bearer_api_key():
    ctx = Context()
    auth = RetellAuthModel(api_key="test-key")
    header = await RetellAuth.get_auth_header(ctx=ctx, app_auth=auth)
    assert header == {"Authorization": "Bearer test-key"}


def test_retell_is_added_to_app_types_enum():
    assert AppTypes.retell.value == "retell"


def test_retell_is_registered_in_app_type_to_auth_registry_file():
    repo_root = Path(__file__).resolve().parents[2]
    registry_path = repo_root / "src" / "autobots" / "integrations" / "app_type_to_integration_class.py"
    text = registry_path.read_text(encoding="utf-8")
    assert (
        "from src.autobots.integrations.integration_type.retell_auth import RetellAuth" in text
    )
    assert "AppTypes.retell" in text
