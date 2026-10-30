
from pathlib import Path


def test_workday_redirect_helper_has_no_dev_hardcode():
    text = Path('src/autobots/api/v1/api_auth/api_workday_auth.py').read_text()
    assert 'https://dev.meetkiwi.ai' not in text
