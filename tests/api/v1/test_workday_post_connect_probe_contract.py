from pathlib import Path


def test_workday_jwt_connect_response_includes_post_connect_probe():
    source = Path('src/autobots/api/v1/api_auth/api_workday_auth.py').read_text()
    assert 'post_connect_probe' in source
    assert '_run_workday_post_connect_probe' in source
    assert 'Workday post-connect probe failed' in source
    assert "'post_connect_probe': post_connect_probe" in source
    assert 'await WorkdayAuth.health_check(' in source
    assert 'await WorkdayAuth.get_auth_header(' in source


def test_action_api_has_no_provider_recovery_hook_reference():
    source = Path('src/autobots/action/action_type/action_text2text/action_api.py').read_text()
    assert 'get_provider_recovery' not in source
    assert 'provider recovery hook requested retry' not in source
    assert 'config.app_name == AppTypes.workday' not in source
