from pathlib import Path


def test_workday_health_surface_matrix_contract_present():
    src = Path("src/autobots/integrations/integration_type/workday_auth.py").read_text()
    assert "'rest_workers'" in src
    assert "'rest_workers_me'" in src
    assert "'wql_data_sources'" in src
    assert "permission_denied" in src
    assert "token_not_accepted" in src
    assert '"surfaces"' in src
    assert '"business_ready"' in src
