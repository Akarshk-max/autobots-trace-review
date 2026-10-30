import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.autobots.conn.only_auth.datev.datev_client import (  # noqa: E402
    DATEV_DEFAULT_SCOPES,
    Datev,
)


def test_normalize_scopes_uses_configured_clients_plus_dataexchange_default_bundle():
    normalized, debug = Datev.normalize_scopes(None)

    assert normalized == DATEV_DEFAULT_SCOPES
    assert normalized == "openid profile email datev:accounting:clients datev:accounting:exchange"
    assert debug["used_default_scopes"] is True
    assert debug["scope_list"] == DATEV_DEFAULT_SCOPES.split()


def test_normalize_scopes_rewrites_aliases_without_mutating_product_selection():
    normalized, debug = Datev.normalize_scopes(
        "profile+email accounting:clients accounting:documents accounting:exchange accounting:documents"
    )

    assert normalized == (
        "openid profile email datev:accounting:clients accounting:documents "
        "datev:accounting:exchange"
    )
    assert debug["aliases_applied"] == {
        "accounting:clients": "datev:accounting:clients",
        "accounting:exchange": "datev:accounting:exchange",
    }


def test_datev_environment_helpers_distinguish_sandbox_and_production_urls():
    assert Datev.get_userinfo_url("sandbox") == "https://sandbox-api.datev.de/userinfo"
    assert Datev.get_userinfo_url("production") == "https://api.datev.de/userinfo"

    assert Datev.get_url_environment("https://api.datev.de/userinfo") == "production"
    assert Datev.get_url_environment("https://sandbox-api.datev.de/userinfo") == "sandbox"
    assert (
        Datev.get_url_environment(
            "https://accounting-clients.api.datev.de/platform-sandbox/v2/clients"
        )
        == "sandbox"
    )
    assert (
        Datev.get_url_environment(
            "https://accounting-dataexchange.api.datev.de/platform/v1/state"
        )
        == "production"
    )

    assert (
        Datev.is_environment_compatible(
            "sandbox", "https://accounting-clients.api.datev.de/platform-sandbox/v2/clients"
        )
        is True
    )
    assert (
        Datev.is_environment_compatible(
            "sandbox", "https://accounting-dataexchange.api.datev.de/platform/v1/state"
        )
        is False
    )


def test_client_id_header_is_only_added_for_resource_apis():
    assert Datev.requires_client_id_header("https://accounting-documents.api.datev.de/platform/v2/documents") is True
    assert Datev.requires_client_id_header("https://api.datev.de/userinfo") is False
    assert Datev.requires_client_id_header("https://sandbox-api.datev.de/token") is False

    headers = Datev.augment_headers_for_url(
        {"Authorization": "Bearer token"},
        "https://accounting-documents.api.datev.de/platform-sandbox/v2/documents",
        "client-123",
    )
    assert headers["X-DATEV-Client-Id"] == "client-123"

    untouched = Datev.augment_headers_for_url(
        {"Authorization": "Bearer token"},
        "https://sandbox-api.datev.de/userinfo",
        "client-123",
    )
    assert "X-DATEV-Client-Id" not in untouched


def test_normalize_scopes_warns_for_offline_access_dataexchange_combo():
    normalized, debug = Datev.normalize_scopes(
        "openid offline_access datev:accounting:clients datev:accounting:exchange"
    )

    assert normalized == "openid offline_access datev:accounting:clients datev:accounting:exchange"
    assert any("offline_access is currently not available with DATEV accounting:dataexchange API" in warning for warning in debug["warnings"])


def test_classify_api_error_highlights_client_mismatch_and_subscription_problems():
    assert "another client" in Datev.classify_api_error(
        "https://sandbox-api.datev.de/accounting/v1/clients",
        401,
        '{"error": "invalid_token", "error_description": "Token issued to another client"}',
    ).lower()
    assert "x-datev-client-id" in Datev.classify_api_error(
        "https://accounting-clients.api.datev.de/platform-sandbox/v2/clients",
        401,
        '{"httpCode":"401","detail":"Invalid client id or secret."}',
    ).lower()
    assert "subscribed api product" in Datev.classify_api_error(
        "https://accounting-documents.api.datev.de/platform-sandbox/v2/clients",
        401,
        'Cannot find valid subscription for the incoming API request.',
    ).lower()


def test_canonicalize_resource_url_repairs_legacy_gateway_paths():
    assert Datev.canonicalize_resource_url(
        "sandbox", "https://api.datev.de/userinfo"
    ) == "https://sandbox-api.datev.de/userinfo"

    assert Datev.canonicalize_resource_url(
        "sandbox", "https://sandbox-api.datev.de/accounting/v1/clients"
    ) == "https://accounting-clients.api.datev.de/platform-sandbox/v2/clients"

    assert Datev.canonicalize_resource_url(
        "sandbox", "https://sandbox-api.datev.de/accounting/v1/clients/455148-2/fiscal-years"
    ) == "https://accounting-data-exchange.api.datev.de/platform-sandbox/v1/clients/455148-2/fiscal-years"


def test_choose_scope_input_prefers_explicit_settings_over_app_creds():
    scope, source = Datev.choose_scope_input(
        "openid profile email datev:accounting:clients datev:accounting:exchange",
        "openid datev:accounting:clients",
    )

    assert source == "settings"
    assert scope == "openid profile email datev:accounting:clients datev:accounting:exchange"


def test_dataexchange_get_requests_default_to_ndjson_accept():
    assert Datev.expected_response_format_for_url(
        "https://accounting-data-exchange.api.datev.de/platform-sandbox/v1/clients/455148-2/fiscal-years",
        "GET",
    ) == "application/x-ndjson"
    assert Datev.default_accept_header_for_url(
        "https://accounting-data-exchange.api.datev.de/platform-sandbox/v1/clients/455148-2/fiscal-years",
        "GET",
    ) == "application/x-ndjson"

    headers = Datev.apply_default_headers_for_url(
        {"Authorization": "Bearer token"},
        "https://accounting-data-exchange.api.datev.de/platform-sandbox/v1/clients/455148-2/fiscal-years",
        "GET",
    )
    assert headers["Accept"] == "application/x-ndjson"


def test_non_exchange_or_explicit_accept_headers_are_preserved():
    assert Datev.default_accept_header_for_url(
        "https://accounting-clients.api.datev.de/platform-sandbox/v2/clients",
        "GET",
    ) is None

    explicit = Datev.apply_default_headers_for_url(
        {"Authorization": "Bearer token", "Accept": "application/json"},
        "https://accounting-data-exchange.api.datev.de/platform-sandbox/v1/clients/455148-2/fiscal-years",
        "GET",
    )
    assert explicit["Accept"] == "application/json"


def test_classify_api_error_highlights_media_type_and_fiscal_year_hints():
    assert "application/x-ndjson" in Datev.classify_api_error(
        "https://accounting-data-exchange.api.datev.de/platform-sandbox/v1/clients/455148-2/fiscal-years",
        406,
        '{"detail":"Acceptable representations: [application/x-ndjson, application/problem+json].","status":406}',
    )
    assert "fiscal-years" in Datev.classify_api_error(
        "https://accounting-data-exchange.api.datev.de/platform-sandbox/v1/clients/455148-2/fiscal-years/20240101",
        404,
        '{"detail":"No fiscal year data found for: clientId=455148-2, fiscalYearId=20240101","status":404}',
    )
