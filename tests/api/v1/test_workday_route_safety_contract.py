from pathlib import Path


def test_workday_health_saved_route_has_safe_error_helpers():
    src = Path("src/autobots/api/v1/api_auth/api_workday_auth.py").read_text()
    assert "def _base_workday_route_response" in src
    assert "def _connection_info_error_response" in src
    assert "def _workday_result_needs_auth_retry" in src


def test_workday_health_saved_route_handles_auth_and_retry_contractually():
    src = Path("src/autobots/api/v1/api_auth/api_workday_auth.py").read_text()
    assert "auth_header_error" in src
    assert "missing_credentials" in src
    assert "backend_error" in src
    assert "error_stage" in src
    assert "sync_with_creds" in src
    assert "auth_retry_attempted" in src
    assert "auth_retry_recovered" in src
