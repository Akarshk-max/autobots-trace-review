import base64
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from src.autobots.data_model.context import Context
from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds
from src.autobots.integrations.app_type_to_integration_class import AppTypeToIntegrationClass
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.crypto import encoder_decoder
from src.autobots.integrations.crypto.helpers import decrypt_if_needed, encrypt_if_needed
from src.autobots.integrations.integration_type.custom_auth import CustomAuth, CustomAuthModel
from src.autobots.llm.tools.action_tools import ActionTools

API_KEY = "sk-live-abcdef123456"


# ---------------------------------------------------------------- auth header


@pytest.mark.asyncio
async def test_auth_header_is_bearer_api_key():
    header = await CustomAuth.get_auth_header(
        ctx=Context(), app_auth=CustomAuthModel(api_key=API_KEY)
    )
    assert header == {"Authorization": f"Bearer {API_KEY}"}


def test_api_key_is_required():
    with pytest.raises(ValidationError):
        CustomAuthModel()


def test_credential_carries_only_the_key_and_its_placement():
    """No free-form header map: anything non-secret belongs in the action config."""
    assert set(CustomAuthModel(api_key=API_KEY).model_dump()) == {
        "api_key",
        "header_name",
        "header_value_prefix",
        "base_url",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields, expected",
    [
        pytest.param({}, {"Authorization": f"Bearer {API_KEY}"}, id="default_bearer"),
        pytest.param(
            {"header_name": "X-Api-Key", "header_value_prefix": ""},
            {"X-Api-Key": API_KEY},
            id="bare_key_in_custom_header",
        ),
        pytest.param(
            {"header_value_prefix": "Token"},
            {"Authorization": f"Token {API_KEY}"},
            id="non_bearer_scheme",
        ),
        pytest.param(
            {"header_value_prefix": "Basic"},
            {"Authorization": f"Basic {API_KEY}"},
            id="basic_scheme",
        ),
    ],
)
async def test_key_placement_is_configurable(fields, expected):
    header = await CustomAuth.get_auth_header(
        ctx=Context(), app_auth=CustomAuthModel(api_key=API_KEY, **fields)
    )
    assert header == expected


def test_prefix_survives_the_whitespace_strip_applied_on_save():
    """Regression: creds are stripped on save, so the separator cannot live in the prefix.

    `_strip_spaces_from_creds` turns "Bearer " into "Bearer"; concatenating that
    directly would send "Bearersk-live-...".
    """
    submitted = {"api_key": API_KEY, "header_value_prefix": "Bearer "}
    stored = UserAppCreds._strip_spaces_from_creds(submitted)
    assert stored["header_value_prefix"] == "Bearer"

    assert CustomAuthModel.model_validate(stored).auth_header() == {
        "Authorization": f"Bearer {API_KEY}"
    }


@pytest.mark.parametrize(
    "pasted",
    [
        pytest.param(f"Bearer {API_KEY}", id="exact_case"),
        pytest.param(f"bearer {API_KEY}", id="lowercase"),
    ],
)
def test_a_key_pasted_with_its_scheme_is_not_double_prefixed(pasted):
    assert CustomAuthModel(api_key=pasted).auth_header() == {"Authorization": pasted}


def test_a_key_merely_starting_with_the_scheme_is_still_prefixed():
    """"Bearerish" is a key, not a scheme - only "<prefix> " counts."""
    assert CustomAuthModel(api_key="Bearerish").auth_header() == {
        "Authorization": "Bearer Bearerish"
    }


def test_a_credential_stored_before_placement_existed_still_works():
    """Defaults reproduce the old hardcoded behaviour, so old rows are unaffected."""
    assert CustomAuthModel.model_validate({"api_key": API_KEY}).auth_header() == {
        "Authorization": f"Bearer {API_KEY}"
    }


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"header_name": "X Api Key"}, id="name_with_space"),
        pytest.param({"header_name": ""}, id="empty_name"),
        pytest.param({"header_name": "X-Api-Key:"}, id="colon_in_name"),
        pytest.param({"header_name": "X-Api\r\nEvil"}, id="crlf_in_name"),
        pytest.param({"header_value_prefix": "Bearer\r\nX-Evil: yes"}, id="crlf_in_prefix"),
    ],
)
def test_invalid_placement_is_rejected(fields):
    with pytest.raises(ValidationError):
        CustomAuthModel(api_key=API_KEY, **fields)


