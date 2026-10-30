from pathlib import Path


def test_workday_health_probe_helpers_present():
    src = Path("src/autobots/integrations/integration_type/workday_auth.py").read_text()
    assert "def _preferred_rest_base_url" in src
    assert "def _build_rest_workers_probe_urls" in src
    assert "def _build_rest_workers_me_probe_urls" in src
    assert "def _build_wql_probe_urls" in src
    assert "/workers?limit=1" in src
    assert "/workers/me" in src


def test_workday_health_connection_info_contract_fields_present():
    src = Path("src/autobots/integrations/integration_type/workday_auth.py").read_text()
    assert '"recommended_surface"' in src
    assert '"recommended_primary_probe"' in src
    assert '"recommended_secondary_probe"' in src
    assert '"wql_candidate_urls"' in src
