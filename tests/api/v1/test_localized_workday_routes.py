
from pathlib import Path


def test_api_app_auth_has_no_generic_connection_info_or_health_routes():
    text = Path('src/autobots/api/v1/api_app_creds/api_app_auth.py').read_text()
    assert '/{app_name}/connection-info' not in text
    assert '/{app_name}/health' not in text


def test_api_workday_auth_contains_local_helper_routes():
    text = Path('src/autobots/api/v1/api_auth/api_workday_auth.py').read_text()
    assert "@router.post('/connection-info')" in text
    assert "@router.post('/health-saved')" in text