# ------------------------------------------------------------------ lifecycle


@pytest.mark.asyncio
async def test_create_auth_copies_creds():
    creds = CustomAuthModel(api_key=API_KEY)
    assert await CustomAuth.create_auth(ctx=Context(), app_creds=creds) == creds


@pytest.mark.asyncio
async def test_refresh_is_identity_and_never_required():
    ctx = Context()
    creds = CustomAuthModel(api_key=API_KEY)
    existing = CustomAuthModel(api_key="other")

    assert await CustomAuth.is_refresh_required(ctx=ctx, app_auth=existing) is False
    assert await CustomAuth.refresh_auth(ctx=ctx, app_creds=creds, app_auth=existing) == existing
    assert await CustomAuth.refresh_auth(ctx=ctx, app_creds=creds, app_auth=None) == creds


def test_same_model_backs_both_creds_and_auth():
    """`creds` on AppCreds and `auth` on AppAuth are the same shape - no exchange step."""
    assert CustomAuth.get_creds_model_type() is CustomAuthModel
    assert CustomAuth.get_auth_model_type() is CustomAuthModel


def test_creds_json_schema_exposes_placement_with_defaults():
    """This schema is what `GET /app-creds/types/custom` serves to the UI.

    Property order is the order the Connect Custom form renders in, so it is a
    contract, not an accident.
    """
    schema = CustomAuth.get_creds_model_type().model_json_schema()
    assert list(schema["properties"]) == [
        "base_url",
        "api_key",
        "header_name",
        "header_value_prefix",
    ]
    assert schema["required"] == ["api_key"]
    assert schema["properties"]["header_name"]["default"] == "Authorization"
    assert schema["properties"]["header_value_prefix"]["default"] == "Bearer"


# ------------------------------------------------------------------ base url


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url, url, expected",
    [
        pytest.param("", "https://erp.acme.com/v1/x", "https://erp.acme.com/v1/x", id="no_base_passes_through"),
        pytest.param(
            "https://erp.acme.com",
            "https://placeholder/v1/invoices",
            "https://erp.acme.com/v1/invoices",
            id="host_replaced",
        ),
        pytest.param(
            "https://erp.acme.com",
            "https://old.example.com:8443/v1/x",
            "https://erp.acme.com/v1/x",
            id="port_not_inherited",
        ),
        pytest.param(
            "https://erp.acme.com/api/v2",
            "https://placeholder/invoices",
            "https://erp.acme.com/api/v2/invoices",
            id="base_path_prefix",
        ),
        pytest.param(
            "https://erp.acme.com/api/v2/",
            "https://placeholder/invoices",
            "https://erp.acme.com/api/v2/invoices",
            id="base_trailing_slash",
        ),
        pytest.param(
            "https://erp.acme.com",
            "https://placeholder/v1/x?page=2&q=a#frag",
            "https://erp.acme.com/v1/x?page=2&q=a#frag",
            id="query_and_fragment_preserved",
        ),
        pytest.param(
            "http://localhost:8080",
            "https://placeholder/v1/x",
            "http://localhost:8080/v1/x",
            id="scheme_and_port_come_from_base",
        ),
    ],
)
async def test_base_url_rewrites_the_host(base_url, url, expected):
    auth = CustomAuthModel(api_key=API_KEY, base_url=base_url)
    assert auth.rebase(url) == expected
    assert (
        await CustomAuth.get_updated_url(ctx=Context(), app_creds=None, app_auth=auth, url=url)
        == expected
    )


@pytest.mark.asyncio
async def test_base_url_falls_back_to_creds_when_no_auth_doc():
    creds = CustomAuthModel(api_key=API_KEY, base_url="https://erp.acme.com")
    assert (
        await CustomAuth.get_updated_url(
            ctx=Context(), app_creds=creds, app_auth=None, url="https://placeholder/v1/x"
        )
        == "https://erp.acme.com/v1/x"
    )


