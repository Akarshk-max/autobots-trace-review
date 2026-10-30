from pathlib import Path


def test_workday_redirect_helper_no_dashboard_hardcode():
    src = Path("src/autobots/api/v1/api_auth/api_workday_auth.py").read_text()
    assert "/dashboard/connections" not in src


def test_workday_redirect_helper_relative_fallback_and_query_merge_present():
    src = Path("src/autobots/api/v1/api_auth/api_workday_auth.py").read_text()
    assert "parse_qsl" in src
    assert "return '/?' + urllib.parse.urlencode(extra_query)" in src
    assert "path = parsed.path or '/'" in src