@pytest.mark.parametrize(
    "base_url",
    [
        pytest.param("erp.acme.com", id="no_scheme"),
        pytest.param("ftp://erp.acme.com", id="non_http_scheme"),
        pytest.param("https://", id="no_host"),
        pytest.param("https://erp.acme.com\r\nX-Evil: yes", id="crlf"),
    ],
)
def test_invalid_base_url_is_rejected(base_url):
    with pytest.raises(ValidationError):
        CustomAuthModel(api_key=API_KEY, base_url=base_url)


def test_base_url_defaults_to_empty_and_is_a_plain_string():
    """Auth models are dumped into Mongo, and BSON cannot encode a pydantic Url."""
    dumped = CustomAuthModel(api_key=API_KEY, base_url="https://erp.acme.com").model_dump()
    assert dumped["base_url"] == "https://erp.acme.com"
    assert isinstance(dumped["base_url"], str)
    assert CustomAuthModel(api_key=API_KEY).model_dump()["base_url"] == ""


@pytest.mark.asyncio
async def test_base_url_survives_create_auth():
    creds = CustomAuthModel(api_key=API_KEY, base_url="https://erp.acme.com")
    assert (await CustomAuth.create_auth(ctx=Context(), app_creds=creds)).base_url == creds.base_url


@pytest.mark.asyncio
async def test_get_api_domain_returns_the_base_url():
    creds = CustomAuthModel(api_key=API_KEY, base_url="https://erp.acme.com")
    assert await CustomAuth.get_api_domain(app_creds=creds) == "https://erp.acme.com"
    assert await CustomAuth.get_api_domain(app_creds=None) == ""


# ------------------------------------------------------------- registration


def test_custom_is_added_to_app_types_enum():
    assert AppTypes.custom.value == "custom"


def test_custom_is_registered_in_app_type_to_auth_registry():
    assert AppTypeToIntegrationClass.get(AppTypes.custom) is CustomAuth


def test_custom_is_registered_in_app_type_to_auth_registry_file():
    repo_root = Path(__file__).resolve().parents[2]
    registry_path = repo_root / "src" / "autobots" / "integrations" / "app_type_to_integration_class.py"
    text = registry_path.read_text(encoding="utf-8")
    assert (
        "from src.autobots.integrations.integration_type.custom_auth import CustomAuth" in text
    )
    assert "AppTypes.custom" in text


# --------------------------------------------------------------- encryption


@pytest.fixture
def crypto_keys():
    """Initialise the envelope crypto with a throwaway keypair, then put it back.

    `init_crypto_keys` writes module globals, so a test that leaves its own key
    installed would silently change what every later test encrypts against.
    """
    previous = (encoder_decoder._public_key, encoder_decoder._private_key)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    encoder_decoder.init_crypto_keys(
        public_key_b64=base64.b64encode(
            private_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).decode(),
        private_key_b64=base64.b64encode(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        ).decode(),
    )
    yield
    encoder_decoder._public_key, encoder_decoder._private_key = previous


def test_model_is_encrypted_at_rest_and_survives_the_round_trip(crypto_keys):
    """`creds` and `auth` both go through this, so it covers what gets stored in Mongo."""
    plain = CustomAuthModel(
        api_key=API_KEY, header_name="X-Api-Key", base_url="https://erp.acme.com"
    ).model_dump()

    stored = encrypt_if_needed(plain)

    assert stored["_enc"] is True
    # Nothing recoverable from the stored document without the key.
    assert API_KEY not in str(stored)

    assert decrypt_if_needed(stored) == plain


def test_encrypting_twice_does_not_double_wrap(crypto_keys):
    once = encrypt_if_needed(CustomAuthModel(api_key=API_KEY).model_dump())
    assert encrypt_if_needed(once) == once


# ------------------------------------------------------------ secret scrubbing


def test_api_key_is_scrubbed_before_reaching_the_llm():
    """`get_connected_app_details` returns decrypted auth; the key must not ride along.

    This passes only because the credential is a bare `api_key` - that scrub drops
    keys by name, so a field it does not know stays in the payload.
    """
    filtered = ActionTools._filter_auth(CustomAuthModel(api_key=API_KEY).model_dump())
    assert "api_key" not in filtered
    assert API_KEY not in str(filtered)
